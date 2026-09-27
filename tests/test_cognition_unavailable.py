# tests/test_cognition_unavailable.py — 认知不可用时到期的激活（WORLD-1）。
#
# 世界发生了，决定没有发生，原因有记录：
#   1. REJECTED_UNAVAILABLE 不产出事件、观察、记忆；审计记录与交接确认同生共死；
#   2. 记录必须说清全部原因（闭集、去重、排序）并指向时间线区间，不许冒充问过策略；
#   3. 跟着存档走，被改过的记录在加载时就被拒绝。
#
# `_close_unavailable` 是引擎内部能力，这里直接调用它只为测记录本身的形状与
# 原子性；产品路径只能经由协调器的到期收尾入口到达它。
#
# 运行: python -m unittest discover -s tests -p test_cognition_unavailable.py
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from grants_support import grant_everything
from pns.models.activation import ActivationKind, ScheduledActivation
from pns.models.agency import AgencyError, AgencyOutcome, AgencyRecord
from pns.models.cognition import CognitionCause, CognitionTimeline
from pns.models.session import SessionState, SessionStateError
from pns.models.world_state import WorldState
from pns.runtime.agency.engine import AgencyEngine, AgencyEngineError
from pns.runtime.autonomy.outcome import ActivationOutcome, outcome_for
from pns.runtime.scheduler import PersistentScheduler
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph

CLOCK = datetime(2026, 9, 26, 19, 0)


def _rig():
    world = WorldState(
        clock=CLOCK,
        locations=build_default_location_graph(),
        channels=build_default_channel_registry(),
    )
    grant_everything(world)
    world.place_character("mizuki", "mizuki_home_room")
    world.place_character("ena", "ena_home_studio")
    grant_everything(world)
    state = SessionState(session_id="s1", scene="gate", characters=["mizuki", "ena"])
    state.attach_world_state(world)
    state.initialize_runtime("开场")
    scheduler = PersistentScheduler(state)
    engine = AgencyEngine(state)
    scheduler.schedule(
        ScheduledActivation(
            activation_id="wake",
            kind=ActivationKind.CHARACTER_ACTIVATION,
            due_at=CLOCK + timedelta(minutes=10),
            character_id="mizuki",
        )
    )
    due = scheduler.advance_by(10).due[0]
    return state, engine, due


def _counts(state):
    return (
        len(state.events),
        len(state.observations),
        len(state.memories),
        len(state.agency),
        len(state.activation_outbox.pending()),
    )


class ClosingIsAnOrdinaryAtomicOutcomeTests(unittest.TestCase):
    def test_it_leaves_an_audit_record_and_nothing_in_the_world(self):
        state, engine, due = _rig()
        events, observations, memories, agency, pending = _counts(state)
        record = engine._close_unavailable(
            due,
            [CognitionCause.OPERATOR_PAUSED, CognitionCause.NOT_STARTED],
            interval=3,
        )
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_UNAVAILABLE)
        self.assertEqual(record.detail["reason"], "cognition_unavailable")
        self.assertEqual(list(record.detail["causes"]), ["not_started", "operator_paused"])
        self.assertEqual(record.detail["interval"], 3)
        self.assertEqual(record.policy, "")
        self.assertIsNone(record.event_id)
        self.assertEqual(
            _counts(state), (events, observations, memories, agency + 1, pending - 1)
        )
        self.assertTrue(state.activation_outbox.is_acknowledged(due.due_id))

    def test_a_due_is_closed_at_most_once(self):
        state, engine, due = _rig()
        engine._close_unavailable(due, ["fault"], interval=0)
        with self.assertRaises(AgencyEngineError):
            engine._close_unavailable(due, ["fault"], interval=0)
        self.assertEqual(len(state.agency), 1)

    def test_a_failed_acknowledgement_rolls_the_record_back(self):
        state, engine, due = _rig()
        before = _counts(state)
        with patch.object(
            type(state.activation_outbox),
            "_acknowledge",
            side_effect=RuntimeError("disk said no"),
        ):
            with self.assertRaises(RuntimeError):
                engine._close_unavailable(due, ["fault"], interval=0)
        self.assertEqual(_counts(state), before)
        self.assertFalse(state.activation_outbox.is_acknowledged(due.due_id))

    def test_bad_causes_write_nothing(self):
        state, engine, due = _rig()
        before = _counts(state)
        for causes in ([], ["bored"], "fault", ["fault", "fault"], None):
            with self.subTest(causes=causes):
                with self.assertRaises(AgencyEngineError):
                    engine._close_unavailable(due, causes, interval=0)
        for interval in (-1, True, "0", None):
            with self.subTest(interval=interval):
                with self.assertRaises(AgencyEngineError):
                    engine._close_unavailable(due, ["fault"], interval=interval)
        self.assertEqual(_counts(state), before)

    def test_the_runtime_sees_it_as_a_rejection(self):
        self.assertIs(
            outcome_for(AgencyOutcome.REJECTED_UNAVAILABLE), ActivationOutcome.REJECTED
        )
        self.assertTrue(AgencyOutcome.REJECTED_UNAVAILABLE.rejected)


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


