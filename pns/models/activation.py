# pns/models/activation.py — 排期激活的领域模型
#
# ScheduledActivation 只回答一个问题：**到了哪个模拟时刻，什么变得有资格发生**。
# 它不回答角色想不想动、要做什么、说什么 —— 那是 Agency / Planner（后续阶段）
# 的事。排期到点只产出一条 ActivationDue 记录，不产出台词，也不改世界状态。
#
# 时间口径跟 WorldState.clock 完全一致：timezone-naive 的模拟时间。带时区的
# datetime 一律拒绝 —— 两种口径混着比较不是"稍微不准"，而是要么直接抛
# TypeError，要么悄悄按 UTC 偏移错开几个小时，两种都比拒绝难查得多。
#
# 和 Event 一样，激活构造出来就不可变，payload 在构造时深冻结，to_dict() 才
# 交出可以随便改的普通字典。
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Mapping, Optional, Tuple
from uuid import uuid4

from pns.models.frozen import freeze_json_value, thaw_json_value


class ActivationError(ValueError):
    """激活本身不合法（缺 ID、未知类型、带时区的时间、非法周期值等）。"""


class ActivationKind(str, Enum):
    """本阶段有完整、已实现语义的激活类型。

    这里只有一个成员，这是刻意的：一个激活类型算"已实现"，标准是它到期时
    调度器**真的知道该产出什么**。CHARACTER_ACTIVATION 的语义是完整的 ——
    到点产出一条"该轮到这个角色考虑行动了"的记录，交给 P9 去决定要不要动。
    其它想得到的类型（作息切换、地点移动、提醒触发）到期之后都得由某个还
    不存在的层来执行，写进枚举就只是个占位符，占位符会让调用方以为排它有用。
    """

    CHARACTER_ACTIVATION = "character.activation"


def new_activation_id(prefix: str = "act") -> str:
    """给没有天然稳定 ID 的激活生成一个。"""
    return f"{prefix}_{uuid4().hex}"


