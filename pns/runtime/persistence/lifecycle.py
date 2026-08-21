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

from pns.models.session import SessionState, TransactionBoundaryError
from pns.runtime.agency.engine import AgencyEngine
from pns.runtime.autonomy.coordinator import AutonomousRuntime, AutonomyError
from pns.runtime.memory.encoder import MemoryEncoder
from pns.runtime.persistence.archive import ArchiveError, WorldArchive
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
from pns.runtime.scheduler import PersistentScheduler


class LifecycleError(RuntimeError):
    """这次生命周期操作不被允许，或者做不到。"""


class CheckpointError(LifecycleError):
    """这次 checkpoint 没有落地。磁盘上仍然是上一份完整存档。"""


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
    name: str = "autonomy"

    def __post_init__(self) -> None:
        if self.auditor is None or not callable(getattr(self.auditor, "audit", None)):
            raise LifecycleError("运行时适配器必须包含一个提供 audit() 的判分器")
        if self.policy_factory is not None and not callable(self.policy_factory):
            raise LifecycleError("policy_factory 必须是可调用对象")

    def bind(self, state: SessionState) -> AutonomousRuntime:
        """把服务显式绑到这份**已经恢复好**的状态上。

        四步都写出来，不靠任何一层"没有就顺手建一个"的默认行为：读代码的人
        应该一眼看见这个世界有哪几个服务，以及它们绑的是同一份状态。
        """
        if not isinstance(state, SessionState):
            raise LifecycleError("只能把服务绑在 SessionState 上")
        if state.scheduler is None:
            PersistentScheduler(state)
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
        return AutonomousRuntime(
            state,
            auditor=self.auditor,
            retry=self.retry,
            recall_budget=self.recall_budget,
            name=self.name,
        )


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
        digest,
    )


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
        self._fingerprint = _fingerprint(state)
        # 最后一次保存的耐久性证据。True/False 只由本进程亲自完成的保存得出；
        # 从存档恢复时没有携带这份文件系统证据，因此必须是 None（未知），不能
        # 因为文件此刻读得出来就把过去一次未经目录同步的保存重新说成耐久。
        self._durable = durable
        self._directory_synced = directory_synced

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
                self._world_id, payload, revision=revision
            )
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
        self._adopt(
            archive,
            fingerprint,
            reason,
            durable=True,
            synced=result.directory_synced,
        )
        return self._status_locked()

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
                payload = self._state.to_dict()
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

    # ── 关闭 ────────────────────────────────────────────────────────────
    def close(self, reason: str = "closed", *, force: bool = False) -> Dict:
        """按固定顺序收尾：停准入 → 等事务落定 → 存 → 标记关闭 → 还所有权。

        `force=True` 是**放弃**这个世界：最后一次 checkpoint 失败时照样把所有权
        还回去，代价是最后一次成功 checkpoint 之后的工作确实丢了。它不会被自动
        触发 —— 丢工作必须是一次明确的人为决定，而且返回的状态里
        `clean=False`、`durable_revision` 写着真正能恢复到的那一版。
        """
        self._refuse_inside_transaction("关闭世界")
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

            # 3./4. 标记关闭，归还所有权。
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
        with self._lock:
            if self._closed:
                return self._status_locked()
            self._runtime.stop(reason)
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
            "residue": list(self._store.residue(self._world_id)),
            "running": self._runtime.running,
            "stop_reason": self._runtime.stop_reason,
            "clock": self._state.world_state.clock.isoformat(),
            "archive_path": str(self._store.archive_path(self._world_id)),
            "boundaries_since_checkpoint": self._boundaries,
            "policy": self._policy.to_dict(),
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
                self._world_id, payload, revision=self._revision
            )
            result = self._store.save(archive)
            self._adopt(
                archive,
                fingerprint,
                "created",
                durable=True,
                synced=result.directory_synced,
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
    ) -> PersistentWorld:
        """建一个新世界，并且当场写下第 1 版存档。

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
            world._first_save()
            if start:
                runtime.start()
        except BaseException:
            handle.release()
            raise
        return self._remember(name, world, handle)

    def restore(
        self,
        world_id: str,
        *,
        adapters: RuntimeAdapters,
        checkpoint_policy: Optional[CheckpointPolicy] = None,
        snapshot_timeout: Optional[float] = None,
        start: bool = True,
    ) -> PersistentWorld:
        """把最后一次成功 checkpoint 的那个世界拿回来，并且重新跑起来。

        顺序是刻意的：**先**拿所有权，**再**读存档。反过来的话，两个进程会
        双双读到同一份存档、双双恢复出一个"权威"世界，然后互相覆盖。

        任何一步失败都会把所有权还回去：损坏的存档、不认识的版本、对不上的
        身份、起不来的适配器 —— 一次失败的恢复不该让世界永久锁死。
        """
        name = validate_world_id(world_id)
        adapters = self._require_adapters(adapters)
        policy = self._require_policy(checkpoint_policy)

        self._refuse_if_open(name)
        handle = self._store.acquire(name)
        try:
            archive = self._store.load(name)
            # 数据在前：恢复出一份冷状态，跨段校验全部走既有构造函数。
            state = archive.restore_state()
            # 服务在后：调用方的冷适配器显式绑定，存档里一个活对象都没有。
            runtime = adapters.bind(state)
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
            )
            if start:
                runtime.start()
        except BaseException:
            handle.release()
            raise
        return self._remember(name, world, handle)

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
            "clock": None,
            "archive_path": None,
            "boundaries_since_checkpoint": None,
            "policy": None,
        }
        try:
            report["archive_path"] = str(self._store.archive_path(name))
            report["residue"] = list(self._store.residue(name))
        except (StorageError, ArchiveError) as e:
            report["error"] = f"{type(e).__name__}: {e}"
            return report
        try:
            archive = self._store.load(name)
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
