# tests/test_event.py — Event 领域模型与 EventStore 世界历史的不变量。
#
# 这里只测事件自身的形状与历史容器的规则，不涉及世界状态；跟世界打交道的
# 提交边界在 tests/test_event_commit.py。
#
# 运行: python -m unittest tests.test_event -v
import unittest
from datetime import datetime

from pns.models.event import (
    Event,
    EventError,
    EventScope,
    EventType,
    new_event_id,
)
from pns.models.event_store import EventStore, EventStoreError

CLOCK = datetime(2026, 8, 20, 2, 0)


def _dialogue(event_id="e1", clock=CLOCK, **overrides):
    payload = {
        "event_id": event_id,
        "type": EventType.DIALOGUE_SPOKEN,
        "occurred_at": clock,
        "scope": EventScope.LOCATION,
        "actor_id": "mizuki",
        "location_id": "kamiyama_high_gate",
        "payload": {"text": "喵？"},
    }
    payload.update(overrides)
    return Event(**payload)


class EventShapeTests(unittest.TestCase):
    def test_minimal_dialogue_event(self):
        event = _dialogue()
        self.assertEqual(event.type, EventType.DIALOGUE_SPOKEN)
        self.assertEqual(event.scope, EventScope.LOCATION)
        self.assertEqual(event.payload["text"], "喵？")
        self.assertEqual(event.participants, ())

    def test_event_id_must_be_a_non_empty_string(self):
        for bad in ("", None, 7):
            with self.subTest(bad=bad), self.assertRaises(EventError):
                _dialogue(event_id=bad)

    def test_unknown_type_and_scope_are_rejected(self):
        with self.assertRaises(EventError):
            _dialogue(type="dialogue.telepathy")
        with self.assertRaises(EventError):
            _dialogue(scope="everywhere")

    def test_ambient_is_the_same_scope_as_public(self):
        # 架构文档把这一档写作 "public / ambient"，两个写法必须落到同一档。
        self.assertIs(EventScope("ambient"), EventScope.PUBLIC)

    def test_occurred_at_must_be_a_datetime(self):
        with self.assertRaises(EventError):
            _dialogue(occurred_at="2026-08-20T02:00:00")

    def test_new_event_ids_are_unique(self):
        self.assertNotEqual(new_event_id(), new_event_id())


class EventScopeValidationTests(unittest.TestCase):
    """每档 scope 的必填字段 —— 没有边界的事件不算声明了传播边界。"""

    def test_channel_scope_requires_a_channel(self):
        with self.assertRaises(EventError):
            _dialogue(scope=EventScope.CHANNEL, channel_id=None)

    def test_location_scope_requires_a_location(self):
        with self.assertRaises(EventError):
            _dialogue(scope=EventScope.LOCATION, location_id=None, channel_id="nightcord")

    def test_participant_scope_requires_participants(self):
        with self.assertRaises(EventError):
            _dialogue(scope=EventScope.PARTICIPANT, participants=())
        event = _dialogue(scope=EventScope.PARTICIPANT, participants=("mizuki", "ena"))
        self.assertEqual(event.participants, ("mizuki", "ena"))

    def test_private_scope_requires_an_actor(self):
        with self.assertRaises(EventError):
            Event(
                event_id="e1",
                type=EventType.WORLD_TIME_ADVANCED,
                occurred_at=CLOCK,
                scope=EventScope.PRIVATE,
                payload={"minutes": 5},
            )

    def test_participants_reject_duplicates_and_empty_ids(self):
        with self.assertRaises(EventError):
            _dialogue(participants=("mizuki", "mizuki"))
        with self.assertRaises(EventError):
            _dialogue(participants=("mizuki", ""))
        with self.assertRaises(EventError):
            _dialogue(participants="mizuki")  # 字符串不是角色序列


