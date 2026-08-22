# pns/runtime/autonomy/driver.py — 有界的自主驱动
#
# P11 的协调器能把**一条**到期资格走完整条链，P12 能让一个世界活过进程重启，
# 但两者都没有回答一个很朴素的问题：**谁来推时间。** 在此之前答案是"测试
# 或者操作者手动调 advance()"，于是一个持久世界虽然开着，却一动不动。
#
# 这一层就是那个"谁"，而且它刻意只是那个"谁"：一个进程内的、有界的、可以
# 被显式启停的循环。它自己一条领域规则都不懂 —— 不选动作、不写台词、不判分、
# 不决定什么时候该存盘（那是 P12 的 CheckpointPolicy），它只负责按节拍去问
# 协调器"往前走一点"。
#
# 六条硬约束：
#
#   1. **一个世界至多一个 worker。** 执行它的是一把锁加一个 `_thread` 字段，
#      不是"调用方记得别按两次"。两个并发的 start() 里只有一个会造出线程，
#      另一个原样返回同一份状态。
#   2. **自动模型调用是 opt-in。** 建世界、恢复世界都不会起驱动 —— 起驱动
#      只有一条路：有人显式调用 start()。进程重启之后一样：驱动状态是进程内
#      的操作状态，它一个字节都不进存档，所以一个"重启前在跑"的世界重启之后
#      不会自己接着烧 API 额度。
#   3. **Stop 是可重启的暂停，不是终局。** 它绝不调用 P11 的
#      `AutonomousRuntime.stop()` —— 那一次是终局的（停过就不能再启动，见
#      coordinator.start()），拿它来"暂停一下"等于把世界一次性关死。
#   4. **Stop 说的是实话。** 它先关掉未来的 tick，再**有界地**等当前这一次
#      落定。等到了就报 stopped，等不到就报 stopping，绝不把"还在跑"说成
#      "停了"。报 stopped 的那一刻 worker 线程已经不存在，因此没有任何后续
#      tick 能提交。
#   5. **失败的 tick 不许变成忙等。** 每一轮之间恒有一次有界的等待，无论上
#      一轮成功还是失败。世界关了、运行时终局停机了 —— 这两种 worker 自己
#      收摊，不留一个每 N 秒失败一次的僵尸循环。
#   6. **驱动状态里不出现 provider 那侧的任何东西，日志里也不出现。** 未预期
#      类型的异常只留一句固定的话。理由跟 composition.py 里那段一样：异常的
#      类型名装得下一把 API Key，而日志跟 API 响应一样是外面看得见的地方。
#   7. **花费有两道边界，而且它们是两件不同的事。**
#      *单次 Start 的额度*（`max_activations_per_run`）是操作边界：它活在
#      进程里、只由一次显式 Start 重新装满，用完了 worker 自己停下并说清楚
#      原因。它回答"我按一下 Start，最多花多少"。
#      *世界一生的动作上限*（P9 的 `AgencyBudget`，从耐久 Agency 日志推导）
#      是累计边界：它跨重启、跨恢复都成立，也**必须**如此，否则重启一次就能
#      把上限再用一遍。驱动在每一轮之前拿同一个数字对一次，到了就**响亮
#      停机**——而不是让引擎从此把每一条激活都静静判成 rejected_budget，
#      让一个世界在没人看得出为什么的情况下永远失声。
#      两道边界共用**一个**权威数字：世界那道直接从引擎的预算上读，驱动这边
#      不另存一份，免得两个数字各说各话。
#
# 这个模块不 import 持久化层（那会跟 lifecycle → coordinator 的方向撞成一个
# 循环），它只鸭子类型地用世界句柄的四样东西：`world_id`、`runtime`、
# `closed`、`checkpoint_if_due()`。import 它不建线程、不碰磁盘、不起循环。
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

# 自动 checkpoint 的理由码。它会进存档的 last_checkpoint_reason，所以是一个
# 固定的、可以被搜索的词，不是一句随手写的话。
CHECKPOINT_REASON = "autonomy_tick"

# 驱动状态里错误文本的长度上限。
MAX_ERROR_CHARS = 300

# 未预期类型的异常对外那句固定的话。它是**常量**，不是模板。
OPAQUE_ERROR = "驱动 tick 遇到未预期类型的错误"

# 各项节拍的上界。安全预算，不是审美。
MAX_TICK_MINUTES = 24 * 60
MAX_INTERVAL_SECONDS = 3600.0
MAX_STOP_TIMEOUT_SECONDS = 300.0
MAX_ACTIVATIONS_PER_RUN = 100_000

