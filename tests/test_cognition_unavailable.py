# tests/test_cognition_unavailable.py — 认知可用性在提交边界上的判定（WORLD-1 设计 §13–§14）。
#
# 世界发生了，决定没有发生，原因有记录：
#   1. 判定在 AgencyEngine.commit() 的事务内：此刻不可用，任何计划都改写成
#      REJECTED_UNAVAILABLE，不产出事件、观察、记忆；
#   2. 不能凭空制造不可用记录：时间线说可用、或会话没有时间线，就拒绝；
#   3. 单次额度、世界上限用尽的转换与触发它的记录同一事务，中间插不进别的提交；
#   4. 每条记录的判定都能从存档本身重新推出来，篡改在加载时被拒绝。
#
# 运行: python -m unittest discover -s tests -p test_cognition_unavailable.py
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import patch

from grants_support import grant_everything
from pns.models.activation import ActivationKind, ScheduledActivation
from pns.models.agency import AgencyBudget, AgencyError, AgencyOutcome, AgencyRecord
from pns.models.cognition import CognitionCause as C, CognitionTimeline
from pns.models.event import EventType
from pns.models.session import SessionState, SessionStateError
from pns.models.world_state import WorldState
from pns.runtime.agency.engine import AgencyEngine, AgencyEngineError
from pns.runtime.agency.policy import AbstainPolicy, FirstLegalActionPolicy
from pns.runtime.autonomy.outcome import ActivationOutcome, outcome_for
from pns.runtime.scheduler import PersistentScheduler
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph

CLOCK = datetime(2026, 9, 26, 19, 0)


def _rig(*, policy=None, budget=None, timeline=True):
    world = WorldState(
        clock=CLOCK,
        locations=build_default_location_graph(),
        channels=build_default_channel_registry(),
    )
    grant_everything(world)
    world.place_character("mizuki", "mizuki_home_room")
    world.place_character("ena", "ena_home_studio")
    state = SessionState(session_id="s1", scene="gate", characters=["mizuki", "ena"])
    state.attach_world_state(world)
    state.initialize_runtime("开场")
    scheduler = PersistentScheduler(state)
    engine = AgencyEngine(state, policy=policy, budget=budget)
    if timeline:
        _transition(state, lambda tl, n: CognitionTimeline.open(log_length=n, sim=CLOCK, wall="w"))
    return state, scheduler, engine


def _transition(state, make):
    with state.atomic_commit():
        state.set_cognition(make(state.cognition, len(state.agency)))


def _start(state, allowance=None):
    now = state.world_state.clock
    _transition(
        state,
        lambda tl, n: tl.started(
            log_length=n, sim=now, wall="w", run_allowance=allowance,
        ),
    )


def _stop(state):
    _transition(
        state, lambda tl, n: tl.stopped(log_length=n, sim=state.world_state.clock, wall="w")
    )


def _dues(scheduler, *characters, minutes=10):
    """每个角色排一条激活，一起推进到期，返回到期记录。"""
    for index, character_id in enumerate(characters):
        scheduler.schedule(
            ScheduledActivation(
                activation_id=f"wake-{index}",
                kind=ActivationKind.CHARACTER_ACTIVATION,
                due_at=scheduler.clock + timedelta(minutes=minutes),
                character_id=character_id,
            )
        )
    return scheduler.advance_by(minutes).due


def _decide(engine, due):
    return engine.commit(engine.propose(due))


def _counts(state):
    return (
        len(state.events.by_type(EventType.CHARACTER_LOCATION_CHANGED))
        + len(state.events.by_type(EventType.PRESENCE_JOINED_CHANNEL)),
        len(state.observations),
        len(state.memories),
        len(state.agency),
        len(state.activation_outbox.pending()),
    )


