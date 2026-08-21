# pns/runtime/scheduler.py — 持久化调度器
#
# 调度器回答的问题只有一个：**模拟时间往前走了，哪些排期变得可以发生了**。
#
# 它不回答的问题（写在这里免得以后被顺手加进来）：角色想不想动、要说什么、
# 该谁接话。到期只是资格，不是意图 —— 选择属于 Agency / Planner，生成属于
# 角色生成层。所以这个模块不 import 任何生成/判分/Router 代码，产出的也只有
# ActivationDue 这一种记录。
#
# 三条硬约束：
#
#   1. 时间只能通过 world.time_advanced 事件推进。这里从不调用
#      WorldState.advance_time() —— 那会是一次没有记录在世界历史里的状态变更，
#      而世界历史必须能解释时钟为什么是现在这个值。
#   2. 一次推进是一个事务。时钟、事件历史、曝光判定、激活队列、产出的到期
#      记录，要么全部成立，要么一起回到推进之前的样子。中途失败留下"时间走了
#      但队列没动"或者"队列动了但事件没记下"都是不可接受的。
#   3. 到期记录必须是耐久的。一条一次性激活到期时已经从队列里摘掉了，如果到期
#      记录只活在本次调用的返回值里，下游还没处理进程就中断的话，这次到期就
#      永远消失了。所以每条到期记录都在同一个事务里落进 SessionState 的
#      activation_outbox，在被明确确认（acknowledge）之前一直可读。
#
# 权威归属：队列和投递箱归 **SessionState** 所有，调度器是它们之上的服务，
# 一个会话只能绑一个。所以会话存档不可能存下时钟却丢掉排期，恢复也只会恢复
# 同一个权威实例管理的那一份状态，不会凭空多出一份并行的排期。
#
# 会话调度（确定性 round robin）跟这里是两件事，不是一件事的两种写法：前者
# 决定研究会话里谁下一个说话，后者决定世界时间什么时候往前走。P8 不让研究
# 会话去调用这里 —— 那会改变可复现的轮转顺序，而且"到点了要不要真的动"本来
# 就是 P9 的判断。
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Mapping, Optional, Tuple

from pns.models.activation import (
    ActivationDue,
    ActivationError,
    ActivationKind,
    ScheduledActivation,
)
from pns.models.activation_outbox import ActivationOutbox, ActivationOutboxError
from pns.models.activation_queue import ActivationQueue, ActivationQueueError
from pns.models.event import Event, EventScope, EventType
from pns.models.session import SessionState, SessionStateError
from pns.models.world_state import WorldState
from pns.runtime.event_commit import commit_session_event

_MINUTE = timedelta(minutes=1)


class SchedulerError(ValueError):
    """调度器拒绝了这次操作（排到过去、角色不存在、时间倒退、存档损坏等）。"""


@dataclass(frozen=True)
class TickResult:
    """一次时间推进的完整结果。"""

    from_clock: datetime
    to_clock: datetime
    minutes: int
    # 已提交的 world.time_advanced 事件投影（含它在世界历史里的序号）。
    event: Dict = field(default_factory=dict)
    due: Tuple[ActivationDue, ...] = ()

    @property
    def due_ids(self) -> Tuple[str, ...]:
        return tuple(record.activation_id for record in self.due)

    def to_dict(self) -> Dict:
        return {
            "from_clock": self.from_clock.isoformat(),
            "to_clock": self.to_clock.isoformat(),
            "minutes": self.minutes,
            "event": dict(self.event),
            "due": [record.to_dict() for record in self.due],
        }


