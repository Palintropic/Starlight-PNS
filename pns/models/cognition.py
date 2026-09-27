# pns/models/cognition.py — 认知不可用的原因
#
# 世界时间与确定性作息在世界打开期间一直走；需要模型的认知（Agency 决策、生成、
# 判分）只在"认知可用"时发生。不可用时到期的激活以 REJECTED_UNAVAILABLE 收尾，
# 记录里写明**全部**生效的原因。
#
# 这些原因属于 Operational History（Article XIII）：它们解释的是"这一刻为什么
# 没有做决定"，不是任何 resident 的经历，也不会进入观察、记忆或提示词。
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple


class CognitionCause(str, Enum):
    """认知不可用的原因。闭集；一个区间可以同时有多个。"""

    NOT_STARTED = "not_started"  # 世界打开后操作员还没有 Start
    OPERATOR_PAUSED = "operator_paused"  # 操作员 Stop
    RUN_BUDGET_EXHAUSTED = "run_budget_exhausted"  # 单次 Start 的额度用完
    WORLD_ACTION_CAP = "world_action_cap"  # 世界一生的动作上限到顶
    PROCESS_STOPPED = "process_stopped"  # 进程停机；恢复前后没来得及决定
    FAULT = "fault"  # 时钟 worker 故障期间及其补跑
    WALL_CLOCK_BEHIND = "wall_clock_behind"  # 现实时钟晚于存档时钟


class CognitionCauseError(ValueError):
    """原因集合不合法（空、未知值、重复）。"""


def normalize_causes(causes: Iterable) -> Tuple[CognitionCause, ...]:
    """校验并规范化一组原因：非空、闭集内、去重后排序。"""
    if isinstance(causes, (str, bytes)):
        raise CognitionCauseError("原因必须是一组值，不是单个字符串")
    try:
        items = [CognitionCause(cause) for cause in causes]
    except (TypeError, ValueError):
        raise CognitionCauseError(f"未知的认知不可用原因: {causes!r}") from None
    if not items:
        raise CognitionCauseError("认知不可用必须至少有一个原因")
    if len(set(items)) != len(items):
        raise CognitionCauseError(f"认知不可用原因重复: {causes!r}")
    return tuple(sorted(items, key=lambda cause: cause.value))


# ── 认知时间线 ──────────────────────────────────────────────────────────
#
# 时间线回答："Agency 日志第 p 条记录提交的那一刻，认知可不可用、为什么不可用。"
#
# 索引是 **Agency 日志位置**（也就是提交顺序），不是 outbox 的落箱位置，也不是
# 模拟分钟：转换与 Agency 记录的追加都在 SessionState 的同一把事务锁下发生，
# 所以"一条记录落在哪个区间"是一个全序事实，与到期记录按什么顺序、以什么
# 并发度被处理无关。
#
# 三条硬约束：
#
#   1. **不可变。** 每次转换返回一条新时间线；SessionState 在事务里整体替换
#      引用，于是它跟 Agency 日志一起回滚。
#   2. **判定是并集。** 一条到期资格不可用的原因 = 当前区间的原因 ∪ 所有仍覆盖
#      它的 backlog 条目的原因。运行时与存档加载只调用 `unavailable_causes()`。
#   3. **backlog 是规范形。** 按 until_sim 严格递增；同一 cutoff 的两次追加合并
#      成一条（原因取并集）；条目原因非空。加载时拒绝任何非规范形。

# Start 能清掉的原因：它们都是操作者或本次运行层面的状态。物理条件（故障、
# 现实时钟落后）和世界一生的动作上限不归 Start 管。
OPERATOR_CLEARABLE = frozenset(
    {
        CognitionCause.NOT_STARTED,
        CognitionCause.OPERATOR_PAUSED,
        CognitionCause.RUN_BUDGET_EXHAUSTED,
    }
)


class CognitionTimelineError(ValueError):
    """认知时间线本身不合法，或一次转换不成立。"""