class EventTypeValidationTests(unittest.TestCase):
    def test_dialogue_needs_text_and_somewhere_to_happen(self):
        with self.assertRaises(EventError):
            _dialogue(payload={})
        with self.assertRaises(EventError):
            _dialogue(payload={"text": "   "})
        with self.assertRaises(EventError):
            _dialogue(scope=EventScope.PUBLIC, location_id=None, channel_id=None)

    def test_message_sent_requires_a_channel(self):
        with self.assertRaises(EventError):
            Event(
                event_id="e1",
                type=EventType.MESSAGE_SENT,
                occurred_at=CLOCK,
                scope=EventScope.PUBLIC,
                actor_id="mizuki",
                payload={"text": "hi"},
            )

    def test_presence_events_require_actor_and_channel(self):
        for event_type in (
            EventType.PRESENCE_JOINED_CHANNEL,
            EventType.PRESENCE_LEFT_CHANNEL,
        ):
            with self.subTest(event_type=event_type), self.assertRaises(EventError):
                Event(
                    event_id="e1",
                    type=event_type,
                    occurred_at=CLOCK,
                    scope=EventScope.CHANNEL,
                    channel_id="nightcord",
                )

    def test_time_advance_is_a_world_event_with_a_sane_amount(self):
        with self.assertRaises(EventError):
            # 时间推进没有 actor：不是谁做的，是世界本身。
            Event(
                event_id="e1",
                type=EventType.WORLD_TIME_ADVANCED,
                occurred_at=CLOCK,
                scope=EventScope.PUBLIC,
                actor_id="mizuki",
                payload={"minutes": 5},
            )
        for minutes in (-1, 0, "5", True, None):
            with self.subTest(minutes=minutes), self.assertRaises(EventError):
                Event(
                    event_id="e1",
                    type=EventType.WORLD_TIME_ADVANCED,
                    occurred_at=CLOCK,
                    scope=EventScope.PUBLIC,
                    payload={"minutes": minutes},
                )

    def test_location_change_requires_a_destination(self):
        with self.assertRaises(EventError):
            Event(
                event_id="e1",
                type=EventType.CHARACTER_LOCATION_CHANGED,
                occurred_at=CLOCK,
                scope=EventScope.PUBLIC,
                actor_id="mizuki",
            )