class PersistentScheduler:
    """一个会话里唯一一份排期与时间推进服务。

    调度器状态是**运行时权威状态**，不是配置：它属于某一个会话的那一份
    SessionState，跟着世界时钟走。没有任何构造它的路径读磁盘配置，
    ContentRegistry 也没有任何字段能碰到它 —— 重载换掉配置快照，影响不到
    已经存在的队列和时钟。
    """

    def __init__(self, state: SessionState) -> None:
        if not isinstance(state, SessionState):
            raise SchedulerError("调度器必须绑定在一个 SessionState 上")
        if not isinstance(state.world_state, WorldState):
            raise SchedulerError("调度器绑定的会话还没有权威 WorldState")
        self._state = state
        # 绑定只允许一次。第二个调度器会带来第二份队列，两份都在推同一个时钟，
        # 于是"这条一次性激活触发过没有"会有两个互相看不见的答案。
        try:
            state.attach_scheduler(self)
        except RuntimeError as e:
            raise SchedulerError(str(e)) from e
        self._validate_bound_state()

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
    def queue(self) -> ActivationQueue:
        """本会话的排期队列。权威副本在 SessionState 上，这里不另存引用 ——
        存档恢复会就地换掉它，缓存一份就会读到被替换掉的旧队列。"""
        return self._state.activations

    @property
    def outbox(self) -> ActivationOutbox:
        """本会话的到期投递箱（已触发、等待消费的记录）。"""
        return self._state.activation_outbox

    def pending(self) -> Tuple[ScheduledActivation, ...]:
        return self.queue.pending()

    def pending_due(self) -> Tuple[ActivationDue, ...]:
        """还没被确认的到期记录，按触发顺序。恢复之后要接着处理的就是这些。"""
        return self.outbox.pending()

    def acknowledge(self, due_id: str) -> bool:
        """确认一条到期记录已被消费。

        幂等：第一次确认返回 True，之后再确认同一条返回 False，都不抛异常。
        确认一条**不存在**的 due_id 会抛错 —— 那不是重复确认，而是消费方拿着
        一条这个会话里没发生过的到期记录，静默吞掉只会让错误更晚才暴露。

        确认改的是"这条处理过了"，不改时钟、不改队列，因此不需要走时间推进
        那套事务；但它仍然在 atomic_commit() 的回滚范围内，所以放在一次提交
        块里确认、提交失败时确认也会一并撤销。
        """
        try:
            return self.outbox._acknowledge(due_id)
        except ActivationOutboxError as e:
            raise SchedulerError(str(e)) from e

    def next_due_at(self) -> Optional[datetime]:
        activation = self.queue.next_due()
        return activation.due_at if activation is not None else None

    def preview_due(self, target: datetime) -> Tuple[ScheduledActivation, ...]:
        """如果时钟推进到 target，哪些激活会到期。纯读取，不改任何状态。"""
        target = self._require_simulation_time(target, "target")
        return tuple(
            activation for _, activation in self.queue.due_at_or_before(target)
        )

    # ── 排期 ────────────────────────────────────────────────────────────
    def schedule(self, activation: ScheduledActivation) -> int:
        """把一条激活排进队列，返回它的 sequence。

        全部校验都在任何状态变更之前完成：类型、是否排到了过去、角色在不在
        这个世界里、ID 有没有撞车。任何一条不过，队列一个字节都不动。
        """
        if not isinstance(activation, ScheduledActivation):
            raise SchedulerError("只能排入 ScheduledActivation")
        self._require_future(activation)
        self._require_known_character(activation)
        try:
            self.queue._check_can_append(activation)
        except ActivationQueueError as e:
            raise SchedulerError(str(e)) from e
        return self.queue._append(activation)

    def cancel(self, activation_id: str) -> bool:
        """取消一条还没触发的激活。

        幂等语义是明确的：**取消成功返回 True，队列里本来就没有这条（从没排过、
        已经触发过、或者已经取消过）返回 False**，两种情况都不抛异常。想把
        "取消了一个不存在的 ID"当错误处理的调用方，检查返回值即可。
        取消只对还没触发的激活有意义 —— 已经产出的到期记录和已经提交的事件
        不会被取消操作追溯掉。
        """
        if not isinstance(activation_id, str) or not activation_id:
            raise SchedulerError("activation_id 必须是非空字符串")
        if not self.queue.has(activation_id):
            return False
        self.queue._remove(activation_id)
        return True

    # ── 推进时间 ────────────────────────────────────────────────────────
    def advance_by(self, minutes: int) -> TickResult:
        """把模拟时间往前推 minutes 分钟，并触发这段时间里到期的激活。"""
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            raise SchedulerError(f"minutes 必须是整数，收到 {minutes!r}")
        if minutes <= 0:
            # 0 分钟不是"什么都不做"，而是一次不该被记进世界历史的空事件；
            # 负数是时间倒流。两者都在这里拦下，不留给下游去发现。
            raise SchedulerError(f"模拟时间只能向前推进，收到 minutes={minutes}")
        try:
            target = self.clock + timedelta(minutes=minutes)
        except OverflowError:
            raise SchedulerError("推进后的模拟时间超出可表示的时间范围") from None
        return self._tick(target, minutes)

    def advance_to(self, target: datetime) -> TickResult:
        """把模拟时间推进到 target。

        target 必须严格晚于当前时钟，并且距离当前时钟是整分钟 —— 世界历史里
        的时间推进以分钟计，接受一个带秒的目标只会让时钟和事件对不上。
        """
        target = self._require_simulation_time(target, "target")
        delta = target - self.clock
        if delta <= timedelta(0):
            raise SchedulerError(
                f"模拟时间不能倒退：目标 {target.isoformat()} 不晚于当前时钟 "
                f"{self.clock.isoformat()}"
            )
        if delta % _MINUTE:
            raise SchedulerError(
                f"目标时间必须与当前时钟相差整分钟，收到 {target.isoformat()}"
            )
        return self._tick(target, delta // _MINUTE)

    def advance_to_next_due(self) -> Optional[TickResult]:
        """推进到下一条激活到期的那一刻；队列为空就返回 None，不动时钟。"""
        activation = self.queue.next_due()
        if activation is None:
            return None
        delta = activation.due_at - self.clock
        if delta <= timedelta(0):
            # 队列里的激活恒严格晚于时钟（排期和恢复都校验过），走到这里说明
            # 有人绕过公共接口改了队列或时钟。
            raise SchedulerError(
                f"队列里的激活 '{activation.activation_id}' 已经早于当前时钟，"
                "调度器状态被绕过公共接口修改了"
            )
        minutes = delta // _MINUTE + (1 if delta % _MINUTE else 0)
        return self._tick(self.clock + timedelta(minutes=minutes), minutes)

    # ── 事务本体 ────────────────────────────────────────────────────────
    def _tick(self, target: datetime, minutes: int) -> TickResult:
        """一次推进 = 一条 world.time_advanced 事件 + 队列变更，同生共死。

        顺序是刻意的：先在不改任何状态的前提下算出"这次会触发什么"，再进事务。
        事务里出任何岔子 —— 事件校验失败、追加失败、队列变更失败 —— 世界时钟、
        事件历史、曝光/观察和队列会一起回到进入之前的样子。
        """
        state = self._state
        world = state.world_state
        from_clock = world.clock

        plan = self._plan_due(target)
        event = self._time_advanced_event(minutes, target, plan)

        # 队列和投递箱都在 SessionState.atomic_commit() 的回滚范围内，所以这里
        # 不需要（也不应该）另建一套快照：一次推进的全部后果由同一个事务兜底。
        with state.atomic_commit():
            committed = commit_session_event(state, event)
            if world.clock != target:
                # 事件的状态效果没把时钟落在预期的位置上：宁可整体作废，
                # 也不能让队列按一个跟世界不一致的时间去触发。
                raise SchedulerError(
                    f"时间推进后的时钟 {world.clock.isoformat()} 与目标 "
                    f"{target.isoformat()} 不一致"
                )
            due = self._apply_due(plan, fired_at=target)

        return TickResult(
            from_clock=from_clock,
            to_clock=world.clock,
            minutes=minutes,
            event=committed,
            due=due,
        )

    def _plan_due(
        self, target: datetime
    ) -> Tuple[Tuple[int, ScheduledActivation, Optional[ScheduledActivation], int], ...]:
        """算出推进到 target 时会触发什么。纯函数，不碰任何状态。

        返回 (sequence, 到期的激活, 下一次的激活或 None, 被跨过的次数)。
        """
        plan: List[
            Tuple[int, ScheduledActivation, Optional[ScheduledActivation], int]
        ] = []
        for sequence, activation in self.queue.due_at_or_before(target):
            if not activation.is_recurring:
                plan.append((sequence, activation, None, 0))
                continue
            try:
                following, missed = activation.next_occurrence(target)
            except ActivationError as e:
                raise SchedulerError(str(e)) from e
            plan.append((sequence, activation, following, missed))
        return tuple(plan)

    def _apply_due(self, plan, *, fired_at: datetime) -> Tuple[ActivationDue, ...]:
        """把计划落到队列上，产出到期记录，并把它们落进投递箱。

        一次性激活从队列里摘掉 —— 它至多触发一次，摘掉之后再怎么推进时间都
        不会重新出现。周期激活换成它的下一次触发（严格晚于本次触发时刻），
        因此同一次推进里不可能把同一条激活触发两遍。

        摘掉队列和落进投递箱是同一步：先摘后落之间如果能被打断，那条到期资格
        就会凭空消失，而这正是投递箱要解决的问题。两者都在调用方的事务里。
        """
        records: List[ActivationDue] = []
        for sequence, activation, following, missed in plan:
            if following is None:
                self.queue._remove(activation.activation_id)
            else:
                self.queue._reschedule(following)
            records.append(
                ActivationDue(
                    activation_id=activation.activation_id,
                    kind=activation.kind,
                    due_at=activation.due_at,
                    fired_at=fired_at,
                    sequence=sequence,
                    character_id=activation.character_id,
                    missed_occurrences=missed,
                    next_due_at=following.due_at if following is not None else None,
                    payload=activation.payload,
                )
            )
        for record in records:
            try:
                self.outbox._append(record)
            except ActivationOutboxError as e:
                raise SchedulerError(str(e)) from e
        return tuple(records)

    def _time_advanced_event(
        self, minutes: int, target: datetime, plan
    ) -> Event:
        """构造这次推进对应的 world.time_advanced 事件。

        scope 是 public：时间推进不是谁做的事，也不落在某个地点或频道上。
        没有落点的 public 事件对谁都不构成感知（曝光层判为"没撞上"），所以
        一次时钟推进不会凭空变成任何角色的观察 —— 角色感知到的是事情，
        不是时间本身。
        """
        store = self._state.events
        latest = store.latest()
        return Event(
            event_id=f"{self.session_id}:clock:"
            f"{len(store.by_type(EventType.WORLD_TIME_ADVANCED))}",
            type=EventType.WORLD_TIME_ADVANCED,
            occurred_at=self.clock,
            scope=EventScope.PUBLIC,
            payload={"minutes": minutes},
            # 系统侧信息，不进任何角色的观察：这次推进是调度器发起的，推到了
            # 哪一刻，触发了哪些排期。
            provenance={
                "kind": "scheduler",
                "session_id": self.session_id,
                "advanced_to": target.isoformat(),
                "due_activations": [
                    activation.activation_id for _, activation, _, _ in plan
                ],
            },
            causation_id=latest.event_id if latest is not None else None,
            correlation_id=self.session_id,
        )

    # ── 校验 ────────────────────────────────────────────────────────────
    @staticmethod
    def _require_simulation_time(value, label: str) -> datetime:
        if not isinstance(value, datetime):
            raise SchedulerError(f"{label} 必须是 datetime（模拟时钟时间）")
        if value.tzinfo is not None:
            raise SchedulerError(
                f"{label} 必须是 timezone-naive 的模拟时间，收到带时区的 {value!r}"
            )
        return value

    def _validate_bound_state(self) -> None:
        """绑定/恢复之后确认队列与投递箱跟当前时钟自洽。"""
        for activation in self.queue.pending():
            self._require_future(activation)
        latest = self.outbox.latest()
        if latest is not None and latest.fired_at > self.clock:
            raise SchedulerError(
                f"到期记录 '{latest.due_id}' 的触发时间 "
                f"{latest.fired_at.isoformat()} 晚于当前模拟时钟 "
                f"{self.clock.isoformat()}"
            )

    def _require_future(self, activation: ScheduledActivation) -> None:
        """排期必须严格晚于当前时钟。

        "不能排到过去"是显然的；等于当前时刻也拒绝，是因为队列里的东西只能靠
        一次时间推进来触发：一条正好等于当前时钟的激活会立刻处在"已经到期却
        没有任何推进能触发它"的状态。与其留这个说不清的中间态，不如让它在
        排入的时候就响亮地失败。
        """
        if activation.due_at <= self.clock:
            raise SchedulerError(
                f"激活 '{activation.activation_id}' 的到期时间 "
                f"{activation.due_at.isoformat()} 不晚于当前模拟时钟 "
                f"{self.clock.isoformat()}"
            )

    def _require_known_character(self, activation: ScheduledActivation) -> None:
        """激活引用的角色必须在这个世界里真实存在。

        口径跟事件提交边界完全一样（WorldState.known_characters()），不看会话
        选了谁：被选进会话不等于在场。

        这道检查只在**排期那一刻**成立。排完之后角色被移出世界的话，到期记录
        照样会产出 —— 调度器不替下游做"这个角色还在不在"的判断，安静地丢掉一条
        到期记录，比交出一条下游需要自己复核的记录危险得多。
        """
        if activation.character_id is None:
            return
        if activation.character_id not in self.world.known_characters():
            raise SchedulerError(
                f"激活 '{activation.activation_id}' 引用了世界里不存在的角色: "
                f"{activation.character_id}"
            )

    # ── 序列化 ──────────────────────────────────────────────────────────
    def to_dict(self) -> Dict:
        """调度状态的持久化形状。

        形状由 SessionState.scheduler_archive() 定义，这里只是转发：调度存档
        是会话存档的一部分（`session.to_dict()["scheduler"]` 就是这一份），
        同一份状态不该有两种写法。
        """
        return self._state.scheduler_archive()

    def restore(self, payload: Mapping) -> "PersistentScheduler":
        """按存档恢复本调度器管理的队列与投递箱，返回自己。

        刻意是实例方法而不是 classmethod：恢复必须落回**同一个**权威实例，
        新建第二个调度器会让同一个会话出现两份互相看不见的排期。校验（会话
        对不对得上、时钟对不对得上、排期是不是还在未来、到期记录是不是不晚于
        时钟）由 SessionState 统一做，它是这份状态的所有者。
        """
        try:
            self._state.restore_scheduler_archive(payload)
        except SessionStateError as e:
            raise SchedulerError(str(e)) from e
        self._validate_bound_state()
        return self

    # ── 调试投影 ────────────────────────────────────────────────────────
    def debug_projection(self) -> Dict:
        """只读的调度状态投影（JSON 安全），供测试和调试 UI 读。

        跟曝光的解释通道同一条规矩：这些数据是系统视角，不进角色上下文。
        """
        clock = self.clock
        return {
            "session_id": self.session_id,
            "clock": clock.isoformat(),
            "pending": len(self.queue),
            "next_due_at": (
                self.next_due_at().isoformat()
                if self.next_due_at() is not None
                else None
            ),
            "time_advanced_events": len(
                self._state.events.by_type(EventType.WORLD_TIME_ADVANCED)
            ),
            "due_total": len(self.outbox),
            "due_pending": len(self.outbox.pending()),
            "pending_due_ids": [record.due_id for record in self.outbox.pending()],
            "queue": [
                {
                    "sequence": sequence,
                    "activation_id": activation.activation_id,
                    "kind": activation.kind.value,
                    "character_id": activation.character_id,
                    "due_at": activation.due_at.isoformat(),
                    "due_in_minutes": (activation.due_at - clock) // _MINUTE,
                    "interval_minutes": activation.interval_minutes,
                }
                for sequence, activation in self.queue.entries()
            ],
        }


__all__ = [
    "ActivationDue",
    "ActivationKind",
    "ActivationOutbox",
    "ActivationQueue",
    "PersistentScheduler",
    "ScheduledActivation",
    "SchedulerError",
    "TickResult",
]