class TransitionKind(str, Enum):
    """区间是怎么开始的。闭集。"""

    OPENED = "opened"  # 世界创建
    RESTORED = "restored"  # 从存档恢复
    STARTED = "started"  # 操作员 Start
    STOPPED = "stopped"  # 操作员 Stop
    RUN_BUDGET_EXHAUSTED = "run_budget_exhausted"
    WORLD_ACTION_CAP = "world_action_cap"
    FAULT_BEGAN = "fault_began"
    FAULT_CLEARED = "fault_cleared"
    WALL_CLOCK_BEHIND = "wall_clock_behind"
    WALL_CLOCK_CAUGHT_UP = "wall_clock_caught_up"


def _causes(values, *, allow_empty: bool) -> FrozenSet[CognitionCause]:
    if isinstance(values, (str, bytes)):
        raise CognitionTimelineError("原因必须是一组值，不是单个字符串")
    try:
        items = frozenset(CognitionCause(value) for value in values)
    except (TypeError, ValueError):
        raise CognitionTimelineError(f"未知的认知不可用原因: {values!r}") from None
    if not items and not allow_empty:
        raise CognitionTimelineError("这里的原因集合不能为空")
    return items


def _sim_time(value, label: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise CognitionTimelineError(f"{label} 不是合法时间: {value!r}") from None
    if not isinstance(value, datetime):
        raise CognitionTimelineError(f"{label} 必须是 datetime")
    if value.tzinfo is not None:
        raise CognitionTimelineError(f"{label} 必须是 timezone-naive 的模拟时间")
    if value.second or value.microsecond:
        raise CognitionTimelineError(f"{label} 必须落在整分钟上")
    return value


def _sorted_values(causes) -> List[str]:
    return sorted(cause.value for cause in causes)


@dataclass(frozen=True)
class BacklogItem:
    """在 until_sim 之前触发的到期资格，额外带着这些原因。"""

    until_sim: datetime
    causes: FrozenSet[CognitionCause]

    def __post_init__(self) -> None:
        object.__setattr__(self, "until_sim", _sim_time(self.until_sim, "until_sim"))
        object.__setattr__(self, "causes", _causes(self.causes, allow_empty=False))

    def to_dict(self) -> Dict:
        return {
            "until_sim": self.until_sim.isoformat(),
            "causes": _sorted_values(self.causes),
        }


def _append_backlog(
    backlog: Tuple[BacklogItem, ...], item: Optional[BacklogItem]
) -> Tuple[BacklogItem, ...]:
    """加入一条并保持规范形：按 cutoff 严格递增，同一 cutoff 合并（原因取并集）。

    通常新 cutoff 晚于所有旧的，落在末尾。更早的 cutoff 也合法：恢复时现实时钟
    若被往回拨过，这一次的切点可以早于上一个进程留下的切点。按序插入不改变
    任何一条到期资格的判定——并集只看 until_sim 是否晚于 fired_at。
    """
    if item is None:
        return backlog
    items = list(backlog)
    for position, existing in enumerate(items):
        if existing.until_sim == item.until_sim:
            items[position] = BacklogItem(existing.until_sim, existing.causes | item.causes)
            return tuple(items)
        if existing.until_sim > item.until_sim:
            items.insert(position, item)
            return tuple(items)
    return backlog + (item,)


@dataclass(frozen=True)
class CognitionInterval:
    """时间线上的一段：从 Agency 日志位置 from_log 起生效，直到下一段开始。"""

    index: int
    from_log: int
    causes: FrozenSet[CognitionCause]
    backlog: Tuple[BacklogItem, ...]
    run_allowance: Optional[int]
    # 额度是从日志哪个位置开始算的（设置它的那次 Start）。额度跟着区间往后带，
    # 消耗也必须从这里起算：否则中途插一次故障转换，额度就被悄悄装满了。
    allowance_since_log: Optional[int]
    opened_by: TransitionKind
    # 转换发生的模拟分钟。按时钟生效的转换（Stop、额度、上限、故障开始、现实时钟
    # 落后）记当时已提交的时钟；按现实时间生效的转换（恢复、Start、故障解除、
    # 追上）记锚点换算出的分钟，backlog 的 cutoff 由它派生。补跑期间两者不同，
    # 所以它**不要求单调**：区间按日志位置切，这个字段只供审计和派生 cutoff。
    opened_at_sim: datetime
    opened_at_wall: str

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        for label in ("index", "from_log"):
            value = getattr(self, label)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CognitionTimelineError(f"{label} 必须是非负整数")
        set_(self, "causes", _causes(self.causes, allow_empty=True))
        backlog: Tuple[BacklogItem, ...] = ()
        for item in self.backlog:
            if not isinstance(item, BacklogItem):
                raise CognitionTimelineError("backlog 里只能放 BacklogItem")
            if backlog and item.until_sim <= backlog[-1].until_sim:
                raise CognitionTimelineError(
                    "backlog 必须按 until_sim 严格递增（同一 cutoff 应当合并）"
                )
            backlog += (item,)
        set_(self, "backlog", backlog)
        allowance = self.run_allowance
        if allowance is not None and (
            isinstance(allowance, bool) or not isinstance(allowance, int) or allowance < 1
        ):
            # 0 不是"一次也不许用"的写法：额度在记录落地**之后**才结算，零额度
            # 会先放行一条认知结局再关门。不想让认知运行，就不要 Start。
            raise CognitionTimelineError("run_allowance 必须是正整数或 None")
        since = self.allowance_since_log
        if (allowance is None) != (since is None):
            raise CognitionTimelineError("run_allowance 与 allowance_since_log 必须同时给出或同时为空")
        if since is not None and (
            isinstance(since, bool)
            or not isinstance(since, int)
            or since < 0
            or since > self.from_log
        ):
            raise CognitionTimelineError("allowance_since_log 必须是不晚于区间起点的非负整数")
        try:
            set_(self, "opened_by", TransitionKind(self.opened_by))
        except ValueError:
            raise CognitionTimelineError(f"未知的转换: {self.opened_by!r}") from None
        set_(self, "opened_at_sim", _sim_time(self.opened_at_sim, "opened_at_sim"))
        if not isinstance(self.opened_at_wall, str) or not self.opened_at_wall:
            raise CognitionTimelineError("opened_at_wall 必须是非空字符串")

    @property
    def available(self) -> bool:
        return not self.causes

    def to_dict(self) -> Dict:
        return {
            "index": self.index,
            "from_log": self.from_log,
            "causes": _sorted_values(self.causes),
            "backlog": [item.to_dict() for item in self.backlog],
            "run_allowance": self.run_allowance,
            "allowance_since_log": self.allowance_since_log,
            "opened_by": self.opened_by.value,
            "opened_at_sim": self.opened_at_sim.isoformat(),
            "opened_at_wall": self.opened_at_wall,
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> "CognitionInterval":
        if not isinstance(payload, Mapping):
            raise CognitionTimelineError("区间必须是字典")
        try:
            return cls(
                index=payload["index"],
                from_log=payload["from_log"],
                causes=payload["causes"],
                backlog=tuple(
                    BacklogItem(item["until_sim"], item["causes"])
                    for item in payload["backlog"]
                ),
                run_allowance=payload["run_allowance"],
                allowance_since_log=payload["allowance_since_log"],
                opened_by=payload["opened_by"],
                opened_at_sim=payload["opened_at_sim"],
                opened_at_wall=payload["opened_at_wall"],
            )
        except (KeyError, TypeError) as e:
            raise CognitionTimelineError(f"区间缺字段或形状不对: {e}") from None


def unavailable_causes(
    interval: CognitionInterval, fired_at: datetime
) -> FrozenSet[CognitionCause]:
    """一条在 fired_at 触发的到期资格，此刻在 interval 里不可用的全部原因。

    空集表示可用。这是运行时与存档加载**共用**的唯一判定。
    """
    fired_at = _sim_time(fired_at, "fired_at")
    causes = set(interval.causes)
    for item in interval.backlog:
        if item.until_sim > fired_at:
            causes |= item.causes
    return frozenset(causes)


@dataclass(frozen=True)
class CognitionTimeline:
    """一个世界的认知时间线。不可变；每次转换返回新的一条。"""

    intervals: Tuple[CognitionInterval, ...]

    def __post_init__(self) -> None:
        intervals = tuple(self.intervals)
        if not intervals:
            raise CognitionTimelineError("认知时间线至少有一个区间")
        for position, interval in enumerate(intervals):
            if not isinstance(interval, CognitionInterval):
                raise CognitionTimelineError("时间线里只能放 CognitionInterval")
            if interval.index != position:
                raise CognitionTimelineError("区间 index 必须从 0 连续编号")
        first = intervals[0]
        if (
            first.opened_by is not TransitionKind.OPENED
            or first.causes != {CognitionCause.NOT_STARTED}
            or first.backlog
            or first.run_allowance is not None
        ):
            raise CognitionTimelineError("时间线必须从一个\"世界打开、还没 Start\"的区间开始")
        # 之后的每个区间都必须能由前一个区间按它自己的转换种类重放出来。只查
        # 形状的话，单改一个区间的原因或 cutoff 仍是一条"合法"的时间线。
        for previous, interval in zip(intervals, intervals[1:]):
            replayed = _next_interval(
                previous,
                interval.opened_by,
                log_length=interval.from_log,
                sim=interval.opened_at_sim,
                wall=interval.opened_at_wall,
                run_allowance=(
                    interval.run_allowance
                    if interval.opened_by is TransitionKind.STARTED
                    else None
                ),
            )
            if replayed != interval:
                raise CognitionTimelineError(
                    f"区间 {interval.index} 不是区间 {previous.index} 经 "
                    f"{interval.opened_by.value} 转换的结果"
                )
        object.__setattr__(self, "intervals", intervals)

    # ── 构造 ────────────────────────────────────────────────────────────
    @classmethod
    def open(cls, *, log_length: int, sim: datetime, wall: str) -> "CognitionTimeline":
        """新世界：认知从"还没 Start"开始。"""
        return cls(
            (
                CognitionInterval(
                    index=0,
                    from_log=log_length,
                    causes=frozenset({CognitionCause.NOT_STARTED}),
                    backlog=(),
                    run_allowance=None,
                    allowance_since_log=None,
                    opened_by=TransitionKind.OPENED,
                    opened_at_sim=sim,
                    opened_at_wall=wall,
                ),
            )
        )

    # ── 查询 ────────────────────────────────────────────────────────────
    @property
    def current(self) -> CognitionInterval:
        return self.intervals[-1]

    def locate(self, log_position: int) -> Optional[CognitionInterval]:
        """日志位置 p 属于 from_log ≤ p 的**最后一个**区间；早于时间线则 None。

        连续转换之间没有记录时会产生零宽区间（相同 from_log），它们只是历史，
        不覆盖任何记录。
        """
        if isinstance(log_position, bool) or not isinstance(log_position, int):
            raise CognitionTimelineError("日志位置必须是整数")
        found = None
        for interval in self.intervals:
            if interval.from_log <= log_position:
                found = interval
            else:
                break
        return found

    # ── 转换 ────────────────────────────────────────────────────────────
    #
    # 每种转换怎么改原因、要不要追加 backlog、cutoff 落在哪，全部由
    # `_next_interval()` 按转换种类决定；调用方只给"何时（日志位置、模拟分钟、
    # 现实时间）"和 Start 的额度。存档加载用同一个函数把每个区间从前一个区间
    # 重放出来，于是单改一个区间的原因、cutoff 或额度都对不上。
    def _append(self, kind, *, log_length, sim, wall, run_allowance=None):
        return CognitionTimeline(
            self.intervals
            + (
                _next_interval(
                    self.current,
                    kind,
                    log_length=log_length,
                    sim=sim,
                    wall=wall,
                    run_allowance=run_allowance,
                ),
            )
        )

    def restored(self, *, log_length, sim, wall) -> "CognitionTimeline":
        """恢复：停机期间（与恢复后的第一个完整分钟之前）触发的，一律 process_stopped。

        世界一生的动作上限跨重启仍然成立；故障与现实时钟落后随旧进程结束，由新进程
        重新判定。`sim` 是恢复那一刻的模拟分钟（anchor_now 向下取整）。
        """
        return self._append(TransitionKind.RESTORED, log_length=log_length, sim=sim, wall=wall)

    def started(self, *, log_length, sim, wall, run_allowance) -> "CognitionTimeline":
        return self._append(
            TransitionKind.STARTED,
            log_length=log_length,
            sim=sim,
            wall=wall,
            run_allowance=run_allowance,
        )

    def stopped(self, *, log_length, sim, wall) -> "CognitionTimeline":
        return self._append(TransitionKind.STOPPED, log_length=log_length, sim=sim, wall=wall)

    def run_budget_exhausted(self, *, log_length, sim, wall) -> "CognitionTimeline":
        return self._append(
            TransitionKind.RUN_BUDGET_EXHAUSTED, log_length=log_length, sim=sim, wall=wall
        )

    def world_action_cap(self, *, log_length, sim, wall) -> "CognitionTimeline":
        return self._append(
            TransitionKind.WORLD_ACTION_CAP, log_length=log_length, sim=sim, wall=wall
        )

    def fault_began(self, *, log_length, sim, wall) -> "CognitionTimeline":
        return self._append(TransitionKind.FAULT_BEGAN, log_length=log_length, sim=sim, wall=wall)

    def fault_cleared(self, *, log_length, sim, wall) -> "CognitionTimeline":
        return self._append(
            TransitionKind.FAULT_CLEARED, log_length=log_length, sim=sim, wall=wall
        )

    def wall_clock_behind(self, *, log_length, sim, wall) -> "CognitionTimeline":
        return self._append(
            TransitionKind.WALL_CLOCK_BEHIND, log_length=log_length, sim=sim, wall=wall
        )

    def wall_clock_caught_up(self, *, log_length, sim, wall) -> "CognitionTimeline":
        return self._append(
            TransitionKind.WALL_CLOCK_CAUGHT_UP, log_length=log_length, sim=sim, wall=wall
        )

    # ── 序列化 ──────────────────────────────────────────────────────────
    def to_dict(self) -> Dict:
        return {"intervals": [interval.to_dict() for interval in self.intervals]}

    @classmethod
    def from_dict(cls, payload: Mapping) -> "CognitionTimeline":
        if not isinstance(payload, Mapping) or "intervals" not in payload:
            raise CognitionTimelineError("认知时间线必须是带 intervals 的字典")
        intervals = payload["intervals"]
        if not isinstance(intervals, list):
            raise CognitionTimelineError("intervals 必须是列表")
        return cls(tuple(CognitionInterval.from_dict(item) for item in intervals))


_ADDED_BY = {
    TransitionKind.STOPPED: CognitionCause.OPERATOR_PAUSED,
    TransitionKind.RUN_BUDGET_EXHAUSTED: CognitionCause.RUN_BUDGET_EXHAUSTED,
    TransitionKind.WORLD_ACTION_CAP: CognitionCause.WORLD_ACTION_CAP,
    TransitionKind.FAULT_BEGAN: CognitionCause.FAULT,
    TransitionKind.WALL_CLOCK_BEHIND: CognitionCause.WALL_CLOCK_BEHIND,
}

_CLEARED_BY = {
    TransitionKind.FAULT_CLEARED: CognitionCause.FAULT,
    TransitionKind.WALL_CLOCK_CAUGHT_UP: CognitionCause.WALL_CLOCK_BEHIND,
}


def _next_interval(
    current: CognitionInterval,
    kind,
    *,
    log_length: int,
    sim,
    wall: str,
    run_allowance: Optional[int] = None,
) -> CognitionInterval:
    """一次转换之后的区间。转换规则的唯一实现（设计 §13.3、§14.1）。

    | 转换 | 新区间的原因 | backlog 追加（cutoff 恒为 sim + 1min） |
    |---|---|---|
    | 恢复 | {not_started} ∪（上一区间 ∩ {world_action_cap}） | 上一区间 ∪ {process_stopped} |
    | Start | 上一区间 − 操作员可清的原因 | 上一区间 |
    | 故障解除 / 现实时钟追上 | 上一区间 − 对应原因 | 上一区间 |
    | 其余 | 上一区间 ∪ 对应原因 | 无 |

    cutoff 不由调用方给：`fired_at == sim` 的到期资格仍在旧原因下触发，
    `sim + 1min` 是它们与之后的分界（§14.1）。
    """
    try:
        kind = TransitionKind(kind)
    except ValueError:
        raise CognitionTimelineError(f"未知的转换: {kind!r}") from None
    sim = _sim_time(sim, "sim")
    if isinstance(log_length, bool) or not isinstance(log_length, int):
        raise CognitionTimelineError("log_length 必须是整数")
    if log_length < current.from_log:
        raise CognitionTimelineError("转换的日志位置不能早于当前区间")
    if kind is not TransitionKind.STARTED and run_allowance is not None:
        raise CognitionTimelineError("只有 Start 设置单次额度")

    allowance, since = current.run_allowance, current.allowance_since_log
    carried: FrozenSet[CognitionCause] = frozenset()
    if kind is TransitionKind.OPENED:
        raise CognitionTimelineError("opened 只能是时间线的第一个区间")
    if kind is TransitionKind.RESTORED:
        causes = frozenset({CognitionCause.NOT_STARTED}) | (
            current.causes & {CognitionCause.WORLD_ACTION_CAP}
        )
        carried = current.causes | {CognitionCause.PROCESS_STOPPED}
        allowance = since = None
    elif kind is TransitionKind.STARTED:
        causes = current.causes - OPERATOR_CLEARABLE
        carried = current.causes
        allowance = run_allowance
        since = None if run_allowance is None else log_length
    elif kind in _CLEARED_BY:
        cleared = _CLEARED_BY[kind]
        if cleared not in current.causes:
            raise CognitionTimelineError(f"{kind.value}：当前并没有 {cleared.value}")
        causes = current.causes - {cleared}
        carried = current.causes
    else:
        added = _ADDED_BY[kind]
        if kind is not TransitionKind.STOPPED and added in current.causes:
            raise CognitionTimelineError(f"{kind.value}：{added.value} 已经生效")
        if kind is TransitionKind.RUN_BUDGET_EXHAUSTED and current.run_allowance is None:
            raise CognitionTimelineError("没有单次额度，谈不上额度用完")
        causes = current.causes | {added}

    backlog = current.backlog
    if carried:
        backlog = _append_backlog(backlog, BacklogItem(sim + timedelta(minutes=1), carried))
    return CognitionInterval(
        index=current.index + 1,
        from_log=log_length,
        causes=causes,
        backlog=backlog,
        run_allowance=allowance,
        allowance_since_log=since,
        opened_by=kind,
        opened_at_sim=sim,
        opened_at_wall=wall,
    )


def wall_now() -> str:
    """现实时间（UTC，ISO）。只进运维记录，不进世界。"""
    return datetime.now(timezone.utc).isoformat()


def consumes_allowance(outcome) -> bool:
    """一条 Agency 结局是否用掉了认知（从而消耗单次额度）。

    只有"认知不可用"没有用到认知；其余结局——行动、弃权、各种拒绝——都是
    认知真的运行过之后给出的。
    """
    return getattr(outcome, "value", outcome) != "rejected_unavailable"


def next_minute_after(moment: datetime) -> datetime:
    """严格晚于 moment 的第一个完整分钟（backlog 的 cutoff 都用它算）。"""
    if not isinstance(moment, datetime):
        raise CognitionTimelineError("需要一个 datetime")
    return moment.replace(second=0, microsecond=0) + timedelta(minutes=1)


__all__ = [
    "BacklogItem",
    "CognitionCause",
    "CognitionCauseError",
    "CognitionInterval",
    "CognitionTimeline",
    "CognitionTimelineError",
    "OPERATOR_CLEARABLE",
    "TransitionKind",
    "consumes_allowance",
    "next_minute_after",
    "normalize_causes",
    "unavailable_causes",
    "wall_now",
]
