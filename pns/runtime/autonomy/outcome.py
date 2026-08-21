# pns/runtime/autonomy/outcome.py — 一条到期资格的处理结局
#
# 这个模块只回答一个问题：**这次处理，到底算什么？**
#
# 边界要求每条到期资格都有一个耐久、可查的终局，而且"从没被处理过"必须跟
# "处理过，结论是不动"分得开。所以结局码是闭集，而且每一个都对应协调器里
# 真实存在的一条分支：
#
#   ACTED             提案通过判分与校验并已提交，世界历史里有对应事件。
#   ABSTAINED         显式不动。合法结果，不是错误。
#   REJECTED          评估过，结论是不行（Router 判它 OOC、输出是垃圾、
#                     动作不合法、过期、超预算）。
#   FAILED_RETRYABLE  基础设施性的失败（模型不可用、判分器不可用、提交事务
#                     被打断）。**没有提交任何东西**，到期记录仍然待处理。
#   FAILED_TERMINAL   重试预算用完了，或者失败本身说明这条到期没救。
#   STOPPED           运行时被要求停止，这次处理在安全边界上放手了。
#
# 前五个是边界点名要求的那五个；STOPPED 是第六个，刻意单列而不是塞进
# FAILED_RETRYABLE：一次主动停机不是一次失败，把两者混成一个码会让状态面板
# 说谎，也会让"停机期间攒了多少条待处理"这个问题没法回答。
#
# **耐久性归属**：这些结果对象本身是给调用方看的投影，不是权威存储。权威的
# 那份在会话里 —— 一条到期资格的终局，就是"Agency 日志里有它的记录，而且
# 投递箱里它被确认了"。FAILED_RETRYABLE 与 STOPPED 恰恰是**没有**那条记录、
# 也没有被确认，于是它们在存档里的表现就是"仍然待处理"，恢复之后接着处理，
# 不会静默消失。
#
# 重试计数活在协调器进程里，不进存档：跨进程重启的持久化是 P12 的事。写在
# 这里，是为了让这个已知边界是明说的，而不是以后被当成 bug 发现。
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, Mapping, Optional

from pns.models.agency import AgencyOutcome


class OutcomeError(ValueError):
    """结果对象或重试预算不合法。"""


class ActivationOutcome(str, Enum):
    """一次到期处理的结局码。"""

    ACTED = "acted"
    ABSTAINED = "abstained"
    REJECTED = "rejected"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    STOPPED = "stopped"

    @property
    def terminal(self) -> bool:
        """这条到期资格就此了结了吗。

        终局的定义是"不会再被处理"，而不是"成功了"：被拒和终局失败同样是
        终局。可重试的失败与停机不是 —— 它们把到期记录留在待处理里。
        """
        return self in _TERMINAL

    @property
    def committed(self) -> bool:
        return self is ActivationOutcome.ACTED


_TERMINAL = frozenset(
    {
        ActivationOutcome.ACTED,
        ActivationOutcome.ABSTAINED,
        ActivationOutcome.REJECTED,
        ActivationOutcome.FAILED_TERMINAL,
    }
)

# Agency 的结论码 → 协调器的结局码。刻意是一张显式的表而不是几个 if：
# Agency 那边加一个结论码，这里就会在查表时响亮失败，而不是悄悄落进
# "别的都算 rejected"。
_FROM_AGENCY = {
    AgencyOutcome.ACTED: ActivationOutcome.ACTED,
    AgencyOutcome.ABSTAINED: ActivationOutcome.ABSTAINED,
    AgencyOutcome.REJECTED_ILLEGAL: ActivationOutcome.REJECTED,
    AgencyOutcome.REJECTED_STALE: ActivationOutcome.REJECTED,
    AgencyOutcome.REJECTED_BUDGET: ActivationOutcome.REJECTED,
    AgencyOutcome.REJECTED_POLICY_ERROR: ActivationOutcome.REJECTED,
}