# worker 自己收摊的两种"花完了"。它们是**固定的码**，会进状态投影，所以
# 前端可以据此说人话，而不用去匹配一句会改的中文。
EXIT_RUN_BUDGET = "run_budget_exhausted"
EXIT_WORLD_CAP = "world_action_cap"


class DriverError(RuntimeError):
    """这次驱动操作根本不该发生（世界已经关了、运行时已经终局停机）。"""


class DriverBusy(DriverError):
    """上一个 worker 还没走干净，现在起不了新的。

    它跟"已经在跑"是两件事：已经在跑是幂等成功，这一档是**说不清**，所以
    它必须是一次失败。静默返回成功会让操作者以为世界又动起来了。
    """


@dataclass(frozen=True)
class DriverConfig:
    """驱动的节拍。服务器侧配置，不接受浏览器传值。"""

    # 每次 tick 往前推多少**模拟**分钟。
    tick_minutes: int = 5
    # 两次 tick 之间的**真实**秒数。
    interval_seconds: float = 30.0
    # 一次 stop 最多等当前 tick 落定多少**真实**秒。等不到就如实报 stopping。
    stop_timeout_seconds: float = 10.0
    # **单次 Start** 最多处理多少条到期资格。一条至多一次生成 + 一次判分，
    # 所以它就是这一轮 API 调用次数的上限。
    #
    # 它刻意只活在进程里，而且只由一次显式 Start 重新装满：这样"我按一下
    # Start 会花多少"有一个操作者说得出口的答案，而"这个世界一生做过多少"
    # 由 P9 那个从耐久日志推导的上限单独管（见模块头第 7 条）。用完了不是
    # 失败，是这一轮跑到头了。
    max_activations_per_run: int = 200

    def __post_init__(self) -> None:
        if isinstance(self.tick_minutes, bool) or not isinstance(
            self.tick_minutes, int
        ):
            raise DriverError(f"tick_minutes 必须是整数，收到 {self.tick_minutes!r}")
        if not 1 <= self.tick_minutes <= MAX_TICK_MINUTES:
            raise DriverError(
                f"tick_minutes 必须落在 1–{MAX_TICK_MINUTES}，收到 {self.tick_minutes}"
            )
        if isinstance(self.max_activations_per_run, bool) or not isinstance(
            self.max_activations_per_run, int
        ):
            raise DriverError(
                f"max_activations_per_run 必须是整数，收到 {self.max_activations_per_run!r}"
            )
        if not 1 <= self.max_activations_per_run <= MAX_ACTIVATIONS_PER_RUN:
            raise DriverError(
                f"max_activations_per_run 必须落在 1–{MAX_ACTIVATIONS_PER_RUN}，"
                f"收到 {self.max_activations_per_run}"
            )
        for name, high in (
            ("interval_seconds", MAX_INTERVAL_SECONDS),
            ("stop_timeout_seconds", MAX_STOP_TIMEOUT_SECONDS),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise DriverError(f"{name} 必须是数字，收到 {value!r}")
            if not 0 < float(value) <= high:
                raise DriverError(f"{name} 必须落在 (0, {high}]，收到 {value}")

    def to_dict(self) -> Dict:
        return {
            "tick_minutes": self.tick_minutes,
            "interval_seconds": float(self.interval_seconds),
            "stop_timeout_seconds": float(self.stop_timeout_seconds),
            "max_activations_per_run": self.max_activations_per_run,
        }


def _safe_error(error: BaseException) -> str:
    """一次 tick 失败对外能说的那句话。

    仓库自己的异常（`pns.*`）原文交出去：操作者要靠它判断该重试还是该去看
    那块盘，而它们的文本是我们自己写的。**别的**一律只留一句固定的话 ——
    provider 那侧的异常连类型名都可能是一把 API Key
    （`type(api_key, (RuntimeError,), {})` 是合法 Python），所以只要还有任何
    一处从它派生的数据能过边界，这条边界就还是漏的。

    走到 else 分支说明协调器漏了一个它本该吞掉的异常 —— 那是个 bug，值得留下
    痕迹。但**痕迹里同样不许有原文**：日志跟 API 响应一样是"外面看得见的
    地方"，把 key 打进日志只是换个地方泄漏（composition.py 里那段同理）。
    所以这里只打一句固定的话，说明"这儿出过一次没见过的错"。
    """
    module = getattr(type(error), "__module__", "") or ""
    if module == "pns" or module.startswith("pns."):
        text = " ".join(f"{type(error).__name__}: {error}".split())
        if len(text) > MAX_ERROR_CHARS:
            return text[: MAX_ERROR_CHARS - 1] + "…"
        return text
    print("[autonomy-driver] tick 遇到未预期类型的错误（原文不外传）", flush=True)
    return OPAQUE_ERROR


class WorldDriver:
    """一个已经打开的持久世界的进程内驱动。

    它拥有的东西只有三样：一个 worker 线程、一个停止信号、以及一份"上一次
    tick 怎么样了"的操作记录。世界真相一样都不在它手上 —— 时钟、事件、观察、
    记忆全部仍然由 SessionState 和 P12 的句柄守着。所以这份状态**不进存档**，
    也不该进：它描述的是"这台服务器此刻在不在推这个世界"，不是"这个世界是
    什么样"。
    """

    def __init__(self, world, *, config: Optional[DriverConfig] = None) -> None:
        for attribute in ("world_id", "runtime", "checkpoint_if_due"):
            if not hasattr(world, attribute):
                raise DriverError(f"驱动需要一个持久世界句柄（缺 {attribute}）")
        self._world = world
        self._config = config if config is not None else DriverConfig()
        if not isinstance(self._config, DriverConfig):
            raise DriverError("config 必须是 DriverConfig")
        # 只保护自己的簿记。绝不在持有它的时候去 join 线程、调世界或运行时
        # 的方法 —— 那会把这把小锁挂到一次模型调用的时长上去。
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # worker 自认为还该继续跑。它由 worker 自己在退出前翻成 False，
        # 所以"线程还活着但已经在收尾"这个中间态是看得见的。
        self._alive = False
        self._stop_reason: Optional[str] = None
        self._exit_reason: Optional[str] = None
        self._ticks = 0
        self._failures = 0
        self._consecutive_failures = 0
        # **这一轮**已经处理掉多少条到期资格。只有一次显式 start() 会把它归零
        # ——幂等的第二次 start（已经在跑）走的是提前返回那条路，碰不到它，
        # 所以反复按 Start 刷不出额度来。
        self._processed = 0
        self._last_error: Optional[str] = None
        self._last_tick_at: Optional[str] = None
        self._last_tick: Optional[Dict] = None

    # ── 读 ──────────────────────────────────────────────────────────────
    @property
    def world(self):
        """被驱动的那个世界句柄。驱动不拥有它 —— 它只是按节拍去问它。"""
        return self._world

    @property
    def world_id(self) -> str:
        return self._world.world_id

    @property
    def config(self) -> DriverConfig:
        return self._config

    @property
    def running(self) -> bool:
        with self._lock:
            return self._state_locked() == "running"

    # ── 启停 ────────────────────────────────────────────────────────────
    def start(self) -> Dict:
        """开始按节拍推这个世界。幂等：已经在跑就原样返回同一份状态。

        它**不**碰 P11 的运行时启停 —— 那一份生命周期由 P12 在创建/恢复时
        管好了。这里管的只是"有没有人在推它"。
        """
        with self._lock:
            self._reap_locked()
            if self._thread is not None:
                state = self._state_locked()
                if state == "running":
                    # 已经在跑。第二次 start 不该造出第二个 worker，也不该
                    # 被当成一次失败。
                    return self._status_locked()
                raise DriverBusy(
                    f"世界 '{self.world_id}' 的驱动正在停止（{state}），"
                    "等它停干净再启动"
                )
            if getattr(self._world, "closed", False):
                raise DriverError(f"世界 '{self.world_id}' 已经关闭，不能再驱动它")
            actions = self._world_actions_locked()
            if actions["remaining"] is not None and actions["remaining"] <= 0:
                # 世界一生的动作上限已经到了。这一档**不是**"这一轮花完了"：
                # 它跨重启、跨恢复都成立（计数从耐久 Agency 日志推导），所以
                # 再按多少次 Start 都不会变。响亮拒绝，并且说清楚怎么解开 ——
                # 让它变成引擎里一条条静默的 rejected_budget，才是真正的
                # "世界莫名其妙不说话了"。
                raise DriverError(
                    f"世界 '{self.world_id}' 已经用掉一生的动作上限"
                    f"（{actions['committed']}/{actions['cap']}）。这个数字跨"
                    "重启和恢复都成立；要接着跑，先调高服务器侧的上限，再重新"
                    "打开这个世界"
                )
            runtime = self._world.runtime
            if not runtime.running:
                # P11 的终局停机已经发生。再起一个 worker 只会每 N 秒失败
                # 一次 —— 要接着跑，得从存档恢复出一个新的世界。
                raise DriverError(
                    f"世界 '{self.world_id}' 的运行时已经停止"
                    f"（{runtime.stop_reason}），驱动起不来；"
                    "要接着跑就重新恢复这个世界"
                )
            self._stop_event = threading.Event()
            self._stop_reason = None
            self._exit_reason = None
            self._alive = True
            # 新的一轮：额度在这里、而且**只在这里**重新装满。
            self._processed = 0
            self._spawn_locked(self._stop_event)
            return self._status_locked()

    def stop(self, reason: str = "stopped", timeout: Optional[float] = None) -> Dict:
        """请求暂停，并**有界地**等当前这一次 tick 落定。

        返回的 `state` 有且只有两种可能：

          * `stopped` —— worker 线程已经不存在了。当前 tick 已经整个结束
            （包括它那次提交），而且没有任何后续 tick 能再发生。
          * `stopping` —— 等超时了。当前那次 tick 还在跑，很可能正卡在一次
            模型调用上。这时**不许**说自己停了：一个还会落地一次提交的驱动，
            报成 stopped 就是一句会被后续事实拆穿的假话。

        幂等：已经停了的再停一次仍然返回 stopped。

        这里保证的是**"报 stopped 就真的停了"**，不是"返回值只有那两种"：
        如果另一个线程在这次 stop 落定之后紧接着又 start 了，这里会如实报
        `running`。那不是谎话，是那一刻的真相 —— 谁在同时按停止和开始，就该
        看到这个。
        """
        if not isinstance(reason, str) or not reason:
            raise DriverError("stop 的理由必须是非空字符串")
        wait = self._config.stop_timeout_seconds if timeout is None else float(timeout)
        if wait < 0:
            raise DriverError(f"timeout 不能是负数，收到 {timeout!r}")
        with self._lock:
            self._reap_locked()
            thread = self._thread
            if thread is None:
                return self._status_locked()
            if self._stop_reason is None:
                # 第一个理由才是真正的原因，后来的都是它的后果。
                self._stop_reason = reason
            self._stop_event.set()
        # 锁外等：join 可能要等一次模型调用回来，而状态查询不该被它堵住。
        thread.join(wait)
        with self._lock:
            self._reap_locked()
            return self._status_locked()

    def status(self) -> Dict:
        """驱动此刻的样子。一致快照，每次返回全新的结构。"""
        with self._lock:
            self._reap_locked()
            return self._status_locked()

    def _spawn_locked(self, stop_event: threading.Event) -> None:
        """造出 worker 并让它起跑。调用方持着锁 —— 这一整段是临界区。

        `_thread` 在线程**真的起跑之后**才赋值：一个"已登记、还没起跑"的线程
        会让 `_reap_locked()` 撞上 join() 一个没起跑的线程。锁保证了同一时刻
        只有一个线程在这段里，所以先起跑后登记不会漏掉 worker 自己的收尾
        （它的 finally 要拿同一把锁）。
        """
        thread = threading.Thread(
            target=self._run,
            args=(stop_event,),
            name=f"pns-autonomy-{self.world_id}",
            daemon=True,
        )
        thread.start()
        self._thread = thread

    # ── worker ──────────────────────────────────────────────────────────
    def _run(self, stop_event: threading.Event) -> None:
        try:
            while not stop_event.is_set():
                if not self._tick_once():
                    break
                # 无论上一轮成功还是失败，都在这里等满一个节拍：失败的 tick
                # 绝不能变成一个全速重试的忙循环。
                stop_event.wait(self._config.interval_seconds)
        finally:
            with self._lock:
                self._alive = False

    def _tick_once(self) -> bool:
        """跑一次。返回 False 表示这个 worker 该自己收摊了。"""
        world = self._world
        if getattr(world, "closed", False):
            self._finish("world_closed")
            return False
        runtime = world.runtime
        if not runtime.running:
            self._finish("runtime_stopped")
            return False

        # 两道花费边界，在**一轮开始之前**判 —— 判完再进 tick 的话，这一轮
        # 已经在花钱了。它们都不打断已经开始的那一轮：撕开一次处理会留下
        # 半截事务，比多花一轮严重得多。所以实际用量至多超出最后一轮那几条，
        # 这一点写在 status 里（used 会大于 limit），不假装没发生。
        with self._lock:
            if self._processed >= self._config.max_activations_per_run:
                self._finish_locked(EXIT_RUN_BUDGET)
                return False
        actions = self._world_actions()
        if actions["remaining"] is not None and actions["remaining"] <= 0:
            self._finish(EXIT_WORLD_CAP)
            return False

        try:
            report = runtime.advance(self._config.tick_minutes)
        except BaseException as e:  # noqa: BLE001 - 记下来，绝不让 worker 静默死掉
            return self._record_failure(e)

        checkpointed = None
        try:
            # 已经完成的权威边界。存不存由 P12 的 CheckpointPolicy 说了算 ——
            # 驱动不自己判断什么时候该存，它只负责报告"又过去一个边界"。
            checkpointed = world.checkpoint_if_due(CHECKPOINT_REASON)
        except BaseException as e:  # noqa: BLE001
            return self._record_failure(e)

        self._record_success(report, checkpointed)
        return True

    def _record_success(self, report: Dict, checkpointed: Optional[Dict]) -> None:
        results = report.get("results") or []
        outcomes: Dict[str, int] = {}
        for result in results:
            key = str(result.get("outcome", "unknown"))
            outcomes[key] = outcomes.get(key, 0) + 1
        with self._lock:
            self._ticks += 1
            # 每一条被处理的到期资格都花过钱（至多一次生成 + 一次判分），
            # 所以计的是"处理了几条"，不是"成了几条"——只算成功的话，一个
            # 每次都被判分拒掉的世界就能无限花下去。
            self._processed += len(results)
            self._consecutive_failures = 0
            self._last_error = None
            self._last_tick_at = datetime.now().isoformat(timespec="seconds")
            self._last_tick = {
                "from_clock": report.get("from_clock"),
                "to_clock": report.get("to_clock"),
                "minutes": report.get("minutes"),
                "due": len(report.get("due_ids") or []),
                "processed": len(results),
                "outcomes": outcomes,
                "checkpoint_revision": (
                    checkpointed.get("revision") if checkpointed else None
                ),
            }

    def _record_failure(self, error: BaseException) -> bool:
        """记一次失败的 tick，并回答"这个 worker 还该不该继续"。"""
        message = _safe_error(error)
        with self._lock:
            self._ticks += 1
            self._failures += 1
            self._consecutive_failures += 1
            self._last_error = message
            self._last_tick_at = datetime.now().isoformat(timespec="seconds")
            self._last_tick = {"failed": True}
        world = self._world
        if getattr(world, "closed", False):
            self._finish("world_closed")
            return False
        if not world.runtime.running:
            self._finish("runtime_stopped")
            return False
        return True

    def _finish(self, reason: str) -> None:
        with self._lock:
            self._finish_locked(reason)

    def _finish_locked(self, reason: str) -> None:
        """记下 worker 自己收摊的原因。调用方持着锁。第一个原因才算数。"""
        if self._exit_reason is None:
            self._exit_reason = reason

    # ── 内部 ────────────────────────────────────────────────────────────
    def _world_actions(self) -> Dict:
        with self._lock:
            return self._world_actions_locked()

    def _world_actions_locked(self) -> Dict:
        """这个世界一生用掉多少动作、上限是多少。

        两个数都从**权威那一侧**读，驱动不另存一份：用量来自耐久的 Agency
        日志（所以跨重启、跨恢复都成立），上限来自引擎自己的预算（所以驱动
        这道闸和引擎那道闸永远是同一个数字，不会各说各话）。

        读不出来就返回 None 而不是猜一个数：说不清的时候不该拦住操作者，
        真正的硬闸仍然在引擎里。
        """
        runtime = getattr(self._world, "runtime", None)
        committed = None
        cap = None
        try:
            committed = runtime.state.agency.committed_actions()
            cap = runtime.agency.budget.max_committed_actions_per_session
        except Exception:  # pragma: no cover - 读不到就当不知道
            pass
        remaining = None
        if isinstance(committed, int) and isinstance(cap, int):
            remaining = max(0, cap - committed)
        return {"committed": committed, "cap": cap, "remaining": remaining}

    def _run_budget_locked(self) -> Dict:
        limit = self._config.max_activations_per_run
        return {
            "limit": limit,
            "used": self._processed,
            "remaining": max(0, limit - self._processed),
        }

    def _reap_locked(self) -> None:
        """线程已经真的结束了就把它收掉。调用方持着锁。

        `join(0)` 只在线程已经死掉时调用，所以它不会在锁里等任何东西。
        """
        thread = self._thread
        if thread is not None and not thread.is_alive():
            thread.join(0)
            self._thread = None
            self._alive = False

    def _state_locked(self) -> str:
        if self._thread is None:
            return "stopped"
        if self._stop_event.is_set() or not self._alive:
            # 要么有人要它停、要么它自己在收摊。两种都还**没**停干净。
            return "stopping"
        return "running"

    def _status_locked(self) -> Dict:
        state = self._state_locked()
        runtime = self._world.runtime
        next_due = None
        try:
            due_at = runtime.scheduler.next_due_at()
            next_due = due_at.isoformat() if due_at is not None else None
        except Exception:  # pragma: no cover - 状态查询不该因为读队列而失败
            next_due = None
        return {
            "world_id": self.world_id,
            "state": state,
            # 三个布尔量是给 UI 用的，含义都取自 state 这一个真相。
            "running": state == "running",
            "stopping": state == "stopping",
            "stopped": state == "stopped",
            "stop_reason": self._stop_reason,
            "exit_reason": self._exit_reason,
            "ticks": self._ticks,
            "failures": self._failures,
            "consecutive_failures": self._consecutive_failures,
            "last_error": self._last_error,
            "last_tick_at": self._last_tick_at,
            "last_tick": dict(self._last_tick) if self._last_tick else None,
            "next_due_at": next_due,
            "cadence": self._config.to_dict(),
            # 两道花费边界，分开报：一道按 Start 重置，一道跟着世界一辈子。
            "run_budget": self._run_budget_locked(),
            "world_actions": self._world_actions_locked(),
        }


class DriverRegistry:
    """本进程里"哪个世界有驱动"的唯一账本。

    它跟 P12 的 `WorldLifecycleService` 是两本账，而且刻意不合并：那本记的是
    所有权与耐久性，这本记的是"这台服务器此刻在不在推它"。合并会让一次
    重启后的恢复顺手把驱动也带起来 —— 而自动模型调用必须是显式的。
    """

    def __init__(self, config: Optional[DriverConfig] = None) -> None:
        self._config = config if config is not None else DriverConfig()
        if not isinstance(self._config, DriverConfig):
            raise DriverError("config 必须是 DriverConfig")
        self._lock = threading.Lock()
        self._drivers: Dict[str, WorldDriver] = {}

    @property
    def config(self) -> DriverConfig:
        return self._config

    def get(self, world_id: str) -> Optional[WorldDriver]:
        with self._lock:
            return self._drivers.get(world_id)

    def for_world(self, world) -> WorldDriver:
        """取这个世界的驱动，没有就造一个。**造出来不等于跑起来。**

        句柄换了（世界被关掉又恢复过）就换一个驱动：旧驱动攥着的是一个已经
        关闭的句柄，拿它去推新世界只会每次都失败。
        """
        world_id = world.world_id
        with self._lock:
            driver = self._drivers.get(world_id)
            if driver is not None and driver.world is world:
                return driver
            if driver is not None and driver.status()["state"] != "stopped":
                raise DriverBusy(
                    f"世界 '{world_id}' 上一个句柄的驱动还没停干净"
                )
            driver = WorldDriver(world, config=self._config)
            self._drivers[world_id] = driver
            return driver

    def discard(self, world_id: str) -> None:
        with self._lock:
            self._drivers.pop(world_id, None)

    def stop(self, world_id: str, reason: str, timeout: Optional[float] = None):
        """停这个世界的驱动；本来就没有驱动就返回 None。"""
        driver = self.get(world_id)
        if driver is None:
            return None
        return driver.stop(reason, timeout)

    def stop_all(self, reason: str, timeout: Optional[float] = None) -> List[Dict]:
        with self._lock:
            drivers = list(self._drivers.values())
        return [driver.stop(reason, timeout) for driver in drivers]

    def statuses(self) -> Dict[str, Dict]:
        with self._lock:
            drivers = dict(self._drivers)
        return {world_id: driver.status() for world_id, driver in drivers.items()}


__all__ = [
    "CHECKPOINT_REASON",
    "EXIT_RUN_BUDGET",
    "EXIT_WORLD_CAP",
    "MAX_ACTIVATIONS_PER_RUN",
    "MAX_ERROR_CHARS",
    "OPAQUE_ERROR",
    "DriverBusy",
    "DriverConfig",
    "DriverError",
    "DriverRegistry",
    "WorldDriver",
]
