# pns/runtime/agency — 能动性层
#
#     事件真相 → 曝光资格 → 观察投影 → **能动性** → （以后的）记忆
#                    ↑                      ↓
#              调度器说"该考虑了"      提案 → 校验 → 事件（回到 P5 提交边界）
#
# 这一层只走一步：给定一个角色此刻感知到什么、有资格做什么，它选择行动吗，
# 提出哪一个已声明的动作。它不决定什么时候该考虑（调度器）、谁能感知到
# （曝光）、说出来像不像本人（Router）、记不记得住（记忆）。
#
# 这个包的初始化保持轻：只从子模块转出公开名字，不做任何 I/O、不读配置、
# 不初始化重载边界（有子进程测试盯着）。
from pns.models.action import (
    ActionDefinition,
    ActionError,
    ActionId,
    ActionProposal,
    LegalAction,
    Precondition,
    TargetKind,
    action_definition,
    catalogue,
    catalogue_ids,
    new_proposal_id,
)
from pns.models.agency import (
    AgencyBudget,
    AgencyError,
    AgencyLog,
    AgencyOutcome,
    AgencyRecord,
)
from pns.runtime.agency.context import (
    AgencyContext,
    AgencyContextError,
    build_agency_context,
)
from pns.runtime.agency.effects import AgencyEffectError, event_for_proposal
from pns.runtime.agency.engine import AgencyEngine, AgencyEngineError, ProposalPlan
from pns.runtime.agency.policy import (
    AbstainPolicy,
    AgencyPolicy,
    AgencyPolicyError,
    FirstLegalActionPolicy,
    ModelBackedPolicy,
    PolicyDecision,
    ScriptedPolicy,
    default_policy,
    derived_proposal_id,
)
from pns.runtime.agency.preconditions import (
    failed_preconditions,
    is_legal,
    legal_actions,
)

__all__ = [
    "AbstainPolicy",
    "ActionDefinition",
    "ActionError",
    "ActionId",
    "ActionProposal",
    "AgencyBudget",
    "AgencyContext",
    "AgencyContextError",
    "AgencyEffectError",
    "AgencyEngine",
    "AgencyEngineError",
    "AgencyError",
    "AgencyLog",
    "AgencyOutcome",
    "AgencyPolicy",
    "AgencyPolicyError",
    "AgencyRecord",
    "FirstLegalActionPolicy",
    "LegalAction",
    "ModelBackedPolicy",
    "PolicyDecision",
    "Precondition",
    "ProposalPlan",
    "ScriptedPolicy",
    "TargetKind",
    "action_definition",
    "build_agency_context",
    "catalogue",
    "catalogue_ids",
    "default_policy",
    "derived_proposal_id",
    "event_for_proposal",
    "failed_preconditions",
    "is_legal",
    "legal_actions",
    "new_proposal_id",
]