class CommitJudgementTests(unittest.TestCase):
    def test_an_acting_plan_becomes_unavailable_when_cognition_is_off(self):
        state, scheduler, engine = _rig(policy=FirstLegalActionPolicy())
        (due,) = _dues(scheduler, "mizuki")
        plan = engine.propose(due)
        self.assertTrue(plan.would_act)
        actions, observations, memories, agency, pending = _counts(state)
        record = engine.commit(plan)
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_UNAVAILABLE)
        self.assertEqual(list(record.detail["causes"]), ["not_started"])
        self.assertEqual(record.detail["interval"], 0)
        self.assertEqual(record.policy, "")
        self.assertIsNone(record.event_id)
        self.assertEqual(
            _counts(state), (actions, observations, memories, agency + 1, pending - 1)
        )

    def test_stop_during_the_model_call_wins_at_commit(self):
        # R2-5：提案时可用，模型还没回来操作员就 Stop；提交时判为不可用。
        state, scheduler, engine = _rig(policy=FirstLegalActionPolicy())
        _start(state)
        (due,) = _dues(scheduler, "mizuki")
        plan = engine.propose(due)
        _stop(state)
        record = engine.commit(plan)
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_UNAVAILABLE)
        self.assertEqual(list(record.detail["causes"]), ["operator_paused"])

    def test_an_available_record_names_its_interval(self):
        state, scheduler, engine = _rig(policy=FirstLegalActionPolicy())
        _start(state)
        (due,) = _dues(scheduler, "mizuki")
        record = _decide(engine, due)
        self.assertIs(record.outcome, AgencyOutcome.ACTED)
        self.assertEqual(record.detail["cognition_interval"], 1)

    def test_a_backlog_due_stays_unavailable_after_start(self):
        # 补跑途中 Start：Start 之前触发的到期资格仍按旧原因关闭。
        state, scheduler, engine = _rig(policy=FirstLegalActionPolicy())
        (due,) = _dues(scheduler, "mizuki")  # 19:10 触发，还没决定
        _start(state)  # cutoff 19:11
        record = _decide(engine, due)
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_UNAVAILABLE)
        self.assertEqual(list(record.detail["causes"]), ["not_started"])

    def test_sessions_without_a_timeline_are_unaffected(self):
        state, scheduler, engine = _rig(policy=FirstLegalActionPolicy(), timeline=False)
        (due,) = _dues(scheduler, "mizuki")
        record = _decide(engine, due)
        self.assertIs(record.outcome, AgencyOutcome.ACTED)
        self.assertNotIn("cognition_interval", record.detail)


class CloseUnavailableTests(unittest.TestCase):
    def test_it_closes_with_the_causes_the_timeline_gives(self):
        state, scheduler, engine = _rig()
        (due,) = _dues(scheduler, "mizuki")
        record = engine._close_unavailable(due)
        self.assertEqual(list(record.detail["causes"]), ["not_started"])
        self.assertTrue(state.activation_outbox.is_acknowledged(due.due_id))

    def test_it_cannot_fabricate_unavailability(self):
        for label, setup in {
            "available": lambda state: _start(state),
            "no timeline": None,
        }.items():
            with self.subTest(label):
                state, scheduler, engine = _rig(timeline=setup is not None)
                if setup:
                    setup(state)
                (due,) = _dues(scheduler, "mizuki")
                with self.assertRaises(AgencyEngineError):
                    engine._close_unavailable(due)
                self.assertEqual(len(state.agency), 0)
                self.assertFalse(state.activation_outbox.is_acknowledged(due.due_id))

    def test_a_due_is_closed_at_most_once(self):
        state, scheduler, engine = _rig()
        (due,) = _dues(scheduler, "mizuki")
        engine._close_unavailable(due)
        with self.assertRaises(AgencyEngineError):
            engine._close_unavailable(due)
        self.assertEqual(len(state.agency), 1)

    def test_a_failed_acknowledgement_rolls_the_record_back(self):
        state, scheduler, engine = _rig()
        (due,) = _dues(scheduler, "mizuki")
        before = _counts(state)
        with patch.object(
            type(state.activation_outbox), "_acknowledge", side_effect=RuntimeError("disk said no")
        ):
            with self.assertRaises(RuntimeError):
                engine._close_unavailable(due)
        self.assertEqual(_counts(state), before)

    def test_the_runtime_sees_it_as_a_rejection(self):
        self.assertIs(
            outcome_for(AgencyOutcome.REJECTED_UNAVAILABLE), ActivationOutcome.REJECTED
        )
        self.assertTrue(AgencyOutcome.REJECTED_UNAVAILABLE.rejected)


