# pns/runtime/autonomy/coordinator.py — 自主运行时的编排
#
#     调度到期 → 角色作用域的 Agency 提案 → （需要台词就）生成
#              → Router 判分与生成审计 → 校验后的事件提交
#              → 曝光 / 观察 → 主观记忆编码 → 到期资格的终局
#
# 这是 P4–P10 各层之间**唯一**的编排者，而且它刻意只做编排：调度、Agency、
# 事件提交、曝光、记忆仍然是各自独立的服务，各自守着自己的权威和事务边界。
# 协调器不拥有它们中任何一份状态 —— 它一条到期资格都存不下来，存的地方是
# SessionState。
#
# 五条硬约束：
#
#   1. **一个会话一个协调器。** 两个协调器会从同一个投递箱里各自取走到期
#      资格、各自跑生成与判分、各自往同一份世界历史里提交。绑定只允许一次。
#   2. **台词只有一条路。** 提案里的台词必须先拿到一份被接受、且绑定到这一句
#      的 GenerationAudit，才可能进世界历史。这道闸在
#      `pns/models/action.py` 的构造函数里，不是在这里 —— 协调器绕不过它，
#      别的调用方也绕不过。
#   3. **一次处理是一个事务。** Agency 提交（事件 + 曝光 + 观察 + 审计 +
#      交接确认）与记忆编码落在**同一个** atomic_commit() 里。中途任何一步
#      失败，世界回到处理之前的样子，到期记录仍然待处理，可以重来。
#   4. **生命周期状态只在一条线性化边界上变。** start()、stop() 和"这次写入
#      准不准"共用同一把闸门锁，所以不存在"检查通过了、翻转之前被人插进来"
#      的窗口：并发的 start/stop 不会得到一个既在跑、又已经被要求停止的运行时。
#      慢调用（生成、判分）**绝不持锁** —— 一个卡住的模型调用不该让停机也跟着
#      卡住。放手不消耗重试预算，停机不是失败。
#   4b. **停机有两种返回，而且必须分清楚。** 外部线程调用的 stop() 会在闸门上
#      等到在跑的那次提交**整个结束**（含 Agency 记录与交接确认）才返回，所以
#      它返回之后没有任何提交能落地。事务**内部**调用的 stop() 没法等自己跑完，
#      于是它只**登记**：立刻返回，但如实报告 running 仍然是 True、停机尚未
#      生效；它在当前事务结束的那一刻才真正生效。把这两种混成一个"立刻返回
#      且立刻生效"，就会出现"stop() 已经返回、Agency 记录随后才落地"。
#   5. **每条到期都有交代。** 要么留下一条耐久的终局记录（Agency 日志里有它、
#      投递箱里它被确认了），要么明确地仍然待处理。没有第三种状态，也没有
#      "无限期悬着"——重试预算用完就写终局失败记录。
#
# 研究会话的 /ws/run 确定性 round robin 跟这里没有任何关系：那条路上时钟不动，
# 什么都不会到期，而且 session_runtime.py 不 import 这个包（有 AST 测试盯着）。
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, List, Mapping, Optional, Set, Tuple

from pns.models.activation import ActivationDue
from pns.models.agency import AgencyBudget, AgencyOutcome
from pns.models.authored import GenerationAudit
from pns.models.session import SessionState
from pns.models.world_state import WorldState
from pns.runtime.agency.engine import AgencyEngine, AgencyEngineError, ProposalPlan
from pns.runtime.autonomy.audit import AuditError, AuditRequest
from pns.runtime.autonomy.context import DIALOGUE_OUTPUT_RULES
from pns.runtime.autonomy.outcome import (
    ActivationOutcome,
    ActivationResult,
    RetryPolicy,
    outcome_for,
)
from pns.runtime.memory.encoder import MemoryEncoder
from pns.runtime.memory.recall import MemoryRecall
from pns.runtime.scheduler import PersistentScheduler

# 状态投影里默认回看多少条。
_RECENT = 20
_AUDIT_RECENT_LINES = 12


class AutonomyError(ValueError):
    """这次调用根本不该发生（绑了第二个协调器、停机后还要推时钟等）。

    它跟 ActivationOutcome 里的失败码是两类东西：失败码是"处理过，结论是
    没成"，会留下可查的结果；AutonomyError 是"这次调用的前提就不成立"。
    """


