# tests/test_planning_gate.py — PLAN-1：Planner 插槽、前置门与 resting ⇒ ASLEEP
#
# 盯住的东西按审查编号排（kickoff/PLAN_1_*，设计审查 D1–D4 与二次确认）：
#   D1  睡眠优先于一切刺激：睡着的人同屋有人、睡前有观察，也一次都不想。
#   D2  QUIET 只能由引擎签发：手拼的、replace() 出来的、提交那一刻门条件刚好
#       成立的同形计划一律拒绝；签发检查先于"认知不可用"的改判；签发一次只能
#       用一次，失败之后重新 propose() 重新签发。
#   D2-R1  协调器对 QUIET 有自己的终局结局，返回值、日志、投递箱、额度一致。
#   D3  availability_of() 从 resting 推导 ASLEEP；存下的 ASLEEP 是独立的锁。
#   D4  观察游标：自言自语不刷新冷却，别人的话放行，存档往返结果相同。
#
# 运行: python -m unittest tests.test_planning_gate -v
import dataclasses
import json
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from grants_support import grant_everything
from pns.models.activation import ActivationKind, ScheduledActivation
from pns.models.agency import AgencyBudget, AgencyError, AgencyOutcome, AgencyRecord
from pns.models.cognition import CognitionTimeline, consumes_allowance
from pns.models.event import Event, EventScope, EventType
from pns.models.exposure import ExposureReason
from pns.models.session import SessionState, SessionStateError
from pns.models.world_state import ActivityKind, Availability, WorldState
from pns.runtime.agency.engine import AgencyEngine, AgencyEngineError, ProposalPlan
from pns.runtime.agency.planning import (
    PlanningReason,
    PlanProposal,
    build_planning_context,
    plan_activation,
)
from pns.runtime.agency.policy import AbstainPolicy
from pns.runtime.agency.preconditions import legal_actions
from pns.runtime.autonomy.audit import ScriptedAuditor
from pns.runtime.autonomy.coordinator import AutonomousRuntime
from pns.runtime.autonomy.generation import AuthoredLinePolicy, ScriptedLineGenerator
from pns.runtime.autonomy.outcome import ActivationOutcome, OutcomeError, outcome_for
from pns.runtime.event_commit import commit_session_event
from pns.runtime.exposure import evaluate_exposure
from pns.runtime.formal_world import YOAKE_MAE, formal_session_state
from pns.runtime.memory.recall import MemoryRecall
from pns.runtime.reload import BOUNDARY
from pns.runtime.scheduler import PersistentScheduler
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph

CLOCK = datetime(2026, 10, 1, 22, 0)
ROOM = "mizuki_home_room"
COOLDOWN = 60


def _world(*, join_nightcord=()):
    """瑞希一个人在自己房间；绘名在自己家。默认谁都不在频道里：瑞希是独处的。"""
    world = WorldState(
        clock=CLOCK,
        locations=build_default_location_graph(),
        channels=build_default_channel_registry(),
    )
    grant_everything(world)
    world.place_character("mizuki", ROOM)
    world.place_character("ena", "ena_home_studio")
    for character_id in join_nightcord:
        world.join_channel(character_id, "nightcord")
    return world


def _session(world=None):
    state = SessionState(session_id="s1", scene="gate", characters=["mizuki", "ena"])
    state.attach_world_state(world if world is not None else _world())
    state.initialize_runtime("开场")
    return state


class _CountingAbstain(AbstainPolicy):
    """确定性弃权，并记下被问了几次。弃权记录是计费认知（PLAN-1 §9 D4）。"""

    def __init__(self):
        super().__init__()
        self.calls = 0

    def decide(self, context):
        self.calls += 1
        return super().decide(context)


def _engine_rig(*, cooldown=COOLDOWN, world=None):
    state = _session(world)
    scheduler = PersistentScheduler(state)
    policy = _CountingAbstain()
    engine = AgencyEngine(
        state, policy=policy, budget=AgencyBudget(quiet_cooldown_minutes=cooldown)
    )
    return state, scheduler, engine, policy


def _schedule(scheduler, activation_id, *, at, character_id="mizuki"):
    scheduler.schedule(
        ScheduledActivation(
            activation_id=activation_id,
            kind=ActivationKind.CHARACTER_ACTIVATION,
            due_at=at,
            character_id=character_id,
        )
    )


def _at(minute):
    """第一条认知在 CLOCK 后一分钟；minute 从那一刻算起（到期必须晚于开局时钟）。"""
    return CLOCK + timedelta(minutes=minute + 1)