def outcome_for(agency_outcome) -> ActivationOutcome:
    agency_outcome = AgencyOutcome(agency_outcome)
    try:
        return _FROM_AGENCY[agency_outcome]
    except KeyError:
        raise OutcomeError(
            f"Agency 结论码 {agency_outcome.value} 还没有对应的运行时结局码"
        ) from None


@dataclass(frozen=True)
class RetryPolicy:
    """重试预算。显式、有限，而且有一条真实分支盯着。

    没有预算的重试等于"永远待处理"，那正是边界不允许的状态：一条到期资格
    不能在没有明说的重试策略下无限期悬着。预算用完之后，协调器写一条终局
    失败记录并确认交接 —— 结局可能是"没做成"，但绝不会是"不知道"。
    """

    max_attempts: int = 3

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(
            self.max_attempts, int
        ):
            raise OutcomeError(f"max_attempts 必须是整数，收到 {self.max_attempts!r}")
        if self.max_attempts <= 0:
            raise OutcomeError(f"max_attempts 必须大于 0，收到 {self.max_attempts}")

    def exhausted(self, attempt: int) -> bool:
        return attempt >= self.max_attempts

    def to_dict(self) -> Dict:
        return {"max_attempts": self.max_attempts}


@dataclass(frozen=True)
class ActivationResult:
    """一次到期处理的完整结果投影。

    它是**报告**，不是权威状态：权威在 Agency 日志、投递箱和世界历史里。
    所以它可以随便被交给调用方 —— 但交出去的必须是新的可变结构，见
    to_dict()。
    """

    due_id: str
    character_id: str
    outcome: ActivationOutcome
    attempt: int
    at: datetime
    agency_outcome: Optional[AgencyOutcome] = None
    event_id: Optional[str] = None
    memories: int = 0
    detail: Mapping = field(default_factory=dict)

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        if not isinstance(self.due_id, str) or not self.due_id:
            raise OutcomeError("due_id 必须是非空字符串")
        try:
            set_(self, "outcome", ActivationOutcome(self.outcome))
        except ValueError:
            raise OutcomeError(f"未知的结局码: {self.outcome!r}") from None
        if self.agency_outcome is not None:
            set_(self, "agency_outcome", AgencyOutcome(self.agency_outcome))
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int):
            raise OutcomeError("attempt 必须是整数")
        if self.attempt < 0:
            raise OutcomeError("attempt 不能是负数")
        if not isinstance(self.at, datetime):
            raise OutcomeError("at 必须是 datetime（模拟时钟时间）")
        if not isinstance(self.detail, Mapping):
            raise OutcomeError("detail 必须是字典")
        if self.outcome.committed and self.event_id is None:
            # acted 却指不出一条事件，等于报告说"做了"而世界说"没有"。
            raise OutcomeError("acted 结果必须指向它提交出来的那条事件")
        if not self.outcome.committed and self.event_id is not None:
            raise OutcomeError(f"{self.outcome.value} 结果不能指向任何事件")

    @property
    def terminal(self) -> bool:
        return self.outcome.terminal

    def to_dict(self) -> Dict:
        """JSON 安全的新结构。改它影响不到任何已发生的事。"""
        return {
            "due_id": self.due_id,
            "character_id": self.character_id,
            "outcome": self.outcome.value,
            "terminal": self.terminal,
            "attempt": self.attempt,
            "at": self.at.isoformat(),
            "agency_outcome": (
                self.agency_outcome.value if self.agency_outcome is not None else None
            ),
            "event_id": self.event_id,
            "memories": self.memories,
            "detail": _plain(self.detail),
        }


def _plain(value):
    """把可能是只读视图的结构复制成普通可变结构。"""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


__all__ = [
    "ActivationOutcome",
    "ActivationResult",
    "OutcomeError",
    "RetryPolicy",
    "outcome_for",
]
