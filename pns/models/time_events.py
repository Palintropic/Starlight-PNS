# pns/models/time_events.py — 安静的分钟记不记成世界事件（WORLD-1 存档增长设计 §3）
#
# 1:1 的持久世界每个现实分钟走一个时钟步。默认（record）每一步都写一条
# world.time_advanced，这是 P8 的原始契约。项目所有者可以在面板上把一个世界拨到
# skip：此后**安静**的时钟步（没有到期、没有作息/行程/频道事件）照样推进
# WorldState.clock，但不写时间事件。
#
# 这份账本回答的问题只有一个：**某一段模拟时间里，安静的分钟有没有被记下来**。
# 于是读历史的人能把"没记"和"丢了"分开：两条时间事件之间出现空档，那段时间
# 必须整个落在 skip 生效的范围里，否则这份存档就是缺了事件（validate_time_gaps）。
#
# 时间事件链有一个独立的起点 epoch：会话绑定世界状态那一刻的时钟。它存在这份
# 账本里，而不是靠"第一条时间事件自己说从哪开始" —— 否则把时间事件整条删掉，
# 就没有任何东西能证明时钟是怎么走到现在的（全量审查 F3）。
#
# 规则：
#   * 每拨一次追加一条记录 {value, from_sim, at_wall}，与拨动同一个事务；
#     值与当前相同的"拨动"不是拨动，不记。
#   * 只追加，不改写；from_sim 不倒退；相邻两条的值必须不同。
#   * 没有任何记录 = record（阶段一的行为）。
#   * 切换只影响之后：已经写下的时间事件一条都不动。
#
# 它是运维记录（Article XIII），不是世界真相，不进任何角色的上下文。不可变值
# 对象，整体替换，随事务回滚、随存档往返。
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Iterable, List, Optional, Tuple


class TimeEventPolicyError(ValueError):
    """时间事件策略账本不合法，或一次拨动不成立。"""


class QuietTime(str, Enum):
    RECORD = "record"
    SKIP = "skip"


def _sim(value, label: str) -> datetime:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            raise TimeEventPolicyError(f"{label} 不是 ISO 时间: {value!r}") from None
    else:
        raise TimeEventPolicyError(f"{label} 必须是 ISO 时间")
    if moment.tzinfo is not None:
        raise TimeEventPolicyError(f"{label} 必须是不带时区的模拟时间")
    return moment


@dataclass(frozen=True)
class TimeEventPolicyRecord:
    """一次拨动：从模拟时刻 from_sim 起，安静的分钟按 value 处理。"""

    value: QuietTime
    from_sim: datetime
    at_wall: str

    def to_dict(self) -> Dict:
        return {
            "policy": "quiet_time_events",
            "value": self.value.value,
            "from_sim": self.from_sim.isoformat(),
            "at_wall": self.at_wall,
        }

    @classmethod
    def from_dict(cls, payload) -> "TimeEventPolicyRecord":
        if not isinstance(payload, dict):
            raise TimeEventPolicyError("策略记录必须是字典")
        if payload.get("policy") != "quiet_time_events":
            raise TimeEventPolicyError(f"不认识的策略: {payload.get('policy')!r}")
        try:
            value = QuietTime(payload.get("value"))
        except ValueError:
            raise TimeEventPolicyError(
                f"quiet_time_events 的值只能是 record / skip，收到 {payload.get('value')!r}"
            ) from None
        at_wall = payload.get("at_wall")
        if not isinstance(at_wall, str) or not at_wall:
            raise TimeEventPolicyError("策略记录的 at_wall 必须是非空字符串")
        return cls(value=value, from_sim=_sim(payload.get("from_sim"), "from_sim"), at_wall=at_wall)


