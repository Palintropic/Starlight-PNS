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
#   1. **提案不是世界真相。** propose() 不改权威状态：它建上下文、过前置门、
#      问策略、判合法性，世界、日志、存档一个字节都不改。它唯一会写的是引擎
#      进程内的签发表（前置门收尾的 QUIET 计划登记在那里，见 PLAN-1 §9 D2），
#      那不是世界状态，也不进存档。只有 commit() 里被接受的提案才经由 P5 的
#      提交边界变成事件。这条分离不是为了好看 —— 它让"模型建议了什么"和"世界发生了
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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Dict, Mapping, Optional, Tuple

from pns.models.action import ActionId, ActionProposal
from pns.models.activation import ActivationDue, ActivationKind, ScheduledActivation
from pns.models.authored import GenerationAudit
from pns.models.agency import (
    AgencyBudget,
    AgencyError,
    AgencyLog,
    AgencyOutcome,
    AgencyRecord,
)
from pns.models.activation_outbox import ActivationOutboxError
from pns.models.cognition import (
    REPLY_LAPSED,
    CognitionCause,
    consumes_allowance,
    unavailable_causes,
    wall_now,
)
from pns.models.event import EventType
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
from pns.runtime.agency.planning import (
    PlanProposal,
    build_planning_context,
    plan_activation,
)
from pns.runtime.agency.preconditions import failed_preconditions
from pns.runtime.event_commit import commit_session_event
from pns.runtime.scheduler import REPLY_ACTIVATION_PREFIX


# 回话机会的来源必须是一句话，而且回话只能回到那句话所在的媒介里。
# 同一个媒介里，这段时间内**由回话说出的**句子达到 AgencyBudget.reply_burst_lines
# 条之后，回话就停下，对话退回固定节拍。窗口按世界历史算，不按因果链：固定节拍
# 在对话中途开口时会开一条新链，按链计数的上限因此永远到不了。固定节拍自己说的
# 话不计入：否则人一多，节拍本身就能把名额占满，回话再也排不上。
REPLY_BURST_WINDOW = timedelta(minutes=30)


# 角色已经不在世界里时的收尾理由。
UNKNOWN_CHARACTER = "unknown_character"


def free_closure_kind(plan) -> Optional[str]:
    """这份计划的形状是不是一条"没问策略"的免费收尾；是就返回种类，否则 None。

    三种：前置门收尾（quiet）、到期时已失效的回话（reply_lapsed）、从没建出
    上下文的角色已不在世界里（unknown_character，策略名为空）。形状只用来认出
    "这份计划声称自己没问过策略"；声称成不成立，由引擎的签发表与事务内重判
    决定（PLAN-1 实现复核 R3）。
    """
    verdict = plan.verdict
    if verdict is AgencyOutcome.QUIET:
        return "quiet"
    reason = plan.detail.get("reason") if isinstance(plan.detail, Mapping) else None
    if verdict is AgencyOutcome.REJECTED_STALE and reason == REPLY_LAPSED:
        return REPLY_LAPSED
    if (
        verdict is AgencyOutcome.REJECTED_ILLEGAL
        and reason == UNKNOWN_CHARACTER
        and plan.policy == ""
    ):
        return UNKNOWN_CHARACTER
    return None


def _cursor_of(record: AgencyRecord) -> int:
    """记录写下的观察游标。旧记录没有 → 0（全部观察都算新，只会多放行一次）。"""
    cursor = record.detail.get("observation_cursor", 0)
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        return 0
    return cursor


def _reply_medium(event) -> Optional[Tuple[str, str]]:
    """一句话发生在哪个媒介：("channel", 频道) 或 ("location", 地点)。不是话返回 None。"""
    if event.type is EventType.MESSAGE_SENT and event.channel_id:
        return ("channel", event.channel_id)
    if event.type is EventType.DIALOGUE_SPOKEN and event.location_id:
        return ("location", event.location_id)
    return None


def _reply_action(medium: Tuple[str, str]) -> Tuple[ActionId, Optional[str]]:
    kind, anchor = medium
    if kind == "channel":
        return ActionId.SEND_CHANNEL_MESSAGE, anchor
    return ActionId.SPEAK_HERE, None


