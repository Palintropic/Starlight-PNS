# tests/test_autonomous_runtime.py — P11 自主运行时编排的不变量。
#
# 盯住的东西按"错了会怎样"排：
#   1. 台词只有一条提交路径：生成 → Router 判分 → 审计绑定 → 提交。
#      没有审计、审计对不上、审计判它 OOC，一律不产出事件。
#   2. 交给生成层的上下文是角色作用域的：别人的记忆、曝光拒绝理由、
#      全知事件 payload，一个字都不许渗进去。
#   3. 一条到期资格至多被处理一次，重试不会重复提交。
#   4. 任何一步失败都不留半截世界：事件、观察、曝光判定、审计、记忆
#      要么全在，要么全不在。
#   5. 停止之后迟到的模型结果提交不了。
#   6. 每条到期都有耐久且可查的终局，或者仍然显式待处理。
#   7. 一个会话只能绑一个协调器。
#   8. 研究会话的 /ws/run round robin 一点没变。
#   9. 排期 payload 默认不进模型输入：角色不知道自己有一张排期表。
#  10. 停机与提交许可是线性化的：慢调用不持锁，stop() 返回之后没有提交能落地。
#
# 运行: python -m unittest tests.test_autonomous_runtime -v
import ast
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from pns.models.action import (
    ActionEventMismatch,
    ActionId,
    ActionProposal,
    LegalAction,
    agency_event_fields,
)
from pns.models.activation import ActivationDue, ActivationKind, ScheduledActivation
from pns.models.agency import AgencyOutcome
from pns.models.authored import AuthoredTextError, GenerationAudit
from pns.models.event import Event, EventScope, EventType
from pns.models.session import SessionState, SessionStateError
from pns.models.world_state import WorldState
from pns.runtime.agency.engine import AgencyEngine, AgencyEngineError
from pns.runtime.agency.policy import AbstainPolicy
from pns.runtime.autonomy import context as context_mod
from pns.runtime.autonomy import coordinator as coordinator_mod
from pns.runtime.autonomy.audit import AuditError, RouterAuditor, ScriptedAuditor
from pns.runtime.autonomy.context import (
    MAX_CUE_CHARS,
    ActivationCue,
    GenerationContext,
    GenerationContextError,
)
from pns.runtime.autonomy.coordinator import AutonomousRuntime, AutonomyError
from pns.runtime.autonomy.generation import (
    ABSTAIN_TOKEN,
    MAX_LINE_CHARS,
    AuthoredLinePolicy,
    GenerationError,
    ScriptedLineGenerator,
    parse_line,
)
from pns.runtime.autonomy.outcome import ActivationOutcome, RetryPolicy
from pns.runtime.memory.encoder import MemoryEncoder
from pns.runtime.memory.recall import MemoryRecall
from pns.runtime.scheduler import PersistentScheduler
from pns.runtime import session_runtime as session_runtime_mod
from pns.runtime.reload import SessionSupervisor
from pns.runtime.session_runtime import SessionRuntime
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph

CLOCK = datetime(2026, 8, 21, 23, 50)
AUTONOMY_DIR = Path(coordinator_mod.__file__).resolve().parent


# ── 夹具 ────────────────────────────────────────────────────────────────
def _world(clock=CLOCK, *, join_nightcord=("mizuki", "ena")):
    world = WorldState(
        clock=clock,
        locations=build_default_location_graph(),
        channels=build_default_channel_registry(),
    )
    world.place_character("mizuki", "mizuki_home_room")
    world.place_character("ena", "ena_home_studio")
    for character_id in join_nightcord:
        world.join_channel(character_id, "nightcord")
    return world


def _session(world=None, session_id="s1"):
    world = _world() if world is None else world
    state = SessionState(
        session_id=session_id, scene="gate", characters=["mizuki", "ena"]
    )
    state.attach_world_state(world)
    state.initialize_runtime("开场")
    return state


def _rig(
    *,
    world=None,
    session_id="s1",
    lines=None,
    generator=None,
    auditor=None,
    chooser=None,
    names=None,
    retry=None,
    start=True,
):
    state = _session(world=world, session_id=session_id)
    scheduler = PersistentScheduler(state)
    if generator is None:
        generator = ScriptedLineGenerator(
            lines if lines is not None else {"mizuki": "在的哦", "ena": "……嗯"}
        )
    policy = AuthoredLinePolicy(
        generator, recall=MemoryRecall(state), chooser=chooser, names=names
    )
    runtime = AutonomousRuntime(
        state,
        policy=policy,
        auditor=auditor if auditor is not None else ScriptedAuditor(),
        retry=retry,
    )
    if start:
        runtime.start()
    return state, scheduler, runtime


def _due(
    scheduler, activation_id="wake", *, character_id="mizuki", minutes=10, payload=None
):
    scheduler.schedule(
        ScheduledActivation(
            activation_id=activation_id,
            kind=ActivationKind.CHARACTER_ACTIVATION,
            due_at=scheduler.clock + timedelta(minutes=minutes),
            character_id=character_id,
            payload=payload or {},
        )
    )
    return scheduler.advance_by(minutes).due[0]


def _proposal(character_id="mizuki", text="在的哦", proposal_id="p1"):
    return ActionProposal(
        proposal_id=proposal_id,
        character_id=character_id,
        action_id=ActionId.SEND_CHANNEL_MESSAGE,
        target_id="nightcord",
        payload={"text": text},
    )


def _audit(proposal, *, score=1.0, at=None, threshold=5.0, **kwargs):
    return GenerationAudit(
        proposal_id=proposal.proposal_id,
        character_id=proposal.character_id,
        payload=dict(proposal.payload),
        drift_score=score,
        threshold=threshold,
        audited_at=at if at is not None else CLOCK,
        evaluator_model="scripted",
        **kwargs,
    )