@dataclass(frozen=True)
class TimeEventPolicy:
    """一个世界的安静分钟策略：时间事件链的起点 + 拨动记录，按时间顺序。"""

    epoch: datetime
    records: Tuple[TimeEventPolicyRecord, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "epoch", _sim(self.epoch, "epoch"))
        object.__setattr__(self, "records", tuple(self.records))
        previous: Optional[TimeEventPolicyRecord] = None
        for record in self.records:
            if not isinstance(record, TimeEventPolicyRecord):
                raise TimeEventPolicyError("策略账本里只能是 TimeEventPolicyRecord")
            if record.from_sim < self.epoch:
                raise TimeEventPolicyError("策略记录的 from_sim 早于时间事件链的起点")
            if previous is None:
                if record.value is QuietTime.RECORD:
                    # 默认就是 record：第一条记录只能是拨到 skip。
                    raise TimeEventPolicyError("第一条策略记录必须是拨到 skip")
            else:
                if record.value is previous.value:
                    raise TimeEventPolicyError("相邻两条策略记录的值必须不同")
                if record.from_sim < previous.from_sim:
                    raise TimeEventPolicyError("策略记录的 from_sim 不能倒退")
            previous = record

    @property
    def current(self) -> QuietTime:
        return self.records[-1].value if self.records else QuietTime.RECORD

    @property
    def since(self) -> Optional[TimeEventPolicyRecord]:
        """当前值从哪一条记录起生效；从来没拨过是 None。"""
        return self.records[-1] if self.records else None

    def flipped(self, value: QuietTime, *, sim: datetime, wall: str) -> "TimeEventPolicy":
        """拨到 value。值与当前相同就是拨不动（调用方先判断 current）。"""
        value = QuietTime(value)
        if value is self.current:
            raise TimeEventPolicyError(f"安静的分钟已经是 {value.value}，没有可拨的")
        return TimeEventPolicy(
            self.epoch,
            self.records
            + (TimeEventPolicyRecord(value=value, from_sim=_sim(sim, "sim"), at_wall=wall),),
        )

    def record_spans(self) -> List[Tuple[Optional[datetime], Optional[datetime]]]:
        """record 生效的各段 [起, 止)，None 表示无界。长度为 0 的段不算。"""
        spans: List[Tuple[Optional[datetime], Optional[datetime]]] = []
        start: Optional[datetime] = None  # 开头默认 record，从负无穷起
        recording = True
        for record in self.records:
            if recording and record.value is QuietTime.SKIP:
                if start is None or start < record.from_sim:
                    spans.append((start, record.from_sim))
                recording = False
            elif not recording and record.value is QuietTime.RECORD:
                start = record.from_sim
                recording = True
        if recording:
            spans.append((start, None))
        return spans

    def covers(self, start: datetime, end: datetime) -> bool:
        """[start, end) 整段都在 skip 生效的范围里吗。"""
        if end <= start:
            return True
        for span_start, span_end in self.record_spans():
            lower = start if span_start is None else max(start, span_start)
            upper = end if span_end is None else min(end, span_end)
            if lower < upper:
                return False
        return True

    def to_dict(self) -> Dict:
        return {
            "epoch": self.epoch.isoformat(),
            "records": [record.to_dict() for record in self.records],
        }

    @classmethod
    def from_dict(cls, payload) -> "TimeEventPolicy":
        if not isinstance(payload, dict):
            raise TimeEventPolicyError("时间事件策略必须是字典")
        records = payload.get("records")
        if not isinstance(records, list):
            raise TimeEventPolicyError("时间事件策略的 records 必须是数组")
        return cls(
            _sim(payload.get("epoch"), "epoch"),
            tuple(TimeEventPolicyRecord.from_dict(item) for item in records),
        )


def time_gaps(steps: Iterable[Tuple[datetime, int]], clock: datetime, epoch: datetime):
    """从 epoch 起、时间事件之间、以及最后一条之后到此刻时钟之间的空档 [起, 止)。

    steps 是按历史顺序的 (occurred_at, minutes)。一条 world.time_advanced 的
    occurred_at 是推进**前**的时刻，推到 occurred_at + minutes；下一条应当正好从
    那里开始，第一条应当正好从 epoch 开始。一条都没有时，[epoch, clock) 整段是空档。
    """
    if clock < epoch:
        raise TimeEventPolicyError(
            f"世界时钟 {clock.isoformat()} 早于时间事件链的起点 {epoch.isoformat()}"
        )
    gaps = []
    reached: datetime = epoch
    for occurred_at, minutes in steps:
        if occurred_at != reached:
            if occurred_at < reached:
                raise TimeEventPolicyError(
                    f"时间事件重叠：{occurred_at.isoformat()} 早于上一条推到的 "
                    f"{reached.isoformat()}"
                )
            gaps.append((reached, occurred_at))
        reached = occurred_at + timedelta(minutes=minutes)
    if clock != reached:
        if clock < reached:
            raise TimeEventPolicyError(
                f"世界时钟 {clock.isoformat()} 早于最后一条时间事件推到的 {reached.isoformat()}"
            )
        gaps.append((reached, clock))
    return gaps


def legacy_epoch(steps: Iterable[Tuple[datetime, int]], clock: datetime) -> datetime:
    """版本 2 存档没有 epoch：按旧规矩从第一条时间事件起算，一条都没有就是此刻。

    这是旧存档的已知上限：它们本来就没记起点，所以"整条时间事件被删掉"在
    版本 2 上无法被发现。版本 3 起 epoch 必填。
    """
    for occurred_at, _ in steps:
        return occurred_at
    return clock


def quiet_time_report(policy: Optional[TimeEventPolicy]) -> Dict:
    """给状态面看的一份：现在记不记、从哪一刻起、拨过几次。"""
    since = policy.since if policy is not None else None
    current = policy.current if policy is not None else QuietTime.RECORD
    return {
        "record": current is QuietTime.RECORD,
        "since_sim": since.from_sim.isoformat() if since is not None else None,
        "since_wall": since.at_wall if since is not None else None,
        "flips": len(policy.records) if policy is not None else 0,
    }