def reply_activation_id(character_id: str, source_event_id: str) -> str:
    """一次回话机会的激活 ID。它就是回话身份：只有 _offer_replies 用这个格式排。"""
    return f"{REPLY_ACTIVATION_PREFIX}{character_id}:{source_event_id}"


def _reply_source(due: ActivationDue) -> Optional[str]:
    """这条到期是不是一次回话机会；是就返回它回的那条事件 ID。

    payload 里有 reply_to 不算数：任何排期都能带一个。回话身份由激活 ID 与
    reply_to 互相印证 —— 激活 ID 必须恰好是 _offer_replies 为这个人、这条
    来源排出来的那个。
    """
    source = due.payload.get("reply_to")
    if not isinstance(source, str) or not due.character_id:
        return None
    if due.activation_id != reply_activation_id(due.character_id, source):
        return None
    return source


def _is_reply_line(event) -> bool:
    """这句话是不是由一次回话机会说出来的（看它自己的 provenance）。"""
    provenance = event.provenance or {}
    activation_id = provenance.get("activation_id")
    return (
        provenance.get("kind") == "agency"
        and isinstance(activation_id, str)
        and activation_id.startswith(REPLY_ACTIVATION_PREFIX)
    )


def _still_in(world: WorldState, character_id: str, medium: Tuple[str, str]) -> bool:
    kind, anchor = medium
    if kind == "channel":
        return anchor in world.channels_for(character_id)
    return world.location_of(character_id) == anchor


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
    # 这条提案里那句话的 Router 判分凭据。propose() 永远交回 None ——
    # 判分发生在提案之后，而提案是纯的。附上它的只能是那条走完了
    # 生成 → 判分 的编排路径（P11 的协调器）。
    audit: Optional[GenerationAudit] = None
    # 前置门的结论（PLAN-1）。它带着这次认知读到的观察日志游标，提交时写进
    # 记录，下一次前置门从那里往后数新观察。
    planning: Optional[PlanProposal] = None

    @property
    def would_act(self) -> bool:
        return self.verdict.acted

    @property
    def requires_audit(self) -> bool:
        """这个计划要落地的话，必须带一份被接受的判分凭据吗。

        从动作声明推导，不另存一个布尔字段：两处说法迟早会不一致，而不一致
        的那一次就是一句没判过分的台词进了世界历史。
        """
        return (
            self.proposal is not None
            and self.proposal.definition.requires_authored_text
        )

    def with_audit(self, audit: Optional[GenerationAudit]) -> "ProposalPlan":
        """带上判分凭据，返回**新的**计划。原计划一个字节都不变。

        刻意不是就地赋值：计划是不可变值对象，一个能被改写的计划意味着
        "我判的是哪一句"可以在判分之后被换掉。
        """
        return replace(self, audit=audit)

    def refused(self, verdict: AgencyOutcome, **detail) -> "ProposalPlan":
        """把这个计划变成一条被拒的计划，理由并进 detail。

        提案对象留着不动：commit() 只会给 acted 记录写提案，被拒记录的细节
        全在 detail 里（见 AgencyRecord 的字段一致性约束）。
        """
        merged = dict(self.detail)
        merged.update(detail)
        return replace(self, verdict=AgencyOutcome(verdict), detail=merged)

    def annotated(self, **detail) -> "ProposalPlan":
        """只往 detail 里补几条说明，不改结论。"""
        merged = dict(self.detail)
        merged.update(detail)
        return replace(self, detail=merged)

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
            "requires_audit": self.requires_audit,
            "audit": self.audit.to_dict() if self.audit is not None else None,
            "planning": (
                {
                    **self.planning.provenance(),
                    "observation_cursor": self.planning.observation_cursor,
                }
                if self.planning is not None
                else None
            ),
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
        # 引擎签发出去的免费收尾计划（QUIET、失效回话、角色已不在），按 due_id
        # 存**对象本身**。commit() 用 `is` 比对：手拼的、replace() 出来的、改过
        # detail 的副本都不是签发的那一个，哪怕字段全对、提交那一刻条件也恰好
        # 成立（PLAN-1 §9 D2、实现复核 R3）。它只活在进程里：进程没了，计划对象
        # 也没了，重新 propose() 就重新签发。
        self._issued: Dict[str, ProposalPlan] = {}
        # 同一条规矩用在走到策略那一步的前置门结论上：记录里的观察游标和规划
        # 理由，只认 propose() 发出去的那个 PlanProposal 对象。手拼一个更大的
        # 游标，等于让记录声称这次认知读过它没读过的观察。免费收尾的结论不在
        # 这里，它们随整份计划在签发表里核对。
        self._planned: Dict[str, PlanProposal] = {}
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

    # ── 前置门 ──────────────────────────────────────────────────────────
    def _plan(self, due: ActivationDue, character_id: str, context: AgencyContext) -> PlanProposal:
        """这条到期此刻过不过前置门。纯读取。

        只读这个角色自己的观察（游标之后那一段）和自己的 Agency 记录，世界的
        其它部分全部经由 AgencyContext。
        """
        observations = self._state.observations
        cursor = len(observations)
        last = self._last_charged(character_id)
        since = _cursor_of(last) if last is not None else 0
        if since > cursor:
            # 提交时核对过、存档加载也核对过；走到这里说明有人绕过了两者。不截断：
            # 截断会让游标一直追着日志长度，之后的新观察全被吞掉。
            raise AgencyEngineError(
                f"记录 '{last.due_id}' 的观察游标 {since} 超过了观察日志长度 {cursor}"
            )
        planning_context = build_planning_context(
            context,
            reply=_reply_source(due) is not None,
            observation_cursor=cursor,
            observations_since_charged=observations.for_character_since(
                character_id, since
            ),
            last_charged_at=last.decided_at if last is not None else None,
        )
        minutes = self._budget.quiet_cooldown_minutes
        return plan_activation(
            planning_context,
            timedelta(minutes=minutes) if minutes is not None else None,
        )

    def _last_charged(self, character_id: str) -> Optional[AgencyRecord]:
        """这个角色最后一条计费认知记录。QUIET、不可用、回话失效都不算。"""
        for record in reversed(self._state.agency.for_character(character_id)):
            if consumes_allowance(record):
                return record
        return None

    def _closure(self, due: ActivationDue, character_id: str) -> Optional[ProposalPlan]:
        """此刻这条到期能不能不问策略就收尾；能就交回一份**未签发**的计划。纯读取。

        三种收尾，按次序：

          角色已不在世界里   只在这条到期从没走到策略那一步时才是免费收尾；
                             走到过（之前 propose() 发出过结论、问过策略）就交给
                             propose() 带着那份结论计费结案，游标不丢。
          前置门收尾         asleep / cooldown。
          回话已失效         来源、媒介、能不能开口、名额，见 _reply_gate()。
        """
        proposed_at = self.clock
        if character_id not in self.world.known_characters():
            if due.due_id in self._planned:
                return None
            return ProposalPlan(
                due=due,
                character_id=character_id,
                policy="",
                proposed_at=proposed_at,
                verdict=AgencyOutcome.REJECTED_ILLEGAL,
                detail={"reason": UNKNOWN_CHARACTER, "character_id": character_id},
            )
        planning = self._plan(due, character_id, self.context_for(due))
        if planning.quiet:
            return ProposalPlan(
                due=due,
                character_id=character_id,
                policy="",
                proposed_at=proposed_at,
                verdict=AgencyOutcome.QUIET,
                detail={"reason": planning.reason.value},
                planning=planning,
            )
        if _reply_source(due) is not None:
            _, lapse = self._reply_gate(due, character_id)
            if lapse is not None:
                return ProposalPlan(
                    due=due,
                    character_id=character_id,
                    policy="",
                    proposed_at=proposed_at,
                    verdict=AgencyOutcome.REJECTED_STALE,
                    detail=lapse,
                    planning=planning,
                )
        return None

    def _issue(self, plan: ProposalPlan) -> ProposalPlan:
        # 免费收尾只进签发表，不进 _planned：_planned 只记走到了策略那一步的
        # 结论，"这条到期问没问过策略"靠它回答（见 _closure 的未知角色分支）。
        self._issued[plan.due.due_id] = plan
        return plan

    def closure_plan(self, due: ActivationDue) -> Optional[ProposalPlan]:
        """只看免费收尾：此刻能不问策略就收尾，就签发并交回那份计划，否则 None。

        协调器在提交闸门里调它，判定与提交之间插不进别的写入；终局兜底也用它
        重签，这样提交故障之后记下的仍然是"没问策略"，而不是一条计费的失败。
        """
        self._require_handoff(due)
        closure = self._closure(due, self._require_character(due))
        return self._issue(closure) if closure is not None else None

    def is_issued(self, plan: ProposalPlan) -> bool:
        """这份计划是不是本引擎此刻为它那条到期签发着的免费收尾。"""
        return self._issued.get(plan.due.due_id) is plan

    # ── 提案 ────────────────────────────────────────────────────────────
    def propose(self, due: ActivationDue) -> ProposalPlan:
        """判断这条到期资格该不该变成一个动作。**不改变任何权威状态。**

        任何一步得出"不行"，都在这里就变成一个 verdict，而不是抛异常：
        "评估过，结论是不动"是正常结果，值得被记下来。只有"这次评估的前提
        不成立"（到期记录不是本会话的、已经处理过了）才抛 AgencyEngineError。

        免费收尾（角色已不在、前置门收尾、回话已失效）在策略之前：命中就交回
        一份签发过的计划，策略（以及策略里的生成）一次都不调。propose() 唯一
        会写的东西是引擎进程内的签发表，不是世界状态。
        """
        self._require_handoff(due)
        character_id = self._require_character(due)
        proposed_at = self.clock
        planning: Optional[PlanProposal] = None

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
                planning=planning,
            )

        closure = self._closure(due, character_id)
        if closure is not None:
            # 在问策略之前收尾：没有生成、没有判分，也就不花单次额度
            # （见 consumes_allowance）。策略名留空，记录自己说明"没问过策略"。
            return self._issue(closure)
        # 这次不免费收尾：先前给这条到期签发过的（如果有）作废。
        self._issued.pop(due.due_id, None)

        if character_id not in self.world.known_characters():
            # 排期时角色还在，现在不在了。调度器刻意不替下游做这个判断
            # （它宁可交出一条需要复核的记录），复核就在这里。这条到期之前已经
            # 建出过上下文、发过前置门结论（_closure 才没有免费收尾它）：带着那份
            # 结论结案，游标不丢。
            planning = self._planned[due.due_id]
            return plan(
                AgencyOutcome.REJECTED_ILLEGAL,
                detail={"reason": UNKNOWN_CHARACTER, "character_id": character_id},
            )

        context = self.context_for(due)
        planning = self._plan(due, character_id, context)
        self._planned[due.due_id] = planning

        reply_medium = None
        if _reply_source(due) is not None:
            reply_medium, _ = self._reply_gate(due, character_id)

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

        if reply_medium is not None:
            # 回话只能回到来源那句话的媒介里。收窄的是合法枚举本身，所以策略
            # 选不出别的动作，提案期的 has_legal 检查也会拒绝任何别的动作。
            action_id, target_id = _reply_action(reply_medium)
            context = replace(
                context,
                legal_actions=tuple(
                    legal
                    for legal in context.legal_actions
                    if legal.action_id is action_id and legal.target_id == target_id
                ),
            )
        try:
            decision = self._policy.decide(context)
        except AgencyPolicyError as e:
            return plan(
                AgencyOutcome.REJECTED_POLICY_ERROR,
                detail={
                    "reason": "policy_error",
                    "error": str(e),
                    # 策略自述这次失败是不是暂时的。它只是记录，不是权限：
                    # 真要不要重试由上层的重试预算决定。
                    "retryable": bool(getattr(e, "retryable", False)),
                },
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
        # 台词在提案期**不**被拒 —— 判分要判的就是这一句，而这一句得先被
        # 提出来。它在提交期被拦：没有一份被接受、且绑定到这一句的凭据，
        # commit() 一律拒绝（见 _commit_refusal）。于是 evaluate()（提案+提交
        # 一步到位、中间没有判分步骤）对台词的结论和 P9 完全一样。
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

    # ── 认知不可用 ──────────────────────────────────────────────────────
    def _close_unavailable(self, due: ActivationDue) -> AgencyRecord:
        """这条到期资格此刻认知不可用：不问策略、不建上下文，直接收尾。

        **不是公开写入口**，只给协调器的到期收尾路径用。它不自带原因：原因和
        区间序号由 commit() 在事务内从认知时间线算出来。时间线说"此刻可用"时
        它被拒绝 —— 不能凭空制造一条不可用记录。

        世界照常发生了，这一刻的决定没有发生。它走跟其它结论完全相同的提交
        路径（审计记录 + 交接确认同一个事务），不产出事件、观察或记忆。
        """
        self._require_handoff(due)
        character_id = self._require_character(due)
        return self.commit(
            ProposalPlan(
                due=due,
                character_id=character_id,
                policy="",
                proposed_at=self.clock,
                verdict=AgencyOutcome.REJECTED_UNAVAILABLE,
            )
        )

    def unavailable_causes_for(self, due: ActivationDue):
        """此刻若提交这条到期资格，认知不可用的原因（空集表示可用）。

        它是提交前的**省钱预检**：可用性真正的判定在 commit() 的事务里，用的是
        同一个函数。没有认知时间线的会话恒可用。
        """
        timeline = self._state.cognition
        if timeline is None:
            return frozenset()
        return unavailable_causes(timeline.current, due.fired_at)

    # ── 提交（事务） ────────────────────────────────────────────────────
    def commit(self, plan: ProposalPlan) -> AgencyRecord:
        """把一个判断落地：判认知可用性、重判前置条件、写审计、确认交接、必要时提交事件。

        全部落在 SessionState.atomic_commit() 里：世界、事件历史、观察、曝光
        判定、排期队列、到期投递箱、Agency 日志、认知时间线同生共死。中途任何
        一步失败，到期记录仍然是待处理的，可以重来 —— 这正是重试所需要的状态。

        认知可用性在事务内、追加记录之前判定（WORLD-1 设计 §13–§14）。所有结局都
        经过这里，所以没有绕过它的提交口：此刻不可用，无论计划原本是什么，一律
        改写成 REJECTED_UNAVAILABLE，丢弃生成结果，不提交事件。
        """
        if not isinstance(plan, ProposalPlan):
            raise AgencyEngineError("只能提交 propose() 产出的计划")
        self._require_issued(plan)
        due = plan.due
        self._require_handoff(due)
        self._require_plan_integrity(plan)

        decided_at = self.clock
        state = self._state
        with state.atomic_commit():
            verdict, detail, policy = self._judge(plan)
            event_id = None
            if verdict.acted:
                event = event_for_proposal(
                    self.world,
                    state.events,
                    self.session_id,
                    due,
                    plan.proposal,
                    policy=plan.policy,
                    audit=plan.audit,
                )
                commit_session_event(state, event)
                event_id = event.event_id
                self._offer_replies(event)

            record = AgencyRecord(
                due_id=due.due_id,
                character_id=plan.character_id,
                decided_at=decided_at,
                outcome=verdict,
                policy=policy,
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

            # 额度与上限的转换跟触发它的这条记录在**同一个**事务里：两者之间
            # 插不进另一条提交（R4-3）。
            self._settle_cognition(record)
        return record

    def _reply_gate(self, due: ActivationDue, character_id: str):
        """回话机会到期时还成不成立。成立返回 (媒介, None)，否则 (None, 失效细节)。

        来源从世界历史里按 reply_to 取，不信排期 payload 里的任何别的东西。成立
        的条件：来源是一句话；回话的人此刻仍在那句话的媒介里（还在那个频道 /
        还在那个地点）；而且在那里开口的前置条件此刻都满足（比如没睡着）。
        """
        source_id = _reply_source(due)
        events = self._state.events
        if source_id is None or not events.has(source_id):
            return None, {"reason": REPLY_LAPSED, "why": "source_missing"}
        medium = _reply_medium(events.get(source_id))
        if medium is None:
            return None, {"reason": REPLY_LAPSED, "why": "source_not_speech"}
        if not _still_in(self.world, character_id, medium):
            return None, {
                "reason": REPLY_LAPSED,
                "why": "left_the_conversation",
                "medium": list(medium),
            }
        failed = failed_preconditions(self.world, character_id, *_reply_action(medium))
        if failed:
            return None, {
                "reason": REPLY_LAPSED,
                "why": "cannot_answer",
                "failed": [precondition.value for precondition in failed],
            }
        if self._reply_burst_full(medium):
            return None, {
                "reason": REPLY_LAPSED,
                "why": "burst_full",
                "medium": list(medium),
            }
        return medium, None

    def _reply_burst_full(self, medium: Tuple[str, str]) -> bool:
        """这个媒介最近由回话说出的句子是否已经满额。

        排回话、回话到期、回话提交三处都问它：只在排的时候问的话，同一句话
        可以同时给好几个人排上，它们一起到期就把上限冲破了。提交这一问是
        硬的 —— 满额之后没有一句回话能落地。
        """
        lines = sum(
            1
            for earlier in self._state.events.since(self.clock - REPLY_BURST_WINDOW)
            if _reply_medium(earlier) == medium and _is_reply_line(earlier)
        )
        return lines >= self._budget.reply_burst_lines

    def _offer_replies(self, event) -> None:
        """给当时在场的其他人排一次一次性的回话机会（在提交事务之内）。

        只有一句话（频道消息 / 当面说话）才会引来回话；上线、下线、移动不会。
        在场名单取事件自己记下的 participants，不是会话名单；此刻就答不了话
        的人（比如睡着了）不排。这个媒介最近说过的话已经够多时一个都不排，
        对话退回固定节拍。已经有一次不晚于那一刻的排期的人不再加。排期跟触发
        它的事件同生共死，事务回滚它也一起消失。
        """
        delay = self._budget.reply_delay_minutes
        if delay is None or not event.participants:
            return
        medium = _reply_medium(event)
        if medium is None:
            return
        scheduler = self._state.scheduler
        if scheduler is None:
            return
        if self._reply_burst_full(medium):
            return
        action_id, target_id = _reply_action(medium)
        due_at = self.clock + timedelta(minutes=delay)
        for member in event.participants:
            if member == event.actor_id:
                continue
            if failed_preconditions(self.world, member, action_id, target_id):
                continue
            if any(
                pending.character_id == member and pending.due_at <= due_at
                for pending in scheduler.pending()
            ):
                continue
            scheduler._schedule_reply(
                ScheduledActivation(
                    activation_id=reply_activation_id(member, event.event_id),
                    kind=ActivationKind.CHARACTER_ACTIVATION,
                    due_at=due_at,
                    character_id=member,
                    payload={"reply_to": event.event_id},
                )
            )

    def _judge(self, plan: ProposalPlan):
        """在事务内给出这条计划最终的 (结论, 细节, 策略名)。调用方持着事务。"""
        due = plan.due
        timeline = self._state.cognition
        interval = timeline.current if timeline is not None else None

        if interval is not None:
            causes = unavailable_causes(interval, due.fired_at)
            if causes:
                return (
                    AgencyOutcome.REJECTED_UNAVAILABLE,
                    {
                        "reason": "cognition_unavailable",
                        "causes": sorted(cause.value for cause in causes),
                        "interval": interval.index,
                    },
                    "",
                )
        if plan.verdict is AgencyOutcome.REJECTED_UNAVAILABLE:
            raise AgencyEngineError(
                "认知此刻可用（或这个会话不区分认知可用），不能把这条到期记成不可用"
            )
        if free_closure_kind(plan) is not None:
            self._require_still_closed(plan)

        verdict = plan.verdict
        detail = dict(plan.detail)
        if plan.planning is not None:
            cursor = plan.planning.observation_cursor
            if cursor > len(self._state.observations):
                raise AgencyEngineError(
                    f"计划的观察游标 {cursor} 超过了观察日志长度 "
                    f"{len(self._state.observations)}"
                )
            detail["observation_cursor"] = cursor
            detail["planning"] = plan.planning.provenance()
        if verdict.acted:
            refusal = self._commit_refusal(plan)
            if refusal is not None:
                verdict, detail = refusal

        if isinstance(plan.audit, GenerationAudit):
            # 凭据进审计细节，而且是**唯一**一份可以在存档里重新读出来的副本：
            # 存档校验按它重建事件的 provenance，两边对不上就是有人动过其中
            # 一边。被拒的记录也留着它 —— "判过但没通过"和"根本没判过"必须
            # 能分开。
            # 只记真正的凭据。不是凭据的东西已经在 _refuse_audit 里被判成
            # audit_not_bound 了，这里再对它调用 to_dict() 只会把一次干净的
            # 拒绝炸成一个异常。
            detail.setdefault("audit", plan.audit.to_dict())

        if plan.rationale:
            # 策略自己给的说法进审计。它是系统侧记录，不是世界真相，也永远
            # 不会进任何角色的观察 —— 但"为什么没动"如果连策略的说法都不留，
            # 事后就只剩一个结果码。
            detail.setdefault("rationale", plan.rationale)

        if interval is not None:
            # 每条记录都写明它是在哪个认知区间里判定的，存档加载据此复核。
            detail["cognition_interval"] = interval.index
        return verdict, detail, plan.policy

    def _require_issued(self, plan: ProposalPlan) -> None:
        """这份计划的前置门身份是不是本引擎发出去的那一份（PLAN-1 §9 D2）。

        比对放在一切判定之前 —— 尤其在"认知不可用"的改判之前，未签发的
        QUIET 不能被降格成一条免费的不可用记录。

        两张表的生命周期不同：

          QUIET 签发    任何一次提交尝试都取走，成败不论。一个签发对象至多被
                        提交一次；这次没提交成，到期仍待处理，重新 propose()
                        重新签发。
          前置门结论    只记走到策略那一步的，留到这条到期被确认为止。协调器
                        在重试用完时会拿同一份结论再提交一条终局失败记录，它
                        必须还认得出来。

        开了前置门的引擎，普通到期必须带着本引擎发出的那份结论：把 planning
        设成 None 退回旧调用形状，等于让记录不带游标、旧观察下次又算新。没问
        策略的免费收尾（前置门、失效回话、从没建出上下文的未知角色）不走这条，
        它们整份计划在签发表里核对；签过之后改成别的结论一律拒绝。
        """
        due_id = getattr(plan.due, "due_id", None)
        outbox = self._state.activation_outbox
        for stale in [
            key
            for key in self._planned
            if not outbox.has(key) or outbox.is_acknowledged(key)
        ]:
            del self._planned[stale]
        issued = self._issued.pop(due_id, None)
        planned = self._planned.get(due_id)
        if plan.verdict is AgencyOutcome.REJECTED_UNAVAILABLE:
            # 不可用由认知时间线在事务里判，跟前置门无关；时间线说可用时 _judge 拒绝。
            return
        if issued is not None and plan is issued:
            # 整份签发出去的免费收尾：身份已经核过，条件在事务内重判。
            return
        kind = free_closure_kind(plan)
        if kind is not None:
            raise AgencyEngineError(
                f"{kind}：没问策略的免费收尾只能由本引擎签发，这个计划不是签发出去的那一个"
            )
        if issued is not None:
            raise AgencyEngineError(
                "这条到期签发的是免费收尾：不能改成别的结论提交"
            )
        if plan.planning is not planned:
            # 也挡住"签发失败之后，拿留着的收尾结论去配一个计费结论"：免费收尾
            # 的结论从不进 _planned。
            raise AgencyEngineError(
                "计划上的前置门结论不是本引擎为这条到期发出的那一个"
            )
        if planned is None and self._budget.quiet_cooldown_minutes is not None:
            raise AgencyEngineError(
                "开了前置门的引擎只提交经过 propose() 的计划"
            )

    def _require_still_closed(self, plan: ProposalPlan) -> None:
        """签发过的免费收尾在事务内按当前状态再判一次。

        不成立就抛错、不落记录，到期仍待处理：这个计划从没走到策略，把它
        改判成任何一条计费记录都是假记录。
        """
        if self.clock != plan.proposed_at:
            raise AgencyEngineError(
                f"免费收尾判定于 {plan.proposed_at.isoformat()}，"
                f"时钟已到 {self.clock.isoformat()}"
            )
        kind = free_closure_kind(plan)
        current = self._closure(plan.due, plan.character_id)
        current_kind = free_closure_kind(current) if current is not None else None
        if current_kind != kind or (
            kind == "quiet" and current.planning.reason is not plan.planning.reason
        ):
            raise AgencyEngineError(
                f"收尾条件已经变了：签发时是 {kind}"
                f"{'/' + plan.planning.reason.value if kind == 'quiet' else ''}，"
                f"现在是 {current_kind}"
                f"{'/' + current.planning.reason.value if current_kind == 'quiet' else ''}"
            )

    def _settle_cognition(self, record: AgencyRecord) -> None:
        """记录落地之后，额度或世界上限若因此用尽，在同一事务里开启对应区间。"""
        state = self._state
        timeline = state.cognition
        if timeline is None:
            return
        log_length = len(state.agency)
        current = timeline.current
        changed = False
        if (
            consumes_allowance(record)
            and current.run_allowance is not None
            and CognitionCause.RUN_BUDGET_EXHAUSTED not in current.causes
        ):
            used = sum(
                1
                for earlier in state.agency.records()[current.allowance_since_log :]
                if consumes_allowance(earlier)
            )
            if used >= current.run_allowance:
                timeline = timeline.run_budget_exhausted(
                    log_length=log_length, sim=self.clock, wall=wall_now()
                )
                changed = True
        if (
            CognitionCause.WORLD_ACTION_CAP not in timeline.current.causes
            and state.agency.committed_actions()
            >= self._budget.max_committed_actions_per_session
        ):
            timeline = timeline.world_action_cap(
                log_length=log_length, sim=self.clock, wall=wall_now()
            )
            changed = True
        if changed:
            state.set_cognition(timeline)

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
        refusal = self._refuse_audit(plan, proposal)
        if refusal is not None:
            return refusal
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
        if _reply_source(plan.due) is not None:
            # 回话在生成期间可能已经不成立了：当面说话没有目标地点，人换了房间
            # 前置条件照样全过，话就会落在新房间里。所以提交时按来源重判一次。
            # 理由不是 reply_lapsed：到这里模型已经调用过了，这次要花额度。
            medium, lapse = self._reply_gate(plan.due, plan.character_id)
            if lapse is not None:
                return AgencyOutcome.REJECTED_STALE, {**lapse, "reason": "reply_went_stale"}
            if (proposal.action_id, proposal.target_id) != _reply_action(medium):
                return AgencyOutcome.REJECTED_STALE, {
                    "reason": "reply_went_stale",
                    "why": "medium_changed",
                    "medium": list(medium),
                }
        return None

    def _refuse_audit(self, plan: ProposalPlan, proposal: ActionProposal):
        """台词的通行证检查。这是 P11 唯一一处放行台词的判断。

        四种拒法各拦一种真实的绕法：

          没有凭据        手工拼出来的 ACTED 计划、或者 evaluate() 这种中间
                          没有判分步骤的直路。理由码沿用 P9 的
                          `authored_text_not_committable` —— 结论一个字没变。
          凭据对不上      判的是别的句子、别的角色、别的提案。"换一句话再用
                          同一份审计"就是从这里进来的。
          凭据没通过      分数超阈值，或者判分器自己标了需要人工复核。
          凭据过期了      判分发生在别的模拟时刻。一句话像不像本人，依赖它
                          被说出来的那个当下；用一份旧判分给现在的世界背书，
                          跟拿一个旧决定去改变现在的世界是同一种错。

        反过来也拦：不需要台词的动作带着凭据来 —— 那是拿一句判过分的台词
        给一个跟台词无关的动作背书。事件构造那一层还有第三道结构性的拒绝。
        """
        audit = plan.audit
        if not proposal.definition.requires_authored_text:
            if audit is not None:
                return AgencyOutcome.REJECTED_ILLEGAL, {
                    "reason": "audit_without_authored_text",
                    "action_id": proposal.action_id.value,
                }
            return None
        if audit is None:
            return AgencyOutcome.REJECTED_ILLEGAL, {
                "reason": "authored_text_not_committable",
                "action_id": proposal.action_id.value,
            }
        if not isinstance(audit, GenerationAudit) or not audit.binds(proposal):
            return AgencyOutcome.REJECTED_ILLEGAL, {
                "reason": "audit_not_bound",
                "action_id": proposal.action_id.value,
                "proposal_id": proposal.proposal_id,
            }
        if not audit.accepted:
            return AgencyOutcome.REJECTED_ILLEGAL, {
                "reason": "router_rejected",
                "action_id": proposal.action_id.value,
                **audit.refusal(),
            }
        if audit.audited_at != self.clock:
            return AgencyOutcome.REJECTED_STALE, {
                "reason": "audit_stale",
                "audited_at": audit.audited_at.isoformat(),
                "clock": self.clock.isoformat(),
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