def _with_timeline(state):
    """给会话装一条刚打开的认知时间线：区间 0，原因 not_started。"""
    with state.atomic_commit():
        state.set_cognition(
            CognitionTimeline.open(
                log_length=len(state.agency), sim=state.world_state.clock, wall="w"
            )
        )


class ArchiveTests(unittest.TestCase):
    def test_it_survives_a_round_trip(self):
        state, engine, due = _rig()
        _with_timeline(state)
        engine._close_unavailable(due, ["not_started"], interval=0)
        restored = SessionState.from_dict(state.to_dict())
        record = restored.agency.records()[-1]
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_UNAVAILABLE)
        self.assertEqual(list(record.detail["causes"]), ["not_started"])
        self.assertEqual(restored.cognition, state.cognition)

    def test_a_tampered_record_is_refused_on_load(self):
        state, engine, due = _rig()
        _with_timeline(state)
        engine._close_unavailable(due, ["not_started"], interval=0)
        payload = state.to_dict()
        payload["agency"]["log"]["records"][-1]["policy"] = "first_legal"
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(payload)
        payload = state.to_dict()
        payload["agency"]["log"]["records"][-1]["detail"]["causes"] = ["bored"]
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(payload)


class TimelineCrossCheckTests(unittest.TestCase):
    """每条记录的判定都必须能从存档本身重新推出来（设计 §13.2 / §14.1）。"""

    def test_an_unavailable_record_needs_a_timeline(self):
        state, engine, due = _rig()
        engine._close_unavailable(due, ["fault"], interval=0)
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(state.to_dict())

    def test_causes_that_disagree_with_the_timeline_are_refused(self):
        state, engine, due = _rig()
        _with_timeline(state)
        engine._close_unavailable(due, ["fault"], interval=0)  # 时间线说的是 not_started
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(state.to_dict())

    def test_a_wrong_interval_is_refused(self):
        state, engine, due = _rig()
        _with_timeline(state)
        engine._close_unavailable(due, ["not_started"], interval=7)
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(state.to_dict())

    def test_a_timeline_that_skips_ahead_of_the_log_is_refused(self):
        state, engine, due = _rig()
        _with_timeline(state)
        payload = state.to_dict()
        payload["cognition"]["intervals"][0]["from_log"] = 5
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(payload)

    def test_the_timeline_rolls_back_with_its_transaction(self):
        state, _engine, _due = _rig()
        with self.assertRaises(RuntimeError):
            with state.atomic_commit():
                state.set_cognition(
                    CognitionTimeline.open(log_length=0, sim=CLOCK, wall="w")
                )
                raise RuntimeError("boom")
        self.assertIsNone(state.cognition)

    def test_the_timeline_only_changes_inside_a_transaction(self):
        state, _engine, _due = _rig()
        with self.assertRaises(SessionStateError):
            state.set_cognition(CognitionTimeline.open(log_length=0, sim=CLOCK, wall="w"))

    def test_the_timeline_can_only_grow(self):
        state, _engine, _due = _rig()
        _with_timeline(state)
        with self.assertRaises(SessionStateError):
            with state.atomic_commit():
                state.set_cognition(
                    CognitionTimeline.open(log_length=0, sim=CLOCK, wall="other")
                )


if __name__ == "__main__":
    unittest.main()