class EventImmutabilityTests(unittest.TestCase):
    """事件一旦提交就不能被下游从引用上改掉。"""

    def test_fields_cannot_be_reassigned(self):
        event = _dialogue()
        with self.assertRaises(Exception):
            event.actor_id = "ena"

    def test_payload_does_not_keep_the_callers_dictionary(self):
        payload = {"text": "喵？", "nested": {"mood": "playful"}}
        event = _dialogue(payload=payload)
        payload["text"] = "被改掉了"
        payload["nested"]["mood"] = "grim"
        self.assertEqual(event.payload["text"], "喵？")
        self.assertEqual(event.payload["nested"]["mood"], "playful")

    def test_payload_view_is_read_only_at_every_depth(self):
        event = _dialogue(payload={"text": "喵？", "nested": {"mood": "playful"}})
        with self.assertRaises(TypeError):
            event.payload["text"] = "改"
        with self.assertRaises(TypeError):
            event.payload["nested"]["mood"] = "改"

    def test_provenance_is_frozen_too(self):
        provenance = {"turn_number": 1, "dimensions": {"tone": 1}}
        event = _dialogue(provenance=provenance)
        provenance["dimensions"]["tone"] = 99
        self.assertEqual(event.provenance["dimensions"]["tone"], 1)
        with self.assertRaises(TypeError):
            event.provenance["turn_number"] = 2

    def test_serialization_returns_a_fresh_mutable_structure(self):
        event = _dialogue(payload={"text": "喵？", "nested": {"mood": "playful"}})
        first = event.to_dict()
        first["payload"]["nested"]["mood"] = "grim"
        first["participants"].append("ena")
        self.assertEqual(event.payload["nested"]["mood"], "playful")
        self.assertEqual(event.participants, ())
        self.assertEqual(event.to_dict()["payload"]["nested"]["mood"], "playful")

    def test_events_can_live_in_sets_and_dicts(self):
        # 事件的身份是 event_id；冻结后的 payload 不可哈希不该让 set() 炸掉。
        event = _dialogue("e1")
        self.assertEqual(len({event, _dialogue("e1"), _dialogue("e2")}), 2)
        self.assertEqual(event, _dialogue("e1"))

    def test_payload_rejects_values_that_are_not_json_safe(self):
        # 任何别的对象都可能是外部还握着引用的可变结构。
        for bad in (object(), {1, 2}, datetime(2026, 8, 20)):
            with self.subTest(bad=bad), self.assertRaises(EventError):
                _dialogue(payload={"text": "喵？", "bad": bad})
        with self.assertRaises(EventError):
            _dialogue(payload={"text": "喵？", 1: "非字符串键"})

    def test_lists_survive_a_serialization_round_trip(self):
        event = _dialogue(payload={"text": "喵？", "tags": ["a", "b"]})
        self.assertEqual(event.to_dict()["payload"]["tags"], ["a", "b"])
        restored = Event.from_dict(event.to_dict())
        self.assertEqual(restored.to_dict(), event.to_dict())


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = EventStore()

    def test_append_returns_the_sequence_number(self):
        self.assertEqual(self.store._append(_dialogue("e1")), 0)
        self.assertEqual(self.store._append(_dialogue("e2")), 1)
        self.assertEqual(len(self.store), 2)
        self.assertEqual(self.store.sequence_of("e2"), 1)
        self.assertEqual(self.store.latest().event_id, "e2")

    def test_duplicate_event_ids_are_rejected(self):
        self.store._append(_dialogue("e1"))
        with self.assertRaises(EventStoreError):
            self.store._append(_dialogue("e1", payload={"text": "另一句"}))
        self.assertEqual(len(self.store), 1)

    def test_only_events_can_be_appended(self):
        with self.assertRaises(EventStoreError):
            self.store._append({"event_id": "e1"})

    def test_world_history_cannot_run_backwards(self):
        self.store._append(_dialogue("e1", clock=datetime(2026, 8, 20, 2, 0)))
        with self.assertRaises(EventStoreError):
            self.store._append(_dialogue("e2", clock=datetime(2026, 8, 20, 1, 59)))
        self.assertEqual(len(self.store), 1)

    def test_equal_timestamps_keep_append_order(self):
        # P5 的时钟在会话内不推进，所以"同一时刻的多条事件"是常态而非边角。
        for index in range(5):
            self.store._append(_dialogue(f"e{index}"))
        self.assertEqual(
            [event.event_id for event in self.store.events()],
            ["e0", "e1", "e2", "e3", "e4"],
        )
        self.assertEqual(
            [entry["sequence"] for entry in self.store.to_dict()["events"]],
            [0, 1, 2, 3, 4],
        )

    def test_events_accessor_does_not_expose_the_internal_list(self):
        self.store._append(_dialogue("e1"))
        events = self.store.events()
        self.assertIsInstance(events, tuple)
        list(self.store)  # 迭代拿到的是快照，迭代中追加不会炸
        self.store._append(_dialogue("e2"))
        self.assertEqual(len(events), 1)

    def test_serialization_round_trip(self):
        self.store._append(_dialogue("e1"))
        self.store._append(_dialogue("e2", scope=EventScope.PUBLIC))
        restored = EventStore.from_dict(self.store.to_dict())
        self.assertEqual(restored.to_dict(), self.store.to_dict())

    def test_serialization_does_not_leak_mutable_references(self):
        self.store._append(_dialogue("e1", payload={"text": "喵？", "n": {"a": 1}}))
        payload = self.store.to_dict()
        payload["events"][0]["payload"]["n"]["a"] = 99
        self.assertEqual(self.store.get("e1").payload["n"]["a"], 1)

    def test_rollback_releases_the_ids_it_removed(self):
        self.store._append(_dialogue("e1"))
        self.store._append(_dialogue("e2"))
        self.store._rollback_to(1)
        self.assertEqual(len(self.store), 1)
        self.assertFalse(self.store.has("e2"))
        # 回滚掉的 ID 必须能重新使用，否则重试会被自己的残留挡住
        self.store._append(_dialogue("e2"))
        self.assertTrue(self.store.has("e2"))

    def test_rollback_rejects_out_of_range_lengths(self):
        self.store._append(_dialogue("e1"))
        for bad in (-1, 2, "1", True):
            with self.subTest(bad=bad), self.assertRaises(EventStoreError):
                self.store._rollback_to(bad)

    def test_public_store_surface_is_read_only(self):
        self.assertFalse(hasattr(self.store, "append"))
        self.assertFalse(hasattr(self.store, "rollback_to"))

    def test_restore_rejects_missing_or_tampered_sequence(self):
        payload = EventStore([_dialogue("e1"), _dialogue("e2")]).to_dict()
        payload["events"][1]["sequence"] = 9
        with self.assertRaises(EventStoreError):
            EventStore.from_dict(payload)

        payload = EventStore([_dialogue("e1")]).to_dict()
        del payload["events"][0]["sequence"]
        with self.assertRaises(EventStoreError):
            EventStore.from_dict(payload)

    def test_constructor_rejects_a_duplicated_history(self):
        with self.assertRaises(EventStoreError):
            EventStore([_dialogue("e1"), _dialogue("e1")])


if __name__ == "__main__":
    unittest.main()
