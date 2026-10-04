# pns/runtime/persistence/lifecycle.py — 一个持久世界的完整生命周期
#
#     创建或恢复 → 拿下独占所有权 → 绑定运行时服务与适配器
#                → 运行 / 在安全边界上 checkpoint
#                → 停止并等在跑的事务落定 → 写完整存档 → 归还所有权
#                → 重启之后恢复出**同一个**权威世界
#
# 这一层不拥有它编排的任何一份状态：世界真相仍然在 SessionState 里，调度、
# Agency、记忆、自主运行时仍然各自守着自己的权威。它只负责"这个世界什么时候
# 落到磁盘上、谁有权动它、以及怎么把它完整地拿回来"。
#
# 七条硬约束：
#
#   1. **快照只在一条安全边界上取，而且那是互斥、不是一次检查。** checkpoint
#      按固定顺序拿两把锁：协调器闸门（start、stop 和"这次写入准不准"共用的
#      那把），然后是会话自己的独占边界 —— 也就是 atomic_commit() 全程攥着的
#      同一把锁。两把都要：闸门管得住协调器发起的提交，管不住调度器的时间
#      推进和事件提交层，它们直接开 atomic_commit()。
#      "查一下有没有人在事务里"是不够的：查是一个时刻的观察，查完到 to_dict()
#      之间那段窗口里，一次时间推进照样能开起来跟快照并排跑，撕开的方向只是
#      反过来而已。所以两件事共用一把锁、互相排队：有事务在跑就等它做完，
#      有快照在跑就开不了事务。锁是在 atomic_commit() 建回滚快照**之前**拿的，
#      不是之后 —— 否则"已经决定要提交、正在建快照"那段时间里状态看起来还是
#      空闲的。事务**内部**取快照一律拒绝：两把锁都可重入，会放行自己，而那
#      一刻的世界是半截的。
#      锁顺序（全局唯一一条）：**闸门 → 会话边界**。反过来拿本该是一次死锁，
#      所以等边界带**上限**：等不到就响亮失败、把闸门还回去，让系统自己解开，
#      而不是永远挂着。
#   2. **快照在边界里取，写盘在边界外做。** 一次 fsync 不该让停机跟着卡住。
#      边界里只做 to_dict()（确定性、纯内存），序列化和写盘在外面。
#   3. **保存失败绝不推进修订号。** 修订号是"磁盘上那一份是第几版"，不是
#      "我打算写第几版"。失败之后修订号原地不动、错误留在 last_error 里，
#      dirty 继续如实回答"状态跟磁盘上那一份一不一样"。
#      **唯一的例外方向相反**：目录同步失败（ArchiveNotDurable）时那一版
#      **已经在磁盘上、读者已经读得到**，所以账必须按"已经发生"记 —— 修订号
#      照常往前走，否则下一次会拿同一个号写不一样的内容。但话按"保证不到"
#      说：durable 记 False、错误留着、照样抛，没人能把它当成干净的一次保存。
#   4. **写之前先确认所有权仍然成立。** 锁挂在 inode 上：锁文件被删掉之后，
#      下一个进程一拿就拿到，而这一个还以为自己是唯一的写手。防不住那次删除，
#      但能把"两个写手静静互相覆盖"变成"第二笔就响亮失败"。
#   5. **关闭顺序固定**：停准入 → 等在跑的事务落定 → 最后一次 checkpoint →
#      标记关闭 → 归还所有权。最后一次 checkpoint 失败时，既不许说自己干净
#      关闭了，也不许把所有权还回去 —— 那等于宣布"磁盘上那一份是安全的"。
#      要放弃就必须显式 force，而且如实报告丢掉了什么。
#   6. **数据恢复和服务绑定是两步。** 先从存档恢复出一份冷 SessionState，再用
#      调用方交出来的冷适配器显式绑调度器、Agency、记忆和自主运行时。存档里
#      没有、也不该有任何能变成活服务的东西。
#   7. **恢复失败不留锁。** 存档损坏、版本不认识、身份对不上、适配器起不来 ——
#      任何一种，都要把刚拿到的所有权还回去。一次失败的恢复不该让世界永久锁死。
#
# 恢复边界（这是本层最重要的一句实话）：崩溃之后能恢复到的只有
# **最后一次成功的 checkpoint**。那之后的内存工作会丢 —— 这里没有 WAL、
# 没有事件重放、没有零丢失保证，也不打算假装有。已经落箱但还没被确认的到期
# 资格会在恢复之后重跑：交接的一次性由 P9 的投递箱挡着，所以重跑不会变成
# 重复提交。
import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, Optional, Tuple

from pns.models.clock_anchor import ClockAnchor, utc_now
from pns.models.cognition import RENEWAL_POLICIES
from pns.models.session import SessionState, TransactionBoundaryError
from pns.runtime.agency.engine import AgencyEngine
from pns.runtime.autonomy.clock_worker import ClockConfig, ClockWorker
from pns.runtime.autonomy.coordinator import AutonomousRuntime, AutonomyError
from pns.runtime.formal_world import RhythmGate, rhythm_gate
from pns.runtime.memory.encoder import MemoryEncoder
from pns.models.time_events import TimeEventPolicy, TimeEventPolicyError, quiet_time_report
from pns.runtime.persistence.archive import ArchiveError, EventSegment, WorldArchive
from pns.runtime.persistence.naming import validate_world_id
from pns.runtime.persistence.ownership import (
    OwnershipError,
    OwnershipHandle,
    WorldAlreadyOwned,
)
from pns.runtime.persistence.store import (
    ArchiveNotDurable,
    ArchiveNotFound,
    StorageError,
    WorldStore,
)
from pns.runtime.rhythm import RhythmDirector
from pns.runtime.scheduler import PersistentScheduler


class LifecycleError(RuntimeError):
    """这次生命周期操作不被允许，或者做不到。"""


class CheckpointError(LifecycleError):
    """这次 checkpoint 没有落地。磁盘上仍然是上一份完整存档。"""


class ContentNotAdopted(LifecycleError):
    """作息门不齐，这次恢复没有让世界运行（CONTENT-4 过渡设计 v2）。

    抛出它的时候，事情已经全部办完：新的内容冲突已经落盘，世界已经正常关闭、
    所有权已经释放。所以它不是"恢复失败、状态未知"，而是一个确定的结果：世界
    停在关闭状态，一分钟也没走，等项目所有者对 `gate` 里的阻断项做出决定。
    """

    def __init__(self, world_id: str, gate: RhythmGate) -> None:
        self.world_id = world_id
        self.gate = gate
        blocked = "、".join(
            f"{subject.subject}（{subject.reason}）" for subject in gate.blocked()
        )
        super().__init__(
            f"世界 '{world_id}' 的作息内容待决，未恢复运行：{blocked}。"
            "冲突已经存盘，世界已关闭；用内容维护脚本记下决定后再恢复"
        )