class AutonomousRuntime:
    """一个会话里唯一一份自主运行时协调器。

    编排逻辑属于 **cold update**：它是运行时逻辑，不是内容配置。没有任何
    构造它的路径读磁盘配置，ContentRegistry 也没有任何字段能碰到它 ——
    P7 的重载换掉的是配置快照，动不了一个已经在跑的世界。
    """

    def __init__(
        self,
        state: SessionState,
        *,
        policy=None,
        auditor=None,
        budget: Optional[AgencyBudget] = None,
        retry: Optional[RetryPolicy] = None,
        recall_budget=None,
        name: str = "autonomy",
    ) -> None:
        if not isinstance(state, SessionState):
            raise AutonomyError("自主运行时必须绑定在一个 SessionState 上")
        if not isinstance(state.world_state, WorldState):
            raise AutonomyError("自主运行时绑定的会话还没有权威 WorldState")
        if auditor is None or not callable(getattr(auditor, "audit", None)):
            raise AutonomyError("自主运行时需要一个提供 audit() 的判分器")

        self._state = state
        self._auditor = auditor
        self._name = name
        self._retry = retry if retry is not None else RetryPolicy()
        if not isinstance(self._retry, RetryPolicy):
            raise AutonomyError("retry 必须是 RetryPolicy")

        # 已经绑在这个会话上的服务原样复用；没有的才建。协调器不是它们的
        # 拥有者，只是它们的编排者 —— 所以它绝不会造出第二份权威。
        if state.scheduler is not None:
            if not isinstance(state.scheduler, PersistentScheduler):
                raise AutonomyError("会话上绑着的调度器不是 PersistentScheduler")
            self._scheduler = state.scheduler
        else:
            self._scheduler = PersistentScheduler(state)

        if state.agency_engine is not None:
            if policy is not None:
                # 引擎已经绑了策略，再交一个进来会得到两份互相看不见的"谁在
                # 替这些角色做决定"。响亮失败，不静默取其一。
                raise AutonomyError(
                    "这个会话已经绑定过 Agency 引擎，不能再指定策略"
                )
            self._agency = state.agency_engine
        else:
            self._agency = AgencyEngine(state, policy=policy, budget=budget)

        self._memory = (
            state.memory_encoder
            if state.memory_encoder is not None
            else MemoryEncoder(state)
        )
        self._recall = MemoryRecall(state, recall_budget)

        # 提交许可闸门。它保护三样东西，而且只保护这三样：运行标志的翻转、
        # "这次写入准不准"的判断、以及那次写入本身。慢调用一律在闸门之外 ——
        # 一个卡住的模型调用不该让 stop() 也跟着卡住。
        #
        # 用可重入锁而不是普通锁：提交事务里的代码有可能回头调用 stop()。
        # 同一个线程再进来必须放行，否则那次提交会把自己锁死在半截 ——
        # 而那正是这把锁本来要防的事。
        self._gate = threading.RLock()
        self._started = False
        self._running = False
        self._stop_reason: Optional[str] = None
        # 事务内部请求的停机：登记在这里，事务结束时才翻转 _running。
        self._stop_deferred = False
        # 谁正在事务里、嵌了几层。提交全程持锁，所以同一时刻至多一个线程。
        self._committing_thread: Optional[int] = None
        self._committing_depth = 0
        # 正在处理中的到期资格。同一条被两个线程同时处理不会重复提交（交接
        # 是一次性的，提案身份也是推导出来的），但会白跑两次生成和两次判分，
        # 而且第二个线程会在提交那一刻拿到一个含义不明的交接错误。响亮拒绝
        # 比两次模型调用便宜得多。
        self._in_flight: Set[str] = set()
        # 每条到期资格已经失败过几次。刻意只活在进程里：跨进程重启的持久化
        # 是 P12 的事。丢掉它的后果是重试预算重新开始数，而不是重复提交 ——
        # 重复提交由推导出来的提案身份和交接的一次性挡着。
        self._attempts: Dict[str, int] = {}
        self._results: List[ActivationResult] = []

        try:
            state.attach_autonomy(self)
        except (RuntimeError, TypeError) as e:
            raise AutonomyError(str(e)) from e

    # ── 读 ──────────────────────────────────────────────────────────────
    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def session_id(self) -> str:
        return self._state.session_id

    @property
    def world(self) -> WorldState:
        return self._state.world_state

    @property
    def clock(self) -> datetime:
        """当前模拟时间。权威值始终在 WorldState 上，这里不另存一份。"""
        return self._state.world_state.clock

    @property
    def scheduler(self) -> PersistentScheduler:
        return self._scheduler

    @property
    def agency(self) -> AgencyEngine:
        return self._agency

    @property
    def memory(self) -> MemoryEncoder:
        return self._memory

    @property
    def recall(self) -> MemoryRecall:
        return self._recall

    @property
    def auditor(self):
        return self._auditor

    @property
    def retry(self) -> RetryPolicy:
        return self._retry

    @property
    def running(self) -> bool:
        """此刻还接不接受新的写入。

        这是一次**瞬时读取**，不是快照：读完的下一纳秒它就可能变了，而且它
        刻意不拿闸门 —— 拿了的话，"提交进行中吗"这个问题就永远问不出来（问的
        人会被那次提交挡住）。需要一致快照的用 status()。
        """
        return self._running

    @property
    def stop_requested(self) -> bool:
        """有没有被要求停止过。

        它跟 `running` 不是一回事：事务内部登记的停机会让这个为 True、而
        `running` 暂时仍然是 True，直到那个事务结束。
        """
        return self._stop_reason is not None

    @property
    def stopping(self) -> bool:
        """已经登记、但还没生效的停机。"""
        return self.stop_requested and self._running

    @property
    def stop_reason(self) -> Optional[str]:
        return self._stop_reason

    @property
    def in_transaction(self) -> bool:
        """**本线程**此刻是不是正处在一次提交事务里面。

        它刻意不拿闸门：拿了就永远问不出来（问的人会被那次提交挡住），而这
        个问题的用处正是"我现在能不能做一件需要闸门的事"。不拿锁也是安全的 ——
        只有本线程自己会把这两个字段写成"我"，所以答案对本线程永远是准的；
        别的线程正在事务里时，这里得到 False，随后那次加锁会如实等它结束。

        持久化层用它判断"能不能在这里取快照"：事务内部取快照会拿到一份事件
        写了、记忆还没落地的半截世界，而闸门是可重入的，不会替它挡住。
        """
        return bool(self._committing_depth) and self._committing_thread == (
            threading.get_ident()
        )

    @contextmanager
    def lifecycle_boundary(self, timeout: Optional[float] = None):
        """把一次生命周期操作串到跟 start / stop / 提交**同一条**边界上。

        它拿**两把**锁，顺序固定，而且两把都必须拿：

          1. **协调器闸门。** 持有期间没有任何提交能被准入，也没有 start/stop
             能翻转运行标志。
          2. **会话自己的独占边界**（SessionState.snapshot_boundary）。闸门只
             管得住协调器发起的提交；调度器的时间推进和事件提交层会直接开
             atomic_commit()，闸门看不见它们。而且这里必须是**互斥**，不能是
             "查一下有没有人在事务里"——查完到 to_dict() 之间那段窗口里，一次
             时间推进照样能开起来并跟快照重叠。

        锁顺序是全局唯一一条：**闸门 → 会话边界**。反过来拿会死锁。事务内部
        回头拿闸门只有一种合法情形（P11 的"事务内部 stop()"），那时闸门本来就
        在自己手上，拿的是同一把可重入锁。

        块内只许放确定性的纯内存工作（取快照），绝不放 I/O 或模型调用：两把锁
        都攥着，一次 fsync 会把停机一起堵住。

        事务**内部**进来一律拒绝（两层各拒一次，消息不同好定位）：两把锁都是
        可重入的，会放行同一个线程，而那一刻的世界是半截的 —— 放行等于允许存下
        一份不存在过的世界。
        """
        with self._gate:
            if self.in_transaction:
                raise AutonomyError(
                    "不能在一次提交事务内部进入生命周期边界：那一刻的世界是"
                    "半截的（事件已经写了、记忆还没落地）"
                )
            with self._state.snapshot_boundary(timeout):
                yield self

    # ── 启停 ────────────────────────────────────────────────────────────
    def start(self) -> Dict:
        """开始接受到期资格。只能调用一次。

        停过之后不能再启动，**哪怕它根本没启动过**：一个"停了又开"的运行时，
        会让"停机期间发生了什么"这个问题没有单一答案。判据是"有没有被要求
        停止过"，不是"现在跑没跑"——只看后者的话，先 stop 再 start 会得到一个
        既有停机理由、又自称在跑的运行时。要接着跑，就从存档恢复出一个新的
        协调器。

        检查与翻转在**同一次持锁**里完成，跟 stop() 走同一条边界。分开的话，
        两者之间的那一瞬间就是一个竞态：start() 查完发现没停过，stop() 在这时
        落地，start() 接着把 running 翻成 True —— 于是运行时既在跑、又带着一个
        停机理由。两个并发的 start() 也会双双"成功"。
        """
        with self._gate:
            if self._started:
                raise AutonomyError("自主运行时已经启动过了")
            if self._stop_reason is not None:
                raise AutonomyError(
                    f"自主运行时已经被要求停止（{self._stop_reason}），不能启动"
                )
            self._started = True
            self._running = True
            return self._status_locked()

    def stop(self, reason: str = "stopped") -> Dict:
        """请求停止。幂等，而且**保留第一个理由**。

        第一个理由才是真正的原因，后来的都是它的后果。

        返回值有两种，靠 `running` 区分，而且这个区分是本方法的全部要害：

        **已生效**（`running=False`）。外部线程调用一律是这一种。stop() 会在
        闸门上等在跑的那次提交**整个结束**——包括 Agency 记录和交接确认，它们
        都在那个事务里面。于是这句话是真的：*stop() 返回之后，没有任何提交能
        再落地*。

        **已登记、尚未生效**（`running=True`，`stop_requested=True`）。只在
        事务**内部**调用时发生（比如提交路径上的代码发现了必须停机的情况）。
        它没法等自己跑完 —— 那是在等自己，只能死锁 —— 所以它登记下来，在当前
        事务结束的那一刻生效。这时如实报告 running 仍然是 True：谎称已经停了，
        紧接着又把 Agency 记录和确认写下去，正是这条契约要防的事。

        两种都不会打断一个已经进了 atomic_commit() 的事务：撕开它会留下半条
        事件，比晚停一次严重得多。
        """
        if not isinstance(reason, str) or not reason:
            raise AutonomyError("stop 的理由必须是非空字符串")
        with self._gate:
            if self._stop_reason is None:
                self._stop_reason = reason
            if self._committing_depth and self._committing_thread == (
                threading.get_ident()
            ):
                # 拿得到闸门、而且事务是自己开的 —— 只能登记，不能等。
                self._stop_deferred = True
            else:
                # 走到这里说明闸门在手，因此没有任何事务在跑（提交全程持锁）。
                self._running = False
            return self._status_locked()

    @contextmanager
    def _committing(self):
        """标记本线程正处在提交事务里。调用方已经持着闸门。

        它存在只为一件事：让事务内部调用的 stop() 认出"这是我自己"，从而
        登记而不是空等。事务结束（无论成功还是回滚）时，登记过的停机在这里
        真正生效 —— 这一刻已经在 Agency 记录和交接确认之后。
        """
        self._committing_thread = threading.get_ident()
        self._committing_depth += 1
        try:
            yield
        finally:
            self._committing_depth -= 1
            if self._committing_depth == 0:
                self._committing_thread = None
                if self._stop_deferred:
                    self._stop_deferred = False
                    self._running = False

    # ── 处理一条到期资格 ────────────────────────────────────────────────
    def process_due(self, due: ActivationDue) -> ActivationResult:
        """把一条到期资格走完整条链，产出一个结局。

        顺序是刻意的：先在不改任何状态的前提下把提案、生成、判分全做完，
        再一次性进事务。于是慢调用全部发生在事务之外，而事务里只有确定性的
        校验与写入。
        """
        if not isinstance(due, ActivationDue):
            raise AutonomyError("只能处理 ActivationDue")
        with self._gate:
            if not self._running:
                # 还没启动，或者已经停了。什么都不碰 —— 到期记录仍然待处理。
                return self._record(
                    self._stopped_result(
                        due, attempt=self._attempts.get(due.due_id, 0)
                    )
                )
            if due.due_id in self._in_flight:
                raise AutonomyError(
                    f"到期记录 '{due.due_id}' 正在被处理，不能同时处理第二次"
                )
            self._in_flight.add(due.due_id)
            attempt = self._attempts.get(due.due_id, 0) + 1
        try:
            return self._process(due, attempt)
        finally:
            with self._gate:
                self._in_flight.discard(due.due_id)

    def _process(self, due: ActivationDue, attempt: int) -> ActivationResult:
        """一条到期资格的实际处理。慢调用都在这里，而且都在闸门之外。"""

        # ── 提案（含生成，纯的） ───────────────────────────────────────
        plan = self._agency.propose(due)
        if not self._running:
            # 生成期间被要求停止。模型回来晚了，这句话就不算数 —— 不提交、
            # 不确认、不消耗重试预算。
            #
            # 这次检查是**省事**，不是保证：它让一条注定提交不了的提案不必再
            # 白跑一趟判分。真正挡住提交的是闸门里那次加锁判断（见 _commit），
            # 因为"查完"到"进事务"之间永远有一段窗口。
            return self._record(self._stopped_result(due, attempt=attempt - 1))

        retryable = self._retryable_policy_failure(plan)
        if retryable is not None and not self._retry.exhausted(attempt):
            return self._record(
                self._retry_result(due, plan.character_id, attempt, retryable)
            )
        exhausted = retryable is not None
        if exhausted:
            plan = plan.annotated(retry_budget_exhausted=True, attempts=attempt)

        # ── 判分（纯的） ───────────────────────────────────────────────
        if plan.verdict.acted and plan.requires_audit:
            try:
                audit = self._auditor.audit(self._audit_request(plan))
            except AuditError as e:
                return self._record(
                    self._audit_failure(due, plan, attempt, e.retryable, str(e))
                )
            except Exception as e:  # 判分器实现自己的 bug
                return self._record(
                    self._audit_failure(
                        due, plan, attempt, False, f"{type(e).__name__}: {e}"
                    )
                )
            if not isinstance(audit, GenerationAudit):
                # 判分器交回来的不是一份凭据。这不是"判成了不接受"，也不是
                # 一次可以重试的故障 —— 一个返回错类型的判分器不会自己好起来。
                # 不在这里拦的话，它会一路走到审计细节那一行才炸成一个
                # AttributeError，被当成"提交事务被打断"白烧掉重试预算。
                return self._record(
                    self._audit_failure(
                        due,
                        plan,
                        attempt,
                        False,
                        f"判分器返回了 {type(audit).__name__}，不是 GenerationAudit",
                    )
                )
            if not self._running:
                # 判分回来晚了。同上：省事，不是保证 —— 兜底在闸门里。
                return self._record(self._stopped_result(due, attempt=attempt - 1))
            plan = plan.with_audit(audit)

        return self._record(self._commit(due, plan, attempt, as_failure=exhausted))

    def process_pending(
        self, max_results: Optional[int] = None
    ) -> Tuple[ActivationResult, ...]:
        """把投递箱里还没评估的到期资格按触发顺序处理掉。

        停机之后剩下的那些原样留在待处理 —— 处理它们的是恢复之后的下一个
        协调器，不是这一个。

        已经被别的线程拿在手上的那条会被**跳过**，不会让这一轮抛错中断：
        它没有丢，正有人在处理它，而这一轮的返回值只报告"这次调用真的处理
        了哪些"。`max_results` 只限制本次真正处理的条数，剩余记录继续待办。
        点名处理某一条（process_due）则仍然响亮拒绝 —— 调用方要的就是那一
        条，静默跳过会让它以为处理过了。
        """
        if max_results is not None:
            if (
                isinstance(max_results, bool)
                or not isinstance(max_results, int)
                or max_results < 0
            ):
                raise AutonomyError("max_results 必须是非负整数或 None")
        results = []
        for due in self._agency.pending_due():
            if max_results is not None and len(results) >= max_results:
                break
            with self._gate:
                if not self._running:
                    break
                if due.due_id in self._in_flight:
                    continue
            try:
                results.append(self.process_due(due))
            except AutonomyError:
                # 检查与进入之间被别的线程抢走了。跳过，不中断这一轮。
                continue
        return tuple(results)

    # ── 推进模拟时钟 ────────────────────────────────────────────────────
    def advance(self, minutes: int, *, max_results: Optional[int] = None) -> Dict:
        """把模拟时间往前推，并处理这段时间里到期的一切。

        时间推进本身是调度器的事务（时钟 + 世界历史 + 队列 + 投递箱同生
        共死），这里不重复它，也不绕过它。
        """
        with self._gate:
            self._require_running("推进模拟时钟")
            tick = self._scheduler.advance_by(minutes)
        # 保留既有的 `_tick_report(tick)` 调用形状：生命周期并发测试会替换这条
        # 内部缝来精确停在“推进后、处理前”。只有驱动真的交了额度时才扩展参数。
        if max_results is None:
            return self._tick_report(tick)
        return self._tick_report(tick, max_results=max_results)

    def advance_to_next_due(self) -> Optional[Dict]:
        """推进到下一条排期到期的那一刻；队列为空就返回 None，不动时钟。"""
        with self._gate:
            self._require_running("推进模拟时钟")
            tick = self._scheduler.advance_to_next_due()
            if tick is None:
                return None
        return self._tick_report(tick)

    def _tick_report(self, tick, *, max_results: Optional[int] = None) -> Dict:
        results = self.process_pending(max_results=max_results)
        return {
            "from_clock": tick.from_clock.isoformat(),
            "to_clock": tick.to_clock.isoformat(),
            "minutes": tick.minutes,
            "due_ids": list(tick.due_ids),
            "results": [result.to_dict() for result in results],
        }

    def _require_running(self, what: str) -> None:
        if not self._running:
            raise AutonomyError(
                f"自主运行时{'还没启动' if not self._started else '已经停止'}，"
                f"不能{what}"
            )

    # ── 各步骤的结局构造 ────────────────────────────────────────────────
    def _audit_request(self, plan: ProposalPlan) -> AuditRequest:
        proposal = plan.proposal
        recent_lines = []
        for observation in self._state.observations.for_character(
            proposal.character_id
        ):
            line = observation.render_line()
            if line is not None:
                recent_lines.append(line)
        return AuditRequest(
            character_id=proposal.character_id,
            proposal_id=proposal.proposal_id,
            payload=proposal.event_payload(),
            action_id=proposal.action_id,
            target_id=proposal.target_id,
            now=plan.proposed_at,
            recent_lines=tuple(recent_lines[-_AUDIT_RECENT_LINES:]),
            task_instructions=DIALOGUE_OUTPUT_RULES,
        )

    @staticmethod
    def _retryable_policy_failure(plan: ProposalPlan) -> Optional[str]:
        """这次提案是不是一次"值得再来一次"的失败；不是就返回 None。

        判断只看引擎写进审计细节的那两个字段，不看异常类型 —— 异常早就被
        引擎吞掉并变成了一条结论，而结论才是会被记进存档的东西。
        """
        if plan.verdict is not AgencyOutcome.REJECTED_POLICY_ERROR:
            return None
        if not plan.detail.get("retryable"):
            return None
        return str(plan.detail.get("error", "policy error"))

    def _audit_failure(
        self,
        due: ActivationDue,
        plan: ProposalPlan,
        attempt: int,
        retryable: bool,
        error: str,
    ) -> ActivationResult:
        """判分没能给出结果。**绝不**退化成"那就当它通过吧"。"""
        if retryable and not self._retry.exhausted(attempt):
            return self._retry_result(due, plan.character_id, attempt, error)
        refused = plan.refused(
            AgencyOutcome.REJECTED_POLICY_ERROR,
            reason="audit_unavailable",
            error=error,
            retryable=bool(retryable),
            **(
                {"retry_budget_exhausted": True, "attempts": attempt}
                if retryable
                else {}
            ),
        )
        # 判分没发生过，所以这不是"被拒"——什么都没被判。结局是失败，
        # 而耐久记录说明了是哪一种失败。
        return self._commit(due, refused, attempt, as_failure=True)

    def _commit(
        self,
        due: ActivationDue,
        plan: ProposalPlan,
        attempt: int,
        *,
        as_failure: bool = False,
    ) -> ActivationResult:
        """一次处理的全部权威写入，落在一个事务里。

        `as_failure` 把结局码从"评估过，结论是不行"改成"这次处理失败了，
        而且没救了"。两者的**耐久记录是同一条** REJECTED_POLICY_ERROR ——
        Agency 那套结论词汇里没有"重试用完了"这一档，也不该有：那是编排层
        的概念，不是"这个角色选择了什么"的概念。区分留在结局码里，理由留在
        detail 里（retry_budget_exhausted / audit_unavailable）。

        Agency 的提交本身已经是一个事务（事件 + 曝光 + 观察 + 审计 + 交接
        确认）。记忆编码套在**同一个**外层事务里，所以"事件提交了但记忆没写
        成"这种半截世界不存在：编码失败，事件、观察、曝光判定、审计记录、
        交接确认一起回滚，到期记录仍然待处理。

        许可判断（"现在还准写吗"）和写入本身在**同一次持锁**里完成。分成两步
        的话，两者之间的那一瞬间就是一个停机竞态：stop() 已经返回，而一条在它
        之前就查过 _running 的提案照样落了地。
        """
        with self._gate:
            if not self._running:
                # 闸门关了。这是提交路径上**唯一**权威的那次判断 —— 前面几处
                # 检查都只是提前放手，省掉白跑的慢调用。
                return self._stopped_result(due, attempt=attempt - 1)
            return self._commit_admitted(due, plan, attempt, as_failure=as_failure)

    def _commit_admitted(
        self,
        due: ActivationDue,
        plan: ProposalPlan,
        attempt: int,
        *,
        as_failure: bool = False,
    ) -> ActivationResult:
        """已经拿到许可、并且正持着闸门的那次写入。"""
        with self._committing():
            return self._commit_transaction(due, plan, attempt, as_failure=as_failure)

    def _commit_transaction(
        self,
        due: ActivationDue,
        plan: ProposalPlan,
        attempt: int,
        *,
        as_failure: bool = False,
    ) -> ActivationResult:
        state = self._state
        try:
            with state.atomic_commit():
                record = self._agency.commit(plan)
                encoded = 0
                if record.outcome.acted and record.event_id is not None:
                    decisions = self._memory.encode(
                        state.observations.for_event(record.event_id)
                    )
                    encoded = sum(
                        1 for decision in decisions if decision.outcome.encoded
                    )
        except AgencyEngineError:
            # 交接的前提就不成立（这条到期不是本会话的、已经处理过了）。
            # 那不是一次可以重试的失败，也不该被吞掉。
            raise
        except BaseException as e:
            # 提交事务被打断。世界已经回到处理之前的样子，到期记录仍然待
            # 处理。这一档天然可重试 —— 但仍然受预算约束。
            return self._commit_failure(due, plan, attempt, e)

        with self._gate:
            self._attempts.pop(due.due_id, None)
        return ActivationResult(
            due_id=due.due_id,
            character_id=record.character_id,
            outcome=(
                ActivationOutcome.FAILED_TERMINAL
                if as_failure
                else outcome_for(record.outcome)
            ),
            attempt=attempt,
            at=record.decided_at,
            agency_outcome=record.outcome,
            event_id=record.event_id,
            memories=encoded,
            detail={"policy": record.policy, **_plain(record.detail)},
        )

    def _commit_failure(
        self, due: ActivationDue, plan: ProposalPlan, attempt: int, error: BaseException
    ) -> ActivationResult:
        message = f"{type(error).__name__}: {error}"
        if not self._retry.exhausted(attempt):
            return self._retry_result(due, plan.character_id, attempt, message)
        # 预算用完了，而且失败的正是提交路径本身。再试一次**最小**的那条：
        # 一条不产出事件、不碰记忆的终局失败记录。它成了，这条到期就有了
        # 耐久的交代；它也没成，那就如实报告"卡住了"，绝不静默丢弃。
        minimal = plan.refused(
            AgencyOutcome.REJECTED_POLICY_ERROR,
            reason="commit_failed",
            error=message,
            retry_budget_exhausted=True,
            attempts=attempt,
        )
        try:
            with self._state.atomic_commit():
                record = self._agency.commit(minimal)
        except AgencyEngineError:
            raise
        except BaseException as second:
            with self._gate:
                self._attempts[due.due_id] = attempt
            return ActivationResult(
                due_id=due.due_id,
                character_id=plan.character_id,
                outcome=ActivationOutcome.FAILED_TERMINAL,
                attempt=attempt,
                at=self.clock,
                detail={
                    "reason": "stuck",
                    "error": message,
                    "recording_error": f"{type(second).__name__}: {second}",
                    "still_pending": True,
                },
            )
        with self._gate:
            self._attempts.pop(due.due_id, None)
        return ActivationResult(
            due_id=due.due_id,
            character_id=record.character_id,
            outcome=ActivationOutcome.FAILED_TERMINAL,
            attempt=attempt,
            at=record.decided_at,
            agency_outcome=record.outcome,
            detail={"policy": record.policy, **_plain(record.detail)},
        )

    def _retry_result(
        self, due: ActivationDue, character_id: str, attempt: int, error: str
    ) -> ActivationResult:
        """一次可重试的失败：什么都没提交，到期记录仍然待处理。"""
        with self._gate:
            self._attempts[due.due_id] = attempt
        return ActivationResult(
            due_id=due.due_id,
            character_id=character_id or due.character_id or "",
            outcome=ActivationOutcome.FAILED_RETRYABLE,
            attempt=attempt,
            at=self.clock,
            detail={
                "error": error,
                "attempts": attempt,
                "max_attempts": self._retry.max_attempts,
                "still_pending": True,
            },
        )

    def _stopped_result(self, due: ActivationDue, *, attempt: int) -> ActivationResult:
        return ActivationResult(
            due_id=due.due_id,
            character_id=due.character_id or "",
            outcome=ActivationOutcome.STOPPED,
            attempt=max(attempt, 0),
            at=self.clock,
            detail={
                "reason": self._stop_reason or "not_started",
                "still_pending": True,
            },
        )

    def _record(self, result: ActivationResult) -> ActivationResult:
        with self._gate:
            self._results.append(result)
        return result

    # ── 服务 API（给 WEB-1 用的最小面） ─────────────────────────────────
    def status(self) -> Dict:
        """运行时此刻的样子。**一致快照**，而且每次返回全新的结构。

        整份在一次持锁里读出来，所以里面几个字段互相自洽 —— 不会出现"跨着
        一次 stop 读到一半"拼出来的那种 running=True 却已经停了的画面。
        代价是它会被一次进行中的提交挡住；要瞬时读取就用 running 属性。
        """
        with self._gate:
            return self._status_locked()

    def _status_locked(self) -> Dict:
        state = self._state
        log = state.agency
        return {
            "session_id": self.session_id,
            "name": self._name,
            "started": self._started,
            "running": self._running,
            # stop_requested 与 running 可以同时为 True：那就是"事务里登记了
            # 停机、还没生效"这个中间态，stopping 把它单独点出来。
            "stop_requested": self._stop_reason is not None,
            "stopping": self._stop_reason is not None and self._running,
            "stop_reason": self._stop_reason,
            "clock": self.clock.isoformat(),
            "scheduled": len(state.activations),
            "next_due_at": (
                self._scheduler.next_due_at().isoformat()
                if self._scheduler.next_due_at() is not None
                else None
            ),
            "pending_due_ids": [due.due_id for due in self._agency.pending_due()],
            "committed_actions": log.committed_actions(),
            "events": len(state.events),
            "memories": len(state.memories),
            "retry": self._retry.to_dict(),
            "in_flight_due_ids": sorted(self._in_flight),
            "outcomes": {
                outcome.value: sum(
                    1 for result in self._results if result.outcome is outcome
                )
                for outcome in ActivationOutcome
            },
            "agency_outcomes": {
                outcome.value: len(log.for_outcome(outcome))
                for outcome in AgencyOutcome
            },
        }

    def positions(self) -> Dict:
        """每个角色此刻在哪、挂着哪些频道、什么状态。

        每次重新从权威世界状态投影，交出去的是新的可变结构 —— 调用方改它
        影响不到世界（有测试盯着这一条）。
        """
        world = self.world
        return {
            character_id: {
                "location_id": world.location_of(character_id),
                "channels": list(world.channels_for(character_id)),
                "availability": world.availability_of(character_id).value,
            }
            for character_id in world.known_characters()
        }

    def recent_outcomes(self, limit: int = _RECENT) -> List[Dict]:
        """最近几条处理结果。

        它是**报告**，不是权威存储：权威的那份在 Agency 日志和投递箱里，
        而且跟着会话存档走。这个列表活在进程里，重启就没了 —— 但重启之后
        真正要接着处理的东西（还没被确认的到期记录）一条不少。
        """
        limit = self._require_limit(limit)
        with self._gate:
            recent = self._results[-limit:]
        return [result.to_dict() for result in recent]

    def recent_events(self, limit: int = _RECENT) -> List[Dict]:
        """世界历史里最近几条事件的投影（含系统侧 provenance）。

        这是**系统视角**的调试通道，跟角色上下文是两回事：它读的是全知的
        事件历史，所以任何渲染角色提示词的路径都不许读它。
        """
        limit = self._require_limit(limit)
        store = self._state.events
        return [
            {"sequence": store.sequence_of(event.event_id), **event.to_dict()}
            for event in store.events()[-limit:]
        ]

    @staticmethod
    def _require_limit(limit) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise AutonomyError(f"limit 必须是整数，收到 {limit!r}")
        if limit <= 0:
            raise AutonomyError(f"limit 必须大于 0，收到 {limit}")
        return limit

    def debug_projection(self) -> Dict:
        """只读的编排状态投影（JSON 安全），供测试和调试 UI 读。"""
        return {
            **self.status(),
            "positions": self.positions(),
            "recent_outcomes": self.recent_outcomes(),
        }


def _plain(value):
    """把只读视图复制成普通可变结构 —— 交出去的东西不能是内部引用。"""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


__all__ = ["AutonomousRuntime", "AutonomyError"]