# ── AC1 台词只有一条提交路径 ────────────────────────────────────────────
class AuthoredTextHasExactlyOnePathTests(unittest.TestCase):
    """没有审计的台词进不了世界历史；有审计的必须是**这一句**的审计。"""

    def test_an_unaudited_authored_proposal_still_cannot_commit(self):
        state, scheduler, runtime = _rig()
        engine = state.agency_engine
        due = _due(scheduler)
        plan = engine.propose(due)
        self.assertTrue(plan.requires_audit)
        record = engine.commit(plan)  # 直接提交，跳过判分
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_ILLEGAL)
        self.assertEqual(record.detail["reason"], "authored_text_not_committable")
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())
        self.assertEqual(len(state.observations), 0)
        self.assertEqual(len(state.memories), 0)

    def test_external_event_boundary_cannot_bypass_router_for_dialogue(self):
        state, scheduler, runtime = _rig()
        event = Event(
            event_id="forged-dialogue",
            type=EventType.MESSAGE_SENT,
            occurred_at=state.world_state.clock,
            scope=EventScope.CHANNEL,
            actor_id="mizuki",
            channel_id="nightcord",
            payload={"text": "绕过判分"},
        )
        with self.assertRaises(AutonomyError):
            runtime.commit_external_event(event)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())

    def test_evaluate_the_direct_agency_path_still_refuses_dialogue(self):
        # P9 的那条规矩没被 P11 放松：直接 evaluate() 没有判分步骤，
        # 所以它永远拿不到审计。
        state, scheduler, runtime = _rig()
        record = state.agency_engine.evaluate(_due(scheduler))
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_ILLEGAL)
        self.assertEqual(record.detail["reason"], "authored_text_not_committable")
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())

    def test_an_audit_for_a_different_line_does_not_bind(self):
        state, scheduler, runtime = _rig()
        engine = state.agency_engine
        due = _due(scheduler)
        plan = engine.propose(due)
        forged = replace(
            _audit(plan.proposal, at=state.world_state.clock),
            payload={"text": "我改了这句"},
        )
        record = engine.commit(plan.with_audit(forged))
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_ILLEGAL)
        self.assertEqual(record.detail["reason"], "audit_not_bound")
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())

    def test_the_auditor_gets_actor_scoped_physical_and_online_facts(self):
        state, scheduler, runtime = _rig()
        plan = state.agency_engine.propose(_due(scheduler))
        request = runtime._audit_request(plan)
        facts = "\n".join(request.situation_facts)
        self.assertIn("自己的 location_id：mizuki_home_room", facts)
        self.assertIn("自己加入的 channel_id：nightcord", facts)
        self.assertIn("自己的当前活动：unspecified", facts)
        self.assertIn("同处一地的角色 ID：none", facts)
        self.assertIn("仅与自己同在线频道、并非同处一地的角色 ID：ena", facts)
        # 绘名自己的物理地点属于她的私有世界视角，不应交给瑞希的 Router。
        self.assertNotIn("ena_home_studio", facts)

    def test_the_display_name_cannot_be_swapped_after_the_audit(self):
        # 凭据绑的是整份 payload，不只是那句话。台词一个字不改、把 char_name
        # 换成别人，在所有观察者眼里就是一次冒名 —— 而 Router 判的从来不是
        # 那个名字。
        state, scheduler, runtime = _rig(names={"mizuki": "瑞希"})
        engine = state.agency_engine
        due = _due(scheduler)
        plan = engine.propose(due)
        audit = runtime.auditor.audit(runtime._audit_request(plan))
        spoofed = replace(
            plan,
            proposal=replace(
                plan.proposal,
                payload={"text": plan.proposal.payload["text"], "char_name": "绘名"},
            ),
            audit=audit,
        )
        record = engine.commit(spoofed)
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_ILLEGAL)
        self.assertEqual(record.detail["reason"], "audit_not_bound")
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())

    def test_an_audited_payload_commits_with_its_display_name_intact(self):
        state, scheduler, runtime = _rig(names={"mizuki": "瑞希"})
        result = runtime.process_due(_due(scheduler))
        self.assertIs(result.outcome, ActivationOutcome.ACTED)
        event = state.events.get(result.event_id)
        self.assertEqual(event.payload["char_name"], "瑞希")
        for observation in state.observations.for_event(event.event_id):
            self.assertEqual(observation.render_line(), "瑞希：在的哦")

    def test_an_audit_for_a_different_character_does_not_bind(self):
        state, scheduler, runtime = _rig()
        engine = state.agency_engine
        due = _due(scheduler)
        plan = engine.propose(due)
        forged = replace(
            _audit(plan.proposal, at=state.world_state.clock), character_id="ena"
        )
        record = engine.commit(plan.with_audit(forged))
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_ILLEGAL)
        self.assertEqual(record.detail["reason"], "audit_not_bound")

    def test_an_audit_for_a_different_proposal_does_not_bind(self):
        state, scheduler, runtime = _rig()
        engine = state.agency_engine
        due = _due(scheduler)
        plan = engine.propose(due)
        forged = replace(
            _audit(plan.proposal, at=state.world_state.clock), proposal_id="other"
        )
        record = engine.commit(plan.with_audit(forged))
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_ILLEGAL)
        self.assertEqual(record.detail["reason"], "audit_not_bound")

    def test_an_ooc_verdict_commits_nothing(self):
        state, scheduler, runtime = _rig(auditor=ScriptedAuditor(default_score=8.0))
        result = runtime.process_due(_due(scheduler))
        self.assertIs(result.outcome, ActivationOutcome.REJECTED)
        self.assertIs(result.agency_outcome, AgencyOutcome.REJECTED_ILLEGAL)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())
        self.assertEqual(len(state.observations), 0)
        self.assertEqual(len(state.memories), 0)
        # 但它是耐久的终局：审计日志里记着为什么没动。
        record = state.agency.get(result.due_id)
        self.assertEqual(record.detail["reason"], "router_rejected")
        self.assertEqual(record.detail["audit"]["drift_score"], 8.0)

    def test_a_line_flagged_for_human_review_is_not_accepted(self):
        state, scheduler, runtime = _rig(
            auditor=ScriptedAuditor(default_score=1.0, needs_human_review=True)
        )
        result = runtime.process_due(_due(scheduler))
        self.assertIs(result.outcome, ActivationOutcome.REJECTED)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())

    def test_is_ooc_is_derived_from_the_threshold_not_taken_from_the_judge(self):
        # Router 适配器自己说 is_ooc=False，但分数在阈值之上 —— 不接受。
        def judge(request):
            return {"drift_score": 9.0, "is_ooc": False, "confidence": 1.0}

        state, scheduler, runtime = _rig(
            auditor=RouterAuditor(judge, threshold=5.0, evaluator_model="fake")
        )
        result = runtime.process_due(_due(scheduler))
        self.assertIs(result.outcome, ActivationOutcome.REJECTED)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())

    def test_the_legacy_router_failure_fallback_is_not_a_pass(self):
        # pns.logic.router.judge 调用失败时会返回一份 0 分兜底结果并继续跑 ——
        # 研究路径上那只是记一笔（有人看着屏幕）。自主路径上没人看着，所以
        # 那份兜底必须被拦住，否则"Router 挂了"就等于"Router 说没问题"。
        fallback = {
            "drift_score": 0,
            "confidence": 0.0,
            "drift_type": "error",
            "is_ooc": False,
            "needs_human_review": True,
            "dimensions_complete": False,
        }
        state, scheduler, runtime = _rig(
            auditor=RouterAuditor(lambda request: fallback, evaluator_model="fake")
        )
        result = runtime.process_due(_due(scheduler))
        self.assertIs(result.outcome, ActivationOutcome.REJECTED)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())

    def test_an_incomplete_scorecard_is_not_a_pass(self):
        # 字段缺失 = 不知道。在"不知道"和"接受"之间选接受，正是这一层不允许
        # 的退化。
        state, scheduler, runtime = _rig(
            auditor=RouterAuditor(lambda request: {"drift_score": 0.0})
        )
        result = runtime.process_due(_due(scheduler))
        self.assertIs(result.outcome, ActivationOutcome.REJECTED)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())

    def test_a_judge_without_a_score_did_not_judge_at_all(self):
        state, scheduler, runtime = _rig(
            auditor=RouterAuditor(lambda request: {"confidence": 1.0})
        )
        result = runtime.process_due(_due(scheduler))
        self.assertIs(result.outcome, ActivationOutcome.FAILED_TERMINAL)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())

    def test_a_complete_clean_scorecard_does_pass(self):
        state, scheduler, runtime = _rig(
            auditor=RouterAuditor(
                lambda request: {
                    "drift_score": 1.0,
                    "confidence": 0.9,
                    "dimensions_complete": True,
                    "needs_human_review": False,
                    "evaluator_model": "mimo",
                },
                evaluator_model="fallback",
            )
        )
        result = runtime.process_due(_due(scheduler))
        self.assertIs(result.outcome, ActivationOutcome.ACTED)
        event = state.events.get(result.event_id)
        self.assertEqual(event.provenance["audit"]["evaluator_model"], "mimo")

    def test_a_stale_audit_is_refused(self):
        state, scheduler, runtime = _rig()
        engine = state.agency_engine
        due = _due(scheduler)
        plan = engine.propose(due)
        stale = _audit(
            plan.proposal, at=state.world_state.clock - timedelta(minutes=1)
        )
        record = engine.commit(plan.with_audit(stale))
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_STALE)
        self.assertEqual(record.detail["reason"], "audit_stale")

    def test_an_audit_on_a_non_authored_action_is_refused(self):
        # 审计是台词的通行证，不是万能通行证。给一个不需要台词的动作附上
        # 审计，说明有人在用一条判过分的台词给别的动作背书。
        state, scheduler, runtime = _rig(
            chooser=lambda context: LegalAction(
                action_id=ActionId.MOVE_TO, target_id="mizuki_home"
            )
        )
        engine = state.agency_engine
        due = _due(scheduler)
        plan = engine.propose(due)
        self.assertFalse(plan.requires_audit)
        record = engine.commit(plan.with_audit(_audit(_proposal())))
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_ILLEGAL)
        self.assertEqual(record.detail["reason"], "audit_without_authored_text")

    def test_the_event_builder_refuses_authored_text_without_an_audit(self):
        # 结构性的那道闸：构造 agency 事件的函数只有一个。
        proposal = _proposal()
        due = ActivationDue(
            activation_id="wake",
            kind=ActivationKind.CHARACTER_ACTIVATION,
            due_at=CLOCK,
            fired_at=CLOCK,
            sequence=0,
            character_id="mizuki",
        )
        with self.assertRaises(ActionEventMismatch):
            agency_event_fields(
                "s1", due, proposal, occurred_at=CLOCK,
                location_id=None, channel_id="nightcord",
            )
        # 有一份被接受的审计就能构造出来。
        fields = agency_event_fields(
            "s1", due, proposal, occurred_at=CLOCK,
            location_id=None, channel_id="nightcord",
            participants=("mizuki",), audit=_audit(proposal),
        )
        self.assertEqual(fields["provenance"]["audit"]["drift_score"], 1.0)

    def test_the_event_builder_refuses_an_unaccepted_audit(self):
        proposal = _proposal()
        due = ActivationDue(
            activation_id="wake",
            kind=ActivationKind.CHARACTER_ACTIVATION,
            due_at=CLOCK, fired_at=CLOCK, sequence=0, character_id="mizuki",
        )
        with self.assertRaises(ActionEventMismatch):
            agency_event_fields(
                "s1", due, proposal, occurred_at=CLOCK,
                location_id=None, channel_id="nightcord",
                participants=("mizuki",), audit=_audit(proposal, score=9.0),
            )


class GenerationAuditTypeTests(unittest.TestCase):
    """审计记录本身是一个类型，不是一个字典。"""

    def test_the_verdict_is_derived_not_assigned(self):
        proposal = _proposal()
        self.assertFalse(_audit(proposal, score=4.9).is_ooc)
        self.assertTrue(_audit(proposal, score=5.0).is_ooc)
        self.assertTrue(_audit(proposal, score=4.9, threshold=4.0).is_ooc)

    def test_an_empty_line_cannot_be_audited(self):
        with self.assertRaises(AuthoredTextError):
            GenerationAudit(
                proposal_id="p1", character_id="mizuki", payload={"text": "   "},
                drift_score=1.0, threshold=5.0, audited_at=CLOCK,
            )

    def test_a_score_outside_the_scale_is_refused(self):
        for score in (-1.0, 11.0):
            with self.subTest(score=score):
                with self.assertRaises(AuthoredTextError):
                    GenerationAudit(
                        proposal_id="p1", character_id="mizuki", payload={"text": "hi"},
                        drift_score=score, threshold=5.0, audited_at=CLOCK,
                    )

    def test_an_aware_timestamp_is_refused(self):
        from datetime import timezone

        with self.assertRaises(AuthoredTextError):
            GenerationAudit(
                proposal_id="p1", character_id="mizuki", payload={"text": "hi"},
                drift_score=1.0, threshold=5.0,
                audited_at=CLOCK.replace(tzinfo=timezone.utc),
            )

    def test_it_round_trips(self):
        audit = _audit(_proposal(), confidence=0.75, dimensions={"a": {"score": 1}})
        self.assertEqual(GenerationAudit.from_dict(audit.to_dict()), audit)

    def test_to_dict_hands_out_a_fresh_mutable_structure(self):
        audit = _audit(_proposal(), dimensions={"a": {"score": 1}})
        payload = audit.to_dict()
        payload["dimensions"]["a"]["score"] = 99
        self.assertEqual(audit.dimensions["a"]["score"], 1)


# ── AC2 生成上下文的角色作用域 ──────────────────────────────────────────
class GenerationScopeTests(unittest.TestCase):
    """交给生成层的东西必须只有这个角色知道的部分。"""

    def setUp(self):
        self.captured = []

        def spy(context):
            self.captured.append(context)
            return "在的哦"

        self.state, self.scheduler, self.runtime = _rig(
            lines={"mizuki": spy, "ena": "……嗯"}
        )

    def _mizuki_context(self):
        self.runtime.process_due(_due(self.scheduler, "m1", character_id="mizuki"))
        return self.captured[-1]

    def test_the_context_is_the_activated_character_only(self):
        context = self._mizuki_context()
        self.assertEqual(context.character_id, "mizuki")
        for observation in context.observations:
            self.assertEqual(observation.observer_id, "mizuki")

    def test_another_characters_memory_never_reaches_the_prompt(self):
        # 先让绘名说一句 —— 她的记忆里会留下"我说了…"，瑞希的记忆里留下
        # "绘名说了…"。两条内容不同，绝不能串台。
        self.runtime.process_due(
            _due(self.scheduler, "e1", character_id="ena", minutes=5)
        )
        ena_only = [
            record
            for record in self.state.memories.for_owner("ena")
            if record.content.get("self")
        ]
        self.assertTrue(ena_only, "绘名应当留下了自己的记忆")
        context = self._mizuki_context()
        blob = json.dumps(context.to_dict(), ensure_ascii=False)
        for record in ena_only:
            self.assertNotIn(record.memory_id, blob)
        self.assertNotIn('"owner_id": "ena"', blob)

    def test_an_unobserved_event_never_reaches_the_prompt(self):
        # 绘名退出频道，在自己的画室里说一句：瑞希感知不到。
        self.state.world_state.leave_channel("ena", "nightcord")
        self.runtime.process_due(
            _due(self.scheduler, "e1", character_id="ena", minutes=5)
        )
        context = self._mizuki_context()
        blob = json.dumps(context.to_dict(), ensure_ascii=False)
        self.assertNotIn("……嗯", blob)
        self.assertNotIn("ena_home_studio", blob)

    def test_the_exposure_denial_log_never_reaches_the_prompt(self):
        self.state.world_state.leave_channel("ena", "nightcord")
        self.runtime.process_due(
            _due(self.scheduler, "e1", character_id="ena", minutes=5)
        )
        self.assertTrue(len(self.state.exposures) > 0)
        context = self._mizuki_context()
        blob = json.dumps(context.to_dict(), ensure_ascii=False)
        for reason in ("no_channel_access", "not_exposed", "denied"):
            self.assertNotIn(reason, blob)

    def test_the_exposure_reason_code_never_reaches_the_prompt(self):
        # 拒绝理由长不出观察，所以它天然不在这里；但**通过**的理由码
        # （"我因为在频道里所以听见了"）会跟着 Observation.to_dict() 一起
        # 溜进来。它同样是曝光系统的簿记，不是角色经验。
        self.runtime.process_due(_due(self.scheduler, "e1", character_id="ena"))
        context = self._mizuki_context()
        blob = json.dumps(context.to_dict(), ensure_ascii=False)
        for reason in ("channel_member", "self_action", "same_location", "audible_from"):
            self.assertNotIn(reason, blob)
        self.assertNotIn('"reason"', blob)
        # 但该有的内容一条不少。
        self.assertTrue(context.to_dict()["observations"])
        self.assertIn("……嗯", blob)

    def test_system_provenance_never_reaches_the_prompt(self):
        self.runtime.process_due(_due(self.scheduler, "e1", character_id="ena"))
        context = self._mizuki_context()
        blob = json.dumps(context.to_dict(), ensure_ascii=False)
        for key in ("provenance", "drift_score", "correlation_id", "causation_id"):
            self.assertNotIn(key, blob)

    def test_a_recent_observation_is_not_repeated_as_a_memory(self):
        self.runtime.process_due(_due(self.scheduler, "e1", character_id="ena"))
        context = self._mizuki_context()
        self.assertTrue(context.observations)
        self.assertTrue(self.state.memories.for_owner("mizuki"))
        self.assertEqual(context.recalled, ())