@dataclass(frozen=True)
class RuntimeAdapters:
    """调用方交出来的**冷**适配器：一个世界跑起来需要、但存档里没有的东西。

    判分器、策略、预算、重试策略 —— 它们是代码和配置，不是世界状态，所以它们
    每次都由调用方重新交出来，而不是从存档里"恢复"出来。策略给的是工厂而不是
    实例：策略常常要绑在**这一份**恢复出来的状态上（比如召回服务），交实例
    就会出现一个绑着旧状态的策略在新世界上做决定。
    """

    auditor: object
    policy_factory: Optional[Callable[[SessionState], object]] = None
    budget: Optional[object] = None
    memory_budget: Optional[object] = None
    retry: Optional[object] = None
    recall_budget: Optional[object] = None
    # 只在**创建**新世界时交进来的初始排期播种器。它在第一份存档写下去之前
    # 跑，所以它排下去的东西是这个世界与生俱来的一部分。
    #
    # 恢复路径**拿不到**它，而且这是一道真闸不是一句约定：restore() 见到带
    # 播种器的适配器会响亮失败（见下面）。理由是机制性的 —— 存档里已经带着
    # 这个世界自己的队列，恢复时再播一遍，这些角色就会每个周期被叫醒两次、
    # 花两份 API 额度，而"多了一条排期"在状态面上跟正常世界长得一模一样。
    seed: Optional[Callable[[SessionState], None]] = None
    # 内容作者写下的日常作息表集合。它跟判分器、策略同一档：是**内容与代码**，
    # 不是世界状态，所以创建和恢复都由调用方重新交出来，存档里不存它。
    #
    # 恢复路径**照样**要交（跟 seed 相反）：作息表不是这个世界与生俱来的一次性
    # 播种，而是它每一次推进时间都要对照的那张表。恢复之后不交，世界就会停在
    # 最后一次记下来的活动上，永远不再跟着时间走。
    rhythm: Optional[RhythmDirector] = None
    # 作息表来自哪一版内容快照。正式世界据此记冲突待决记录（见 formal_world）。
    content_revision: int = 0
    # 有限 Start 的续额策略 id（COG-1）。来自代码里的正式世界定义；跟作息一样
    # 创建和恢复都由调用方交，存档里的授权只记它、不决定它。
    allowance_renewal: Optional[str] = None
    name: str = "autonomy"

    def __post_init__(self) -> None:
        if self.auditor is None or not callable(getattr(self.auditor, "audit", None)):
            raise LifecycleError("运行时适配器必须包含一个提供 audit() 的判分器")
        if self.policy_factory is not None and not callable(self.policy_factory):
            raise LifecycleError("policy_factory 必须是可调用对象")
        if self.seed is not None and not callable(self.seed):
            raise LifecycleError("seed 必须是可调用对象")
        if self.rhythm is not None and not isinstance(self.rhythm, RhythmDirector):
            # 在这里判，而不是等到 bind()：bind() 发生在所有权已经拿走之后，
            # 那时失败会留下一个没人能用、又已经被占住的世界。
            raise LifecycleError("rhythm 必须是 RhythmDirector")
        if self.allowance_renewal is not None and self.allowance_renewal not in RENEWAL_POLICIES:
            raise LifecycleError(f"未知的续额策略: {self.allowance_renewal!r}")

    def bind(self, state: SessionState) -> AutonomousRuntime:
        """把服务显式绑到这份**已经恢复好**的状态上。

        四步都写出来，不靠任何一层"没有就顺手建一个"的默认行为：读代码的人
        应该一眼看见这个世界有哪几个服务，以及它们绑的是同一份状态。
        """
        runtime, _ = self.bind_gated(state)
        return runtime

    def bind_gated(
        self, state: SessionState
    ) -> Tuple[AutonomousRuntime, Optional[RhythmGate]]:
        """同 `bind`，另外交出作息门的结果。

        门的结果是 None 表示这个世界不设作息门（没有作息、或不是正式世界）。
        """
        if not isinstance(state, SessionState):
            raise LifecycleError("只能把服务绑在 SessionState 上")
        # 持久世界的时钟只归调度器推：在任何回调拿到这份状态之前声明。
        state.claim_clock()
        if state.scheduler is None:
            PersistentScheduler(state)
        if self.rhythm is not None:
            # 作息世界的时钟只归协调器的时钟步推。在把调度器交给 seed /
            # policy_factory 之前就声明，绑定期间也没有旁路（复审 R3-F1）。
            state.scheduler.claim_clock_for_rhythm()
        if self.seed is not None:
            # 新世界的初始排期。它在第一份存档之前落进队列，所以要么这个世界
            # 带着排期诞生，要么它根本没诞生 —— 没有第三种结果。
            self.seed(state)
        policy = self.policy_factory(state) if self.policy_factory is not None else None
        if state.agency_engine is None:
            AgencyEngine(state, policy=policy, budget=self.budget)
        elif policy is not None:
            raise LifecycleError(
                "这份状态已经绑过 Agency 引擎，再交一个策略进来会得到两份"
                "互相看不见的'谁在替这些角色做决定'"
            )
        if state.memory_encoder is None:
            MemoryEncoder(state, self.memory_budget)
        rhythm = self.rhythm
        gate = None
        if rhythm is not None and state.content is not None:
            # 正式世界：只有这个世界明确采用过的那一版作息才生效。内容包里出现
            # 新的一版，记一条待决、不生效，等项目所有者决定（WORLD-1 §4.3）。
            with state.atomic_commit():
                gate = rhythm_gate(
                    state,
                    {cid: rhythm.rhythm_for(cid) for cid in rhythm.characters()},
                    registry_revision=self.content_revision,
                    wall=utc_now().isoformat(),
                )
            rhythm = RhythmDirector(gate.accepted)
        runtime = AutonomousRuntime(
            state,
            auditor=self.auditor,
            retry=self.retry,
            recall_budget=self.recall_budget,
            rhythm=rhythm,
            allowance_renewal=self.allowance_renewal,
            name=self.name,
        )
        return runtime, gate


