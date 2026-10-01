# pns/runtime/agency/planning.py — Planner 插槽与"值不值得想"前置门（PLAN-1）
#
# 这一层回答一个比策略更早的问题：**这条到期资格，此刻有没有东西可想？**
#
# 没有的话，引擎在问策略之前就把它收尾成 QUIET：不建提示、不调模型、不判分，
# 也不花单次额度。有的话，交给策略照旧判断 —— 前置门只会让角色少想，不会替
# 角色选择任何动作。
#
# 输入只有两样：角色作用域的 AgencyContext，以及这个角色**自己的**观察序列与
# Agency 记录推导出的两个数（游标之后有几条新的外部观察、上一次计费认知在
# 什么时候）。它读不到别人的位置、活动或观察，也读不到曝光判定日志。
#
# 判定次序（PLAN-1 §3，第一条命中的决定结果）：
#
#   回话机会          → 放行（回话有自己的门，见 engine._reply_gate）
#   没设前置门        → 放行（研究会话与旧调用方）
#   睡着了            → QUIET/asleep。睡眠优先于一切刺激
#   有人同处或同频道  → 放行
#   有新的外部观察    → 放行
#   从没计费认知过    → 放行（第一次）
#   离上次不到冷却    → QUIET/cooldown
#   否则              → 放行
#
# "认知不可用"在这之前就由引擎按认知时间线判掉了，不归这里管。
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Optional, Sequence, Tuple

from pns.models.cognition import QUIET_REASONS
from pns.models.observation import Observation
from pns.models.world_state import Availability
from pns.runtime.agency.context import AgencyContext


class PlanningError(ValueError):
    """规划上下文或规划结果自身不合法。"""


class PlanningReason(str, Enum):
    """前置门给出的理由。闭集；只有 asleep / cooldown 两个会收尾。"""

    REPLY = "reply"
    NO_GATE = "no_gate"
    ASLEEP = "asleep"
    COMPANY = "company"
    NEW_OBSERVATION = "new_observation"
    FIRST_COGNITION = "first_cognition"
    COOLDOWN = "cooldown"
    COOLDOWN_ELAPSED = "cooldown_elapsed"

    @property
    def quiet(self) -> bool:
        return self.value in QUIET_REASONS


@dataclass(frozen=True)
class PlanningContext:
    """前置门能依据的全部信息。从 AgencyContext 转写，外加两个自推导的数。"""

    character_id: str
    due_id: str
    at: datetime
    reply: bool
    availability: str
    # 此刻感知得到的其他角色（同处一地或同在频道），来自 AgencyContext。
    company: Tuple[str, ...]
    # 构造这份上下文时观察日志的长度。提交时写进记录，下一次从这里往后数。
    observation_cursor: int
    # 上一次计费认知的游标之后，这个角色新感知到的、能渲染成对话行的外部观察。
    new_external_observations: int
    last_charged_at: Optional[datetime]

    def to_dict(self) -> Dict:
        return {
            "character_id": self.character_id,
            "due_id": self.due_id,
            "at": self.at.isoformat(),
            "reply": self.reply,
            "availability": self.availability,
            "company": list(self.company),
            "observation_cursor": self.observation_cursor,
            "new_external_observations": self.new_external_observations,
            "last_charged_at": (
                self.last_charged_at.isoformat()
                if self.last_charged_at is not None
                else None
            ),
        }


def build_planning_context(
    context: AgencyContext,
    *,
    reply: bool,
    observation_cursor: int,
    observations_since_charged: Sequence[Observation],
    last_charged_at: Optional[datetime],
) -> PlanningContext:
    """把角色作用域的 AgencyContext 转写成规划上下文。

    observations_since_charged 必须只含这个角色自己的观察（跟 AgencyContext
    同一条接口规矩：取数在调用点显式写出来）。自己的动作产生的自观察不算
    外部刺激，所以自言自语不会刷新冷却。
    """
    if not isinstance(context, AgencyContext):
        raise PlanningError("规划上下文只能从 AgencyContext 构造")
    if (
        isinstance(observation_cursor, bool)
        or not isinstance(observation_cursor, int)
        or observation_cursor < 0
    ):
        raise PlanningError(f"observation_cursor 必须是非负整数，收到 {observation_cursor!r}")
    new_external = 0
    for observation in observations_since_charged:
        if not isinstance(observation, Observation):
            raise PlanningError("observations_since_charged 只能包含 Observation")
        if observation.observer_id != context.character_id:
            raise PlanningError(
                f"观察 '{observation.source_event_id}' 属于 "
                f"'{observation.observer_id}'，不能进 '{context.character_id}' 的规划"
            )
        if not observation.is_self_observation and observation.render_line() is not None:
            new_external += 1
    return PlanningContext(
        character_id=context.character_id,
        due_id=context.activation.due_id,
        at=context.observed_at,
        reply=reply,
        availability=context.availability,
        company=tuple(context.perceived_characters),
        observation_cursor=observation_cursor,
        new_external_observations=new_external,
        last_charged_at=last_charged_at,
    )


@dataclass(frozen=True)
class PlanProposal:
    """前置门的结论：这条到期是收尾，还是交给策略。

    它不是世界变更，也不是动作提案；它后面仍然是 Agency 校验、生成、Router
    判分和唯一的提交边界。provenance 进 Agency 记录，回答"这次为什么想 /
    为什么没想"。
    """

    character_id: str
    due_id: str
    reason: PlanningReason
    observation_cursor: int
    planner: str = "quiet_gate"

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", PlanningReason(self.reason))

    @property
    def quiet(self) -> bool:
        return self.reason.quiet

    def provenance(self) -> Dict:
        return {"planner": self.planner, "reason": self.reason.value}


def plan_activation(
    context: PlanningContext, cooldown: Optional[timedelta]
) -> PlanProposal:
    """按 PLAN-1 §3 的次序判一次。纯函数。"""
    if not isinstance(context, PlanningContext):
        raise PlanningError("plan_activation 需要 PlanningContext")

    def result(reason: PlanningReason) -> PlanProposal:
        return PlanProposal(
            character_id=context.character_id,
            due_id=context.due_id,
            reason=reason,
            observation_cursor=context.observation_cursor,
        )

    if context.reply:
        return result(PlanningReason.REPLY)
    if cooldown is None:
        return result(PlanningReason.NO_GATE)
    if context.availability == Availability.ASLEEP.value:
        return result(PlanningReason.ASLEEP)
    if context.company:
        return result(PlanningReason.COMPANY)
    if context.new_external_observations:
        return result(PlanningReason.NEW_OBSERVATION)
    if context.last_charged_at is None:
        return result(PlanningReason.FIRST_COGNITION)
    if context.at - context.last_charged_at < cooldown:
        return result(PlanningReason.COOLDOWN)
    return result(PlanningReason.COOLDOWN_ELAPSED)


__all__ = [
    "PlanProposal",
    "PlanningContext",
    "PlanningError",
    "PlanningReason",
    "build_planning_context",
    "plan_activation",
]
