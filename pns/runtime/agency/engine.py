# pns/runtime/agency/engine.py — Agency 的判断与提交
#
# 引擎回答的问题只有一个：**这条到期资格，这个角色选择行动吗？如果行动，
# 提出哪一个已声明的动作？**
#
# 它不回答（写在这里免得以后被顺手加进来）：什么时候该考虑（Scheduler）、
# 谁能感知到（Exposure）、说出来像不像本人（Router）、记不记得住（Memory）。
#
# 四条硬约束：
#
#   1. **提案不是世界真相。** propose() 是纯的：它建上下文、问策略、判合法性，
#      一个字节的状态都不改。只有 commit() 里被接受的提案才经由 P5 的提交边界
#      变成事件。这条分离不是为了好看 —— 它让"模型建议了什么"和"世界发生了
#      什么"在代码里就是两个不同的对象。
#   2. **前置条件在提交那一刻重判。** 提出时合法不代表提交时还合法：时钟可能
#      走了，人可能换了地方，频道成员可能变了。重判不过就是 REJECTED_STALE，
#      不是"尽量提交"。
#   3. **交接只发生一次。** 一条 ActivationDue 至多被评估一次。身份来自投递箱
#      的 due_id，确认（acknowledge）跟审计记录在同一个事务里落地，所以"这条
#      到期处理过没有"永远只有一个答案。
#   4. **被拒的一切都不留痕迹在世界上。** 非法、过期、超预算、策略失败 ——
#      四种都不产出事件、不产出观察、不留半截世界状态。它们只留审计记录，
#      因为"评估过但没动"和"根本没评估"必须能分开。
#
# 归属跟调度器一样：审计日志归 SessionState 所有，引擎是它上面的服务，一个
# 会话只能绑一个。存档里的 agency 段就是那份日志。
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Mapping, Optional, Tuple

from pns.models.action import ActionProposal
from pns.models.activation import ActivationDue
from pns.models.agency import (
    AgencyBudget,
    AgencyError,
    AgencyLog,
    AgencyOutcome,
    AgencyRecord,
)
from pns.models.activation_outbox import ActivationOutboxError
from pns.models.session import SessionState
from pns.models.world_state import WorldState
from pns.runtime.agency.context import AgencyContext, build_agency_context
from pns.runtime.agency.effects import event_for_proposal
from pns.runtime.agency.policy import (
    AgencyPolicy,
    AgencyPolicyError,
    PolicyDecision,
    default_policy,
)
from pns.runtime.agency.preconditions import failed_preconditions
from pns.runtime.event_commit import commit_session_event


class AgencyEngineError(ValueError):
    """这次调用根本不该发生（交接对不上、会话没世界、绑了第二个引擎等）。

    它跟 REJECTED_* 是两类东西：REJECTED_* 是"评估过，结论是不行"，会留下
    审计记录；AgencyEngineError 是"这次评估的前提就不成立"，什么都不留。
    """


@dataclass(frozen=True)
class ProposalPlan:
    """propose() 的产物：一个还没有落地的判断。

    它**不是**世界状态，也不是审计记录。拿着它不提交，世界就当这次判断没
    发生过（到期记录仍然待处理，可以重来）。

    `verdict` 复用 AgencyOutcome 是刻意的：提案期和提交期用同一套结论词汇，
    免得出现两张意思相近但对不上的表。ACTED 在这里的意思是"通过了提案期的
    全部校验，可以拿去提交"，不是"已经做了"。
    """

    due: ActivationDue
    character_id: str
    policy: str
    # 判断依据的那一刻模拟时钟。提交时会拿它跟世界时钟对一次。
    proposed_at: datetime
    verdict: AgencyOutcome
    proposal: Optional[ActionProposal] = None
    detail: Mapping = field(default_factory=dict)
    rationale: str = ""

    @property
    def would_act(self) -> bool:
        return self.verdict.acted

    def to_dict(self) -> Dict:
        return {
            "due_id": self.due.due_id,
            "character_id": self.character_id,
            "policy": self.policy,
            "proposed_at": self.proposed_at.isoformat(),
            "verdict": self.verdict.value,
            "proposal": (
                self.proposal.to_dict() if self.proposal is not None else None
            ),
            "detail": dict(self.detail),
            "rationale": self.rationale,
        }