def _due_at(scheduler, activation_id, minute, *, character_id="mizuki"):
    """排一条在 _at(minute) 到期的普通激活，推到那一刻，返回到期记录。"""
    at = _at(minute)
    _schedule(scheduler, activation_id, at=at, character_id=character_id)
    tick = scheduler.advance_by(int((at - scheduler.clock).total_seconds() // 60))
    [due] = [due for due in tick.due if due.activation_id == activation_id]
    return due


def _say(state, actor, text, event_id, location_id=ROOM):
    world = state.world_state
    return commit_session_event(
        state,
        Event(
            event_id=event_id,
            type=EventType.DIALOGUE_SPOKEN,
            occurred_at=world.clock,
            scope=EventScope.LOCATION,
            actor_id=actor,
            participants=tuple(world.characters_at(location_id)),
            location_id=location_id,
            payload={"text": text, "char_name": actor},
        ),
    )


def _set_activity(state, character_id, activity, event_id):
    """经提交边界改活动（跟作息、HTTP 改活动同一条路）。"""
    return commit_session_event(
        state,
        Event(
            event_id=event_id,
            type=EventType.CHARACTER_ACTIVITY_CHANGED,
            occurred_at=state.world_state.clock,
            scope=EventScope.PRIVATE,
            actor_id=character_id,
            payload={"activity": ActivityKind(activity).value},
        ),
    )


def _other_says_and_leaves(state, text, event_id):
    """绘名同一分钟里进瑞希房间说一句、再离开：瑞希留下一条外部观察，身边没人。"""
    world = state.world_state
    world.place_character("ena", ROOM)
    _say(state, "ena", text, event_id)
    world.place_character("ena", "ena_home_studio")


def _fingerprint(state):
    return {
        "world": state.world_state.to_dict(),
        "events": [event.event_id for event in state.events],
        "observations": len(state.observations),
        "outbox": state.activation_outbox.to_dict(),
        "agency": state.agency.to_dict(),
    }


def _charged(state):
    return sum(1 for record in state.agency.records() if consumes_allowance(record))


# ── 判定次序（纯函数） ─────────────────────────────────────────────────
class PlanActivationOrderTests(unittest.TestCase):
    def _context(self, **overrides):
        state = _session()
        scheduler = PersistentScheduler(state)
        engine = AgencyEngine(state, policy=AbstainPolicy())
        due = _due_at(scheduler, "probe", 1)
        values = dict(
            reply=False,
            observation_cursor=0,
            observations_since_charged=(),
            last_charged_at=None,
        )
        values.update(overrides)
        return build_planning_context(engine.context_for(due), **values)

    def test_no_gate_never_closes(self):
        context = dataclasses.replace(self._context(), availability="asleep")
        self.assertIs(plan_activation(context, None).reason, PlanningReason.NO_GATE)

    def test_sleep_wins_over_company_and_new_observations(self):
        context = dataclasses.replace(
            self._context(),
            availability="asleep",
            company=("ena",),
            new_external_observations=3,
        )
        result = plan_activation(context, timedelta(minutes=COOLDOWN))
        self.assertIs(result.reason, PlanningReason.ASLEEP)
        self.assertTrue(result.quiet)

    def test_first_cognition_is_let_through(self):
        context = self._context()
        self.assertIs(
            plan_activation(context, timedelta(minutes=COOLDOWN)).reason,
            PlanningReason.FIRST_COGNITION,
        )

    def test_reply_is_never_gated_here(self):
        context = dataclasses.replace(self._context(reply=True), availability="asleep")
        self.assertIs(
            plan_activation(context, timedelta(minutes=COOLDOWN)).reason,
            PlanningReason.REPLY,
        )


# ── D1 睡眠优先 ─────────────────────────────────────────────────────────
class SleepFirstTests(unittest.TestCase):
    def test_asleep_with_company_and_old_observation_costs_nothing(self):
        state, scheduler, engine, policy = _engine_rig()
        world = state.world_state
        # 睡前：绘名在屋里说了一句，瑞希听见了（一条可渲染的外部观察）。
        world.place_character("ena", ROOM)
        _say(state, "ena", "还不睡吗", "before-sleep")
        self.assertTrue(state.observations.find("mizuki", "before-sleep"))
        _set_activity(state, "mizuki", ActivityKind.RESTING, "sleep")
        self.assertIs(world.availability_of("mizuki"), Availability.ASLEEP)

        due = _due_at(scheduler, "night", 1)
        record = engine.commit(engine.propose(due))

        self.assertIs(record.outcome, AgencyOutcome.QUIET)
        self.assertEqual(record.detail["reason"], "asleep")
        self.assertEqual(policy.calls, 0)
        self.assertFalse(consumes_allowance(record))
        self.assertTrue(state.activation_outbox.is_acknowledged(due.due_id))

    def test_after_waking_in_the_same_minute_the_awake_rules_apply(self):
        state, scheduler, engine, policy = _engine_rig()
        world = state.world_state
        world.place_character("ena", ROOM)
        _set_activity(state, "mizuki", ActivityKind.RESTING, "sleep")
        at = _at(0)
        _schedule(scheduler, "before-wake", at=at)
        _schedule(scheduler, "after-wake", at=at)
        first, second = scheduler.advance_by(1).due

        self.assertIs(engine.commit(engine.propose(first)).outcome, AgencyOutcome.QUIET)
        _set_activity(state, "mizuki", ActivityKind.IDLE, "wake")
        record = engine.commit(engine.propose(second))

        self.assertIs(record.outcome, AgencyOutcome.ABSTAINED)
        self.assertEqual(record.detail["planning"]["reason"], "company")
        self.assertEqual(policy.calls, 1)


# ── D4 观察游标与冷却 ───────────────────────────────────────────────────
class CooldownTests(unittest.TestCase):
    def _first_cognition_then_self_talk(self):
        state, scheduler, engine, policy = _engine_rig()
        first = engine.commit(engine.propose(_due_at(scheduler, "t0", 0)))
        self.assertIs(first.outcome, AgencyOutcome.ABSTAINED)
        self.assertEqual(first.detail["planning"]["reason"], "first_cognition")
        # 自言自语：她自己的话，自观察落在游标之后，但不算外部刺激。
        _say(state, "mizuki", "好安静啊", "self-talk")
        self.assertTrue(
            state.observations.find("mizuki", "self-talk").is_self_observation
        )
        return state, scheduler, engine, policy

    def test_self_talk_does_not_reopen_the_gate(self):
        state, scheduler, engine, policy = self._first_cognition_then_self_talk()
        for minute in (15, 30, 45):
            record = engine.commit(engine.propose(_due_at(scheduler, f"t{minute}", minute)))
            self.assertIs(record.outcome, AgencyOutcome.QUIET, minute)
            self.assertEqual(record.detail["reason"], "cooldown")
            self.assertFalse(consumes_allowance(record))
        self.assertEqual(policy.calls, 1)
        self.assertEqual(_charged(state), 1)

        record = engine.commit(engine.propose(_due_at(scheduler, "t60", 60)))
        self.assertIs(record.outcome, AgencyOutcome.ABSTAINED)
        self.assertEqual(record.detail["planning"]["reason"], "cooldown_elapsed")
        self.assertEqual(policy.calls, 2)

    def test_someone_elses_line_in_the_same_minute_lets_it_through(self):
        state, scheduler, engine, policy = self._first_cognition_then_self_talk()
        engine.commit(engine.propose(_due_at(scheduler, "t15", 15)))
        at = _at(30)
        _schedule(scheduler, "t30", at=at)
        scheduler.advance_by(15)
        _other_says_and_leaves(state, "瑞希？", "ena-line")
        [due] = engine.pending_due()

        record = engine.commit(engine.propose(due))
        self.assertIs(record.outcome, AgencyOutcome.ABSTAINED)
        self.assertEqual(record.detail["planning"]["reason"], "new_observation")

    def test_a_line_heard_later_in_the_cognitions_own_minute_is_new(self):
        # 分钟精度分不出"先想、后听见"：游标按日志位置，不按 observed_at。
        state, scheduler, engine, _ = _engine_rig()
        engine.commit(engine.propose(_due_at(scheduler, "t0", 0)))
        _other_says_and_leaves(state, "刚才那句再说一遍？", "same-minute")
        record = engine.commit(engine.propose(_due_at(scheduler, "t15", 15)))
        self.assertIs(record.outcome, AgencyOutcome.ABSTAINED)
        self.assertEqual(record.detail["planning"]["reason"], "new_observation")

    def test_quiet_does_not_move_the_cursor_or_the_cooldown(self):
        state, scheduler, engine, _ = self._first_cognition_then_self_talk()
        before = engine._last_charged("mizuki")
        engine.commit(engine.propose(_due_at(scheduler, "t15", 15)))
        engine.commit(engine.propose(_due_at(scheduler, "t30", 30)))
        self.assertIs(engine._last_charged("mizuki"), before)

    def test_results_are_the_same_after_an_archive_round_trip(self):
        def run(round_trip):
            state, scheduler, engine, _ = self._first_cognition_then_self_talk()
            engine.commit(engine.propose(_due_at(scheduler, "t15", 15)))
            if round_trip:
                state = SessionState.from_dict(deepcopy(state.to_dict()))
                scheduler = PersistentScheduler(state)
                engine = AgencyEngine(
                    state,
                    policy=_CountingAbstain(),
                    budget=AgencyBudget(quiet_cooldown_minutes=COOLDOWN),
                )
            outcomes = []
            for minute in (30, 45):
                record = engine.commit(engine.propose(_due_at(scheduler, f"t{minute}", minute)))
                outcomes.append((record.outcome, dict(record.detail).get("reason")))
            at = _at(50)
            _schedule(scheduler, "t50", at=at)
            scheduler.advance_by(5)
            _other_says_and_leaves(state, "在吗", "ena-line")
            [due] = engine.pending_due()
            record = engine.commit(engine.propose(due))
            outcomes.append((record.outcome, record.detail["planning"]["reason"]))
            record = engine.commit(engine.propose(_due_at(scheduler, "t70", 70)))
            outcomes.append((record.outcome, dict(record.detail).get("reason")))
            return outcomes

        plain = run(False)
        self.assertEqual(
            plain,
            [
                (AgencyOutcome.QUIET, "cooldown"),
                (AgencyOutcome.QUIET, "cooldown"),
                (AgencyOutcome.ABSTAINED, "new_observation"),
                (AgencyOutcome.QUIET, "cooldown"),
            ],
        )
        self.assertEqual(run(True), plain)

    def test_records_from_before_the_cursor_count_everything_as_new(self):
        # 旧记录没有游标 → 按 0 读：全部观察都算新，只会多放行一次。
        state, scheduler, engine, policy = _engine_rig()
        _other_says_and_leaves(state, "早", "old-line")
        engine.commit(engine.propose(_due_at(scheduler, "t0", 0)))
        archive = state.to_dict()
        for entry in archive["agency"]["log"]["records"]:
            entry["detail"].pop("observation_cursor")
            entry["detail"].pop("planning")
        state = SessionState.from_dict(archive)
        scheduler = PersistentScheduler(state)
        engine = AgencyEngine(
            state, policy=AbstainPolicy(), budget=AgencyBudget(quiet_cooldown_minutes=COOLDOWN)
        )
        record = engine.commit(engine.propose(_due_at(scheduler, "t15", 15)))
        self.assertEqual(record.detail["planning"]["reason"], "new_observation")
        record = engine.commit(engine.propose(_due_at(scheduler, "t30", 30)))
        self.assertIs(record.outcome, AgencyOutcome.QUIET)


# ── D2 签发 ─────────────────────────────────────────────────────────────
def _forge(plan, reason="cooldown"):
    """把一份问过策略的计划改成 QUIET 的形状 —— 公开 dataclass 能做到的全部。"""
    return replace(
        plan,
        verdict=AgencyOutcome.QUIET,
        policy="",
        proposal=None,
        rationale="",
        detail={"reason": reason},
        planning=PlanProposal(
            character_id=plan.character_id,
            due_id=plan.due.due_id,
            reason=reason,
            observation_cursor=0,
        ),
    )


class IssuanceTests(unittest.TestCase):
    def test_reviewers_counterexample_is_refused(self):
        state, scheduler, engine, policy = _engine_rig()
        plan = engine.propose(_due_at(scheduler, "t0", 0))
        self.assertIs(plan.verdict, AgencyOutcome.ABSTAINED)
        self.assertEqual(policy.calls, 1)
        before = _fingerprint(state)
        with self.assertRaises(AgencyEngineError):
            engine.commit(_forge(plan))
        self.assertEqual(_fingerprint(state), before)

    def test_a_same_shape_forgery_is_refused_even_when_the_gate_now_agrees(self):
        state, scheduler, engine, policy = _engine_rig()
        engine.commit(engine.propose(_due_at(scheduler, "t0", 0)))
        world = state.world_state
        world.place_character("ena", ROOM)  # 有人在：策略会被问到
        due = _due_at(scheduler, "t15", 15)
        charged_plan = engine.propose(due)
        self.assertIs(charged_plan.verdict, AgencyOutcome.ABSTAINED)
        self.assertEqual(policy.calls, 2)
        # 同一分钟她离开了：冷却门条件此刻为真，重算会恰好通过。
        world.place_character("ena", "ena_home_studio")
        before = _fingerprint(state)
        with self.assertRaises(AgencyEngineError):
            engine.commit(_forge(charged_plan))
        self.assertEqual(_fingerprint(state), before)

        # 正式签发同一条 due 才能免费收尾。
        signed = engine.propose(due)
        self.assertIs(signed.verdict, AgencyOutcome.QUIET)
        record = engine.commit(signed)
        self.assertIs(record.outcome, AgencyOutcome.QUIET)
        self.assertFalse(consumes_allowance(record))
        self.assertEqual(policy.calls, 2)

    def test_a_hand_built_quiet_is_refused_and_the_engine_issued_one_is_not(self):
        state, scheduler, engine, _ = _engine_rig()
        engine.commit(engine.propose(_due_at(scheduler, "t0", 0)))
        due = _due_at(scheduler, "t15", 15)
        signed = engine.propose(due)
        self.assertIs(signed.verdict, AgencyOutcome.QUIET)
        copy = replace(signed)
        self.assertEqual(copy, signed)
        self.assertIsNot(copy, signed)
        with self.assertRaises(AgencyEngineError):
            engine.commit(copy)
        # 拿副本试过一次，签发条目已经取走；原件也不能再用。
        with self.assertRaises(AgencyEngineError):
            engine.commit(signed)
        record = engine.commit(engine.propose(due))
        self.assertIs(record.outcome, AgencyOutcome.QUIET)

    def test_unsigned_quiet_is_not_downgraded_to_free_unavailable(self):
        state, scheduler, engine, _ = _engine_rig()
        with state.atomic_commit():
            state.set_cognition(
                CognitionTimeline.open(log_length=len(state.agency), sim=CLOCK, wall="w")
            )
        _set_activity(state, "mizuki", ActivityKind.RESTING, "sleep")
        due = _due_at(scheduler, "t1", 1)
        self.assertTrue(engine.unavailable_causes_for(due))
        signed = engine.propose(due)
        self.assertIs(signed.verdict, AgencyOutcome.QUIET)
        before = _fingerprint(state)
        with self.assertRaises(AgencyEngineError):
            engine.commit(replace(signed))
        self.assertEqual(_fingerprint(state), before)
        # 签发过的 QUIET 遇上认知不可用：时间线是更高的权威，照旧记不可用。
        record = engine.commit(engine.propose(due))
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_UNAVAILABLE)

    def test_a_passed_plan_relabelled_quiet_is_not_downgraded_either(self):
        # 只有 QUIET 那一道检查挡得住：前置门结论是引擎发出的那一份、这条到期也
        # 没签过 QUIET，只把结论改成 QUIET。认知不可用时它不能落成免费的不可用记录。
        state, scheduler, engine, _ = _engine_rig()
        with state.atomic_commit():
            state.set_cognition(
                CognitionTimeline.open(log_length=len(state.agency), sim=CLOCK, wall="w")
            )
        state.world_state.place_character("ena", ROOM)
        due = _due_at(scheduler, "t1", 1)
        plan = engine.propose(due)
        self.assertIs(plan.verdict, AgencyOutcome.ABSTAINED)
        relabelled = replace(
            plan,
            verdict=AgencyOutcome.QUIET,
            policy="",
            rationale="",
            detail={"reason": "cooldown"},
        )
        before = _fingerprint(state)
        with self.assertRaises(AgencyEngineError):
            engine.commit(relabelled)
        self.assertEqual(_fingerprint(state), before)

    def test_a_failed_commit_takes_the_signature_and_re_proposing_works(self):
        state, scheduler, engine, _ = _engine_rig()
        engine.commit(engine.propose(_due_at(scheduler, "t0", 0)))
        due = _due_at(scheduler, "t15", 15)
        signed = engine.propose(due)
        before = _fingerprint(state)
        outbox = state.activation_outbox
        with patch.object(outbox, "_acknowledge", side_effect=RuntimeError("断电")):
            with self.assertRaises(RuntimeError):
                engine.commit(signed)
        self.assertEqual(_fingerprint(state), before)
        with self.assertRaises(AgencyEngineError):
            engine.commit(signed)
        record = engine.commit(engine.propose(due))
        self.assertIs(record.outcome, AgencyOutcome.QUIET)

    def test_a_signed_quiet_is_re_judged_inside_the_transaction(self):
        state, scheduler, engine, policy = _engine_rig()
        engine.commit(engine.propose(_due_at(scheduler, "t0", 0)))
        due = _due_at(scheduler, "t15", 15)
        signed = engine.propose(due)
        state.world_state.place_character("ena", ROOM)  # 签发之后有人来了
        before = _fingerprint(state)
        with self.assertRaises(AgencyEngineError):
            engine.commit(signed)
        self.assertEqual(_fingerprint(state), before)
        record = engine.commit(engine.propose(due))
        self.assertIs(record.outcome, AgencyOutcome.ABSTAINED)
        self.assertEqual(policy.calls, 2)

    def test_a_charged_plan_cannot_carry_a_hand_made_cursor(self):
        # 游标改大 = 记录声称这次认知读过之后的观察，下一次就看不见它们。
        state, scheduler, engine, _ = _engine_rig()
        _other_says_and_leaves(state, "早", "old-line")
        plan = engine.propose(_due_at(scheduler, "t0", 0))
        inflated = replace(
            plan,
            planning=replace(plan.planning, observation_cursor=len(state.observations)),
        )
        before = _fingerprint(state)
        with self.assertRaises(AgencyEngineError):
            engine.commit(inflated)
        self.assertEqual(_fingerprint(state), before)
        record = engine.commit(engine.propose(plan.due))
        self.assertIs(record.outcome, AgencyOutcome.ABSTAINED)

    def test_a_signed_quiet_cannot_be_turned_into_a_charged_abstention(self):
        # 实现审查 F1：签过 QUIET 的到期，改成别的结论提交 = 把"没想"记成"想过"。
        state, scheduler, engine, policy = _engine_rig()
        _set_activity(state, "mizuki", ActivityKind.RESTING, "sleep")
        due = _due_at(scheduler, "t1", 1)
        for forged in (
            dict(planning=None),  # 退回旧调用形状
            dict(),  # 保留签发出去的那份前置门结论
        ):
            signed = engine.propose(due)
            self.assertIs(signed.verdict, AgencyOutcome.QUIET)
            relabelled = replace(
                signed,
                verdict=AgencyOutcome.ABSTAINED,
                policy="abstain",
                detail={},
                **forged,
            )
            before = _fingerprint(state)
            with self.assertRaises(AgencyEngineError, msg=forged):
                engine.commit(relabelled)
            self.assertEqual(_fingerprint(state), before)
        self.assertEqual(policy.calls, 0)
        record = engine.commit(engine.propose(due))
        self.assertIs(record.outcome, AgencyOutcome.QUIET)
        self.assertFalse(consumes_allowance(record))

    def test_a_charged_plan_cannot_drop_its_planning(self):
        # 实现审查 F1：没有游标的计费记录，下一次会把旧观察又算成新刺激。
        state, scheduler, engine, policy = _engine_rig()
        _other_says_and_leaves(state, "早", "old-line")
        plan = engine.propose(_due_at(scheduler, "t0", 0))
        before = _fingerprint(state)
        with self.assertRaises(AgencyEngineError):
            engine.commit(replace(plan, planning=None))
        self.assertEqual(_fingerprint(state), before)
        engine.commit(plan)  # 原件仍认得
        record = engine.commit(engine.propose(_due_at(scheduler, "t15", 15)))
        self.assertIs(record.outcome, AgencyOutcome.QUIET)
        self.assertEqual(record.detail["reason"], "cooldown")
        self.assertEqual(policy.calls, 1)

    def test_a_failed_quiet_cannot_be_relabelled_with_its_kept_planning(self):
        # 复核 R2-F1：签发失败之后结论还留着，但它只能以 QUIET 结案。
        state, scheduler, engine, policy = _engine_rig()
        _set_activity(state, "mizuki", ActivityKind.RESTING, "sleep")
        due = _due_at(scheduler, "t1", 1)
        signed = engine.propose(due)
        self.assertIs(signed.verdict, AgencyOutcome.QUIET)
        _set_activity(state, "mizuki", ActivityKind.IDLE, "wake")
        with self.assertRaises(AgencyEngineError):
            engine.commit(signed)  # 事务内重判：门已经变了
        _set_activity(state, "mizuki", ActivityKind.RESTING, "sleep-again")
        relabelled = replace(
            signed, verdict=AgencyOutcome.ABSTAINED, policy="abstain", detail={}
        )
        before = _fingerprint(state)
        with self.assertRaises(AgencyEngineError):
            engine.commit(relabelled)
        self.assertEqual(_fingerprint(state), before)
        self.assertEqual(policy.calls, 0)
        record = engine.commit(engine.propose(due))
        self.assertIs(record.outcome, AgencyOutcome.QUIET)

    def test_a_never_proposed_plan_cannot_skip_the_gate(self):
        # 开了前置门：没经过 propose() 的计划不带游标，旧观察下次会被当成新的。
        state, scheduler, engine, _ = _engine_rig()
        due = _due_at(scheduler, "t0", 0)
        hand_built = ProposalPlan(
            due=due,
            character_id="mizuki",
            policy="abstain",
            proposed_at=state.world_state.clock,
            verdict=AgencyOutcome.ABSTAINED,
        )
        before = _fingerprint(state)
        with self.assertRaises(AgencyEngineError):
            engine.commit(hand_built)
        self.assertEqual(_fingerprint(state), before)

    def test_the_unknown_character_exemption_cannot_erase_an_issued_cursor(self):
        # 复核 R2-F2：例外看"发没发过结论"，不看提交那一刻角色在不在。
        state, scheduler, engine, _ = _engine_rig()
        _other_says_and_leaves(state, "早", "old-line")
        plan = engine.propose(_due_at(scheduler, "t0", 0))
        self.assertIsNotNone(plan.planning)
        state.world_state.remove_character("mizuki")
        before = _fingerprint(state)
        with self.assertRaises(AgencyEngineError):
            engine.commit(replace(plan, planning=None))
        self.assertEqual(_fingerprint(state), before)

    def test_unknown_character_after_a_proposal_keeps_the_cursor(self):
        # 复核 R3-F2（此前已提案）：带着那份结论计费结案，旧观察不会再算新。
        state, scheduler, engine, policy = _engine_rig()
        _other_says_and_leaves(state, "早", "old-line")
        due = _due_at(scheduler, "t0", 0)
        first = engine.propose(due)
        self.assertEqual(first.planning.reason, PlanningReason.NEW_OBSERVATION)
        state.world_state.remove_character("mizuki")
        again = engine.propose(due)
        self.assertIs(again.planning, first.planning)
        record = engine.commit(again)
        self.assertEqual(record.detail["reason"], "unknown_character")
        self.assertIn("observation_cursor", record.detail)
        self.assertTrue(consumes_allowance(record))
        # 改成"从没建出上下文"的免费形状：不是签发的那一份。
        state.world_state.place_character("mizuki", ROOM)
        record = engine.commit(engine.propose(_due_at(scheduler, "t15", 15)))
        self.assertIs(record.outcome, AgencyOutcome.QUIET)
        self.assertEqual(record.detail["reason"], "cooldown")
        self.assertEqual(policy.calls, 1)

    def test_unknown_character_without_a_context_is_free_and_issued(self):
        # 复核 R3-F2（从没建出上下文）：没问策略，不计费；形状不能自证。
        state, scheduler, engine, policy = _engine_rig()
        due = _due_at(scheduler, "t0", 0)
        state.world_state.remove_character("mizuki")  # 到期时人已经不在：建不出上下文
        plan = engine.propose(due)
        self.assertEqual(plan.policy, "")
        before = _fingerprint(state)
        with self.assertRaises(AgencyEngineError):
            engine.commit(replace(plan))
        self.assertEqual(_fingerprint(state), before)
        record = engine.commit(engine.propose(due))
        self.assertEqual(record.detail["reason"], "unknown_character")
        self.assertFalse(consumes_allowance(record))
        self.assertEqual(policy.calls, 0)

    def test_a_free_closure_never_counts_as_having_asked_the_policy(self):
        # 签过一份免费收尾（这里是 QUIET）又没提交成；角色随后不在世界里：
        # 这条到期从没问过策略，未知角色的收尾也必须免费。
        state, scheduler, engine, policy = _engine_rig()
        _set_activity(state, "mizuki", ActivityKind.RESTING, "sleep")
        due = _due_at(scheduler, "t1", 1)
        self.assertIs(engine.propose(due).verdict, AgencyOutcome.QUIET)
        state.world_state.remove_character("mizuki")
        record = engine.commit(engine.propose(due))
        self.assertEqual(record.detail["reason"], "unknown_character")
        self.assertFalse(consumes_allowance(record))
        self.assertEqual(policy.calls, 0)

    def test_without_a_gate_a_free_closure_still_cannot_change_verdict(self):
        # 没开前置门的引擎（研究会话）也签发免费收尾；签过之后改成计费结论照样拒绝。
        state, scheduler, engine, policy = _engine_rig(cooldown=None)
        due = _due_at(scheduler, "t0", 0)
        state.world_state.remove_character("mizuki")
        free = engine.propose(due)
        self.assertTrue(engine.is_issued(free))
        with self.assertRaises(AgencyEngineError):
            engine.commit(replace(free, verdict=AgencyOutcome.ABSTAINED, policy="abstain", detail={}))
        self.assertEqual(policy.calls, 0)

    def test_a_charged_unknown_cannot_pose_as_the_free_one(self):
        state, scheduler, engine, _ = _engine_rig()
        due = _due_at(scheduler, "t0", 0)
        engine.propose(due)
        state.world_state.remove_character("mizuki")
        charged = engine.propose(due)
        self.assertNotEqual(charged.policy, "")
        with self.assertRaises(AgencyEngineError):
            engine.commit(replace(charged, policy="", planning=None))

    def test_without_a_gate_no_quiet_can_be_committed(self):
        state, scheduler, engine, policy = _engine_rig(cooldown=None)
        _set_activity(state, "mizuki", ActivityKind.RESTING, "sleep")
        plan = engine.propose(_due_at(scheduler, "t1", 1))
        self.assertIsNot(plan.verdict, AgencyOutcome.QUIET)
        self.assertEqual(policy.calls, 1)
        with self.assertRaises(AgencyEngineError):
            engine.commit(_forge(plan, "asleep"))


# ── D2-R1 协调器结局 ────────────────────────────────────────────────────
class _CountingAuditor(ScriptedAuditor):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def audit(self, request):
        self.calls += 1
        return super().audit(request)


class CoordinatorQuietTests(unittest.TestCase):
    def setUp(self):
        self.state = _session()
        self.scheduler = PersistentScheduler(self.state)
        self.generations = 0

        def speak(context):
            self.generations += 1
            return "好安静啊"

        self.policy = AuthoredLinePolicy(
            ScriptedLineGenerator({"mizuki": speak, "ena": speak}),
            recall=MemoryRecall(self.state),
        )
        self.decisions = 0
        decide = self.policy.decide

        def counting(context):
            self.decisions += 1
            return decide(context)

        self.policy.decide = counting
        self.auditor = _CountingAuditor()
        self.runtime = AutonomousRuntime(
            self.state,
            policy=self.policy,
            auditor=self.auditor,
            budget=AgencyBudget(quiet_cooldown_minutes=COOLDOWN),
        )
        self.runtime.start()

    def _counts(self):
        # 时钟推进本身会写 world.time_advanced；这里只数世界里"发生了事"的那些。
        spoken = [
            event
            for event in self.state.events
            if event.type is not EventType.WORLD_TIME_ADVANCED
        ]
        return (self.decisions, self.generations, self.auditor.calls, len(spoken))

    def test_self_talk_then_quiet_cooldown_through_process_due(self):
        first = self.runtime.process_due(_due_at(self.scheduler, "t0", 0))
        self.assertIs(first.outcome, ActivationOutcome.ACTED)  # 独处时自言自语
        counts = self._counts()
        for minute in (15, 30, 45):
            due = _due_at(self.scheduler, f"t{minute}", minute)
            result = self.runtime.process_due(due)
            self.assertIs(result.outcome, ActivationOutcome.QUIET, minute)
            self.assertTrue(result.terminal)
            self.assertIs(result.agency_outcome, AgencyOutcome.QUIET)
            self.assertIsNone(result.event_id)
            self.assertEqual(result.detail["reason"], "cooldown")
            json.dumps(result.to_dict())
            record = self.state.agency.get(due.due_id)
            self.assertIs(record.outcome, AgencyOutcome.QUIET)
            self.assertFalse(consumes_allowance(record))
            self.assertTrue(self.state.activation_outbox.is_acknowledged(due.due_id))
            self.assertEqual(self._counts(), counts)
            with self.assertRaises(AgencyEngineError):
                self.runtime.process_due(due)  # 已确认：不出现"报错但已处理"之外的第二个结局
        self.assertEqual(_charged(self.state), 1)
        status = self.runtime.status()
        self.assertEqual(status["outcomes"]["quiet"], 3)
        self.assertEqual(status["agency_outcomes"]["quiet"], 3)

        later = self.runtime.process_due(_due_at(self.scheduler, "t60", 60))
        self.assertIs(later.outcome, ActivationOutcome.ACTED)

    def test_asleep_through_process_due(self):
        with self.state.atomic_commit():
            _set_activity(self.state, "mizuki", ActivityKind.RESTING, "sleep")
        due = _due_at(self.scheduler, "t1", 1)
        result = self.runtime.process_due(due)
        self.assertIs(result.outcome, ActivationOutcome.QUIET)
        self.assertEqual(result.detail["reason"], "asleep")
        self.assertEqual(self._counts()[:3], (0, 0, 0))
        self.assertFalse(consumes_allowance(self.state.agency.get(due.due_id)))

    def test_the_terminal_failure_record_still_recognises_the_planning(self):
        # 重试用完时，协调器拿同一份前置门结论再提交一条终局失败记录。
        runtime = self.runtime
        runtime._retry = type(runtime._retry)(max_attempts=1)
        outbox = self.state.activation_outbox
        original = outbox._acknowledge
        calls = {"n": 0}

        def flaky(due_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("提交途中断电")
            return original(due_id)

        due = _due_at(self.scheduler, "t0", 0)
        with patch.object(outbox, "_acknowledge", side_effect=flaky):
            result = runtime.process_due(due)
        self.assertIs(result.outcome, ActivationOutcome.FAILED_TERMINAL)
        record = self.state.agency.get(due.due_id)
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_POLICY_ERROR)
        self.assertIn("observation_cursor", record.detail)

    def _flaky_ack(self, *, on_failure=None):
        outbox = self.state.activation_outbox
        original = outbox._acknowledge
        calls = {"n": 0}

        def flaky(due_id):
            calls["n"] += 1
            if calls["n"] == 1:
                if on_failure is not None:
                    on_failure()
                raise RuntimeError("提交途中断电")
            return original(due_id)

        return patch.object(outbox, "_acknowledge", side_effect=flaky)

    def test_a_quiet_that_fails_to_commit_ends_quiet_not_charged(self):
        # 复核 R2-F1：终局兜底不能把没发生的认知记成计费的失败。
        self.runtime.process_due(_due_at(self.scheduler, "t0", 0))  # 计费的一句
        self.runtime._retry = type(self.runtime._retry)(max_attempts=1)
        counts = self._counts()
        due = _due_at(self.scheduler, "t15", 15)
        with self._flaky_ack():
            result = self.runtime.process_due(due)
        self.assertIs(result.outcome, ActivationOutcome.QUIET)
        self.assertTrue(result.terminal)
        record = self.state.agency.get(due.due_id)
        self.assertIs(record.outcome, AgencyOutcome.QUIET)
        self.assertFalse(consumes_allowance(record))
        self.assertTrue(self.state.activation_outbox.is_acknowledged(due.due_id))
        self.assertEqual(self._counts(), counts)
        self.assertEqual(_charged(self.state), 1)

    def test_a_failed_quiet_whose_gate_opened_stays_pending(self):
        self.runtime.process_due(_due_at(self.scheduler, "t0", 0))
        self.runtime._retry = type(self.runtime._retry)(max_attempts=1)
        due = _due_at(self.scheduler, "t15", 15)
        world = self.state.world_state
        agency = self.runtime.agency
        original_quiet_plan = agency.closure_plan
        calls = {"n": 0}

        def closure_plan(d):
            calls["n"] += 1
            if calls["n"] == 2:
                # 提交失败之后、兜底之前有人来了：门不再收尾。
                world.place_character("ena", ROOM)
            return original_quiet_plan(d)

        with self._flaky_ack(), patch.object(agency, "closure_plan", side_effect=closure_plan):
            result = self.runtime.process_due(due)
        self.assertEqual(calls["n"], 2)
        self.assertIs(result.outcome, ActivationOutcome.FAILED_RETRYABLE)
        self.assertFalse(self.state.agency.has(due.due_id))
        self.assertFalse(self.state.activation_outbox.is_acknowledged(due.due_id))
        self.assertEqual(_charged(self.state), 1)

    def test_a_stuck_quiet_is_reported_as_still_pending(self):
        # 复核 R3-F3：兜底也没写成 = 没有记录、没有确认，不能报 terminal。
        self.runtime.process_due(_due_at(self.scheduler, "t0", 0))
        self.runtime._retry = type(self.runtime._retry)(max_attempts=1)
        due = _due_at(self.scheduler, "t15", 15)
        outbox = self.state.activation_outbox
        with patch.object(outbox, "_acknowledge", side_effect=RuntimeError("一直断电")):
            result = self.runtime.process_due(due)
        self.assertIs(result.outcome, ActivationOutcome.FAILED_RETRYABLE)
        self.assertFalse(result.terminal)
        self.assertTrue(result.detail["still_pending"])
        self.assertFalse(self.state.agency.has(due.due_id))
        self.assertFalse(outbox.is_acknowledged(due.due_id))

    def test_the_outcome_map_covers_quiet(self):
        self.assertIs(outcome_for(AgencyOutcome.QUIET), ActivationOutcome.QUIET)
        self.assertTrue(ActivationOutcome.QUIET.terminal)
        self.assertFalse(ActivationOutcome.QUIET.committed)
        for outcome in AgencyOutcome:
            outcome_for(outcome)  # 新结论码漏映射就在这里响亮失败

    def test_a_missing_mapping_would_fail_after_the_commit(self):
        # 变异的形状：表里没有 QUIET 时，process_due 在交接确认之后才炸。
        from pns.runtime.autonomy import outcome as outcome_mod

        trimmed = {
            key: value
            for key, value in outcome_mod._FROM_AGENCY.items()
            if key is not AgencyOutcome.QUIET
        }
        with self.state.atomic_commit():
            _set_activity(self.state, "mizuki", ActivityKind.RESTING, "sleep")
        due = _due_at(self.scheduler, "t1", 1)
        with patch.dict(outcome_mod._FROM_AGENCY, trimmed, clear=True):
            with self.assertRaises(OutcomeError):
                self.runtime.process_due(due)
        self.assertTrue(self.state.activation_outbox.is_acknowledged(due.due_id))


class ReplyClosureTests(unittest.TestCase):
    """复核 R3-F1 / R3-F4：失效回话是免费收尾，同样只能由引擎签发。"""

    def setUp(self):
        world = _world()
        world.place_character("ena", ROOM)
        self.state = _session(world)
        self.scheduler = PersistentScheduler(self.state)
        self.generations = 0

        def speak(context):
            self.generations += 1
            return "嗯"

        self.runtime = AutonomousRuntime(
            self.state,
            policy=AuthoredLinePolicy(
                ScriptedLineGenerator({"mizuki": speak, "ena": speak}),
                recall=MemoryRecall(self.state),
            ),
            auditor=ScriptedAuditor(),
            budget=AgencyBudget(quiet_cooldown_minutes=COOLDOWN, reply_delay_minutes=1),
        )
        self.runtime.start()
        spoken = self.runtime.process_due(_due_at(self.scheduler, "t0", 0, character_id="mizuki"))
        self.assertIs(spoken.outcome, ActivationOutcome.ACTED)
        [self.reply] = [
            due
            for due in self.scheduler.advance_by(1).due
            if due.activation_id.startswith("reply.activation:ena:")
        ]

    def test_a_live_reply_cannot_be_relabelled_as_lapsed(self):
        engine = self.runtime.agency
        plan = engine.propose(self.reply)
        self.assertIs(plan.verdict, AgencyOutcome.ACTED)
        self.assertEqual(self.generations, 2)  # 瑞希一句 + 绘名这次
        forged = replace(
            plan,
            verdict=AgencyOutcome.REJECTED_STALE,
            policy="",
            proposal=None,
            rationale="",
            detail={"reason": "reply_lapsed", "why": "left_the_conversation"},
        )
        before = _fingerprint(self.state)
        with self.assertRaises(AgencyEngineError):
            engine.commit(forged)
        self.assertEqual(_fingerprint(self.state), before)

    def test_a_lapsed_reply_cannot_be_relabelled_as_charged(self):
        self.state.world_state.place_character("ena", "ena_home_studio")
        engine = self.runtime.agency
        lapsed = engine.propose(self.reply)
        self.assertTrue(engine.is_issued(lapsed))
        with self.assertRaises(AgencyEngineError):
            engine.commit(replace(lapsed, verdict=AgencyOutcome.ABSTAINED, policy="x", detail={}))
        record = engine.commit(engine.propose(self.reply))
        self.assertEqual(record.detail["reason"], "reply_lapsed")
        self.assertFalse(consumes_allowance(record))

    def test_a_lapsed_reply_that_fails_to_commit_stays_free(self):
        self.state.world_state.place_character("ena", "ena_home_studio")  # 回话到期前走开
        self.runtime._retry = type(self.runtime._retry)(max_attempts=1)
        outbox = self.state.activation_outbox
        original = outbox._acknowledge
        calls = {"n": 0}

        def flaky(due_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("提交途中断电")
            return original(due_id)

        with patch.object(outbox, "_acknowledge", side_effect=flaky):
            result = self.runtime.process_due(self.reply)
        self.assertTrue(result.terminal)
        self.assertIs(result.agency_outcome, AgencyOutcome.REJECTED_STALE)
        record = self.state.agency.get(self.reply.due_id)
        self.assertEqual(record.detail["reason"], "reply_lapsed")
        self.assertFalse(consumes_allowance(record))
        self.assertTrue(outbox.is_acknowledged(self.reply.due_id))
        self.assertEqual(self.generations, 1)


# ── D3 resting ⇒ ASLEEP，显式睡眠锁 ─────────────────────────────────────
def _line_in_room(event_id="e1", participants=()):
    return Event(
        event_id=event_id,
        type=EventType.DIALOGUE_SPOKEN,
        occurred_at=CLOCK,
        scope=EventScope.LOCATION,
        actor_id="ena",
        location_id=ROOM,
        participants=participants,
        payload={"text": "醒醒", "char_name": "绘名"},
    )


def _round_trip(world):
    return WorldState.from_dict(deepcopy(world.to_dict()))


class AvailabilityTests(unittest.TestCase):
    def test_resting_is_asleep_and_waking_restores_the_stored_value(self):
        world = _world()
        world.set_activity("mizuki", ActivityKind.RESTING)
        self.assertIs(world.availability_of("mizuki"), Availability.ASLEEP)
        self.assertNotIn("mizuki", world.character_availability)  # 推导，不落盘
        self.assertIs(_round_trip(world).availability_of("mizuki"), Availability.ASLEEP)
        world.set_activity("mizuki", ActivityKind.EATING)
        self.assertIs(world.availability_of("mizuki"), Availability.AVAILABLE)
        self.assertIs(_round_trip(world).availability_of("mizuki"), Availability.AVAILABLE)

    def test_an_explicit_asleep_is_a_lock_that_waking_does_not_open(self):
        world = _world()
        world.set_activity("mizuki", ActivityKind.RESTING)
        world.set_availability("mizuki", Availability.ASLEEP)
        world.set_activity("mizuki", ActivityKind.EATING)
        for view in (world, _round_trip(world)):
            self.assertIs(view.availability_of("mizuki"), Availability.ASLEEP)
            self.assertIs(
                evaluate_exposure(view, _line_in_room(), "mizuki").reason,
                ExposureReason.UNAVAILABLE,
            )
            self.assertEqual(legal_actions(view, "mizuki")[0], ())
        world.set_availability("mizuki", Availability.AVAILABLE)
        self.assertIs(world.availability_of("mizuki"), Availability.AVAILABLE)
        self.assertIs(
            evaluate_exposure(world, _line_in_room(), "mizuki").reason,
            ExposureReason.SAME_LOCATION,
        )

    def test_busy_is_covered_by_sleep_and_comes_back(self):
        world = _world()
        world.set_availability("mizuki", Availability.BUSY)
        world.set_activity("mizuki", ActivityKind.RESTING)
        self.assertIs(world.availability_of("mizuki"), Availability.ASLEEP)
        self.assertEqual(legal_actions(world, "mizuki")[0], ())
        world.set_activity("mizuki", ActivityKind.IDLE)
        self.assertIs(world.availability_of("mizuki"), Availability.BUSY)
        self.assertTrue(legal_actions(world, "mizuki")[0])  # BUSY 不挡 Agency

    def test_exposure_follows_the_activity_event_in_commit_order(self):
        state = _session()
        world = state.world_state
        world.place_character("ena", ROOM)
        _set_activity(state, "mizuki", ActivityKind.RESTING, "sleep")
        # 入睡事件自己的自观察照常。
        self.assertTrue(state.observations.find("mizuki", "sleep").is_self_observation)
        _say(state, "ena", "睡了吗", "asleep-line")
        commit_session_event(
            state,
            Event(
                event_id="named",
                type=EventType.DIALOGUE_SPOKEN,
                occurred_at=world.clock,
                scope=EventScope.PRIVATE,
                actor_id="ena",
                location_id=ROOM,
                participants=("ena", "mizuki"),
                payload={"text": "瑞希", "char_name": "ena"},
            ),
        )
        self.assertIsNone(state.observations.find("mizuki", "asleep-line"))
        self.assertIsNone(state.observations.find("mizuki", "named"))
        exposures_before = state.exposures.to_dict()

        _set_activity(state, "mizuki", ActivityKind.IDLE, "wake")
        _say(state, "ena", "早", "awake-line")
        self.assertIsNotNone(state.observations.find("mizuki", "awake-line"))
        # 醒来不补观察，历史判定原样保留。
        self.assertIsNone(state.observations.find("mizuki", "asleep-line"))
        restored = SessionState.from_dict(deepcopy(state.to_dict()))
        self.assertEqual(
            restored.exposures.to_dict()["decisions"][: len(exposures_before["decisions"])],
            exposures_before["decisions"],
        )

    def test_a_formal_world_opened_in_a_resting_minute_is_asleep(self):
        spec = dataclasses.replace(YOAKE_MAE, start=None)
        # 2026-09-27 20:00 UTC = 09-28 05:00 JST：瑞希 04:00 起 resting。
        state = formal_session_state(
            spec,
            BOUNDARY.active(),
            session_id="yoake-mae_s1",
            wall=datetime(2026, 9, 27, 20, 0, tzinfo=timezone.utc),
        )
        world = state.world_state
        self.assertIs(world.activity_of("mizuki").kind, ActivityKind.RESTING)
        self.assertIs(world.availability_of("mizuki"), Availability.ASLEEP)
        restored = SessionState.from_dict(deepcopy(state.to_dict()))
        self.assertIs(restored.world_state.availability_of("mizuki"), Availability.ASLEEP)


# ── 记录形状与免单判据 ─────────────────────────────────────────────────
class QuietRecordShapeTests(unittest.TestCase):
    def _record(self, **overrides):
        values = dict(
            due_id="due:mizuki:1",
            character_id="mizuki",
            decided_at=CLOCK,
            outcome=AgencyOutcome.QUIET,
            detail={"reason": "cooldown"},
        )
        values.update(overrides)
        return AgencyRecord(**values)

    def test_a_quiet_record_must_look_untouched(self):
        self.assertFalse(consumes_allowance(self._record()))
        for overrides in (
            {"policy": "abstain"},
            {"detail": {"reason": "napping"}},
            {"detail": {"reason": "cooldown", "rationale": "想睡"}},
            {"due_id": "reply.activation:mizuki:e1"},
        ):
            with self.assertRaises(AgencyError, msg=overrides):
                self._record(**overrides)

    def test_an_outcome_code_alone_cannot_prove_it_was_free(self):
        self.assertTrue(consumes_allowance("quiet"))
        self.assertTrue(consumes_allowance(AgencyOutcome.QUIET))

    def test_old_records_are_charged_exactly_as_before(self):
        cases = {
            AgencyOutcome.ACTED: True,
            AgencyOutcome.ABSTAINED: True,
            AgencyOutcome.REJECTED_ILLEGAL: True,
            AgencyOutcome.REJECTED_STALE: True,
            AgencyOutcome.REJECTED_BUDGET: True,
            AgencyOutcome.REJECTED_POLICY_ERROR: True,
            AgencyOutcome.REJECTED_UNAVAILABLE: False,
        }
        for outcome, charged in cases.items():
            self.assertEqual(consumes_allowance(outcome), charged, outcome)

    def test_a_tampered_quiet_record_does_not_load(self):
        state, scheduler, engine, _ = _engine_rig()
        engine.commit(engine.propose(_due_at(scheduler, "t0", 0)))
        engine.commit(engine.propose(_due_at(scheduler, "t15", 15)))
        archive = state.to_dict()
        records = archive["agency"]["log"]["records"]
        self.assertEqual(records[1]["outcome"], "quiet")
        tampered = deepcopy(archive)
        tampered["agency"]["log"]["records"][1]["policy"] = "abstain"
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(tampered)
        SessionState.from_dict(archive)

    def test_an_out_of_range_cursor_does_not_load(self):
        # 实现审查 F2：超界游标会让之后的新观察一直被吞，加载时就拒绝。
        state, scheduler, engine, _ = _engine_rig()
        _other_says_and_leaves(state, "早", "old-line")
        engine.commit(engine.propose(_due_at(scheduler, "t0", 0)))
        scheduler.advance_by(5)  # 分钟精度：同一分钟里的先后只有日志位置分得出
        _other_says_and_leaves(state, "还在吗", "later-line")
        archive = state.to_dict()
        SessionState.from_dict(deepcopy(archive))
        detail = archive["agency"]["log"]["records"][0]["detail"]
        for bad in (1_000_000, -1, True, "3", None, len(state.observations)):
            tampered = deepcopy(archive)
            tampered["agency"]["log"]["records"][0]["detail"]["observation_cursor"] = bad
            if bad == len(state.observations):
                # 不超长度，但指到了这次决定之后才听见的那句。
                self.assertGreater(bad, detail["observation_cursor"])
            with self.assertRaises(SessionStateError, msg=bad):
                SessionState.from_dict(tampered)


if __name__ == "__main__":
    unittest.main()
