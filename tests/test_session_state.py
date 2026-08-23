import unittest
from datetime import datetime

from pns.models.event import Event, EventScope, EventType
from pns.models.session import SessionState, SessionStateError, Turn
from pns.models.world_state import WorldState
from pns.runtime.event_commit import commit_session_event
from pns.world.locations import build_default_location_graph


class SessionStateTests(unittest.TestCase):
    def setUp(self):
        self.state = SessionState(
            session_id="session-1",
            scene="gate",
            characters=["mizuki", "ena"],
        )
        self.state.initialize_runtime("A scene")

    def test_turn_is_single_source_for_wire_and_drift_views(self):
        turn = Turn(
            turn_number=1,
            character="mizuki",
            char_name="Mizuki",
            prompt="A scene\nPlease begin",
            response="Hello",
            timestamp="2026-08-20T00:00:00",
            score=6,
            is_ooc=True,
            confidence=0.8,
            correction="Use a shorter reply",
            dimensions={"language_structure": {"score": 6}},
            generator_provider="provider",
            generator_model="generator",
            evaluator_provider="provider",
            evaluator_model="evaluator",
        )

        wire = turn.to_wire_dict()
        drift = turn.to_drift_record(self.state.session_id)

        self.assertEqual(
            set(wire),
            {
                "turn", "character", "char_name", "reply", "score", "is_ooc",
                "drift_type", "reason", "correction", "needs_human_review",
                "dimensions", "dimensions_complete", "methodology_version",
                "generator_provider", "generator_model", "evaluator_provider",
                "evaluator_model",
            },
        )
        self.assertEqual(
            set(drift),
            {
                "session_id", "turn", "character", "char_name", "text",
                "drift_score", "confidence", "drift_type", "reason",
                "needs_human_review", "correction", "scene_id", "lore_tag",
                "router_reference_status", "dimensions", "dimensions_complete",
                "methodology_version", "generator_provider", "generator_model",
                "evaluator_provider", "evaluator_model", "original_request",
                "correction_applied", "timestamp",
            },
        )
        self.assertEqual(wire["reply"], turn.response)
        self.assertEqual(wire["score"], turn.score)
        self.assertEqual(drift["text"], turn.response)
        self.assertEqual(drift["drift_score"], turn.score)
        self.assertEqual(drift["original_request"], turn.prompt)
        self.assertEqual(drift["generator_model"], wire["generator_model"])
        self.assertEqual(drift["evaluator_model"], wire["evaluator_model"])

    def test_record_turn_updates_all_authoritative_session_state(self):
        self.state.start()
        turn = Turn(
            turn_number=1,
            character="mizuki",
            char_name="Mizuki",
            prompt="prompt",
            response="reply",
            timestamp="now",
            score=6,
            is_ooc=True,
            correction="correct it",
        )

        self.state.record_turn(turn)
        self.state.advance_character()

        self.assertEqual(self.state.turns, [turn])
        self.assertEqual(self.state.final_stats()["ooc_count"], 1)
        self.assertEqual(self.state.final_stats()["corrections"], 1)
        self.assertEqual(self.state.pending_corrections["mizuki"], "correct it")
        self.assertEqual(self.state.current_character, "ena")
        self.assertEqual(self.state.histories["mizuki"][-1]["role"], "assistant")
        self.assertEqual(self.state.histories["ena"][-1]["role"], "user")
        self.assertEqual(self.state.final_stats()["total_turns"], 1)

    def test_record_turn_rejects_state_invariant_violations(self):
        unknown_character = Turn(
            turn_number=1,
            character="unknown",
            prompt="prompt",
            response="reply",
            timestamp="now",
        )
        skipped_number = Turn(
            turn_number=2,
            character="mizuki",
            prompt="prompt",
            response="reply",
            timestamp="now",
        )

        with self.assertRaises(ValueError):
            self.state.record_turn(unknown_character)
        with self.assertRaises(ValueError):
            self.state.record_turn(skipped_number)
        self.assertEqual(self.state.turns, [])

    def test_legacy_add_turn_works_without_live_runtime_state(self):
        state = SessionState(
            session_id="persisted-session",
            scene="gate",
            characters=["mizuki", "ena"],
        )
        turn = Turn(
            turn_number=1,
            character="mizuki",
            prompt="prompt",
            response="reply",
            timestamp="now",
            score=2,
        )

        state.add_turn(turn)

        self.assertEqual(state.turns, [turn])
        self.assertEqual(state.histories, {})
        self.assertEqual(state.pending_corrections, {})
        self.assertEqual(state.final_stats()["total_turns"], 1)


class SessionWorldStateTests(unittest.TestCase):
    def setUp(self):
        self.state = SessionState(
            session_id="session-1", scene="gate", characters=["mizuki", "ena"]
        )
        self.world = WorldState(
            clock=datetime(2026, 8, 20, 17, 30),
            locations=build_default_location_graph(),
        )

    def test_world_state_starts_unattached_and_serializes_as_empty(self):
        self.assertIsNone(self.state.world_state)
        self.assertEqual(self.state.to_dict()["world_state"], {})

    def test_attached_world_state_is_the_same_object(self):
        self.state.attach_world_state(self.world)
        self.assertIs(self.state.world_state, self.world)

        self.world.place_character("mizuki", "kamiyama_high_gate")
        self.assertEqual(
            self.state.to_dict()["world_state"]["character_locations"],
            {"mizuki": "kamiyama_high_gate"},
        )

    def test_world_state_can_only_be_attached_once(self):
        self.state.attach_world_state(self.world)
        with self.assertRaises(RuntimeError):
            self.state.attach_world_state(self.world)

    def test_world_state_must_be_typed(self):
        with self.assertRaises(TypeError):
            self.state.attach_world_state({"time": "17:30"})


class ActivityHistoryRestoreTests(unittest.TestCase):
    """当前活动必须跟最后一条活动事件对得上 —— 但"回到未指定"没有 since。

    未指定在世界状态里不留记录，activity_of() 交回来的 since 是**此刻**合成的
    值，不是存下来的事实。拿它跟事件时间比，会让一个完全正常的世界在时钟往前
    走一分钟之后就再也恢复不了。
    """

    def _state(self):
        world = WorldState(
            clock=datetime(2026, 8, 21, 2, 0),
            locations=build_default_location_graph(),
        )
        world.place_character("mizuki", "mizuki_home_room")
        state = SessionState(
            session_id="s1", scene="nightcord", characters=["mizuki"]
        )
        state.attach_world_state(world)
        return state, world

    def _activity_event(self, world, event_id, activity):
        return Event(
            event_id=event_id,
            type=EventType.CHARACTER_ACTIVITY_CHANGED,
            occurred_at=world.clock,
            scope=EventScope.PRIVATE,
            actor_id="mizuki",
            payload={"activity": activity},
        )

    def test_returning_to_unspecified_stays_restorable_as_time_moves_on(self):
        state, world = self._state()
        commit_session_event(state, self._activity_event(world, "a1", "drawing"))
        commit_session_event(state, self._activity_event(world, "a2", "unspecified"))
        world.advance_time(30)

        restored = SessionState.from_dict(state.to_dict())
        self.assertEqual(restored.world_state.character_activities, {})

    def test_a_current_activity_that_contradicts_the_last_event_is_still_caught(self):
        state, world = self._state()
        commit_session_event(state, self._activity_event(world, "a1", "drawing"))
        commit_session_event(state, self._activity_event(world, "a2", "unspecified"))
        payload = state.to_dict()
        payload["world_state"]["character_activities"] = {
            "mizuki": {"kind": "drawing", "since": world.clock.isoformat()}
        }
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