@dataclass(frozen=True)
class CheckpointPolicy:
    """什么时候存。

    最小面是**手动 + 干净关闭**，这两条永远在。自动 checkpoint 是可选的，
    而且刻意做成"在已经完成的权威边界上、由驱动方来问一句"：

      * 它没有后台写手线程。一次事件一个写手，会让写盘次数跟世界活跃度成
        正比，而且那些写手会各自在不同时刻取快照。
      * 它是**合并**的：`every_boundaries` 个边界之内最多写一次，
        `min_interval_seconds` 之内也最多写一次。
      * 它跟手动 checkpoint 走**同一条**边界判断，没有"因为是自动的所以
        放行"这种捷径。
    """

    every_boundaries: Optional[int] = None
    min_interval_seconds: float = 0.0
    on_close: bool = True

    def __post_init__(self) -> None:
        if self.every_boundaries is not None:
            if isinstance(self.every_boundaries, bool) or not isinstance(
                self.every_boundaries, int
            ):
                raise LifecycleError("every_boundaries 必须是整数")
            if self.every_boundaries < 1:
                raise LifecycleError("every_boundaries 必须 ≥ 1")
        if not isinstance(self.min_interval_seconds, (int, float)) or isinstance(
            self.min_interval_seconds, bool
        ):
            raise LifecycleError("min_interval_seconds 必须是数字")
        if self.min_interval_seconds < 0:
            raise LifecycleError("min_interval_seconds 不能是负数")

    def due(self, boundaries: int, seconds_since_last: Optional[float]) -> bool:
        if self.every_boundaries is None:
            return False
        if boundaries < self.every_boundaries:
            return False
        if (
            self.min_interval_seconds
            and seconds_since_last is not None
            and seconds_since_last < self.min_interval_seconds
        ):
            return False
        return True

    def to_dict(self) -> Dict:
        return {
            "every_boundaries": self.every_boundaries,
            "min_interval_seconds": self.min_interval_seconds,
            "on_close": self.on_close,
        }


def _held_report(gate: Optional[RhythmGate]) -> Optional[Dict]:
    """搁置报告：为什么这个世界打开着却不运行。每次返回全新的结构。"""
    if gate is None:
        return None
    return {
        "reason": "content_pending",
        "subjects": [subject.to_dict() for subject in gate.blocked()],
    }