def _require_simulation_time(value, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise ActivationError(f"{label} 必须是 datetime（模拟时钟时间）")
    if value.tzinfo is not None:
        raise ActivationError(
            f"{label} 必须是 timezone-naive 的模拟时间，收到带时区的 {value!r}"
        )
    return value


def _require_id(value, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ActivationError(f"{label} 必须是非空字符串")
    return value


def _require_positive_int(value, label: str) -> int:
    # bool 是 int 的子类：True 当成 1 分钟周期会让一个明显写错的配置跑起来。
    if isinstance(value, bool) or not isinstance(value, int):
        raise ActivationError(f"{label} 必须是整数，收到 {value!r}")
    if value <= 0:
        raise ActivationError(f"{label} 必须大于 0，收到 {value}")
    return value


@dataclass(frozen=True)
class ScheduledActivation:
    """一条排进队列的激活。"""

    activation_id: str
    kind: ActivationKind
    due_at: datetime
    character_id: Optional[str] = None
    # None 表示一次性；正整数表示"每隔这么多分钟再来一次"。
    interval_minutes: Optional[int] = None
    payload: Mapping = field(default_factory=dict)

    def __post_init__(self) -> None:
        set_ = object.__setattr__

        set_(self, "activation_id", _require_id(self.activation_id, "activation_id"))
        try:
            set_(self, "kind", ActivationKind(self.kind))
        except ValueError:
            raise ActivationError(f"未知的激活类型: {self.kind!r}") from None

        due_at = _require_simulation_time(self.due_at, "due_at")
        if due_at.second or due_at.microsecond:
            # 模拟时钟只落在整分钟上（场景投影出来的初始时钟是整分钟，事件
            # 推进也以分钟为单位），所以带秒的到期时间永远不会被正好命中，
            # 实际语义会变成"它之后的第一个整分钟"。与其让这个偏移悄悄存在，
            # 不如在排进去的时候就拒绝。
            raise ActivationError(
                f"due_at 必须落在整分钟上，收到 {due_at.isoformat()}"
            )
        set_(self, "due_at", due_at)

        if self.kind is ActivationKind.CHARACTER_ACTIVATION:
            set_(self, "character_id", _require_id(self.character_id, "character_id"))

        if self.interval_minutes is not None:
            set_(
                self,
                "interval_minutes",
                _require_positive_int(self.interval_minutes, "interval_minutes"),
            )

        if not isinstance(self.payload, Mapping):
            raise ActivationError("payload 必须是字典")
        set_(
            self,
            "payload",
            freeze_json_value(self.payload, path="payload", error=ActivationError),
        )

    def __hash__(self) -> int:
        # 冻结后的 payload 不可哈希，而激活的身份本来就是 activation_id
        # （一个队列里它是唯一的）。
        return hash(self.activation_id)

    @property
    def is_recurring(self) -> bool:
        return self.interval_minutes is not None

    # ── 周期推算 ────────────────────────────────────────────────────────
    def next_occurrence(self, after: datetime) -> Tuple["ScheduledActivation", int]:
        """算出 after 之后的下一次触发，返回 (新的激活, 被跨过的次数)。

        用整数步长从**原始 due_at** 起算，而不是从 after 起算：后者会让每次
        触发都把相位往后拖，一个"每天 07:00"的排期跑几次就漂成"每天 07:13"。
        步数取 elapsed // interval + 1，保证结果严格晚于 after。

        一次推进跨过了多次触发时，这里不会假装它们没发生：返回的第二个值就是
        被跨过的次数，调用方（PersistentScheduler）把它原样写进到期记录。
        跨零点、跨月、跨年都由 timedelta 自己算，这里不做任何日期特判 ——
        特判正是"跨零点少跑一次"这类 bug 的来源。
        """
        if self.interval_minutes is None:
            raise ActivationError(
                f"一次性激活 '{self.activation_id}' 没有下一次触发"
            )
        after = _require_simulation_time(after, "after")
        interval = timedelta(minutes=self.interval_minutes)
        elapsed = after - self.due_at
        steps = 1 if elapsed < timedelta(0) else (elapsed // interval) + 1
        try:
            next_due = self.due_at + steps * interval
        except OverflowError:
            raise ActivationError(
                f"激活 '{self.activation_id}' 的下一次触发超出可表示的时间范围"
            ) from None
        return (
            ScheduledActivation(
                activation_id=self.activation_id,
                kind=self.kind,
                due_at=next_due,
                character_id=self.character_id,
                interval_minutes=self.interval_minutes,
                payload=thaw_json_value(self.payload),
            ),
            steps - 1,
        )

    # ── 序列化 ──────────────────────────────────────────────────────────
    def to_dict(self) -> Dict:
        """完整公开形状；返回值是全新的可变结构，改它影响不到激活本身。"""
        return {
            "activation_id": self.activation_id,
            "kind": self.kind.value,
            "due_at": self.due_at.isoformat(),
            "character_id": self.character_id,
            "interval_minutes": self.interval_minutes,
            "payload": thaw_json_value(self.payload),
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "ScheduledActivation":
        if not isinstance(payload, Mapping):
            raise ActivationError("激活必须是字典")
        for required in ("activation_id", "kind", "due_at"):
            if required not in payload:
                raise ActivationError(f"激活缺少必填字段: {required}")
        due_at = payload["due_at"]
        if isinstance(due_at, str):
            try:
                due_at = datetime.fromisoformat(due_at)
            except ValueError:
                raise ActivationError(f"无法解析的 due_at: {payload['due_at']!r}") from None
        return cls(
            activation_id=payload["activation_id"],
            kind=payload["kind"],
            due_at=due_at,
            character_id=payload.get("character_id"),
            interval_minutes=payload.get("interval_minutes"),
            payload=payload.get("payload", {}),
        )


@dataclass(frozen=True)
class ActivationDue:
    """一条"到期"记录：某个激活在这一次时间推进里变得可以发生了。

    它是调度器唯一的产出形式。刻意**不含**任何"要做什么"的字段：没有台词、
    没有动作、没有目标。到期只是资格，选择留给 P9。
    """

    activation_id: str
    kind: ActivationKind
    due_at: datetime
    fired_at: datetime
    sequence: int
    character_id: Optional[str] = None
    # 这一次推进跨过的额外触发次数（一次性激活恒为 0）。周期激活被合并成
    # 一条记录时，跨过了几次就写在这里 —— 少跑的次数必须是明说的，不能是
    # 悄悄消失的。
    missed_occurrences: int = 0
    # 还会再来的话，下一次是什么时候；一次性激活是 None。
    next_due_at: Optional[datetime] = None
    payload: Mapping = field(default_factory=dict)

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "activation_id", _require_id(self.activation_id, "activation_id"))
        try:
            set_(self, "kind", ActivationKind(self.kind))
        except ValueError:
            raise ActivationError(f"未知的激活类型: {self.kind!r}") from None

        due_at = _require_simulation_time(self.due_at, "due_at")
        fired_at = _require_simulation_time(self.fired_at, "fired_at")
        if fired_at < due_at:
            raise ActivationError(
                f"激活 '{self.activation_id}' 的触发时间 {fired_at.isoformat()} "
                f"早于到期时间 {due_at.isoformat()}"
            )
        set_(self, "due_at", due_at)
        set_(self, "fired_at", fired_at)

        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise ActivationError("sequence 必须是整数")
        if self.sequence < 0:
            raise ActivationError("sequence 不能是负数")
        if isinstance(self.missed_occurrences, bool) or not isinstance(
            self.missed_occurrences, int
        ):
            raise ActivationError("missed_occurrences 必须是整数")
        if self.missed_occurrences < 0:
            raise ActivationError("missed_occurrences 不能是负数")

        if self.character_id is not None:
            set_(self, "character_id", _require_id(self.character_id, "character_id"))

        if self.next_due_at is not None:
            next_due_at = _require_simulation_time(self.next_due_at, "next_due_at")
            if next_due_at <= fired_at:
                # 下一次不晚于这一次的触发时刻，等于队列里留了一条已经过期的
                # 激活 —— 那正是"同一个激活反复到期"这类死循环的形状。
                raise ActivationError(
                    f"激活 '{self.activation_id}' 的下一次触发 "
                    f"{next_due_at.isoformat()} 不晚于本次触发 {fired_at.isoformat()}"
                )
            set_(self, "next_due_at", next_due_at)

        if not isinstance(self.payload, Mapping):
            raise ActivationError("payload 必须是字典")
        set_(
            self,
            "payload",
            freeze_json_value(self.payload, path="payload", error=ActivationError),
        )

    def __hash__(self) -> int:
        return hash((self.activation_id, self.fired_at))

    def to_dict(self) -> Dict:
        return {
            "activation_id": self.activation_id,
            "kind": self.kind.value,
            "due_at": self.due_at.isoformat(),
            "fired_at": self.fired_at.isoformat(),
            "sequence": self.sequence,
            "character_id": self.character_id,
            "missed_occurrences": self.missed_occurrences,
            "next_due_at": (
                self.next_due_at.isoformat() if self.next_due_at is not None else None
            ),
            "payload": thaw_json_value(self.payload),
        }