class ActivationPayloadIsNotCharacterVisibleTests(unittest.TestCase):
    """排期 payload 是调度侧与内容侧的簿记，默认一个字都不进模型输入。

    只把 to_dict() 删干净是挡不住的：生成器手上还有那个对象，一句
    `context.activation.payload` 就全读到了。所以交给生成层的必须是一个
    **投影类型**，而不是那条到期记录本身。
    """

    SECRET = "调度内部标记-不该被角色看见"

    def _capture(self, payload):
        captured = []
        state, scheduler, runtime = _rig(
            lines={"mizuki": lambda context: (captured.append(context), "在的哦")[1]}
        )
        runtime.process_due(_due(scheduler, payload=payload))
        self.assertTrue(captured, "生成器应当被调用过")
        return captured[-1]

    def test_the_context_carries_a_projection_not_the_due_record(self):
        context = self._capture({"secret": self.SECRET})
        self.assertIsInstance(context.activation, ActivationCue)
        self.assertNotIsInstance(context.activation, ActivationDue)
        # 对象上根本没有 payload 这个属性可读。
        self.assertIsNone(getattr(context.activation, "payload", None))

    def test_the_scheduler_payload_reaches_the_generator_nowhere(self):
        context = self._capture({"secret": self.SECRET, "route": "morning_routine"})
        # 三条通道一起堵：JSON 投影、dataclass 的 repr、以及对象属性遍历。
        self.assertNotIn(self.SECRET, json.dumps(context.to_dict(), ensure_ascii=False))
        self.assertNotIn(self.SECRET, repr(context))
        self.assertNotIn("morning_routine", repr(context))
        for name in dir(context.activation):
            if name.startswith("__"):
                continue
            self.assertNotIn(
                self.SECRET, repr(getattr(context.activation, name, None)), name
            )

    def test_the_scheduler_bookkeeping_is_not_visible_either(self):
        # 角色不知道自己有一张排期表：没有 due_id、没有队列登记号、
        # 没有"跨过了几次"、没有"下一次什么时候"。
        context = self._capture({"secret": self.SECRET})
        projection = context.activation.to_dict()
        self.assertEqual(sorted(projection), ["at", "cue", "kind"])
        blob = json.dumps(context.to_dict(), ensure_ascii=False)
        for leaked in ("due_id", "activation_id", "sequence", "missed_occurrences",
                       "next_due_at", "due_at"):
            self.assertNotIn(leaked, blob)

    def test_only_an_explicitly_declared_cue_comes_through(self):
        context = self._capture({"secret": self.SECRET, "cue": "该起床了"})
        self.assertEqual(context.activation.cue, "该起床了")
        blob = json.dumps(context.to_dict(), ensure_ascii=False)
        self.assertIn("该起床了", blob)
        self.assertNotIn(self.SECRET, blob)

    def test_no_cue_means_no_cue(self):
        self.assertIsNone(self._capture({"secret": self.SECRET}).activation.cue)
        self.assertIsNone(self._capture({"cue": "   "}).activation.cue)

    def test_a_malformed_cue_fails_loudly_instead_of_being_truncated(self):
        for payload in ({"cue": 7}, {"cue": "长" * (MAX_CUE_CHARS + 1)}):
            with self.subTest(payload=payload):
                with self.assertRaises(GenerationContextError):
                    ActivationCue.from_due(
                        ActivationDue(
                            activation_id="wake",
                            kind=ActivationKind.CHARACTER_ACTIVATION,
                            due_at=CLOCK, fired_at=CLOCK, sequence=0,
                            character_id="mizuki", payload=payload,
                        )
                    )

    def test_a_context_built_with_a_raw_due_record_is_refused(self):
        # 把整条到期记录塞回去也不行 —— 类型本身就是那道闸。
        with self.assertRaises(GenerationContextError):
            GenerationContext(
                character_id="mizuki",
                activation=ActivationDue(
                    activation_id="wake",
                    kind=ActivationKind.CHARACTER_ACTIVATION,
                    due_at=CLOCK, fired_at=CLOCK, sequence=0, character_id="mizuki",
                ),
                now=CLOCK,
                action_id=ActionId.SPEAK_HERE,
            )


class ContextModuleBoundaryTests(unittest.TestCase):
    """静态保证：生成上下文构造器读不到全知数据。"""

    def _attributes(self, path):
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

    def _names(self, path):
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        return {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }

    def test_the_generation_context_touches_no_omniscient_store(self):
        attributes = self._attributes(context_mod.__file__)
        for forbidden in ("exposures", "events", "memories", "agency", "turns"):
            self.assertNotIn(forbidden, attributes)

    def test_the_generation_context_never_receives_a_session(self):
        self.assertNotIn("SessionState", self._names(context_mod.__file__))

    def test_no_autonomy_module_reads_the_exposure_log(self):
        for path in sorted(AUTONOMY_DIR.glob("*.py")):
            self.assertNotIn(
                "exposures", self._attributes(path), f"{path.name} 读了曝光判定日志"
            )


# ── AC3 生成输出是不可信提案 ────────────────────────────────────────────
class UntrustedOutputTests(unittest.TestCase):
    def setUp(self):
        self.context = GenerationContext(
            character_id="mizuki",
            activation=ActivationCue(
                kind=ActivationKind.CHARACTER_ACTIVATION.value, at=CLOCK
            ),
            now=CLOCK,
            action_id=ActionId.SEND_CHANNEL_MESSAGE,
            target_id="nightcord",
        )

    def test_garbage_output_is_refused(self):
        for raw in (None, 42, [], {}, {"text": ""}, {"text": 7}, "   ", b"hi"):
            with self.subTest(raw=raw):
                with self.assertRaises(GenerationError):
                    parse_line(raw, self.context)

    def test_an_undeclared_key_is_refused_not_dropped(self):
        with self.assertRaises(GenerationError):
            parse_line({"text": "hi", "char_name": "冒名"}, self.context)
        with self.assertRaises(GenerationError):
            parse_line({"text": "hi", "character_id": "ena"}, self.context)

    def test_an_overlong_line_is_refused(self):
        with self.assertRaises(GenerationError):
            parse_line("啊" * (MAX_LINE_CHARS + 1), self.context)

    def test_a_plain_string_is_accepted_and_stripped(self):
        self.assertEqual(parse_line("  在的哦 \n", self.context), "在的哦")

    def test_a_leading_stage_direction_is_refused(self):
        for raw in ("（稍微停顿了一下）嗯…", "(sighs) 算了"):
            with self.subTest(raw=raw):
                with self.assertRaises(GenerationError):
                    parse_line(raw, self.context)

    def test_japanese_kana_is_structurally_refused(self):
        for raw in ("ボク也睡不着", "まだ没睡"):
            with self.subTest(raw=raw):
                with self.assertRaises(GenerationError):
                    parse_line(raw, self.context)

    def test_parentheses_inside_dialogue_are_not_overblocked(self):
        self.assertEqual(parse_line("这个（大概）没问题。", self.context), "这个（大概）没问题。")

    def test_the_exact_abstain_token_becomes_a_durable_abstention(self):
        state, scheduler, runtime = _rig(lines={"mizuki": ABSTAIN_TOKEN})
        result = runtime.process_due(_due(scheduler))
        self.assertIs(result.outcome, ActivationOutcome.ABSTAINED)
        self.assertIs(result.agency_outcome, AgencyOutcome.ABSTAINED)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())
        self.assertEqual(len(state.observations), 0)
        self.assertEqual(len(state.memories), 0)
        self.assertTrue(state.activation_outbox.is_acknowledged(result.due_id))

    def test_abstention_does_not_bypass_undeclared_key_validation(self):
        state, scheduler, runtime = _rig(
            lines={"mizuki": {"text": ABSTAIN_TOKEN, "character_id": "ena"}}
        )
        result = runtime.process_due(_due(scheduler))
        self.assertIs(result.outcome, ActivationOutcome.REJECTED)
        self.assertIs(result.agency_outcome, AgencyOutcome.REJECTED_POLICY_ERROR)

    def test_the_abstain_token_inside_a_sentence_is_ordinary_dialogue(self):
        state, scheduler, runtime = _rig(lines={"mizuki": f"不是 {ABSTAIN_TOKEN} 啦"})
        result = runtime.process_due(_due(scheduler))
        self.assertIs(result.outcome, ActivationOutcome.ACTED)

    def test_a_malformed_generation_is_a_terminal_rejection(self):
        state, scheduler, runtime = _rig(lines={"mizuki": lambda c: {"nope": 1}})
        result = runtime.process_due(_due(scheduler))
        self.assertIs(result.outcome, ActivationOutcome.REJECTED)
        self.assertIs(result.agency_outcome, AgencyOutcome.REJECTED_POLICY_ERROR)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())
        self.assertTrue(state.activation_outbox.is_acknowledged(result.due_id))

    def test_the_model_cannot_choose_its_own_actor(self):
        # 生成层只交回一句话；身份由 Agency 的上下文决定，不由输出决定。
        state, scheduler, runtime = _rig(
            lines={"mizuki": lambda c: {"text": "hi", "character_id": "ena"}}
        )
        result = runtime.process_due(_due(scheduler))
        self.assertIs(result.outcome, ActivationOutcome.REJECTED)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())

    def test_the_chooser_cannot_pick_an_illegal_action(self):
        state, scheduler, runtime = _rig(
            chooser=lambda context: LegalAction(
                action_id=ActionId.SEND_CHANNEL_MESSAGE, target_id="nowhere"
            )
        )
        result = runtime.process_due(_due(scheduler))
        self.assertIs(result.outcome, ActivationOutcome.REJECTED)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())