def _fingerprint(state: SessionState) -> Optional[Tuple]:
    """一份"权威状态变过没有"的指纹。读不到一致的一份就返回 None。

    它由两部分组成，而且两部分都可以论证：

      * **各条日志的长度。** 事件、观察、曝光判定、Agency 记录、记忆、轮次
        都是只追加的，回滚只会把它们截回事务开始时的长度。checkpoint 只在
        事务之外发生，所以存下去的那些条目之后不会再被撤销 —— 长度相等就
        意味着内容相同的前缀，也就意味着内容相同。
      * **世界状态的摘要。** 位置、频道成员、可用性这些不体现在长度里的东西
        走一次哈希。
    """
    world = state.world_state
    try:
        digest = hashlib.sha256(
            json.dumps(world.to_dict(), ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest()
    except (RuntimeError, TypeError, ValueError):
        # 状态查询刻意不拿边界（拿了的话，一次进行中的提交会把"现在怎么样"
        # 这个问题也堵住），所以这一遍有可能撞上一次正在改的世界，比如
        # "dictionary changed size during iteration"。
        #
        # 这里返回 None，调用方把它当成"不确定 → 按脏算"。方向是刻意的：
        # 宁可多报一次"有没存的工作"，也不能在读不准的时候说"干净"。
        return None
    return (
        world.clock.isoformat(),
        state.status,
        len(state.events),
        len(state.turns),
        len(state.observations),
        len(state.exposures),
        len(state.agency),
        len(state.memories),
        len(state.activations),
        len(state.activation_outbox),
        sum(len(items) for items in state.histories.values()),
        # 运维账本：只按了一次 Start、只记了一段处置，世界也是"变过了"——
        # 否则 checkpoint 策略会当它干净，一次崩溃就把这些操作悄悄丢掉。
        len(state.cognition.intervals) if state.cognition is not None else None,
        len(state.rhythm_dispositions),
        state.anchor.to_dict() if state.anchor is not None else None,
        state.content.to_dict() if state.content is not None else None,
        state.time_events.to_dict() if state.time_events is not None else None,
        digest,
    )


def _abandon(state: Optional[SessionState]) -> None:
    """一次失败的创建 / 恢复：关上那份状态，不让它作为一个半成品继续可写。"""
    if state is None:
        return
    try:
        state.fence("lifecycle assembly failed")
    except Exception:  # pragma: no cover - 收尾不该盖住原始错误
        pass


def _footprint(
    world_bytes: Optional[int], segments: Tuple[EventSegment, ...], *, active: int
) -> Dict:
    """存档在磁盘上占了多少、分成了几卷（WORLD-1 存档增长设计 §2.5）。"""
    sealed_bytes = sum(segment.bytes for segment in segments)
    return {
        "total_bytes": (world_bytes or 0) + sealed_bytes,
        "world_bytes": world_bytes,
        "segments": len(segments),
        "sealed_events": segments[-1].last_sequence + 1 if segments else 0,
        "sealed_bytes": sealed_bytes,
        "active_events": active,
    }


class PersistentWorld:
    """一个已经拿下所有权、正在跑、并且能被完整存下来的世界。

    句柄本身是线程安全的：checkpoint、close 和状态读取都串在同一把世界锁上。
    这把锁跟 P11 的闸门有严格的顺序 —— **先世界锁，再闸门**，而且判断"我是不是
    正在自己的事务里"发生在拿世界锁**之前**。反过来会死锁：一个线程拿着世界锁
    在闸门上等，另一个线程在事务里（持着闸门）来拿世界锁。
    """

    def __init__(
        self,
        *,
        world_id: str,
        store: WorldStore,
        state: SessionState,
        runtime: AutonomousRuntime,
        ownership: OwnershipHandle,
        revision: int,
        saved_at: Optional[str],
        checkpoint_policy: CheckpointPolicy,
        service: Optional["WorldLifecycleService"] = None,
        snapshot_timeout: Optional[float] = None,
        durable: Optional[bool] = None,
        directory_synced: Optional[bool] = None,
        segments: Tuple[EventSegment, ...] = (),
        baseline: Optional[Tuple] = None,
    ) -> None:
        self._world_id = world_id
        self._store = store
        self._state = state
        self._runtime = runtime
        self._ownership = ownership
        self._revision = revision
        self._saved_at = saved_at
        self._policy = checkpoint_policy
        self._service = service
        # 等一次事务让路的上限。None 用 SessionState 那边的默认值。
        self._snapshot_timeout = snapshot_timeout
        self._lock = threading.RLock()
        self._closed = False
        self._clean = False
        self._last_error: Optional[str] = None
        self._last_reason: Optional[str] = None
        self._boundaries = 0
        self._last_checkpoint_at: Optional[datetime] = None
        # 磁盘上那一版的指纹。恢复路径必须交进来**读档那一刻**的指纹：绑定适配器
        # 时可能已经改了状态（记下内容冲突、认知转换），那些改动还没落盘，不能被
        # 当成"干净"（全量审查 F4）。
        self._fingerprint = baseline if baseline is not None else _fingerprint(state)
        # 最后一次保存的耐久性证据。True/False 只由本进程亲自完成的保存得出；
        # 从存档恢复时没有携带这份文件系统证据，因此必须是 None（未知），不能
        # 因为文件此刻读得出来就把过去一次未经目录同步的保存重新说成耐久。
        self._durable = durable
        self._directory_synced = directory_synced
        # 世界时钟 worker（WORLD-1 设计 §7）。按现实时间走的世界才有；由生命周期
        # 服务在句柄登记之后启动，关闭时第一个停。
        self._clock_worker: Optional[ClockWorker] = None
        # 磁盘上那一版的分卷清单（存档版本 3）。只随成功写下去的一版改变；
        # checkpoint 的快照只序列化清单之后的事件。
        self._segments: Tuple[EventSegment, ...] = tuple(segments)
        # 作息门不齐时的搁置报告（CONTENT-4 过渡设计 v2）。只由恢复路径在登记
        # 之前设置；设置时运行时已经终局停止，这个世界不会再运行。
        self._held: Optional[RhythmGate] = None

    # ── 读 ──────────────────────────────────────────────────────────────
    @property
    def world_id(self) -> str:
        return self._world_id

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def runtime(self) -> AutonomousRuntime:
        return self._runtime

    @property
    def revision(self) -> int:
        """磁盘上那一份是第几版。**不是**"我打算写第几版"。"""
        return self._revision

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def clock_worker(self) -> Optional[ClockWorker]:
        return self._clock_worker

    @property
    def held(self) -> Optional[Dict]:
        """作息门不齐、运行时已终局停止时的报告；正常世界是 None。"""
        return _held_report(self._held)

    # ── checkpoint ──────────────────────────────────────────────────────
    def checkpoint(self, reason: str = "manual") -> Dict:
        """在一个安全边界上，把这个世界完整地写下去。

        成功之后：修订号 +1，dirty 归零，磁盘上是这一刻的完整状态。
        失败之后：抛 CheckpointError，修订号不动，磁盘上仍然是上一份完整存档，
        错误留在 status()["last_error"] 里。两者之间没有第三种结果。
        """
        self._refuse_inside_transaction("checkpoint")
        with self._lock:
            if self._closed:
                raise LifecycleError(f"世界 '{self._world_id}' 已经关闭，不能再存")
            return self._checkpoint_locked(reason)

    def checkpoint_if_due(self, reason: str = "auto") -> Optional[Dict]:
        """记一次已经完成的权威边界，并且**在需要时**存一次。

        驱动方在每处理完一条到期资格之后调一次。它自己就是一次边界计数，
        所以计数和写盘的判断是同一次，不会出现"记了边界但没人来问"。
        """
        self._refuse_inside_transaction("checkpoint")
        with self._lock:
            if self._closed:
                raise LifecycleError(f"世界 '{self._world_id}' 已经关闭，不能再存")
            self._boundaries += 1
            since = None
            if self._last_checkpoint_at is not None:
                since = (datetime.now() - self._last_checkpoint_at).total_seconds()
            if not self._policy.due(self._boundaries, since):
                return None
            return self._checkpoint_locked(reason)

    def _checkpoint_locked(self, reason: str) -> Dict:
        revision = self._revision + 1
        # 写之前先确认所有权仍然成立。锁挂在 inode 上，有人把锁文件删掉之后
        # 下一个进程照样能拿到这个世界 —— 那一刻两个进程都以为自己是拥有者。
        # 这一步把"两个写手静静互相覆盖"变成"第二笔就响亮失败"。
        self._ownership.verify()
        payload, fingerprint = self._snapshot_locked()
        # 边界之外：序列化 + 写盘。一次 fsync 不该让停机跟着卡住。
        archive = None
        try:
            archive = WorldArchive.from_state_payload(
                self._world_id, payload, revision=revision, segments=self._segments
            )
            # 先封存已满的整段（分卷先耐久），再写 world.json 让清单生效。
            archive = self._store.seal(archive)
            result = self._store.save(archive)
        except ArchiveNotDurable as e:
            # 特殊的一档，而且方向跟下面那档相反：这一版**已经在磁盘上**、
            # 读者现在读到的就是它，只是那次改名扛不扛得住掉电证实不了。
            #
            # 所以账要按"已经发生"记 —— 修订号必须往前走，否则下一次
            # checkpoint 会用同一个号写不一样的内容，而修订号本该能认出内容。
            # 话要按"保证不到"说 —— durable 记 False，错误留着，而且照样抛，
            # 于是没有任何人能把这次 checkpoint 当成干净的。
            self._adopt(archive, fingerprint, reason, durable=False, synced=False)
            self._last_error = f"{type(e).__name__}: {e}"
            raise CheckpointError(
                f"世界 '{self._world_id}' 的第 {revision} 版已经写下去了，但它的"
                f"耐久性证实不了: {e}"
            ) from e
        except (StorageError, ArchiveError) as e:
            self._last_error = f"{type(e).__name__}: {e}"
            raise CheckpointError(
                f"世界 '{self._world_id}' 的 checkpoint 失败，磁盘上仍然是第 "
                f"{self._revision} 版: {e}"
            ) from e
        except BaseException as e:
            # 意料之外的中断（KeyboardInterrupt 之类）可能落在 replace 之后：先对一下
            # 磁盘，写上去了就按"已经发生、保证不到"记账，再原样抛出。
            if archive is not None and self._disk_holds(archive):
                self._adopt(archive, fingerprint, reason, durable=False, synced=False)
            self._last_error = f"{type(e).__name__}: {e}"
            raise
        self._adopt(
            archive,
            fingerprint,
            reason,
            durable=True,
            synced=result.directory_synced and archive.history_synced,
        )
        return self._status_locked()

    def _disk_holds(self, archive: WorldArchive) -> bool:
        """磁盘上此刻是不是正好这一版（修订号与保存时刻都对得上）。"""
        try:
            on_disk = self._store.load(self._world_id, history=False)
        except Exception:
            return False
        return on_disk.revision == archive.revision and on_disk.saved_at == archive.saved_at

    def _snapshot_locked(self) -> Tuple[Dict, Tuple]:
        """在独占边界之内取一份一致快照。调用方持着世界锁。

        边界是**互斥**，不是一次检查：进得去就说明没有任何事务在跑，而且块
        结束之前也开不起来。有事务正在跑就在这里等它做完。

        取快照过程里的任何失败都翻译成 CheckpointError —— 等不到边界、边界内
        仍然读不出一致状态、序列化炸了，对调用方都是同一件事：**这次没存成，
        磁盘一个字节没动**。让原始异常漏出去只会逼调用方去 catch 一堆东西。
        """
        try:
            with self._runtime.lifecycle_boundary(self._snapshot_timeout):
                sealed = self._segments[-1].last_sequence + 1 if self._segments else 0
                payload = self._state.to_dict(events_from=sealed)
                fingerprint = _fingerprint(self._state)
                if fingerprint is None:
                    # 边界攥着的时候不该发生。真发生了就说明有代码绕过
                    # atomic_commit() 在改状态 —— 那这份快照本身也不可信。
                    raise TransactionBoundaryError(
                        f"世界 '{self._world_id}' 在独占边界内仍然读不出一致"
                        "状态：有代码绕过 atomic_commit() 在改它"
                    )
                return payload, fingerprint
        except Exception as e:
            self._last_error = f"{type(e).__name__}: {e}"
            raise CheckpointError(
                f"世界 '{self._world_id}' 此刻取不到一致快照，磁盘上仍然是第 "
                f"{self._revision} 版: {e}"
            ) from e

    def _adopt(
        self,
        archive: WorldArchive,
        fingerprint: Tuple,
        reason: str,
        *,
        durable: bool,
        synced: bool,
    ) -> None:
        """把"磁盘上现在是哪一版"记下来。调用方持着世界锁。"""
        self._revision = archive.revision
        self._saved_at = archive.saved_at
        self._fingerprint = fingerprint
        self._boundaries = 0
        self._last_checkpoint_at = datetime.now()
        self._last_error = None
        self._last_reason = reason
        self._durable = durable
        self._directory_synced = synced
        self._segments = archive.segments

    # ── 关闭 ────────────────────────────────────────────────────────────
    def close(self, reason: str = "closed", *, force: bool = False) -> Dict:
        """按固定顺序收尾：停准入 → 等事务落定 → 存 → 标记关闭 → 还所有权。

        `force=True` 是**放弃**这个世界：最后一次 checkpoint 失败时照样把所有权
        还回去，代价是最后一次成功 checkpoint 之后的工作确实丢了。它不会被自动
        触发 —— 丢工作必须是一次明确的人为决定，而且返回的状态里
        `clean=False`、`durable_revision` 写着真正能恢复到的那一版。
        """
        self._refuse_inside_transaction("关闭世界")
        # 0. 先停时钟 worker，而且在世界锁**之外**：它自己的 checkpoint 要拿这把锁，
        #    持锁等它只会等到超时。等不到它退出（多半卡在一次模型调用上）时，
        #    非 force 的关闭拒绝——不存、不标记、不还所有权，调用方稍后重试；
        #    force 则往下走：终局停机之后它晚到的提交会被运行时拒绝。
        worker = self._clock_worker
        if worker is not None and not self._closed:
            if not worker.stop() and not force:
                raise LifecycleError(
                    f"世界 '{self._world_id}' 的时钟 worker 还没停下（多半在等一次"
                    "模型调用），暂不能关闭；稍后重试"
                )
        with self._lock:
            if self._closed:
                return self._status_locked()

            # 1. 停准入，并且等在跑的那次提交整个结束。
            stop_status = self._runtime.stop(reason)
            if stop_status.get("running"):
                # 只登记、还没生效。只可能发生在事务内部调用 —— 上面那道检查
                # 已经挡掉了本线程的情况，走到这里说明协调器的语义变了。
                raise LifecycleError(
                    f"世界 '{self._world_id}' 的停机只登记、尚未生效，"
                    "此刻关闭会在'已经关了'之后还让提交落地"
                )

            # 2. 最后一次 checkpoint。
            clean = True
            if self._policy.on_close:
                try:
                    self._checkpoint_locked(reason)
                except (CheckpointError, OwnershipError) as e:
                    # 存不下去有两种：写盘失败（CheckpointError），以及所有权
                    # 已经不成立了（OwnershipError，锁文件被人删了/换了）。
                    # 两种都不许宣布干净关闭。
                    clean = False
                    self._last_error = f"{type(e).__name__}: {e}"
                    if not force:
                        # 既不宣布干净关闭，也不还所有权：还回去等于宣布磁盘上
                        # 那一份就是最新的，而它不是。
                        raise

            # 3./4. 关上内存状态，标记关闭，归还所有权。fence 必须在归还所有权
            # 之前：此后任何还握着旧引用的线程（醒得太晚的 worker）走受支持的
            # 写方法都会失败，不会跟接手这个世界的下一个进程并行写。
            self._state.fence(f"closed: {reason}")
            self._closed = True
            self._clean = clean
            self._ownership.release()
            if self._service is not None:
                self._service._forget(self._world_id)
            return self._status_locked()

    def release(self, reason: str = "released") -> Dict:
        """**不存**，直接归还所有权。进程收尾用。

        它跟 close(force=True) 的区别只有一个：它连试都不试。所以它同样把
        clean 报成 False —— 最后一次成功 checkpoint 之后的工作丢了。

        它仍然先停准入：所有权都还回去了，运行时却还接着往这份内存状态里写，
        那些写入既不会落盘，又可能跟接手这个世界的下一个进程并行发生。
        """
        self._refuse_inside_transaction("释放世界")
        worker = self._clock_worker
        if worker is not None:
            # 不等它：release 本来就是放手。终局停机之后它什么都写不进去。
            worker.stop(timeout=0)
        with self._lock:
            if self._closed:
                return self._status_locked()
            self._runtime.stop(reason)
            self._state.fence(f"released: {reason}")
            self._closed = True
            self._clean = False
            self._last_reason = reason
            self._ownership.release()
            if self._service is not None:
                self._service._forget(self._world_id)
            return self._status_locked()

    # ── 状态 ────────────────────────────────────────────────────────────
    def status(self) -> Dict:
        """这个世界此刻的样子。每次返回全新的结构。

        `dirty` 是**指示器**：它说"自上一次成功 checkpoint 以来，权威状态变过
        没有"。它刻意不拿 P11 的闸门 —— 拿了的话，一次进行中的提交会把状态
        查询也堵住。所以并发提交期间它可能读到一个正在变的瞬间；要一致快照，
        用 checkpoint 之后的返回值。
        """
        with self._lock:
            return self._status_locked()

    def _status_locked(self) -> Dict:
        recovered = self._ownership.recovered_from
        return {
            "world_id": self._world_id,
            "session_id": self._state.session_id,
            "revision": self._revision,
            # 能恢复到的那一版。正常情况下跟 revision 一样；放弃一个存不下去的
            # 世界之后，它就是那句实话。
            "durable_revision": self._revision,
            # 读不出一致指纹时按脏算（_fingerprint 返回 None）。
            "dirty": _fingerprint(self._state) != self._fingerprint,
            "closed": self._closed,
            "clean": self._clean,
            "owned": self._ownership.held,
            "owner": self._ownership.owner.to_dict() if self._ownership.held else None,
            "recovered_from": recovered.to_dict() if recovered is not None else None,
            "last_saved_at": self._saved_at,
            "last_checkpoint_reason": self._last_reason,
            # 磁盘上那一版的耐久性。False 的意思很具体：它在那儿、读得回来，
            # 但掉电之后可能回到上一版；None 表示这份句柄由存档恢复而来，存档
            # 本身没有携带可验证的目录同步证据。
            "durable": self._durable,
            # 目录项到底同步过没有。None 同样表示恢复路径无法证明。
            "directory_synced": self._directory_synced,
            "last_error": self._last_error,
            "error": None,
            "residue": list(self._store.residue(self._world_id, self._segments)),
            "running": self._runtime.running,
            "stop_reason": self._runtime.stop_reason,
            "held": _held_report(self._held),
            "clock": self._state.world_state.clock.isoformat(),
            "archive_path": str(self._store.archive_path(self._world_id)),
            "boundaries_since_checkpoint": self._boundaries,
            "policy": self._policy.to_dict(),
            "quiet_time_events": quiet_time_report(self._state.time_events),
            "archive": _footprint(
                self._store.archive_bytes(self._world_id),
                self._segments,
                active=len(self._state.events)
                - (self._segments[-1].last_sequence + 1 if self._segments else 0),
            ),
        }

    # ── 内部 ────────────────────────────────────────────────────────────
    def _refuse_inside_transaction(self, what: str) -> None:
        """事务内部不许做生命周期操作。这道检查在拿世界锁**之前**。

        放在之前有两个理由，都是必须的：一是可重入闸门会放行自己的线程，
        真让它进去就会存下一份半截世界；二是先拿世界锁再去拿闸门，会跟一个
        正在事务里、回头来拿世界锁的线程死锁。
        """
        if self._runtime.in_transaction:
            raise CheckpointError(
                f"不能在一次提交事务内部{what}：那一刻的世界是半截的"
                "（事件已经写了、记忆还没落地），存下去就是一份不存在过的世界"
            )

    def _first_save(self) -> None:
        """创建时的第一份存档（第 1 版）。失败由调用方负责还所有权。"""
        with self._lock:
            self._ownership.verify()
            payload, fingerprint = self._snapshot_locked()
            archive = WorldArchive.from_state_payload(
                self._world_id, payload, revision=self._revision, segments=self._segments
            )
            archive = self._store.seal(archive)
            result = self._store.save(archive)
            self._adopt(
                archive,
                fingerprint,
                "created",
                durable=True,
                synced=result.directory_synced and archive.history_synced,
            )


class WorldLifecycleService:
    """持久世界的最小服务面：列出、创建、恢复、checkpoint、关闭、状态。

    这是留给 WEB-1 的接缝。P12 只做到这一层 —— 没有 HTTP 路由，也没有 UI：
    先把"一个世界怎么活、怎么存、谁拥有它"钉死，再去决定它长什么样。

    /ws/run 的研究会话跟这里没有任何关系：那条路不拿世界锁、不写存档根、
    也不 import 这个包（有测试盯着）。只有明确调用这里的人才会得到一个持久
    世界。
    """

    def __init__(self, store: WorldStore) -> None:
        if not isinstance(store, WorldStore):
            raise LifecycleError("生命周期服务需要一个 WorldStore")
        self._store = store
        self._lock = threading.RLock()
        self._open: Dict[str, PersistentWorld] = {}

    @property
    def store(self) -> WorldStore:
        return self._store

    # ── 创建 / 恢复 ─────────────────────────────────────────────────────
    def create(
        self,
        world_id: str,
        state: SessionState,
        *,
        adapters: RuntimeAdapters,
        checkpoint_policy: Optional[CheckpointPolicy] = None,
        snapshot_timeout: Optional[float] = None,
        start: bool = True,
        clock: Optional[ClockConfig] = None,
        wall_clock: Optional[Callable[[], datetime]] = None,
        anchor_wall: Optional[datetime] = None,
    ) -> PersistentWorld:
        """建一个新世界，并且当场写下第 1 版存档。

        `anchor_wall` 只决定锚点的现实端（开局状态是按哪个现实时刻造的，就该
        锚在哪个时刻），不是时钟源：时钟 worker 仍然读 `wall_clock` / 现实时间。

        带 `clock` 的世界按现实时间走：锚点从此刻、从世界的开局时钟开始，认知
        从"还没 Start"开始；句柄登记之后起时钟 worker。

        已经存在的世界不许被创建覆盖：那会把一整个世界的历史一次性抹掉，
        而抹掉它的理由只是调用方传错了一个字符串。
        """
        name = validate_world_id(world_id)
        adapters = self._require_adapters(adapters)
        policy = self._require_policy(checkpoint_policy)
        if not isinstance(state, SessionState):
            raise LifecycleError("创建世界需要一份 SessionState")
        if state.world_state is None:
            raise LifecycleError("创建世界的状态必须已经绑定权威 WorldState")
        for bound, label in (
            (state.scheduler, "调度器"),
            (state.agency_engine, "Agency 引擎"),
            (state.memory_encoder, "记忆编码器"),
            (state.autonomy, "自主运行时"),
        ):
            if bound is not None:
                raise LifecycleError(
                    f"这份状态已经绑过{label}，不能用来创建世界 —— 服务绑定"
                    "是生命周期的一步，必须由适配器显式来做"
                )

        self._refuse_if_open(name)
        handle = self._store.acquire(name)
        try:
            if self._store.exists(name):
                raise LifecycleError(
                    f"世界 '{name}' 已经有存档了。创建不会覆盖它 —— 要接着跑"
                    "就用 restore()"
                )
            runtime = adapters.bind(state)
            if clock is not None:
                if anchor_wall is not None:
                    if anchor_wall.utcoffset() is None:
                        raise LifecycleError("anchor_wall 必须带时区")
                    now = anchor_wall
                else:
                    now = wall_clock() if wall_clock is not None else utc_now()
                runtime.open_clock(
                    ClockAnchor(state.world_state.clock, now, clock.rate), wall=now
                )
            world = PersistentWorld(
                world_id=name,
                store=self._store,
                state=state,
                runtime=runtime,
                ownership=handle,
                revision=1,
                saved_at=None,
                checkpoint_policy=policy,
                service=self,
                snapshot_timeout=snapshot_timeout,
            )
            # 组装完成：从这里起不能再整段换存档或重绑服务。发布先于第一次写盘：
            # 发布时的整体校验没过的状态，一个字节都不许落到磁盘上。
            state.publish()
            world._first_save()
            if start:
                runtime.start()
        except BaseException:
            _abandon(state)
            handle.release()
            raise
        self._remember(name, world, handle)
        if clock is not None:
            # 首存档留在磁盘上：它是一份合法的开局存档（设计 §12.7）。
            self._spawn_clock(world, clock, wall_clock)
        return world

    def restore(
        self,
        world_id: str,
        *,
        adapters: RuntimeAdapters,
        checkpoint_policy: Optional[CheckpointPolicy] = None,
        snapshot_timeout: Optional[float] = None,
        start: bool = True,
        clock: Optional[ClockConfig] = None,
        wall_clock: Optional[Callable[[], datetime]] = None,
    ) -> PersistentWorld:
        """把最后一次成功 checkpoint 的那个世界拿回来，并且重新跑起来。

        带 `clock` 时：停机期间触发的到期一律 process_stopped（恢复转换），现实
        时钟若落后于存档时钟就记 wall_clock_behind；配置的倍率与存档不同时在此刻
        重新锚定。句柄登记之后起时钟 worker，由它把离线的那段补跑完。

        顺序是刻意的：**先**拿所有权，**再**读存档。反过来的话，两个进程会
        双双读到同一份存档、双双恢复出一个"权威"世界，然后互相覆盖。

        任何一步失败都会把所有权还回去：损坏的存档、不认识的版本、对不上的
        身份、起不来的适配器 —— 一次失败的恢复不该让世界永久锁死。

        作息门不齐（CONTENT-4 过渡设计 v2）时，新冲突照样先落盘，然后运行时
        终局停止，不碰时钟、不起 worker：
          * 带 `clock`（服务器的正常恢复）：登记、正常关闭，成功之后抛
            `ContentNotAdopted`。关闭失败就原样抛出关闭的错误，世界留在登记表里，
            仍是搁置状态，可以再关一次。
          * 不带 `clock`（冷维护）：返回一个打开着、不运行的世界，`held` 写着
            原因。`start` 在这里不起作用。
        """
        name = validate_world_id(world_id)
        adapters = self._require_adapters(adapters)
        if adapters.seed is not None:
            # 恢复路径不许带播种器。存档里已经有这个世界自己的队列了，再播
            # 一遍就是给每个角色排两条排期。这道闸放在**拿所有权之前**：
            # 一次配错的恢复不该先把世界锁住再失败。
            raise LifecycleError(
                f"恢复世界 '{name}' 时不能带初始排期播种器 —— 存档里已经有"
                "它自己的排期队列，再播一遍会让每个角色被重复激活"
            )
        policy = self._require_policy(checkpoint_policy)

        self._refuse_if_open(name)
        handle = self._store.acquire(name)
        state = None
        try:
            archive = self._store.load(name)
            # 数据在前：恢复出一份冷状态，跨段校验全部走既有构造函数。
            state = archive.restore_state()
            # 磁盘那一版的样子，在任何绑定改动它之前记下。
            baseline = _fingerprint(state)
            content_on_disk = state.content
            # 服务在后：调用方的冷适配器显式绑定，存档里一个活对象都没有。
            runtime, gate = adapters.bind_gated(state)
            held = gate is not None and not gate.complete
            if held:
                # 作息门不齐（CONTENT-4 过渡设计 v2）：在**任何**时钟操作之前终局
                # 停止。协调器对被要求停止过的运行时拒绝 start()，哪怕它从没启动
                # 过，所以之后无论谁拿到这个句柄都推不动它；内容账本的决定不经过
                # 运行准入，照样能记。时钟不恢复、不重新锚定，离线时间原样留给
                # 门齐之后的那次恢复补跑。
                runtime.stop("content_pending")
            if clock is not None and not held:
                if state.anchor is None:
                    raise LifecycleError(
                        f"世界 '{name}' 的存档没有时钟锚点，不能按现实时间恢复"
                    )
                now = wall_clock() if wall_clock is not None else utc_now()
                runtime.restore_clock(wall=now)
                if state.anchor.rate != float(clock.rate):
                    runtime.rebase_anchor(clock.rate, wall=now)
            world = PersistentWorld(
                world_id=name,
                store=self._store,
                state=state,
                runtime=runtime,
                ownership=handle,
                revision=archive.revision,
                saved_at=archive.saved_at,
                checkpoint_policy=policy,
                service=self,
                snapshot_timeout=snapshot_timeout,
                segments=archive.segments,
                baseline=baseline,
            )
            # 先发布（整体校验），再写任何东西：绑定期间通过公开接口形成的不一致
            # 必须在落盘之前被拒绝，失败的恢复不许把原本合法的存档写坏。
            state.publish()
            if state.content is not content_on_disk:
                # 恢复时识别出了新的内容冲突：它声称"已经记下"，就要在恢复成功之前
                # 真的落盘。存不下去就是恢复失败（下面的 except 归还所有权）。
                with world._lock:
                    world._checkpoint_locked("restore_content_conflict")
            if held:
                # 登记之前就标明原因：登记之后它对外可见，不能有一刻看起来只是
                # "没启动"。
                world._held = gate
            elif start:
                runtime.start()
        except BaseException:
            _abandon(state)
            handle.release()
            raise
        self._remember(name, world, handle)
        if held:
            if clock is None:
                # 冷维护：世界开着、不运行，留给调用方记决定再关闭。
                return world
            # 服务器的正常恢复：从这里起它是一个已登记、可管理的世界，不再属于
            # "失败的恢复"。正常关闭；关不掉就原样抛出（世界留在登记表里、仍是
            # 搁置状态，可以再关一次），不自动 force、不自动释放。
            world.close("content_pending")
            raise ContentNotAdopted(name, gate)
        if clock is not None:
            self._spawn_clock(world, clock, wall_clock)
        return world

    def _spawn_clock(self, world: PersistentWorld, clock: ClockConfig, wall_clock) -> None:
        """起时钟 worker。起不来就整个退回去：从登记表移除、终局停机、还所有权。

        登记与起 worker 是一个启动阶段：不存在"登记了、却没人推时间"的世界。
        """
        try:
            worker = ClockWorker(world, clock, wall_clock=wall_clock)
            world._clock_worker = worker
            worker.start()
        except BaseException:
            world.release("clock worker failed to start")
            raise

    # ── 服务面 ──────────────────────────────────────────────────────────
    def opened(self, world_id: str) -> Optional[PersistentWorld]:
        with self._lock:
            return self._open.get(validate_world_id(world_id))

    def checkpoint(self, world_id: str, reason: str = "manual") -> Dict:
        return self._require_open(world_id).checkpoint(reason)

    def close(
        self, world_id: str, reason: str = "closed", *, force: bool = False
    ) -> Dict:
        return self._require_open(world_id).close(reason, force=force)

    def status(self, world_id: str) -> Dict:
        """一个世界此刻的样子 —— 本进程开着的从句柄读，没开的从磁盘读。

        磁盘那条路**不抛**存档错误：状态面的用处正是回答"这个世界怎么了"，
        一份读不出来的存档要能在列表里显示成"读不出来"。真去恢复它仍然会
        响亮失败（见 restore）。
        """
        name = validate_world_id(world_id)
        world = self.opened(name)
        if world is not None:
            return world.status()
        report = {
            "world_id": name,
            "session_id": None,
            "revision": None,
            "durable_revision": None,
            "dirty": None,
            "closed": None,
            "clean": None,
            "owned": False,
            "owner": None,
            "recovered_from": None,
            "last_saved_at": None,
            "last_checkpoint_reason": None,
            "durable": None,
            "directory_synced": None,
            "last_error": None,
            "error": None,
            "residue": [],
            "running": None,
            "stop_reason": None,
            "held": None,
            "clock": None,
            "archive_path": None,
            "boundaries_since_checkpoint": None,
            "policy": None,
            "quiet_time_events": None,
            "archive": None,
        }
        try:
            report["archive_path"] = str(self._store.archive_path(name))
            report["residue"] = list(self._store.residue(name))
        except (StorageError, ArchiveError) as e:
            report["error"] = f"{type(e).__name__}: {e}"
            return report
        try:
            # 状态面不把整段历史读进来：分卷只核对在不在、大小对不对。
            # 逐卷哈希核对在真正恢复时做（见 restore）。
            archive = self._store.load(name, history=False)
            report["residue"] = list(self._store.residue(name, archive.segments))
            report["archive"] = _footprint(
                self._store.archive_bytes(name),
                archive.segments,
                active=archive.active_count,
            )
        except ArchiveNotFound:
            report["error"] = f"世界 '{name}' 还没有存档"
            return report
        except (StorageError, ArchiveError) as e:
            report["error"] = f"{type(e).__name__}: {e}"
            return report
        report["session_id"] = archive.session_id
        report["revision"] = archive.revision
        report["durable_revision"] = archive.revision
        report["last_saved_at"] = archive.saved_at
        report["clock"] = archive.clock.isoformat()
        try:
            raw = archive.state.get("time_events")
            report["quiet_time_events"] = quiet_time_report(
                TimeEventPolicy.from_dict(raw) if raw is not None else None
            )
        except TimeEventPolicyError as e:
            report["error"] = f"{type(e).__name__}: {e}"
        return report

    def list_worlds(self) -> Tuple[Dict, ...]:
        """磁盘上已知的世界，加上本进程开着的那些。每次返回全新的结构。"""
        known = set(self._store.list_worlds())
        with self._lock:
            known.update(self._open)
        return tuple(self.status(world_id) for world_id in sorted(known))

    def release_all(self) -> Tuple[str, ...]:
        """把本进程持有的世界全部还回去，**不存**。

        进程收尾和测试收尾用。它不是 close 的近义词：它明确地不写存档，
        所以最后一次成功 checkpoint 之后的工作会丢。
        """
        with self._lock:
            worlds = list(self._open.values())
        released = []
        for world in worlds:
            world.release("release_all")
            released.append(world.world_id)
        with self._lock:
            self._open.clear()
        return tuple(released)

    # ── 内部 ────────────────────────────────────────────────────────────
    @staticmethod
    def _require_adapters(adapters) -> RuntimeAdapters:
        if not isinstance(adapters, RuntimeAdapters):
            raise LifecycleError(
                "启动一个世界必须交出 RuntimeAdapters（判分器、策略、预算）——"
                "这些东西存档里没有，也不该有"
            )
        return adapters

    @staticmethod
    def _require_policy(policy) -> CheckpointPolicy:
        if policy is None:
            return CheckpointPolicy()
        if not isinstance(policy, CheckpointPolicy):
            raise LifecycleError("checkpoint_policy 必须是 CheckpointPolicy")
        return policy

    def _refuse_if_open(self, name: str) -> None:
        with self._lock:
            if name in self._open:
                raise WorldAlreadyOwned(
                    f"世界 '{name}' 已经在本进程里开着了 —— 同一个世界不能开两次"
                )

    def _require_open(self, world_id: str) -> PersistentWorld:
        world = self.opened(world_id)
        if world is None:
            raise LifecycleError(
                f"世界 '{validate_world_id(world_id)}' 没有在本进程里开着"
            )
        return world

    def _remember(
        self, name: str, world: PersistentWorld, handle: OwnershipHandle
    ) -> PersistentWorld:
        with self._lock:
            if name in self._open:  # pragma: no cover - 所有权闸已经挡住了
                handle.release()
                raise WorldAlreadyOwned(f"世界 '{name}' 已经在本进程里开着了")
            self._open[name] = world
        return world

    def _forget(self, name: str) -> None:
        with self._lock:
            self._open.pop(name, None)