class AllowanceAndCapTests(unittest.TestCase):
    def test_the_last_allowance_closes_the_interval_in_the_same_transaction(self):
        # R2-4 / R4-3：A、B、C 同一刻到期，额度 1。A 用掉额度的同一事务里开启
        # run_budget_exhausted，B、C 落在那之后。
        state, scheduler, engine = _rig(policy=AbstainPolicy())
        _start(state, allowance=1)
        dues = _dues(scheduler, "mizuki", "ena")
        first = _decide(engine, dues[0])
        self.assertIs(first.outcome, AgencyOutcome.ABSTAINED)
        self.assertEqual(state.cognition.current.causes, {C.RUN_BUDGET_EXHAUSTED})
        self.assertEqual(state.cognition.current.from_log, 1)
        second = _decide(engine, dues[1])
        self.assertIs(second.outcome, AgencyOutcome.REJECTED_UNAVAILABLE)
        self.assertEqual(list(second.detail["causes"]), ["run_budget_exhausted"])
        SessionState.from_dict(state.to_dict())

    def test_a_transaction_that_fails_after_the_record_rolls_the_transition_back(self):
        state, scheduler, engine = _rig(policy=AbstainPolicy())
        _start(state, allowance=1)
        (due,) = _dues(scheduler, "mizuki")
        before = state.cognition
        with self.assertRaises(RuntimeError):
            with state.atomic_commit():
                _decide(engine, due)
                raise RuntimeError("memory encoding failed")
        self.assertIs(state.cognition, before)
        self.assertEqual(len(state.agency), 0)

    def test_the_allowance_is_not_refilled_by_an_unrelated_transition(self):
        state, scheduler, engine = _rig(policy=AbstainPolicy())
        _start(state, allowance=2)
        (first,) = _dues(scheduler, "mizuki")
        _decide(engine, first)
        now = state.world_state.clock
        _transition(state, lambda tl, n: tl.fault_began(log_length=n, sim=now, wall="w"))
        _transition(
            state,
            lambda tl, n: tl.fault_cleared(
                log_length=n, sim=now, wall="w"
            ),
        )
        # 故障解除后的新区间不许把额度重新装满：消耗从 Start 那一刻起算。
        self.assertEqual(state.cognition.current.allowance_since_log, 0)
        (later,) = _dues(scheduler, "ena")  # 故障解除之后才触发
        self.assertIs(_decide(engine, later).outcome, AgencyOutcome.ABSTAINED)
        self.assertIn(C.RUN_BUDGET_EXHAUSTED, state.cognition.current.causes)

    def test_the_world_cap_is_a_cognition_cause_not_a_budget_rejection(self):
        state, scheduler, engine = _rig(
            policy=FirstLegalActionPolicy(),
            budget=AgencyBudget(max_committed_actions_per_session=1),
        )
        _start(state)
        dues = _dues(scheduler, "mizuki", "ena")
        first = _decide(engine, dues[0])
        self.assertIs(first.outcome, AgencyOutcome.ACTED)
        self.assertIn(C.WORLD_ACTION_CAP, state.cognition.current.causes)
        second = _decide(engine, dues[1])
        self.assertIs(second.outcome, AgencyOutcome.REJECTED_UNAVAILABLE)
        self.assertEqual(list(second.detail["causes"]), ["world_action_cap"])
        # Start 清不掉世界上限。
        _start(state)
        self.assertIn(C.WORLD_ACTION_CAP, state.cognition.current.causes)
        SessionState.from_dict(state.to_dict())


    def test_a_plan_proposed_before_the_cap_is_judged_unavailable_at_commit(self):
        # A、B 都在上限到达之前提案；A 提交触顶，B 提交时先判认知可用性，
        # 得到 world_action_cap，而不是普通的 REJECTED_BUDGET。
        state, scheduler, engine = _rig(
            policy=FirstLegalActionPolicy(),
            budget=AgencyBudget(max_committed_actions_per_session=1),
        )
        _start(state)
        dues = _dues(scheduler, "mizuki", "ena")
        plans = [engine.propose(due) for due in dues]
        self.assertTrue(all(plan.would_act for plan in plans))
        engine.commit(plans[0])
        second = engine.commit(plans[1])
        self.assertIs(second.outcome, AgencyOutcome.REJECTED_UNAVAILABLE)
        self.assertEqual(list(second.detail["causes"]), ["world_action_cap"])