# ── AC4/AC5 完整回路 ────────────────────────────────────────────────────
class FullLoopTests(unittest.TestCase):
    """一条到期资格走完整条链，而且完全确定性、不联网。"""

    def test_the_loop_produces_event_observation_and_memory(self):
        state, scheduler, runtime = _rig()
        result = runtime.process_due(_due(scheduler))
        self.assertIs(result.outcome, ActivationOutcome.ACTED)
        self.assertIs(result.agency_outcome, AgencyOutcome.ACTED)

        events = state.events.by_type(EventType.MESSAGE_SENT)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["text"], "在的哦")
        self.assertEqual(events[0].actor_id, "mizuki")
        self.assertEqual(events[0].provenance["kind"], "agency")
        self.assertEqual(events[0].provenance["audit"]["drift_score"], 0.0)

        observers = set(state.observations.observers_of(events[0].event_id))
        self.assertEqual(observers, {"mizuki", "ena"})
        self.assertTrue(len(state.memories) > 0)
        self.assertEqual(result.event_id, events[0].event_id)
        self.assertTrue(result.memories > 0)

    def test_the_loop_is_deterministic(self):
        state1, scheduler1, runtime1 = _rig(session_id="s1")
        state2, scheduler2, runtime2 = _rig(session_id="s1")
        runtime1.process_due(_due(scheduler1))
        runtime2.process_due(_due(scheduler2))
        self.assertEqual(state1.events.to_dict(), state2.events.to_dict())
        self.assertEqual(state1.memories.to_dict(), state2.memories.to_dict())
        self.assertEqual(state1.agency.to_dict(), state2.agency.to_dict())

    def test_no_network_client_is_ever_constructed(self):
        # 整个包不 import 任何 HTTP/模型 SDK，也不 import Router 的调用层。
        for path in sorted(AUTONOMY_DIR.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            for forbidden in ("anthropic", "openai", "httpx", "requests"):
                self.assertNotIn(forbidden, imported, f"{path.name} 引入了 {forbidden}")

    def test_an_abstention_is_a_terminal_outcome_with_no_event(self):
        state, scheduler, runtime = _rig(chooser=lambda context: None)
        result = runtime.process_due(_due(scheduler))
        self.assertIs(result.outcome, ActivationOutcome.ABSTAINED)
        self.assertEqual(len(state.events.by_type(EventType.MESSAGE_SENT)), 0)
        self.assertTrue(state.activation_outbox.is_acknowledged(result.due_id))
        self.assertIs(
            state.agency.get(result.due_id).outcome, AgencyOutcome.ABSTAINED
        )

    def test_an_archive_round_trip_survives_an_authored_commit(self):
        state, scheduler, runtime = _rig()
        runtime.process_due(_due(scheduler))
        payload = json.loads(json.dumps(state.to_dict()))
        restored = SessionState.from_dict(payload)
        self.assertEqual(restored.events.to_dict(), state.events.to_dict())
        self.assertEqual(restored.agency.to_dict(), state.agency.to_dict())

    def test_an_archive_whose_audit_was_softened_is_refused(self):
        state, scheduler, runtime = _rig()
        runtime.process_due(_due(scheduler))
        payload = json.loads(json.dumps(state.to_dict()))
        for record in payload["agency"]["log"]["records"]:
            if record.get("event_id"):
                record["detail"]["audit"]["drift_score"] = 9.9
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(payload)

    def test_an_archive_whose_audit_was_deleted_is_refused(self):
        state, scheduler, runtime = _rig()
        runtime.process_due(_due(scheduler))
        payload = json.loads(json.dumps(state.to_dict()))
        for record in payload["agency"]["log"]["records"]:
            record["detail"].pop("audit", None)
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(payload)

    def test_an_archive_whose_event_provenance_was_softened_is_refused(self):
        state, scheduler, runtime = _rig()
        runtime.process_due(_due(scheduler))
        payload = json.loads(json.dumps(state.to_dict()))
        for event in payload["events"]["events"]:
            if event["type"] == EventType.MESSAGE_SENT.value:
                event["provenance"]["audit"]["drift_score"] = 9.9
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(payload)

    def test_an_archive_whose_event_provenance_lost_its_audit_is_refused(self):
        state, scheduler, runtime = _rig()
        runtime.process_due(_due(scheduler))
        payload = json.loads(json.dumps(state.to_dict()))
        for event in payload["events"]["events"]:
            if event["type"] == EventType.MESSAGE_SENT.value:
                event["provenance"].pop("audit", None)
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(payload)

    def test_an_archive_whose_audited_payload_was_rewritten_is_refused(self):
        # 把凭据里那份 payload 改成别的显示名 —— 凭据与提案就对不上了。
        state, scheduler, runtime = _rig(names={"mizuki": "瑞希"})
        runtime.process_due(_due(scheduler))
        payload = json.loads(json.dumps(state.to_dict()))
        for record in payload["agency"]["log"]["records"]:
            if record.get("event_id"):
                record["detail"]["audit"]["payload"]["char_name"] = "绘名"
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(payload)

    def test_an_archive_whose_line_was_rewritten_is_refused(self):
        state, scheduler, runtime = _rig()
        runtime.process_due(_due(scheduler))
        payload = json.loads(json.dumps(state.to_dict()))
        for event in payload["events"]["events"]:
            if event["type"] == EventType.MESSAGE_SENT.value:
                event["payload"]["text"] = "我改了这句"
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(payload)


# ── AC6 终局、重复与重试 ────────────────────────────────────────────────
class TerminalOutcomeTests(unittest.TestCase):
    def test_every_due_reaches_a_terminal_outcome_or_stays_pending(self):
        state, scheduler, runtime = _rig()
        for index in range(3):
            _due(scheduler, f"a{index}", minutes=5)
        results = runtime.process_pending()
        self.assertEqual(len(results), 3)
        for result in results:
            self.assertTrue(result.terminal)
            self.assertTrue(state.activation_outbox.is_acknowledged(result.due_id))
        self.assertEqual(state.activation_outbox.pending(), ())

    def test_a_failed_acknowledgement_does_not_double_commit_on_retry(self):
        state, scheduler, runtime = _rig()
        due = _due(scheduler)
        acknowledge = state.activation_outbox._acknowledge
        calls = {"n": 0}

        def flaky(due_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("确认失败")
            return acknowledge(due_id)

        state.activation_outbox._acknowledge = flaky
        self.assertIs(
            runtime.process_due(due).outcome, ActivationOutcome.FAILED_RETRYABLE
        )
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())
        self.assertIs(runtime.process_due(due).outcome, ActivationOutcome.ACTED)
        self.assertEqual(len(state.events.by_type(EventType.MESSAGE_SENT)), 1)
        self.assertEqual(len(state.agency), 1)

    def test_a_proposal_that_went_stale_between_generation_and_commit(self):
        # 生成和判分都在事务之外，所以中间世界可以变。提交那一刻重判前置
        # 条件 —— 人已经退出频道，这句话就不该落地。
        state, scheduler, runtime = _rig()
        engine = state.agency_engine
        due = _due(scheduler)
        plan = engine.propose(due)
        audit = runtime.auditor.audit(runtime._audit_request(plan))
        state.world_state.leave_channel("mizuki", "nightcord")
        record = engine.commit(plan.with_audit(audit))
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_STALE)
        self.assertEqual(record.detail["reason"], "failed_preconditions")
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())
        self.assertEqual(len(state.observations), 0)

    def test_a_due_is_never_handled_twice(self):
        state, scheduler, runtime = _rig()
        due = _due(scheduler)
        runtime.process_due(due)
        with self.assertRaises(AgencyEngineError):
            runtime.process_due(due)
        self.assertEqual(len(state.events.by_type(EventType.MESSAGE_SENT)), 1)

    def test_a_retried_activation_does_not_commit_twice(self):
        calls = {"n": 0}

        def flaky(context):
            calls["n"] += 1
            if calls["n"] == 1:
                raise GenerationError("模型暂时不可用", retryable=True)
            return "在的哦"

        state, scheduler, runtime = _rig(lines={"mizuki": flaky})
        due = _due(scheduler)
        first = runtime.process_due(due)
        self.assertIs(first.outcome, ActivationOutcome.FAILED_RETRYABLE)
        self.assertEqual(len(state.events.by_type(EventType.MESSAGE_SENT)), 0)
        self.assertIn(due, state.activation_outbox.pending())

        second = runtime.process_due(due)
        self.assertIs(second.outcome, ActivationOutcome.ACTED)
        self.assertEqual(second.attempt, 2)
        self.assertEqual(len(state.events.by_type(EventType.MESSAGE_SENT)), 1)

    def test_the_retry_budget_is_explicit_and_ends_in_a_durable_failure(self):
        def always_down(context):
            raise GenerationError("模型下线了", retryable=True)

        state, scheduler, runtime = _rig(
            lines={"mizuki": always_down}, retry=RetryPolicy(max_attempts=2)
        )
        due = _due(scheduler)
        self.assertIs(
            runtime.process_due(due).outcome, ActivationOutcome.FAILED_RETRYABLE
        )
        final = runtime.process_due(due)
        self.assertIs(final.outcome, ActivationOutcome.FAILED_TERMINAL)
        record = state.agency.get(due.due_id)
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_POLICY_ERROR)
        self.assertTrue(record.detail["retry_budget_exhausted"])
        self.assertEqual(record.detail["attempts"], 2)
        self.assertTrue(state.activation_outbox.is_acknowledged(due.due_id))
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())

    def test_a_non_retryable_generation_failure_burns_the_activation_at_once(self):
        def broken(context):
            raise GenerationError("提示词模板坏了", retryable=False)

        state, scheduler, runtime = _rig(lines={"mizuki": broken})
        result = runtime.process_due(_due(scheduler))
        self.assertIs(result.outcome, ActivationOutcome.REJECTED)
        self.assertEqual(result.attempt, 1)
        self.assertTrue(state.activation_outbox.is_acknowledged(result.due_id))

    def test_an_auditor_returning_the_wrong_type_is_a_terminal_failure(self):
        # 一个返回错类型的判分器不会自己好起来。不在边界上认出它，它会一路
        # 走到审计细节那一行才炸成 AttributeError，被误判成"提交事务被打断"，
        # 白烧掉整份重试预算。
        class Dicty:
            def audit(self, request):
                return {"drift_score": 0.0}

        state, scheduler, runtime = _rig(auditor=Dicty())
        due = _due(scheduler)
        result = runtime.process_due(due)
        self.assertIs(result.outcome, ActivationOutcome.FAILED_TERMINAL)
        self.assertEqual(result.attempt, 1)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())
        record = state.agency.get(due.due_id)
        self.assertEqual(record.detail["reason"], "audit_unavailable")
        self.assertFalse(record.detail["retryable"])
        self.assertTrue(state.activation_outbox.is_acknowledged(due.due_id))

    def test_no_threshold_can_wave_through_a_ten(self):
        # 阈值是可配置的，但它配不出"什么都接受"：分数量表封顶 10，阈值也是，
        # 而判定是 >= —— 所以 10 分永远不接受。
        for threshold in (0.0, 5.0, 10.0):
            with self.subTest(threshold=threshold):
                audit = GenerationAudit(
                    proposal_id="p1", character_id="mizuki",
                    payload={"text": "崩了的一句"},
                    drift_score=10.0, threshold=threshold, audited_at=CLOCK,
                )
                self.assertFalse(audit.accepted)
        with self.assertRaises(AuthoredTextError):
            GenerationAudit(
                proposal_id="p1", character_id="mizuki", payload={"text": "x"},
                drift_score=10.0, threshold=10.1, audited_at=CLOCK,
            )

    def test_a_retryable_audit_failure_leaves_the_due_pending(self):
        class Down(ScriptedAuditor):
            def audit(self, request):
                raise AuditError("Router 不可用", retryable=True)

        state, scheduler, runtime = _rig(auditor=Down())
        due = _due(scheduler)
        result = runtime.process_due(due)
        self.assertIs(result.outcome, ActivationOutcome.FAILED_RETRYABLE)
        self.assertIn(due, state.activation_outbox.pending())
        self.assertEqual(len(state.agency), 0)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())

    def test_pending_dues_are_reported(self):
        state, scheduler, runtime = _rig(
            lines={"mizuki": lambda c: (_ for _ in ()).throw(
                GenerationError("down", retryable=True)
            )}
        )
        due = _due(scheduler)
        runtime.process_due(due)
        self.assertEqual(runtime.status()["pending_due_ids"], [due.due_id])