class AgencyEngine:
    """一个会话里唯一一份 Agency 服务。

    Agency 的代码与 schema 属于 **cold update**：它是运行时逻辑，不是内容
    配置。没有任何构造它的路径读磁盘配置，ContentRegistry 也没有任何字段能
    碰到它或它的日志 —— P7 的重载换掉的是配置快照，动不了一个已经存在的
    引擎、日志或世界。
    """

    def __init__(
        self,
        state: SessionState,
        policy: Optional[AgencyPolicy] = None,
        budget: Optional[AgencyBudget] = None,
    ) -> None:
        if not isinstance(state, SessionState):
            raise AgencyEngineError("Agency 引擎必须绑定在一个 SessionState 上")
        if not isinstance(state.world_state, WorldState):
            raise AgencyEngineError("Agency 引擎绑定的会话还没有权威 WorldState")

        policy = policy if policy is not None else default_policy()
        if not callable(getattr(policy, "decide", None)):
            raise AgencyEngineError("策略必须提供 decide()")
        budget = budget if budget is not None else AgencyBudget()
        if not isinstance(budget, AgencyBudget):
            raise AgencyEngineError("budget 必须是 AgencyBudget")

        self._state = state
        self._policy = policy
        self._budget = budget
        # 绑定只允许一次。两个引擎会给同一条到期两个互相看不见的结论，
        # 而其中一个的审计记录会说"我处理过了"。
        try:
            state.attach_agency(self)
        except RuntimeError as e:
            raise AgencyEngineError(str(e)) from e

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
    def policy(self) -> AgencyPolicy:
        return self._policy

    @property
    def budget(self) -> AgencyBudget:
        return self._budget

    @property
    def log(self) -> AgencyLog:
        """本会话的 Agency 审计日志。权威副本在 SessionState 上，这里不缓存
        引用 —— 存档恢复会就地换掉它。"""
        return self._state.agency

    def pending_due(self) -> Tuple[ActivationDue, ...]:
        """还等着被评估的到期记录，按触发顺序。"""
        return tuple(
            record
            for record in self._state.activation_outbox.pending()
            if not self._state.agency.has(record.due_id)
        )

    def context_for(self, due: ActivationDue) -> AgencyContext:
        """为一条到期资格构造角色作用域上下文。纯读取。

        观察在这里显式收窄成"这个角色自己的"那些。上下文构造器拿不到会话，
        所以这一行就是全部的取数逻辑，可以一眼审查完。
        """
        character_id = self._require_character(due)
        return build_agency_context(
            self.world,
            character_id,
            due,
            self._state.observations.for_character(character_id),
            max_legal_actions=self._budget.max_legal_actions,
            max_observations=self._budget.max_observations,
        )

    # ── 提案（纯） ──────────────────────────────────────────────────────
    def propose(self, due: ActivationDue) -> ProposalPlan:
        """判断这条到期资格该不该变成一个动作。**不改变任何状态。**

        任何一步得出"不行"，都在这里就变成一个 verdict，而不是抛异常：
        "评估过，结论是不动"是正常结果，值得被记下来。只有"这次评估的前提
        不成立"（到期记录不是本会话的、已经处理过了）才抛 AgencyEngineError。
        """
        self._require_handoff(due)
        character_id = self._require_character(due)
        proposed_at = self.clock

        def plan(verdict, proposal=None, detail=None, rationale="") -> ProposalPlan:
            return ProposalPlan(
                due=due,
                character_id=character_id,
                policy=getattr(self._policy, "name", ""),
                proposed_at=proposed_at,
                verdict=verdict,
                proposal=proposal,
                detail=dict(detail or {}),
                rationale=rationale,
            )

        if character_id not in self.world.known_characters():
            # 排期时角色还在，现在不在了。调度器刻意不替下游做这个判断
            # （它宁可交出一条需要复核的记录），复核就在这里。
            return plan(
                AgencyOutcome.REJECTED_ILLEGAL,
                detail={"reason": "unknown_character", "character_id": character_id},
            )

        committed = self._state.agency.committed_actions()
        if committed >= self._budget.max_committed_actions_per_session:
            return plan(
                AgencyOutcome.REJECTED_BUDGET,
                detail={
                    "reason": "max_committed_actions_per_session",
                    "committed": committed,
                    "limit": self._budget.max_committed_actions_per_session,
                },
            )

        context = self.context_for(due)
        try:
            decision = self._policy.decide(context)
        except AgencyPolicyError as e:
            return plan(
                AgencyOutcome.REJECTED_POLICY_ERROR,
                detail={"reason": "policy_error", "error": str(e)},
            )
        except Exception as e:  # 策略实现的 bug 不该炸穿整个运行时
            return plan(
                AgencyOutcome.REJECTED_POLICY_ERROR,
                detail={
                    "reason": "policy_raised",
                    "error": f"{type(e).__name__}: {e}",
                },
            )

        if not isinstance(decision, PolicyDecision):
            return plan(
                AgencyOutcome.REJECTED_POLICY_ERROR,
                detail={
                    "reason": "policy_returned_wrong_type",
                    "type": type(decision).__name__,
                },
            )

        if decision.abstains:
            # 显式不动：合法结果，不是错误，也不是编造出来的一句台词。
            return plan(AgencyOutcome.ABSTAINED, rationale=decision.rationale)

        if len(decision.proposals) > self._budget.max_proposals_per_activation:
            return plan(
                AgencyOutcome.REJECTED_BUDGET,
                detail={
                    "reason": "max_proposals_per_activation",
                    "proposed": len(decision.proposals),
                    "limit": self._budget.max_proposals_per_activation,
                },
                rationale=decision.rationale,
            )

        proposal = decision.proposals[0]
        refusal = self._refuse_proposal(context, proposal)
        if refusal is not None:
            return plan(
                AgencyOutcome.REJECTED_ILLEGAL,
                detail=refusal,
                rationale=decision.rationale,
            )
        return plan(
            AgencyOutcome.ACTED, proposal=proposal, rationale=decision.rationale
        )

    def _refuse_proposal(
        self, context: AgencyContext, proposal: ActionProposal
    ) -> Optional[Dict]:
        """提案期的合法性检查；通过返回 None，否则返回拒绝细节。"""
        if proposal.character_id != context.character_id:
            return {
                "reason": "actor_mismatch",
                "proposed_for": proposal.character_id,
                "expected": context.character_id,
            }
        if proposal.proposal_id in self._state.agency.proposal_ids():
            # 提案身份撞车。细节写进 detail，提案对象本身不进记录 ——
            # 否则这条拒绝记录会自己撞上日志的提案唯一性约束。
            return {
                "reason": "duplicate_proposal_id",
                "proposal_id": proposal.proposal_id,
            }
        if (
            proposal.definition.requires_authored_text
            and not self._budget.allow_authored_text
        ):
            # 台词属于角色生成层，而生成 → Router 判分 → 审计落盘那条链在
            # Agency 这一侧还没接上。默认拒绝，而不是"先让它说，回头再补审计"。
            return {
                "reason": "authored_text_not_permitted",
                "action_id": proposal.action_id.value,
            }
        if not context.has_legal(proposal.action_id, proposal.target_id):
            return {
                "reason": "illegal_action",
                "action_id": proposal.action_id.value,
                "target_id": proposal.target_id,
            }
        failed = failed_preconditions(
            self.world, proposal.character_id, proposal.action_id, proposal.target_id
        )
        if failed:
            # 合法枚举与前置条件求值同源，正常情况下走不到这里；走到了说明
            # 有人手动构造了一条"枚举里有但条件不过"的提案。
            return {
                "reason": "failed_preconditions",
                "action_id": proposal.action_id.value,
                "target_id": proposal.target_id,
                "failed": [precondition.value for precondition in failed],
            }
        return None

    # ── 提交（事务） ────────────────────────────────────────────────────
    def commit(self, plan: ProposalPlan) -> AgencyRecord:
        """把一个判断落地：重判前置条件、写审计、确认交接、必要时提交事件。

        全部落在 SessionState.atomic_commit() 里：世界、事件历史、观察、曝光
        判定、排期队列、到期投递箱、Agency 日志同生共死。中途任何一步失败，
        到期记录仍然是待处理的，可以重来 —— 这正是重试所需要的状态。
        """
        if not isinstance(plan, ProposalPlan):
            raise AgencyEngineError("只能提交 propose() 产出的计划")
        due = plan.due
        self._require_handoff(due)
        self._require_plan_integrity(plan)

        verdict = plan.verdict
        detail = dict(plan.detail)

        if verdict.acted:
            refusal = self._commit_refusal(plan)
            if refusal is not None:
                verdict, detail = refusal

        if plan.rationale:
            # 策略自己给的说法进审计。它是系统侧记录，不是世界真相，也永远
            # 不会进任何角色的观察 —— 但"为什么没动"如果连策略的说法都不留，
            # 事后就只剩一个结果码。
            detail.setdefault("rationale", plan.rationale)

        decided_at = self.clock
        state = self._state
        with state.atomic_commit():
            event_id = None
            if verdict.acted:
                event = event_for_proposal(
                    self.world,
                    state.events,
                    self.session_id,
                    due,
                    plan.proposal,
                    policy=plan.policy,
                )
                commit_session_event(state, event)
                event_id = event.event_id

            record = AgencyRecord(
                due_id=due.due_id,
                character_id=plan.character_id,
                decided_at=decided_at,
                outcome=verdict,
                policy=plan.policy,
                proposal=plan.proposal if verdict.acted else None,
                event_id=event_id,
                detail=detail,
            )
            try:
                state.agency._append(record)
            except AgencyError as e:
                raise AgencyEngineError(str(e)) from e

            # 确认放在最后：审计先落地，交接才算完成。这一步失败整笔回滚，
            # 于是到期记录留在待处理，不会出现"确认了但没记录"。
            try:
                state.activation_outbox._acknowledge(due.due_id)
            except ActivationOutboxError as e:
                raise AgencyEngineError(str(e)) from e
        return record

    def _require_plan_integrity(self, plan: ProposalPlan) -> None:
        """计划自身必须自洽。

        propose() 产出的计划天然满足这几条；手工拼一个计划直接交给 commit()
        则不一定。这些检查放在事务**之前**，是因为它们说明的是"这个计划本身
        就不成立"，而不是"世界变了"——后者才配得上一条 REJECTED_* 审计记录。
        没有这道闸的话，一个角色对不上的计划会一路走到 AgencyRecord 的构造
        才炸，那时事件已经提交过一次又被回滚，错误信息也指不到真正的原因。
        """
        if plan.character_id != plan.due.character_id:
            raise AgencyEngineError(
                f"计划的角色 '{plan.character_id}' 与到期记录的角色 "
                f"'{plan.due.character_id}' 不一致"
            )
        if plan.verdict.acted and plan.proposal is None:
            raise AgencyEngineError("acted 计划必须带上提案")
        if plan.proposal is not None and plan.proposal.character_id != plan.character_id:
            raise AgencyEngineError(
                f"提案角色 '{plan.proposal.character_id}' 与计划角色 "
                f"'{plan.character_id}' 不一致"
            )

    def _commit_refusal(self, plan: ProposalPlan) -> Optional[Tuple[AgencyOutcome, Dict]]:
        """提交那一刻再判一次：这个判断还成立吗？

        每一条都必须在这里重判，不能只信 propose() 的结论 —— propose() 是纯的，
        所以调用方完全可以先把一批计划都提出来，再一条条提交。中间世界变了
        （时钟、地点、频道），或者别的计划已经先落地了（吃掉了会话预算、占掉了
        提案身份），这几种情况在提案期都还看不见。
        """
        committed = self._state.agency.committed_actions()
        if committed >= self._budget.max_committed_actions_per_session:
            # 预算在提案期也查过一次，但那一次看到的是"当时已提交了几个"。
            # 只在提案期查，等于先 propose 一批再逐条 commit 就能突破上限。
            return AgencyOutcome.REJECTED_BUDGET, {
                "reason": "max_committed_actions_per_session",
                "committed": committed,
                "limit": self._budget.max_committed_actions_per_session,
            }
        if self.clock != plan.proposed_at:
            # 时钟走了。一条到期资格问的是"**那一刻**要不要动"，用一个已经
            # 过去的判断去改变现在的世界，等于让角色在没机会重新考虑的情况下
            # 执行一个旧决定。
            return AgencyOutcome.REJECTED_STALE, {
                "reason": "clock_moved",
                "proposed_at": plan.proposed_at.isoformat(),
                "clock": self.clock.isoformat(),
            }
        proposal = plan.proposal
        if (
            proposal.definition.requires_authored_text
            and not self._budget.allow_authored_text
        ):
            # 提交期也拦一次：手工拼出来的 ACTED 计划根本没经过 propose()，
            # 只在那边设闸等于没设。
            return AgencyOutcome.REJECTED_BUDGET, {
                "reason": "authored_text_not_permitted",
                "action_id": proposal.action_id.value,
            }
        if proposal.proposal_id in self._state.agency.proposal_ids():
            # 另一条计划抢先用掉了这个提案身份。事件 ID 由提案 ID 推导，
            # 硬走下去会撞上世界历史的重复 ID，整笔回滚，到期记录卡住 ——
            # 那不是"没动"，那是"动不了却说不清为什么"。
            return AgencyOutcome.REJECTED_STALE, {
                "reason": "duplicate_proposal_id",
                "proposal_id": proposal.proposal_id,
            }
        failed = failed_preconditions(
            self.world, proposal.character_id, proposal.action_id, proposal.target_id
        )
        if failed:
            return AgencyOutcome.REJECTED_STALE, {
                "reason": "failed_preconditions",
                "action_id": proposal.action_id.value,
                "target_id": proposal.target_id,
                "failed": [precondition.value for precondition in failed],
            }
        return None

    # ── 一步到位 ────────────────────────────────────────────────────────
    def evaluate(self, due: ActivationDue) -> AgencyRecord:
        """propose() + commit()。日常路径走这个。

        两步仍然分开暴露，因为"提案不是世界真相"这条边界必须是可调用的，
        不只是可描述的：调用方能拿到一个判断、检查它、然后决定提不提交。
        """
        return self.commit(self.propose(due))

    def evaluate_pending(self) -> Tuple[AgencyRecord, ...]:
        """把投递箱里还没评估的到期资格按触发顺序全部评估掉。

        这是**自主路径**的驱动入口。研究会话的确定性 round robin 不调用它，
        也不需要它：那条路里时钟不动，什么都不会到期。
        """
        records = []
        for due in self.pending_due():
            records.append(self.evaluate(due))
        return tuple(records)

    # ── 交接校验 ────────────────────────────────────────────────────────
    def _require_handoff(self, due) -> None:
        """这条到期资格必须是本会话产出的、还没被交接过的那一条。

        四道检查各拦一种真实的错法：伪造的到期记录、别的会话的记录、被改过
        字段的记录、已经处理过的记录。前三种在 P8 之前都会安静地"成功"。
        """
        if not isinstance(due, ActivationDue):
            raise AgencyEngineError("只能评估 ActivationDue")
        outbox = self._state.activation_outbox
        if not outbox.has(due.due_id):
            raise AgencyEngineError(
                f"到期记录 '{due.due_id}' 不在本会话的投递箱里"
            )
        if outbox.get(due.due_id) != due:
            raise AgencyEngineError(
                f"到期记录 '{due.due_id}' 与投递箱里那条不一致"
            )
        if outbox.is_acknowledged(due.due_id):
            raise AgencyEngineError(f"到期记录 '{due.due_id}' 已经被确认过")
        if self._state.agency.has(due.due_id):
            raise AgencyEngineError(f"到期记录 '{due.due_id}' 已经被评估过")

    @staticmethod
    def _require_character(due: ActivationDue) -> str:
        character_id = due.character_id
        if not character_id:
            # 现在只有 character.activation 一种类型，它构造时就要求角色 ID。
            # 真出现没有角色的到期记录，那是交接错了，不是"这个角色不动"。
            raise AgencyEngineError(
                f"到期记录 '{due.due_id}' 没有角色，Agency 无从判断"
            )
        return character_id

    # ── 调试投影 ────────────────────────────────────────────────────────
    def debug_projection(self) -> Dict:
        """只读的 Agency 状态投影（JSON 安全），供测试和调试 UI 读。

        跟曝光的解释通道同一条规矩：这些是系统视角的数据，不进角色上下文。
        """
        log = self.log
        return {
            "session_id": self.session_id,
            "clock": self.clock.isoformat(),
            "policy": getattr(self._policy, "name", ""),
            "budget": self._budget.to_dict(),
            "records": len(log),
            "committed_actions": log.committed_actions(),
            "pending_due_ids": [record.due_id for record in self.pending_due()],
            "outcomes": {
                outcome.value: len(log.for_outcome(outcome))
                for outcome in AgencyOutcome
            },
        }


__all__ = ["AgencyEngine", "AgencyEngineError", "ProposalPlan"]