class RecordShapeTests(unittest.TestCase):
    def _record(self, **overrides):
        fields = dict(
            due_id="d1",
            character_id="mizuki",
            decided_at=CLOCK,
            outcome=AgencyOutcome.REJECTED_UNAVAILABLE,
            policy="",
            detail={
                "reason": "cognition_unavailable",
                "causes": ["fault", "process_stopped"],
                "interval": 0,
            },
        )
        fields.update(overrides)
        return AgencyRecord(**fields)

    def test_a_well_formed_record(self):
        self._record()

    def test_it_cannot_claim_a_policy_was_asked(self):
        with self.assertRaises(AgencyError):
            self._record(policy="first_legal")

    def test_the_detail_must_explain_itself(self):
        good = {
            "reason": "cognition_unavailable",
            "causes": ["fault", "process_stopped"],
            "interval": 0,
        }
        broken = {
            "wrong reason": {**good, "reason": "budget"},
            "no causes": {k: v for k, v in good.items() if k != "causes"},
            "empty causes": {**good, "causes": []},
            "unknown cause": {**good, "causes": ["bored"]},
            "unsorted causes": {**good, "causes": ["process_stopped", "fault"]},
            "duplicate causes": {**good, "causes": ["fault", "fault"]},
            "no interval": {k: v for k, v in good.items() if k != "interval"},
            "negative interval": {**good, "interval": -1},
            "bool interval": {**good, "interval": True},
        }
        for label, detail in broken.items():
            with self.subTest(label):
                with self.assertRaises(AgencyError):
                    self._record(detail=detail)