# ── AC7 原子性 ──────────────────────────────────────────────────────────
class AtomicityTests(unittest.TestCase):
    """在每个权威变更点注入故障，世界都不许留下半截。"""

    def _snapshot(self, state):
        return {
            "world": state.world_state.to_dict(),
            "events": state.events.to_dict(),
            "observations": state.observations.to_dict(),
            "exposures": state.exposures.to_dict(),
            "agency": state.agency.to_dict(),
            "memories": state.memories.to_dict(),
            "outbox": state.activation_outbox.to_dict(),
        }

    def test_a_failing_memory_encode_rolls_back_the_whole_activation(self):
        state, scheduler, runtime = _rig()
        due = _due(scheduler)
        before = self._snapshot(state)

        def boom(observations):
            raise RuntimeError("记忆写入失败")

        runtime.memory.encode = boom
        result = runtime.process_due(due)
        self.assertIs(result.outcome, ActivationOutcome.FAILED_RETRYABLE)
        self.assertEqual(self._snapshot(state), before)
        self.assertIn(due, state.activation_outbox.pending())

    def test_a_failing_acknowledgement_rolls_back_the_whole_activation(self):
        state, scheduler, runtime = _rig()
        due = _due(scheduler)
        before = self._snapshot(state)

        def boom(due_id):
            raise RuntimeError("确认失败")

        state.activation_outbox._acknowledge = boom
        result = runtime.process_due(due)
        self.assertIs(result.outcome, ActivationOutcome.FAILED_RETRYABLE)
        self.assertEqual(self._snapshot(state), before)

    def test_a_failing_exposure_rolls_back_the_whole_activation(self):
        state, scheduler, runtime = _rig()
        due = _due(scheduler)
        before = self._snapshot(state)

        def boom(decisions, observations):
            raise RuntimeError("曝光判定失败")

        state.record_observations = boom
        result = runtime.process_due(due)
        self.assertIs(result.outcome, ActivationOutcome.FAILED_RETRYABLE)
        self.assertEqual(self._snapshot(state), before)

    def test_a_failing_event_append_rolls_back_the_whole_activation(self):
        state, scheduler, runtime = _rig()
        due = _due(scheduler)
        before = self._snapshot(state)

        def boom(event):
            raise RuntimeError("世界历史追加失败")

        state.events._append = boom
        result = runtime.process_due(due)
        self.assertIs(result.outcome, ActivationOutcome.FAILED_RETRYABLE)
        self.assertEqual(self._snapshot(state), before)
        self.assertIn(due, state.activation_outbox.pending())

    def test_a_failing_agency_append_rolls_back_the_whole_activation(self):
        # 审计写不进去，事件也不许留下 —— 否则世界说"他做了"，而没有任何
        # 记录说明这是谁的判断。
        state, scheduler, runtime = _rig()
        due = _due(scheduler)
        before = self._snapshot(state)

        def boom(record):
            raise RuntimeError("写审计失败")

        state.agency._append = boom
        result = runtime.process_due(due)
        self.assertIs(result.outcome, ActivationOutcome.FAILED_RETRYABLE)
        self.assertEqual(self._snapshot(state), before)
        self.assertIn(due, state.activation_outbox.pending())

    def test_a_failing_audit_leaves_no_world_change(self):
        # 判分器自己有 bug —— 那句话根本没被判过。这条到期资格仍然要有耐久
        # 的交代（否则它会永远待处理），但那份交代里不许有事件、观察或记忆。
        class Boom(ScriptedAuditor):
            def audit(self, request):
                raise RuntimeError("判分器炸了")

        state, scheduler, runtime = _rig(auditor=Boom())
        due = _due(scheduler)
        observations_before = len(state.observations)
        result = runtime.process_due(due)
        self.assertIs(result.outcome, ActivationOutcome.FAILED_TERMINAL)
        self.assertIsNone(result.event_id)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())
        self.assertEqual(len(state.observations), observations_before)
        self.assertEqual(len(state.memories), 0)
        record = state.agency.get(due.due_id)
        self.assertIsNone(record.event_id)
        self.assertEqual(record.detail["reason"], "audit_unavailable")
        self.assertTrue(state.activation_outbox.is_acknowledged(due.due_id))


# ── AC8 停止边界 ────────────────────────────────────────────────────────
class StopBoundaryTests(unittest.TestCase):
    def test_a_late_generation_cannot_commit_after_stop(self):
        holder = {}

        def slow(context):
            holder["runtime"].stop("配置重载")
            return "这句话来晚了"

        state, scheduler, runtime = _rig(lines={"mizuki": slow})
        holder["runtime"] = runtime
        due = _due(scheduler)
        result = runtime.process_due(due)
        self.assertIs(result.outcome, ActivationOutcome.STOPPED)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())
        self.assertEqual(len(state.agency), 0)
        self.assertIn(due, state.activation_outbox.pending())

    def test_a_late_audit_cannot_commit_after_stop(self):
        holder = {}

        class Stopping(ScriptedAuditor):
            def audit(self, request):
                holder["runtime"].stop("配置重载")
                return super().audit(request)

        state, scheduler, runtime = _rig(auditor=Stopping())
        holder["runtime"] = runtime
        due = _due(scheduler)
        result = runtime.process_due(due)
        self.assertIs(result.outcome, ActivationOutcome.STOPPED)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())
        self.assertIn(due, state.activation_outbox.pending())

    def test_a_commit_already_under_way_is_not_torn_in_half(self):
        # 停止在**安全边界**上生效，不是随处生效：一次已经进了事务的提交
        # 会跑完。撕开它会留下半条事件，而那比晚停一次严重得多。
        state, scheduler, runtime = _rig()
        due = _due(scheduler)
        record_observations = state.record_observations

        def stopping(decisions, observations):
            runtime.stop("事务中途")
            return record_observations(decisions, observations)

        state.record_observations = stopping
        result = runtime.process_due(due)
        self.assertIs(result.outcome, ActivationOutcome.ACTED)
        self.assertEqual(len(state.events.by_type(EventType.MESSAGE_SENT)), 1)
        self.assertTrue(state.activation_outbox.is_acknowledged(due.due_id))
        # 停机在事务结束时生效，所以下一条不再开始。
        self.assertFalse(runtime.running)
        self.assertIs(
            runtime.process_due(_due(scheduler, "next", minutes=5)).outcome,
            ActivationOutcome.STOPPED,
        )

    def test_a_stopped_runtime_starts_no_new_activation(self):
        state, scheduler, runtime = _rig()
        due = _due(scheduler)
        runtime.stop("手动停止")
        result = runtime.process_due(due)
        self.assertIs(result.outcome, ActivationOutcome.STOPPED)
        self.assertEqual(len(state.agency), 0)

    def test_a_stopped_runtime_refuses_to_advance_the_clock(self):
        state, scheduler, runtime = _rig()
        clock = state.world_state.clock
        runtime.stop("手动停止")
        with self.assertRaises(AutonomyError):
            runtime.advance(10)
        self.assertEqual(state.world_state.clock, clock)

    def test_stop_is_idempotent_and_keeps_the_first_reason(self):
        state, scheduler, runtime = _rig()
        runtime.stop("第一次")
        runtime.stop("第二次")
        self.assertEqual(runtime.status()["stop_reason"], "第一次")

    def test_a_runtime_that_never_started_processes_nothing(self):
        state, scheduler, runtime = _rig(start=False)
        due = _due(scheduler)
        result = runtime.process_due(due)
        self.assertIs(result.outcome, ActivationOutcome.STOPPED)
        self.assertEqual(len(state.agency), 0)

    def test_it_cannot_be_restarted_after_stop(self):
        state, scheduler, runtime = _rig()
        runtime.stop("done")
        with self.assertRaises(AutonomyError):
            runtime.start()

    def test_a_runtime_stopped_before_it_started_cannot_start(self):
        # 判据是"有没有被要求停止过"，不是"现在跑没跑"。只看后者的话，
        # 先 stop 再 start 会得到一个既有停机理由、又自称在跑的运行时。
        state, scheduler, runtime = _rig(start=False)
        runtime.stop("还没开就先停了")
        with self.assertRaises(AutonomyError):
            runtime.start()
        self.assertFalse(runtime.running)
        self.assertEqual(runtime.status()["stop_reason"], "还没开就先停了")

    def test_the_audit_payload_projection_is_a_fresh_structure(self):
        audit = _audit(_proposal())
        payload = audit.to_dict()
        payload["payload"]["text"] = "改了"
        self.assertEqual(audit.text, "在的哦")


# ── AC1/AC10 服务 API ───────────────────────────────────────────────────
class StopCommitLinearizationTests(unittest.TestCase):
    """停机与提交许可之间不能有窗口。

    确定性做法：用 Event 当 barrier 把工作线程精确停在某一个点上，再从主线程
    调 stop()，然后看 stop() 是**立刻返回**还是**被挡住**。两者恰好区分了
    "停机发生在事务之前"和"事务已经开始"。

    每个 wait 都带超时，所以哪怕不变量坏掉，测试是失败而不是挂死。
    """

    TIMEOUT = 5

    def _run_in_thread(self, target):
        box = {}

        def run():
            try:
                box["result"] = target()
            except BaseException as e:  # 线程里的异常要带回主线程
                box["error"] = e

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, box

    def _join(self, thread, box, what):
        thread.join(self.TIMEOUT)
        self.assertFalse(thread.is_alive(), f"{what} 没能在超时前结束")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def test_a_slow_generation_holds_no_lock_and_stop_wins(self):
        # 工作线程停在**生成**里（闸门之外）。stop() 必须立刻返回 ——
        # 一个卡住的模型调用不该让停机也跟着卡住。
        entered = threading.Event()
        release = threading.Event()

        def parked(context):
            entered.set()
            release.wait(self.TIMEOUT)
            return "这句话来晚了"

        state, scheduler, runtime = _rig(lines={"mizuki": parked})
        due = _due(scheduler)
        worker, worker_box = self._run_in_thread(lambda: runtime.process_due(due))
        self.assertTrue(entered.wait(self.TIMEOUT), "生成器没被调用")

        stopper, stopper_box = self._run_in_thread(lambda: runtime.stop("配置重载"))
        self._join(stopper, stopper_box, "慢生成期间的 stop()")

        release.set()
        result = self._join(worker, worker_box, "工作线程")
        self.assertIs(result.outcome, ActivationOutcome.STOPPED)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())
        self.assertEqual(len(state.agency), 0)
        self.assertIn(due, state.activation_outbox.pending())

    def test_a_slow_audit_holds_no_lock_and_stop_wins(self):
        entered = threading.Event()
        release = threading.Event()

        class Parked(ScriptedAuditor):
            def audit(inner, request):
                entered.set()
                release.wait(self.TIMEOUT)
                return super().audit(request)

        state, scheduler, runtime = _rig(auditor=Parked())
        due = _due(scheduler)
        worker, worker_box = self._run_in_thread(lambda: runtime.process_due(due))
        self.assertTrue(entered.wait(self.TIMEOUT), "判分器没被调用")

        stopper, stopper_box = self._run_in_thread(lambda: runtime.stop("配置重载"))
        self._join(stopper, stopper_box, "慢判分期间的 stop()")

        release.set()
        result = self._join(worker, worker_box, "工作线程")
        self.assertIs(result.outcome, ActivationOutcome.STOPPED)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())
        self.assertIn(due, state.activation_outbox.pending())

    def test_a_stop_landing_in_the_window_before_the_transaction_wins(self):
        # 这条盯的就是那个被复核出来的窗口：最后一次裸检查已经过了，事务还
        # 没开始。把工作线程精确停在这里，再停机 —— 提案必须提交不了。
        entered = threading.Event()
        release = threading.Event()
        state, scheduler, runtime = _rig()
        due = _due(scheduler)
        commit = runtime._commit

        def parked(*args, **kwargs):
            entered.set()
            release.wait(self.TIMEOUT)
            return commit(*args, **kwargs)

        runtime._commit = parked
        worker, worker_box = self._run_in_thread(lambda: runtime.process_due(due))
        self.assertTrue(entered.wait(self.TIMEOUT), "没走到提交那一步")

        stopper, stopper_box = self._run_in_thread(lambda: runtime.stop("窗口里停机"))
        # 事务还没开始，所以 stop() 不该被挡住。
        self._join(stopper, stopper_box, "窗口里的 stop()")

        release.set()
        result = self._join(worker, worker_box, "工作线程")
        self.assertIs(result.outcome, ActivationOutcome.STOPPED)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())
        self.assertEqual(len(state.agency), 0)
        self.assertEqual(len(state.memories), 0)
        self.assertIn(due, state.activation_outbox.pending())

    def test_a_transaction_already_under_way_makes_stop_wait_for_it(self):
        # 反过来那一半：事务已经开始，stop() 必须等它跑完再返回 —— 撕开
        # 一个进行中的提交会留下半条事件。
        entered = threading.Event()
        release = threading.Event()
        state, scheduler, runtime = _rig()
        due = _due(scheduler)
        record_observations = state.record_observations

        def parked(decisions, observations):
            entered.set()
            release.wait(self.TIMEOUT)
            return record_observations(decisions, observations)

        state.record_observations = parked
        worker, worker_box = self._run_in_thread(lambda: runtime.process_due(due))
        self.assertTrue(entered.wait(self.TIMEOUT), "没走进事务")

        def stop_and_snapshot():
            status = runtime.stop("事务里停机")
            # 就在 stop() 返回的这一刻抓一份：契约说的是"返回之后没有提交能
            # 落地"，那么返回的这一刻，那次提交必须**整个**已经写完 ——
            # 事件、Agency 记录、交接确认，一样都不能还在路上。
            return {
                "status": status,
                "events": len(state.events.by_type(EventType.MESSAGE_SENT)),
                "agency": len(state.agency),
                "acknowledged": state.activation_outbox.is_acknowledged(due.due_id),
            }

        stopper, stopper_box = self._run_in_thread(stop_and_snapshot)
        stopper.join(0.3)
        self.assertTrue(
            stopper.is_alive(), "事务还在跑，stop() 却先返回了 —— 没有线性化"
        )
        self.assertTrue(runtime.running, "事务跑完之前不该已经变成停止状态")

        release.set()
        result = self._join(worker, worker_box, "工作线程")
        snapshot = self._join(stopper, stopper_box, "事务结束后的 stop()")

        self.assertIs(result.outcome, ActivationOutcome.ACTED)
        self.assertEqual(snapshot["events"], 1)
        self.assertEqual(snapshot["agency"], 1, "stop() 返回时 Agency 记录还没写")
        self.assertTrue(snapshot["acknowledged"], "stop() 返回时交接还没确认")
        self.assertFalse(snapshot["status"]["running"])
        self.assertFalse(runtime.running)

    def test_a_stop_from_inside_the_transaction_is_deferred_not_immediate(self):
        # 事务内部调 stop() 没法等自己跑完，所以它只能**登记**。要害在于它
        # 必须如实说自己还没生效：谎称已经停了、紧接着又把 Agency 记录和交接
        # 确认写下去，正是"stop 返回后不再落提交"这条契约要防的事。
        state, scheduler, runtime = _rig()
        due = _due(scheduler)
        record_observations = state.record_observations
        seen = {}

        def stopping(decisions, observations):
            seen["status"] = runtime.stop("事务中途")
            seen["running"] = runtime.running
            # 这一刻 Agency 记录和确认都还没写 —— 正是那段"之后"。
            seen["agency"] = len(state.agency)
            seen["acknowledged"] = state.activation_outbox.is_acknowledged(due.due_id)
            return record_observations(decisions, observations)

        state.record_observations = stopping
        worker, box = self._run_in_thread(lambda: runtime.process_due(due))
        result = self._join(worker, box, "事务里自调 stop 的线程")

        # 登记那一刻：如实报告"还在跑、已登记、尚未生效"。
        self.assertEqual(seen["agency"], 0)
        self.assertFalse(seen["acknowledged"])
        self.assertTrue(seen["running"], "事务里的 stop() 不该立刻宣称已停")
        self.assertTrue(seen["status"]["running"])
        self.assertTrue(seen["status"]["stop_requested"])
        self.assertTrue(seen["status"]["stopping"])
        self.assertEqual(seen["status"]["stop_reason"], "事务中途")

        # 事务照常跑完（撕开它会留下半条事件），然后停机才生效。
        self.assertIs(result.outcome, ActivationOutcome.ACTED)
        self.assertEqual(len(state.events.by_type(EventType.MESSAGE_SENT)), 1)
        self.assertEqual(len(state.agency), 1)
        self.assertTrue(state.activation_outbox.is_acknowledged(due.due_id))
        self.assertFalse(runtime.running)
        self.assertFalse(runtime.stopping)
        status = runtime.status()
        self.assertFalse(status["running"])
        self.assertTrue(status["stop_requested"])

        # 生效之后就真的不再接活了。
        self.assertIs(
            runtime.process_due(_due(scheduler, "next", minutes=5)).outcome,
            ActivationOutcome.STOPPED,
        )

    def test_nothing_lands_after_an_external_stop_returns(self):
        # 契约的最强形式：在 stop() 返回的那一刻抓一份世界快照，再等一切结束
        # 之后抓一份 —— 两份必须**一模一样**。事件、Agency 记录、交接确认，
        # 任何一样在 stop() 返回之后才补上，都算违反。
        for trial in range(50):
            state, scheduler, runtime = _rig()
            due = _due(scheduler)
            worker, worker_box = self._run_in_thread(
                lambda: runtime.process_due(due)
            )

            status = runtime.stop("契约")
            at_return = (
                len(state.events.by_type(EventType.MESSAGE_SENT)),
                len(state.agency),
                state.activation_outbox.is_acknowledged(due.due_id),
            )
            self._join(worker, worker_box, f"第 {trial} 轮的工作线程")
            after = (
                len(state.events.by_type(EventType.MESSAGE_SENT)),
                len(state.agency),
                state.activation_outbox.is_acknowledged(due.due_id),
            )

            # 外部线程的 stop 一律是"已生效"那一种。
            self.assertFalse(status["running"], f"第 {trial} 轮返回时还在跑")
            self.assertFalse(status["stopping"])
            self.assertEqual(
                at_return, after, f"第 {trial} 轮 stop() 返回之后状态还在变"
            )

    def test_an_external_stop_waits_for_a_deferred_stop_to_settle(self):
        # 事务里已经登记了停机，另一个线程这时也来 stop() —— 它必须等那个事务
        # 结束，不能抢先宣布已停（那又会变成"宣布停了、记录随后才落"）。
        state, scheduler, runtime = _rig()
        due = _due(scheduler)
        record_observations = state.record_observations
        seen = {}

        def stopping(decisions, observations):
            runtime.stop("事务内登记")
            outer, outer_box = self._run_in_thread(lambda: runtime.stop("外部"))
            outer.join(0.3)
            seen["outer_blocked"] = outer.is_alive()
            seen["outer"] = (outer, outer_box)
            return record_observations(decisions, observations)

        state.record_observations = stopping
        result = runtime.process_due(due)
        outer, outer_box = seen["outer"]
        outer_status = self._join(outer, outer_box, "事务外的 stop()")

        self.assertTrue(seen["outer_blocked"], "外部 stop 没有等事务跑完")
        self.assertIs(result.outcome, ActivationOutcome.ACTED)
        self.assertFalse(outer_status["running"])
        # 第一个理由才是真正的原因。
        self.assertEqual(runtime.stop_reason, "事务内登记")
        self.assertEqual(len(state.agency), 1)
        self.assertTrue(state.activation_outbox.is_acknowledged(due.due_id))

    def test_a_deferred_stop_still_takes_effect_when_the_transaction_rolls_back(self):
        # 登记过的停机不该因为那次事务失败就一起消失 —— 它是被请求过的。
        state, scheduler, runtime = _rig()
        due = _due(scheduler)

        def stopping(decisions, observations):
            runtime.stop("事务中途")
            raise RuntimeError("提交炸了")

        state.record_observations = stopping
        result = runtime.process_due(due)
        self.assertIs(result.outcome, ActivationOutcome.FAILED_RETRYABLE)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())
        self.assertEqual(len(state.agency), 0)
        self.assertFalse(runtime.running, "回滚之后登记过的停机仍然要生效")
        self.assertIn(due, state.activation_outbox.pending())

    def test_stopping_between_the_tick_and_the_processing_loses_nothing(self):
        # 时钟推进是调度器自己的事务，已经落地了；停机发生在它之后、处理
        # 之前 —— 那几条到期资格必须原样躺在投递箱里，不能凭空消失。
        state, scheduler, runtime = _rig()
        for index in range(3):
            scheduler.schedule(
                ScheduledActivation(
                    activation_id=f"a{index}",
                    kind=ActivationKind.CHARACTER_ACTIVATION,
                    due_at=scheduler.clock + timedelta(minutes=5),
                    character_id="mizuki",
                )
            )
        tick_report = runtime._tick_report

        def stopping(tick):
            runtime.stop("推进后立刻停")
            return tick_report(tick)

        runtime._tick_report = stopping
        report = runtime.advance(5)
        self.assertEqual(report["results"], [])
        self.assertEqual(state.world_state.clock, CLOCK + timedelta(minutes=5))
        self.assertEqual(len(state.activation_outbox.pending()), 3)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())
        self.assertEqual(len(state.agency), 0)

    def test_a_crashed_transaction_does_not_wedge_the_gate(self):
        # 事务里抛异常之后闸门必须已经释放，否则下一条到期会永远卡住。
        state, scheduler, runtime = _rig()
        first = _due(scheduler, "x1", minutes=2)
        state.events._append = lambda event: (_ for _ in ()).throw(RuntimeError("炸"))
        self.assertIs(
            runtime.process_due(first).outcome, ActivationOutcome.FAILED_RETRYABLE
        )
        del state.events._append

        second = _due(scheduler, "x2", minutes=2)
        worker, box = self._run_in_thread(lambda: runtime.process_due(second))
        result = self._join(worker, box, "闸门崩溃之后的下一条")
        self.assertIs(result.outcome, ActivationOutcome.ACTED)

    def test_advancing_the_clock_races_stop_without_tearing(self):
        state, scheduler, runtime = _rig()
        runtime.stop("先停了")
        clock = state.world_state.clock
        with self.assertRaises(AutonomyError):
            runtime.advance(10)
        self.assertEqual(state.world_state.clock, clock)
        self.assertEqual(len(state.events), 0)