class LoadCrossCheckTests(unittest.TestCase):
    """每条记录的判定都必须能从存档本身重新推出来（设计 §13.2 / §14.1）。"""

    def _closed(self):
        state, scheduler, engine = _rig()
        (due,) = _dues(scheduler, "mizuki")
        engine._close_unavailable(due)
        return state

    def test_a_consistent_archive_round_trips(self):
        state = self._closed()
        restored = SessionState.from_dict(state.to_dict())
        self.assertEqual(restored.cognition, state.cognition)
        self.assertIs(
            restored.agency.records()[-1].outcome, AgencyOutcome.REJECTED_UNAVAILABLE
        )

    def test_tampering_is_refused(self):
        def records(p):
            return p["agency"]["log"]["records"]

        cases = {
            "causes disagree": lambda p: records(p)[-1]["detail"].update(causes=["fault"]),
            "wrong interval": lambda p: records(p)[-1]["detail"].update(interval=7),
            "no timeline": lambda p: p.update(cognition=None),
            "policy claimed": lambda p: records(p)[-1].update(policy="first_legal"),
            "unknown cause": lambda p: records(p)[-1]["detail"].update(causes=["bored"]),
            "timeline ahead of the log": lambda p: p["cognition"]["intervals"][0].update(
                from_log=5
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                payload = self._closed().to_dict()
                mutate(payload)
                with self.assertRaises(SessionStateError):
                    SessionState.from_dict(payload)

    def test_an_available_record_with_the_wrong_interval_is_refused(self):
        state, scheduler, engine = _rig(policy=AbstainPolicy())
        _start(state)
        (due,) = _dues(scheduler, "mizuki")
        _decide(engine, due)
        payload = state.to_dict()
        payload["agency"]["log"]["records"][-1]["detail"]["cognition_interval"] = 0
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(payload)

    def test_an_overspent_allowance_is_refused(self):
        state, scheduler, engine = _rig(policy=AbstainPolicy())
        _start(state, allowance=2)
        dues = _dues(scheduler, "mizuki", "ena")
        _decide(engine, dues[0])
        _decide(engine, dues[1])
        payload = state.to_dict()
        # 把额度改小：存档里的两条消耗超过了它。
        for interval in payload["cognition"]["intervals"]:
            if interval["run_allowance"] is not None:
                interval["run_allowance"] = 1
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(payload)

    def test_a_bare_acknowledgement_is_refused(self):
        # 审查 F2：ack ⇒ record。只确认、不写 Agency 记录的到期资格是静默丢失。
        state, scheduler, _engine = _rig()
        (due,) = _dues(scheduler, "mizuki")
        scheduler.acknowledge(due.due_id)
        with self.assertRaisesRegex(SessionStateError, "没有任何 Agency 记录"):
            SessionState.from_dict(state.to_dict())

    def test_timeline_tampering_without_a_record_behind_it_is_refused(self):
        # 审查 F3：被篡改的区间后面没有任何 Agency 记录，逐条记录复核发现不了，
        # 只能靠转换重放。
        def stopped():
            state, _scheduler, _engine = _rig()
            _start(state, allowance=2)
            _stop(state)
            return state

        def restored():
            state, _scheduler, _engine = _rig()
            now = state.world_state.clock
            _transition(state, lambda tl, n: tl.restored(log_length=n, sim=now, wall="w"))
            return state

        def intervals(p):
            return p["cognition"]["intervals"]

        cases = {
            "not_started relabeled as fault": (
                lambda: _rig()[0],
                lambda p: intervals(p)[-1].update(causes=["fault"]),
            ),
            "pause relabeled as fault": (
                stopped,
                lambda p: intervals(p)[-1].update(causes=["fault"]),
            ),
            "restore cutoff moved a minute later": (
                restored,
                lambda p: intervals(p)[-1]["backlog"][-1].update(
                    until_sim=(CLOCK + timedelta(minutes=2)).isoformat()
                ),
            ),
            "carried allowance raised": (
                stopped,
                lambda p: intervals(p)[-1].update(run_allowance=3),
            ),
        }
        for label, (make, mutate) in cases.items():
            with self.subTest(label):
                payload = make().to_dict()
                SessionState.from_dict(deepcopy(payload))  # 未篡改的能加载
                mutate(payload)
                with self.assertRaises(SessionStateError):
                    SessionState.from_dict(payload)

    def test_the_exhaustion_interval_sits_right_after_the_last_allowance(self):
        state, scheduler, engine = _rig(policy=AbstainPolicy())
        _start(state, allowance=2)
        dues = _dues(scheduler, "mizuki", "ena")
        _decide(engine, dues[0])
        _decide(engine, dues[1])
        self.assertEqual(state.cognition.current.causes, {C.RUN_BUDGET_EXHAUSTED})
        archive = state.to_dict()
        SessionState.from_dict(deepcopy(archive))

        def intervals(p):
            return p["cognition"]["intervals"]

        def raise_allowance(p):
            # 额度改大（带着它的区间一起改，重放仍然自洽）：日志并没有在耗尽区间
            # 那里用完额度。
            for interval in intervals(p):
                if interval["run_allowance"] is not None:
                    interval["run_allowance"] = 3

        cases = {
            "exhaustion dropped": lambda p: intervals(p).pop(),
            "allowance raised everywhere": raise_allowance,
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                payload = deepcopy(archive)
                mutate(payload)
                with self.assertRaises(SessionStateError):
                    SessionState.from_dict(payload)

    def test_the_timeline_only_changes_inside_a_transaction(self):
        state, _scheduler, _engine = _rig(timeline=False)
        with self.assertRaises(SessionStateError):
            state.set_cognition(CognitionTimeline.open(log_length=0, sim=CLOCK, wall="w"))

    def test_the_timeline_can_only_grow(self):
        state, _scheduler, _engine = _rig()
        with self.assertRaises(SessionStateError):
            with state.atomic_commit():
                state.set_cognition(
                    CognitionTimeline.open(log_length=0, sim=CLOCK, wall="other")
                )


if __name__ == "__main__":
    unittest.main()