class LifecycleLinearizationTests(unittest.TestCase):
    """start()、stop() 和提交许可必须走同一条边界。

    确定性做法：测试线程自己持住那把闸门锁，把两个竞争者精确地堵在门外，
    再放行 —— 于是"它们是不是真的走同一道闸"是可断言的，而不是靠反复跑碰
    运气。
    """

    TIMEOUT = 5

    def _racers(self, runtime, calls):
        threads, boxes = [], []
        for call in calls:
            box = {}

            def run(call=call, box=box):
                try:
                    box["result"] = call()
                except BaseException as e:
                    box["error"] = e

            thread = threading.Thread(target=run, daemon=True)
            threads.append(thread)
            boxes.append(box)
        return threads, boxes

    def test_start_does_not_flip_state_outside_the_gate(self):
        # 这条才是有判别力的那条：闸门在测试线程手上时，start() 一个字节都
        # 不该翻过去。它光"最后被挡住"是不够的 —— 只要检查与翻转在闸门之外
        # 完成，stop() 就能插在两者之间，于是运行时既在跑、又已被要求停止。
        state, scheduler, runtime = _rig(start=False)
        threads, boxes = self._racers(runtime, [runtime.start])
        with runtime._gate:
            threads[0].start()
            threads[0].join(0.3)
            self.assertTrue(threads[0].is_alive(), "start() 没有被闸门拦住")
            self.assertFalse(
                runtime._started, "闸门在别人手上，start() 却已经翻了 started"
            )
            self.assertFalse(
                runtime.running, "闸门在别人手上，start() 却已经翻了 running"
            )
        threads[0].join(self.TIMEOUT)
        self.assertFalse(threads[0].is_alive())
        self.assertTrue(runtime.running)
        self.assertNotIn("error", boxes[0])

    def test_stop_does_not_flip_state_outside_the_gate(self):
        state, scheduler, runtime = _rig()
        threads, _ = self._racers(runtime, [lambda: runtime.stop("并发停")])
        with runtime._gate:
            threads[0].start()
            threads[0].join(0.3)
            self.assertTrue(threads[0].is_alive(), "stop() 没有被闸门拦住")
            self.assertTrue(
                runtime.running, "闸门在别人手上，stop() 却已经翻了 running"
            )
            self.assertIsNone(runtime.stop_reason)
        threads[0].join(self.TIMEOUT)
        self.assertFalse(threads[0].is_alive())
        self.assertFalse(runtime.running)
        self.assertEqual(runtime.stop_reason, "并发停")

    def test_two_concurrent_starts_produce_exactly_one_winner(self):
        state, scheduler, runtime = _rig(start=False)
        threads, boxes = self._racers(runtime, [runtime.start, runtime.start])
        with runtime._gate:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(0.3)
                self.assertTrue(thread.is_alive(), "start 没有走闸门")
        for thread in threads:
            thread.join(self.TIMEOUT)
            self.assertFalse(thread.is_alive())

        winners = [box for box in boxes if "result" in box]
        losers = [box for box in boxes if "error" in box]
        self.assertEqual(len(winners), 1, "两个 start 都成功了")
        self.assertEqual(len(losers), 1)
        self.assertIsInstance(losers[0]["error"], AutonomyError)
        self.assertTrue(runtime.running)
        self.assertFalse(runtime.stop_requested)

    def test_a_start_racing_a_stop_never_yields_a_running_stopped_runtime(self):
        # 上面两条锁住了"翻转必须在闸门里"；这条按不变量对撞，盯的是结果本身。
        # 用 barrier 让两个线程尽量同一瞬间起跑，免得 stop 总是先跑完。
        for trial in range(200):
            state, scheduler, runtime = _rig(start=False)
            gun = threading.Barrier(2, timeout=self.TIMEOUT)

            def stop_racer():
                gun.wait()
                runtime.stop("对撞")

            def start_racer():
                gun.wait()
                try:
                    runtime.start()
                except AutonomyError:
                    pass

            threads = [
                threading.Thread(target=stop_racer, daemon=True),
                threading.Thread(target=start_racer, daemon=True),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(self.TIMEOUT)
                self.assertFalse(thread.is_alive())
            status = runtime.status()
            self.assertFalse(
                status["running"] and status["stop_requested"],
                f"第 {trial} 轮拼出了一个既在跑、又已被要求停止的运行时: {status}",
            )
            self.assertFalse(runtime.running)

    def test_status_is_a_consistent_snapshot_under_concurrent_lifecycle_changes(self):
        state, scheduler, runtime = _rig()
        dues = [
            _due(scheduler, f"s{index}", character_id=("mizuki", "ena")[index % 2],
                 minutes=2)
            for index in range(6)
        ]
        torn = []
        done = threading.Event()

        def churn():
            try:
                for due in dues:
                    runtime.process_due(due)
            finally:
                runtime.stop("收尾")
                done.set()

        def read():
            while not done.is_set():
                status = runtime.status()
                # 快照内部必须自洽：stopping 恰好是"已请求且还在跑"。
                if status["stopping"] != (
                    status["stop_requested"] and status["running"]
                ):
                    torn.append(status)
                # 没请求过就不可能有理由。
                if status["stop_requested"] != (status["stop_reason"] is not None):
                    torn.append(status)
                # 还没启动就不可能在跑。
                if status["running"] and not status["started"]:
                    torn.append(status)

        threads = [
            threading.Thread(target=churn, daemon=True),
            threading.Thread(target=read, daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
            self.assertFalse(thread.is_alive())
        self.assertEqual(torn[:3], [], "读到了自相矛盾的状态快照")

    def test_stop_before_start_stays_refused_after_the_gate_change(self):
        state, scheduler, runtime = _rig(start=False)
        runtime.stop("还没开就先停了")
        self.assertFalse(runtime.running)
        self.assertTrue(runtime.stop_requested)
        self.assertFalse(runtime.stopping)
        with self.assertRaises(AutonomyError):
            runtime.start()


class ConcurrentProcessingTests(unittest.TestCase):
    """并发 process_due：不许重复提交，也不许白跑两次生成。"""

    TIMEOUT = 5

    def test_the_same_due_cannot_be_processed_twice_at_once(self):
        entered = threading.Event()
        release = threading.Event()
        generations = []

        def parked(context):
            generations.append(context.character_id)
            entered.set()
            release.wait(self.TIMEOUT)
            return "在的哦"

        state, scheduler, runtime = _rig(lines={"mizuki": parked})
        due = _due(scheduler)
        box = {}

        def first():
            box["first"] = runtime.process_due(due)

        worker = threading.Thread(target=first, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(self.TIMEOUT), "第一个线程没走到生成")

        with self.assertRaises(AutonomyError):
            runtime.process_due(due)

        release.set()
        worker.join(self.TIMEOUT)
        self.assertFalse(worker.is_alive())
        self.assertIs(box["first"].outcome, ActivationOutcome.ACTED)
        # 生成只跑了一次，事件只有一条。
        self.assertEqual(generations, ["mizuki"])
        self.assertEqual(len(state.events.by_type(EventType.MESSAGE_SENT)), 1)
        self.assertEqual(len(state.agency), 1)

    def test_a_due_can_be_processed_again_after_the_first_pass_finishes(self):
        # 在途登记是**并发**保护，不是永久占用：处理完就摘掉，之后再处理
        # 同一条会撞上交接那道闸（一次性），而不是撞上这把锁。
        state, scheduler, runtime = _rig()
        due = _due(scheduler)
        runtime.process_due(due)
        with self.assertRaises(AgencyEngineError):
            runtime.process_due(due)

    def test_different_dues_commit_concurrently_without_corruption(self):
        state, scheduler, runtime = _rig()
        first = _due(scheduler, "a0", character_id="mizuki", minutes=5)
        second = _due(scheduler, "a1", character_id="ena", minutes=5)
        results = {}
        ready = threading.Barrier(2, timeout=self.TIMEOUT)

        def run(due, key):
            ready.wait()
            results[key] = runtime.process_due(due)

        threads = [
            threading.Thread(target=run, args=(first, "first"), daemon=True),
            threading.Thread(target=run, args=(second, "second"), daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(self.TIMEOUT)
            self.assertFalse(thread.is_alive())

        self.assertEqual(len(results), 2)
        for result in results.values():
            self.assertIs(result.outcome, ActivationOutcome.ACTED)
        self.assertEqual(len(state.events.by_type(EventType.MESSAGE_SENT)), 2)
        self.assertEqual(len(state.agency), 2)
        self.assertEqual(state.activation_outbox.pending(), ())
        # 世界历史仍然自洽：事件 ID 全局唯一（含两条时钟推进），序号连续，
        # 没有被并发写坏。
        events = state.events.events()
        self.assertEqual(len(set(e.event_id for e in events)), len(events))
        self.assertEqual(
            [state.events.sequence_of(e.event_id) for e in events],
            list(range(len(events))),
        )

    def test_process_pending_skips_an_in_flight_due_instead_of_breaking(self):
        # 点名处理某一条要响亮拒绝；成批驱动则应该跳过 —— 那条没有丢，
        # 正有人在处理它，而这一轮只报告"这次真的处理了哪些"。
        entered = threading.Event()
        release = threading.Event()

        def parked(context):
            entered.set()
            release.wait(self.TIMEOUT)
            return "在的哦"

        state, scheduler, runtime = _rig(lines={"mizuki": parked, "ena": "……嗯"})
        _due(scheduler, "a0", character_id="mizuki", minutes=2)
        worker = threading.Thread(
            target=lambda: runtime.process_pending(), daemon=True
        )
        worker.start()
        self.assertTrue(entered.wait(self.TIMEOUT), "第一个线程没走到生成")

        # 这一轮什么都没处理，但也没有抛错。
        self.assertEqual(runtime.process_pending(), ())
        self.assertEqual(runtime.status()["in_flight_due_ids"], ["a0@2026-08-21T23:52:00"])

        release.set()
        worker.join(self.TIMEOUT)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(state.events.by_type(EventType.MESSAGE_SENT)), 1)
        self.assertEqual(state.activation_outbox.pending(), ())
        self.assertEqual(runtime.status()["in_flight_due_ids"], [])

    def test_a_failed_due_stays_processable_after_the_in_flight_slot_clears(self):
        calls = {"n": 0}

        def flaky(context):
            calls["n"] += 1
            if calls["n"] == 1:
                raise GenerationError("暂时不可用", retryable=True)
            return "在的哦"

        state, scheduler, runtime = _rig(lines={"mizuki": flaky})
        due = _due(scheduler)
        self.assertIs(
            runtime.process_due(due).outcome, ActivationOutcome.FAILED_RETRYABLE
        )
        self.assertIs(runtime.process_due(due).outcome, ActivationOutcome.ACTED)
        self.assertEqual(len(state.events.by_type(EventType.MESSAGE_SENT)), 1)


class ServiceApiTests(unittest.TestCase):
    def test_status_reports_the_shape_web1_needs(self):
        state, scheduler, runtime = _rig()
        status = runtime.status()
        for key in (
            "session_id", "running", "stop_reason", "clock", "pending_due_ids",
            "scheduled", "next_due_at", "outcomes", "committed_actions",
        ):
            self.assertIn(key, status)
        self.assertTrue(status["running"])

    def test_advance_moves_the_simulated_clock_and_processes_what_fell_due(self):
        state, scheduler, runtime = _rig()
        scheduler.schedule(
            ScheduledActivation(
                activation_id="wake",
                kind=ActivationKind.CHARACTER_ACTIVATION,
                due_at=scheduler.clock + timedelta(minutes=10),
                character_id="mizuki",
            )
        )
        report = runtime.advance(10)
        self.assertEqual(state.world_state.clock, CLOCK + timedelta(minutes=10))
        self.assertEqual(len(report["results"]), 1)
        self.assertEqual(report["results"][0]["outcome"], "acted")

    def test_positions_are_a_fresh_projection(self):
        state, scheduler, runtime = _rig()
        positions = runtime.positions()
        self.assertEqual(positions["mizuki"]["location_id"], "mizuki_home_room")
        positions["mizuki"]["location_id"] = "tokyo"
        positions["mizuki"]["channels"].append("hacked")
        self.assertEqual(
            runtime.positions()["mizuki"]["location_id"], "mizuki_home_room"
        )
        self.assertEqual(runtime.positions()["mizuki"]["channels"], ["nightcord"])

    def test_status_and_outcomes_hand_out_fresh_structures(self):
        state, scheduler, runtime = _rig()
        runtime.process_due(_due(scheduler))
        status = runtime.status()
        status["pending_due_ids"].append("hacked")
        status["outcomes"]["acted"] = 999
        self.assertEqual(runtime.status()["outcomes"]["acted"], 1)
        outcomes = runtime.recent_outcomes()
        outcomes[0]["detail"]["hacked"] = True
        self.assertNotIn("hacked", runtime.recent_outcomes()[0]["detail"])

    def test_recent_events_do_not_alias_world_history(self):
        state, scheduler, runtime = _rig()
        runtime.process_due(_due(scheduler))
        events = runtime.recent_events()
        events[-1]["payload"]["text"] = "改掉了"
        self.assertEqual(
            state.events.by_type(EventType.MESSAGE_SENT)[0].payload["text"], "在的哦"
        )


class SingleCoordinatorTests(unittest.TestCase):
    def test_a_session_binds_exactly_one_coordinator(self):
        state, scheduler, runtime = _rig()
        with self.assertRaises(AutonomyError):
            AutonomousRuntime(
                state, policy=AbstainPolicy(), auditor=ScriptedAuditor()
            )

    def test_a_second_policy_cannot_shadow_an_attached_engine(self):
        state = _session()
        PersistentScheduler(state)
        AgencyEngine(state, policy=AbstainPolicy())
        with self.assertRaises(AutonomyError):
            AutonomousRuntime(
                state, policy=AbstainPolicy(), auditor=ScriptedAuditor()
            )

    def test_it_reuses_the_services_already_bound_to_the_session(self):
        state = _session()
        scheduler = PersistentScheduler(state)
        encoder = MemoryEncoder(state)
        runtime = AutonomousRuntime(
            state,
            policy=AuthoredLinePolicy(
                ScriptedLineGenerator({"mizuki": "在的哦"}), recall=MemoryRecall(state)
            ),
            auditor=ScriptedAuditor(),
        )
        self.assertIs(runtime.scheduler, scheduler)
        self.assertIs(runtime.memory, encoder)


# ── AC9 研究会话路径不变 ────────────────────────────────────────────────
class SeparateRuntimePathTests(unittest.TestCase):
    def test_session_runtime_does_not_import_autonomy(self):
        tree = ast.parse(
            Path(session_runtime_mod.__file__).read_text(encoding="utf-8")
        )
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertEqual(
            [name for name in imported if "autonomy" in name],
            [],
            "研究会话的轮转路径不该依赖自主运行时",
        )

    def test_importing_autonomy_does_not_initialize_the_reload_boundary(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import pns.runtime.autonomy; "
                "assert 'pns.runtime.reload' not in sys.modules, "
                "'导入自主运行时顺带拉起了重载边界'; print('ok')",
            ],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_package_holds_no_live_module_level_state(self):
        instances = [
            name
            for name, value in vars(coordinator_mod).items()
            if isinstance(value, (AutonomousRuntime, SessionState))
        ]
        self.assertEqual(instances, [])

    def test_a_research_session_creates_no_coordinator(self):
        self.assertIsNone(SessionState("s", "gate", ["a", "b"]).autonomy)

    def test_a_coordinator_does_not_move_a_research_sessions_clock(self):
        state, scheduler, runtime = _rig()
        clock = state.world_state.clock
        runtime.process_pending()
        self.assertEqual(state.world_state.clock, clock)
        self.assertEqual(len(state.turns), 0)


class ResearchWebSocketCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    """把协调器挂在一条真实的 /ws/run 会话上，那条会话的输出必须逐条不变。

    这比"session_runtime 不 import autonomy"强一档：静态检查说明它们没有
    代码依赖，这里说明它们**共用同一份 SessionState 时也互不干扰** ——
    轮转顺序、消息序列、生成审计、世界时钟，一样都不许动。
    """

    async def _messages(self, *, with_coordinator: bool):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        history_dir = Path(tmp.name) / "history"
        drift_file = Path(tmp.name) / "drift.jsonl"
        drift_file.parent.mkdir(parents=True, exist_ok=True)
        drift_file.touch()

        async def fake_call(client, character, history, world, model, *a, **kw):
            return f"reply-from-{character}"

        async def fake_judge(client, character, message, turn, scene, **kw):
            return {
                "drift_score": 1,
                "is_ooc": False,
                "evaluator_provider": "test",
                "evaluator_model": "test-judge",
            }

        with patch(
            "pns.runtime.session_runtime.router_mod._get_api_key",
            return_value="test-key",
        ), patch(
            "pns.runtime.session_runtime.router_mod.create_client",
            return_value=object(),
        ):
            runtime = SessionRuntime.create(
                {"characters": ["mizuki", "ena"], "max_turns": 2, "api_delay": 0},
                supervisor=SessionSupervisor(),
                history_dir=history_dir,
                drift_scores_file=drift_file,
            )
            coordinator = None
            if with_coordinator:
                coordinator = AutonomousRuntime(
                    runtime.state,
                    policy=AuthoredLinePolicy(
                        ScriptedLineGenerator({"mizuki": "自主的一句"}),
                        recall=MemoryRecall(runtime.state),
                    ),
                    auditor=ScriptedAuditor(),
                )
                coordinator.start()
            with patch(
                "pns.runtime.session_runtime.call_character_async", fake_call
            ), patch("pns.runtime.session_runtime.judge_async", fake_judge):
                messages = [message async for message in runtime.run()]
        return runtime, coordinator, messages

    @staticmethod
    def _stable(messages):
        """抹掉每次运行都会变的东西（会话 ID、归档路径）。

        会话 ID 还会嵌在 event_id 里，所以按整段文本替换，而不是只删字段。
        """
        session_id = messages[0]["session_id"]
        blob = json.dumps(messages, ensure_ascii=False).replace(session_id, "<sid>")
        stripped = []
        for item in json.loads(blob):
            item.pop("history_file", None)
            stripped.append(item)
        return stripped

    async def test_attaching_a_coordinator_changes_no_websocket_message(self):
        _, _, plain = await self._messages(with_coordinator=False)
        runtime, coordinator, wired = await self._messages(with_coordinator=True)
        self.assertEqual(self._stable(wired), self._stable(plain))
        # 协调器一条也没插手：轮转记录、世界时钟、Agency 日志都干净。
        self.assertEqual(len(runtime.state.turns), 2)
        self.assertEqual(len(runtime.state.agency), 0)
        self.assertEqual(len(runtime.state.memories), 0)
        self.assertTrue(coordinator.running)

    async def test_the_coordinator_still_works_on_that_same_session(self):
        runtime, coordinator, _ = await self._messages(with_coordinator=True)
        scheduler = coordinator.scheduler
        before_turns = len(runtime.state.turns)
        report = coordinator.advance(10)
        self.assertEqual(report["results"], [])
        # 排一条激活再推进，这次真的会动。
        scheduler.schedule(
            ScheduledActivation(
                activation_id="wake",
                kind=ActivationKind.CHARACTER_ACTIVATION,
                due_at=scheduler.clock + timedelta(minutes=10),
                character_id="mizuki",
            )
        )
        report = coordinator.advance(10)
        self.assertEqual(len(report["results"]), 1)
        self.assertEqual(report["results"][0]["outcome"], "acted")
        # 自主路径不往生成审计里写东西。
        self.assertEqual(len(runtime.state.turns), before_turns)


if __name__ == "__main__":
    unittest.main()
