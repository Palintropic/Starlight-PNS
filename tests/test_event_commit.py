# tests/test_event_commit.py — 事件提交边界的不变量。
#
# 这里守两条线：
#   1. 状态变更和事件追加同生共死，任何阶段出岔子都不留半提交状态；
#   2. 未知角色/地点/频道、重复 ID、非法 scope 都进不了世界历史。
#
# 运行: python -m unittest tests.test_event_commit -v
import unittest
from datetime import datetime
from unittest.mock import patch

from pns.models.event import Event, EventScope, EventType
from pns.models.event_store import EventStore, EventStoreError
from pns.models.exposure import ExposureReason
from pns.models.session import SessionState, Turn
from pns.models.world_state import WorldState
from pns.runtime import event_commit
from pns.runtime.event_commit import (
    EventCommitError,
    commit_dialogue,
    commit_event,
    commit_session_event,
    dialogue_event_for_turn,
    project_turn_message,
)
from pns.runtime.exposure import explain_character, explain_event
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph

CLOCK = datetime(2026, 8, 20, 2, 0)


def _world(clock=CLOCK, *, channel=False):
    world = WorldState(
        clock=clock,
        locations=build_default_location_graph(),
        channels=build_default_channel_registry(),
    )
    if channel:
        world.place_character("mizuki", "mizuki_home_room")
        world.place_character("ena", "ena_home_studio")
        world.join_channel("mizuki", "nightcord")
        world.join_channel("ena", "nightcord")
    else:
        world.place_character("mizuki", "kamiyama_high_gate")
        world.place_character("ena", "kamiyama_high_gate")
    return world


def _dialogue(event_id="e1", **overrides):
    payload = {
        "event_id": event_id,
        "type": EventType.DIALOGUE_SPOKEN,
        "occurred_at": CLOCK,
        "scope": EventScope.LOCATION,
        "actor_id": "mizuki",
        "location_id": "kamiyama_high_gate",
        "payload": {"text": "喵？"},
    }
    payload.update(overrides)
    return Event(**payload)


def _turn(number=1, character="mizuki", **overrides):
    fields = {
        "turn_number": number,
        "character": character,
        "prompt": "开场",
        "response": f"reply-from-{character}",
        "timestamp": "2026-08-20T02:00:00",
        "char_name": character.upper(),
        "score": 1,
        "generator_provider": "test-gen",
        "generator_model": "test-model",
        "evaluator_provider": "test-eval",
        "evaluator_model": "test-judge",
    }
    fields.update(overrides)
    return Turn(**fields)


def _session(world):
    state = SessionState(session_id="s1", scene="gate", characters=["mizuki", "ena"])
    state.attach_world_state(world)
    state.initialize_runtime("开场")
    return state


class CommitReferenceValidationTests(unittest.TestCase):
    """未知的角色/地点/频道不能绕过校验进入世界历史。"""

    def setUp(self):
        self.world = _world()
        self.store = EventStore()

    def test_unknown_actor_is_rejected(self):
        with self.assertRaises(EventCommitError):
            commit_event(self.world, self.store, _dialogue(actor_id="kanade"))
        self.assertEqual(len(self.store), 0)

    def test_unknown_participant_is_rejected(self):
        with self.assertRaises(EventCommitError):
            commit_event(
                self.world, self.store, _dialogue(participants=("mizuki", "kanade"))
            )
        self.assertEqual(len(self.store), 0)

    def test_unknown_location_is_rejected(self):
        with self.assertRaises(EventCommitError):
            commit_event(self.world, self.store, _dialogue(location_id="atlantis"))
        self.assertEqual(len(self.store), 0)

    def test_unknown_channel_is_rejected(self):
        event = _dialogue(scope=EventScope.CHANNEL, channel_id="discord")
        with self.assertRaises(EventCommitError):
            commit_event(self.world, self.store, event)
        self.assertEqual(len(self.store), 0)

    def test_prose_location_names_are_not_accepted_as_ids(self):
        with self.assertRaises(EventCommitError):
            commit_event(self.world, self.store, _dialogue(location_id="神山高校校门口"))

    def test_a_world_without_state_cannot_accept_events(self):
        with self.assertRaises(EventCommitError):
            commit_event(None, self.store, _dialogue())
        with self.assertRaises(EventCommitError):
            commit_event(self.world, [], _dialogue())

    def test_character_known_only_through_a_channel_still_counts(self):
        world = _world(channel=True)
        world.remove_character("ena")
        world.join_channel("ena", "nightcord")
        event = _dialogue(
            actor_id="ena",
            scope=EventScope.CHANNEL,
            channel_id="nightcord",
            location_id=None,
        )
        commit_event(world, self.store, event)
        self.assertEqual(len(self.store), 1)

    def test_event_time_must_match_the_authoritative_world_clock(self):
        future = _dialogue(occurred_at=datetime(2030, 1, 1))
        with self.assertRaises(EventCommitError):
            commit_event(self.world, self.store, future)
        self.assertEqual(self.world.clock, CLOCK)
        self.assertEqual(len(self.store), 0)


class CommitStateEffectTests(unittest.TestCase):
    def setUp(self):
        self.world = _world()
        self.store = EventStore()

    def test_commit_applies_state_and_appends_exactly_once(self):
        event = Event(
            event_id="move-1",
            type=EventType.CHARACTER_LOCATION_CHANGED,
            occurred_at=CLOCK,
            scope=EventScope.LOCATION,
            actor_id="mizuki",
            location_id="city_streets",
        )
        projection = commit_event(self.world, self.store, event)
        self.assertEqual(self.world.location_of("mizuki"), "city_streets")
        self.assertEqual(len(self.store), 1)
        self.assertEqual(projection["sequence"], 0)
        self.assertEqual(projection["event_id"], "move-1")

    def test_time_advance_moves_the_clock(self):
        event = Event(
            event_id="tick-1",
            type=EventType.WORLD_TIME_ADVANCED,
            occurred_at=CLOCK,
            scope=EventScope.PUBLIC,
            payload={"minutes": 15},
        )
        commit_event(self.world, self.store, event)
        self.assertEqual(self.world.clock, datetime(2026, 8, 20, 2, 15))

    def test_presence_events_change_channel_membership(self):
        join = Event(
            event_id="join-1",
            type=EventType.PRESENCE_JOINED_CHANNEL,
            occurred_at=CLOCK,
            scope=EventScope.CHANNEL,
            actor_id="mizuki",
            channel_id="nightcord",
        )
        commit_event(self.world, self.store, join)
        self.assertTrue(self.world.is_in_channel("mizuki", "nightcord"))
        leave = Event(
            event_id="leave-1",
            type=EventType.PRESENCE_LEFT_CHANNEL,
            occurred_at=CLOCK,
            scope=EventScope.CHANNEL,
            actor_id="mizuki",
            channel_id="nightcord",
        )
        commit_event(self.world, self.store, leave)
        self.assertFalse(self.world.is_in_channel("mizuki", "nightcord"))
        self.assertEqual(len(self.store), 2)

    def test_presence_events_reject_transitions_that_did_not_happen(self):
        leave = Event(
            event_id="leave-without-membership",
            type=EventType.PRESENCE_LEFT_CHANNEL,
            occurred_at=CLOCK,
            scope=EventScope.CHANNEL,
            actor_id="mizuki",
            channel_id="nightcord",
        )
        with self.assertRaises(EventCommitError):
            commit_event(self.world, self.store, leave)

        self.world.join_channel("mizuki", "nightcord")
        duplicate_join = Event(
            event_id="duplicate-join",
            type=EventType.PRESENCE_JOINED_CHANNEL,
            occurred_at=CLOCK,
            scope=EventScope.CHANNEL,
            actor_id="mizuki",
            channel_id="nightcord",
        )
        with self.assertRaises(EventCommitError):
            commit_event(self.world, self.store, duplicate_join)
        self.assertEqual(len(self.store), 0)

    def test_location_change_rejects_a_noop_transition(self):
        event = Event(
            event_id="move-nowhere",
            type=EventType.CHARACTER_LOCATION_CHANGED,
            occurred_at=CLOCK,
            scope=EventScope.LOCATION,
            actor_id="mizuki",
            location_id="kamiyama_high_gate",
        )
        with self.assertRaises(EventCommitError):
            commit_event(self.world, self.store, event)
        self.assertEqual(len(self.store), 0)

    def test_speech_is_an_occurrence_not_a_state_change(self):
        before = self.world.to_dict()
        commit_event(self.world, self.store, _dialogue())
        self.assertEqual(self.world.to_dict(), before)
        self.assertEqual(len(self.store), 1)

    def test_payload_cannot_mutate_world_state_by_itself(self):
        # 任意 payload 键都不该被当成"要写进世界状态的字典"。
        event = _dialogue(
            payload={
                "text": "喵？",
                "clock": "2099-01-01T00:00:00",
                "character_locations": {"mizuki": "atlantis"},
                "channel_members": {"nightcord": ["mizuki"]},
            }
        )
        commit_event(self.world, self.store, event)
        self.assertEqual(self.world.clock, CLOCK)
        self.assertEqual(self.world.location_of("mizuki"), "kamiyama_high_gate")
        self.assertEqual(self.world.channel_members, {})


class CommitAtomicityTests(unittest.TestCase):
    """任何阶段抛异常都不能留下半提交状态。"""

    def setUp(self):
        self.world = _world()
        self.store = EventStore()

    def test_duplicate_event_id_changes_nothing(self):
        move = Event(
            event_id="move-1",
            type=EventType.CHARACTER_LOCATION_CHANGED,
            occurred_at=CLOCK,
            scope=EventScope.LOCATION,
            actor_id="mizuki",
            location_id="city_streets",
        )
        commit_event(self.world, self.store, move)
        replay = Event(
            event_id="move-1",
            type=EventType.CHARACTER_LOCATION_CHANGED,
            occurred_at=CLOCK,
            scope=EventScope.LOCATION,
            actor_id="mizuki",
            location_id="ena_home_studio",
        )
        with self.assertRaises(EventStoreError):
            commit_event(self.world, self.store, replay)
        # 重复 ID 在改世界之前就被挡住了
        self.assertEqual(self.world.location_of("mizuki"), "city_streets")
        self.assertEqual(len(self.store), 1)

    def test_backwards_timestamp_changes_nothing(self):
        commit_event(self.world, self.store, _dialogue("e1"))
        stale = Event(
            event_id="tick-1",
            type=EventType.WORLD_TIME_ADVANCED,
            occurred_at=datetime(2026, 8, 20, 1, 0),
            scope=EventScope.PUBLIC,
            payload={"minutes": 15},
        )
        with self.assertRaises(EventCommitError):
            commit_event(self.world, self.store, stale)
        self.assertEqual(self.world.clock, CLOCK)
        self.assertEqual(len(self.store), 1)

    def test_a_handler_that_half_applies_then_raises_is_rolled_back(self):
        def _half_apply(world, event):
            world.place_character("mizuki", "city_streets")
            world.advance_time(30)
            raise RuntimeError("apply boom")

        before = self.world.to_dict()
        with patch.dict(
            event_commit._APPLY, {EventType.DIALOGUE_SPOKEN: _half_apply}
        ), self.assertRaises(RuntimeError):
            commit_event(self.world, self.store, _dialogue())

        self.assertEqual(self.world.to_dict(), before)
        self.assertEqual(len(self.store), 0)

    def test_a_failure_while_appending_rolls_the_world_back(self):
        move = Event(
            event_id="move-1",
            type=EventType.CHARACTER_LOCATION_CHANGED,
            occurred_at=CLOCK,
            scope=EventScope.LOCATION,
            actor_id="mizuki",
            location_id="city_streets",
        )
        before = self.world.to_dict()
        with patch.object(
            EventStore, "_append", side_effect=RuntimeError("append boom")
        ), self.assertRaises(RuntimeError):
            commit_event(self.world, self.store, move)

        self.assertEqual(self.world.to_dict(), before)
        self.assertEqual(len(self.store), 0)

    def test_an_unimplemented_type_cannot_be_committed(self):
        with patch.dict(event_commit._APPLY, clear=True), self.assertRaises(
            EventCommitError
        ):
            commit_event(self.world, self.store, _dialogue())
        self.assertEqual(len(self.store), 0)

    def test_the_returned_projection_is_a_fresh_structure(self):
        projection = commit_event(self.world, self.store, _dialogue())
        projection["payload"]["text"] = "被改掉了"
        projection["participants"].append("ena")
        self.assertEqual(self.store.get("e1").payload["text"], "喵？")
        self.assertEqual(self.store.get("e1").participants, ())


class SessionCommitTests(unittest.TestCase):
    """事件历史 + 生成记录 + 角色历史必须一起落地或一起不落地。"""

    def setUp(self):
        self.world = _world()
        self.state = _session(self.world)

    def test_dialogue_commit_records_one_event_and_one_turn(self):
        turn = _turn()
        event = dialogue_event_for_turn(self.world, self.state.events, "s1", turn)
        projection = commit_dialogue(self.state, turn, event)

        self.assertEqual(len(self.state.events), 1)
        self.assertEqual(len(self.state.turns), 1)
        self.assertEqual(projection["sequence"], 0)
        self.assertEqual(self.state.events.latest().payload["text"], turn.response)

    def test_a_rejected_turn_rolls_back_the_event_too(self):
        turn = _turn(number=99)  # 轮次编号不对，record_turn 会拒绝
        event = dialogue_event_for_turn(self.world, self.state.events, "s1", turn)
        with self.assertRaises(ValueError):
            commit_dialogue(self.state, turn, event)

        self.assertEqual(len(self.state.events), 0)
        self.assertEqual(len(self.state.turns), 0)
        # 角色历史也不能留下半句话
        self.assertEqual(len(self.state.histories["mizuki"]), 1)
        self.assertEqual(len(self.state.histories["ena"]), 1)

    def test_a_rejected_event_leaves_no_turn(self):
        turn = _turn()
        event = dialogue_event_for_turn(self.world, self.state.events, "s1", turn)
        self.state.events._append(event)  # 模拟内部历史里已经存在同 ID
        with self.assertRaises(EventStoreError):
            commit_dialogue(self.state, turn, event)
        self.assertEqual(len(self.state.turns), 0)
        self.assertEqual(len(self.state.events), 1)

    def test_rollback_restores_world_state_and_pending_corrections(self):
        self.state.pending_corrections["mizuki"] = "留在角色里"
        before = self.world.to_dict()
        with self.assertRaises(RuntimeError), self.state.atomic_commit():
            self.world.advance_time(60)
            self.state.events._append(_dialogue("e1"))
            self.state.record_turn(_turn())
            raise RuntimeError("boom")

        self.assertEqual(self.world.to_dict(), before)
        self.assertEqual(len(self.state.events), 0)
        self.assertEqual(self.state.turns, [])
        self.assertEqual(self.state.pending_corrections["mizuki"], "留在角色里")
        self.assertEqual(len(self.state.histories["mizuki"]), 1)

    def test_a_failure_midway_through_recording_the_turn_is_rolled_back(self):
        # record_turn 先 append turns 再改角色历史；如果它在两者之间炸掉，
        # 事务必须把已经落地的那半截也收回去。
        turn = _turn()
        event = dialogue_event_for_turn(self.world, self.state.events, "s1", turn)
        del self.state.histories["mizuki"]  # 让 record_turn 在 append 之后抛 KeyError

        with self.assertRaises(KeyError):
            commit_dialogue(self.state, turn, event)

        self.assertEqual(self.state.turns, [])
        self.assertEqual(len(self.state.events), 0)
        self.assertEqual(len(self.state.histories["ena"]), 1)

    def test_commit_session_event_is_atomic_as_well(self):
        before = self.world.to_dict()
        with patch.object(
            EventStore, "_append", side_effect=RuntimeError("boom")
        ), self.assertRaises(RuntimeError):
            commit_session_event(self.state, _dialogue())
        self.assertEqual(self.world.to_dict(), before)
        self.assertEqual(len(self.state.events), 0)

    def test_sessions_do_not_share_event_history(self):
        other = _session(_world())
        commit_session_event(self.state, _dialogue("e1"))
        self.assertEqual(len(self.state.events), 1)
        self.assertEqual(len(other.events), 0)
        self.assertIsNot(self.state.events, other.events)

    def test_session_serialization_carries_events_without_leaking_them(self):
        commit_session_event(self.state, _dialogue("e1"))
        payload = self.state.to_dict()
        self.assertEqual(payload["events"]["events"][0]["event_id"], "e1")
        payload["events"]["events"][0]["payload"]["text"] = "改"
        payload["histories"]["mizuki"].append({"role": "user", "content": "改"})
        payload["pending_corrections"]["mizuki"] = "改"
        payload["metadata"]["injected"] = True
        self.assertEqual(self.state.events.get("e1").payload["text"], "喵？")
        self.assertEqual(len(self.state.histories["mizuki"]), 1)
        self.assertIsNone(self.state.pending_corrections["mizuki"])
        self.assertEqual(self.state.metadata, {})


class DialogueEventDerivationTests(unittest.TestCase):
    """发言事件的落点由权威世界状态推导，不看遗留 scene 的散文地名。"""

    def test_co_located_speech_is_a_location_event(self):
        world = _world()
        store = EventStore()
        event = dialogue_event_for_turn(world, store, "s1", _turn())
        self.assertEqual(event.type, EventType.DIALOGUE_SPOKEN)
        self.assertEqual(event.scope, EventScope.LOCATION)
        self.assertEqual(event.location_id, "kamiyama_high_gate")
        self.assertIsNone(event.channel_id)
        self.assertEqual(event.participants, ("ena", "mizuki"))

    def test_online_speech_is_a_channel_event_carrying_the_physical_place(self):
        world = _world(channel=True)
        store = EventStore()
        event = dialogue_event_for_turn(world, store, "s1", _turn())
        self.assertEqual(event.scope, EventScope.CHANNEL)
        self.assertEqual(event.channel_id, "nightcord")
        # 人还是在自己房间里 —— 线上在场不抹掉物理位置
        self.assertEqual(event.location_id, "mizuki_home_room")
        self.assertEqual(event.participants, ("ena", "mizuki"))

    def test_a_text_channel_produces_message_sent(self):
        from pns.models.channel import Channel, ChannelKind, ChannelRegistry

        world = WorldState(
            clock=CLOCK,
            locations=build_default_location_graph(),
            channels=ChannelRegistry(
                [Channel(channel_id="dm", name="DM", kind=ChannelKind.TEXT)]
            ),
        )
        world.place_character("mizuki", "mizuki_home_room")
        world.join_channel("mizuki", "dm")
        event = dialogue_event_for_turn(world, EventStore(), "s1", _turn())
        self.assertEqual(event.type, EventType.MESSAGE_SENT)

    def test_a_character_nowhere_cannot_speak(self):
        world = _world()
        world.remove_character("mizuki")
        with self.assertRaises(EventCommitError):
            dialogue_event_for_turn(world, EventStore(), "s1", _turn())

    def test_provenance_links_back_to_the_generation_record(self):
        world = _world()
        turn = _turn(number=3, score=6, is_ooc=True)
        event = dialogue_event_for_turn(world, EventStore(), "s1", turn)
        self.assertEqual(event.provenance["kind"], "generation")
        self.assertEqual(event.provenance["turn_number"], 3)
        self.assertEqual(event.provenance["session_id"], "s1")
        self.assertEqual(event.provenance["generator_model"], "test-model")
        self.assertEqual(event.provenance["evaluator_model"], "test-judge")
        self.assertEqual(event.provenance["drift_score"], 6)
        self.assertTrue(event.provenance["is_ooc"])
        self.assertEqual(event.correlation_id, "s1")
        self.assertEqual(event.event_id, "s1:t3:dialogue")

    def test_each_event_points_at_the_previous_one(self):
        world = _world()
        store = EventStore()
        first = dialogue_event_for_turn(world, store, "s1", _turn(1))
        store._append(first)
        second = dialogue_event_for_turn(world, store, "s1", _turn(2, "ena"))
        self.assertIsNone(first.causation_id)
        self.assertEqual(second.causation_id, first.event_id)

    def test_the_occurrence_time_is_the_simulation_clock(self):
        world = _world()
        world.advance_time(45)
        event = dialogue_event_for_turn(world, EventStore(), "s1", _turn())
        self.assertEqual(event.occurred_at, world.clock)


class TurnProjectionTests(unittest.TestCase):
    """遗留 turn 消息是投影，不是第二份真相。"""

    def test_the_legacy_wire_shape_is_preserved(self):
        world = _world()
        state = _session(world)
        turn = _turn()
        event = dialogue_event_for_turn(world, state.events, "s1", turn)
        committed = commit_dialogue(state, turn, event)
        message = project_turn_message(committed, turn)

        legacy = turn.to_wire_dict()
        for key, value in legacy.items():
            self.assertEqual(message[key], value, key)
        self.assertEqual(message["type"], "turn")
        # 唯一新增的是回指已提交事件的链接
        self.assertEqual(set(message) - set(legacy) - {"type"}, {"event_id"})
        self.assertEqual(message["event_id"], event.event_id)
        self.assertTrue(state.events.has(message["event_id"]))


if __name__ == "__main__":
    unittest.main()


class CommitTimeExposureTests(unittest.TestCase):
    """提交边界的第三阶段：每条已提交事件都被曝光判定过，且与事件同生共死。"""

    def setUp(self):
        self.world = _world()
        self.state = SessionState(
            session_id="s1", scene="gate", characters=["mizuki", "ena"]
        )
        self.state.attach_world_state(self.world)
        self.state.initialize_runtime("放学后的校门口")

    def _commit(self, turn=None):
        turn = turn or _turn()
        event = dialogue_event_for_turn(self.world, self.state.events, "s1", turn)
        return commit_dialogue(self.state, turn, event), event

    def test_every_committed_event_is_evaluated_for_every_candidate(self):
        _, event = self._commit()
        decisions = self.state.exposures.for_event(event.event_id)
        self.assertEqual(
            sorted(d.character_id for d in decisions), ["ena", "mizuki"]
        )

    def test_only_eligible_characters_get_an_observation(self):
        self.world.place_character("ena", "city_streets")
        _, event = self._commit()
        self.assertEqual(
            self.state.observations.observers_of(event.event_id), ("mizuki",)
        )
        self.assertEqual(
            self.state.exposures.explain(event.event_id, "ena").reason,
            ExposureReason.WRONG_LOCATION,
        )

    def test_the_legacy_history_is_projected_from_observations(self):
        # 绘名不在场 → 这句话不会出现在她的历史里。这正是要拆掉的全知扇出。
        self.world.place_character("ena", "city_streets")
        self._commit()
        self.assertEqual(len(self.state.histories["mizuki"]), 2)
        self.assertEqual(len(self.state.histories["ena"]), 1)

    def test_co_located_characters_still_share_the_conversation(self):
        self._commit()
        self.assertEqual(self.state.histories["ena"][-1]["role"], "user")
        # 行格式与遗留全知扇出完全一致：兼容投影没有改变提示词的样子
        self.assertEqual(
            self.state.histories["ena"][-1]["content"], "MIZUKI：reply-from-mizuki"
        )
        self.assertEqual(self.state.histories["mizuki"][-1]["role"], "assistant")

    def test_a_failed_commit_leaves_no_observation_or_decision(self):
        turn = _turn(number=99)  # 轮次编号不对，record_turn 会拒绝
        event = dialogue_event_for_turn(self.world, self.state.events, "s1", turn)
        with self.assertRaises(ValueError):
            commit_dialogue(self.state, turn, event)
        self.assertEqual(len(self.state.events), 0)
        self.assertEqual(len(self.state.observations), 0)
        self.assertEqual(len(self.state.exposures), 0)

    def test_non_dialogue_events_are_exposed_too(self):
        event = Event(
            event_id="joined",
            type=EventType.PRESENCE_JOINED_CHANNEL,
            occurred_at=self.world.clock,
            scope=EventScope.CHANNEL,
            actor_id="mizuki",
            channel_id="nightcord",
        )
        commit_session_event(self.state, event)
        # 判定跑在状态效果之后：瑞希这时已经在频道里了，所以自己观察到了。
        self.assertEqual(
            self.state.observations.observers_of("joined"), ("mizuki",)
        )
        self.assertEqual(
            self.state.exposures.explain("joined", "ena").reason,
            ExposureReason.NO_CHANNEL_ACCESS,
        )

    def test_joining_later_does_not_backfill_earlier_observations(self):
        # 观察在提交那一刻一次性落地。后来才入频道的角色不会回溯拿到之前的
        # 事件 —— 除非以后显式加一条 replay 规则，而本阶段没有。
        self.world.place_character("ena", "city_streets")
        _, event = self._commit()
        self.assertEqual(self.state.observations.for_character("ena"), ())

        self.world.place_character("ena", "kamiyama_high_gate")
        self.assertEqual(self.state.observations.for_character("ena"), ())
        self.assertEqual(len(self.state.histories["ena"]), 1)

    def test_the_debug_path_explains_both_sides(self):
        self.world.place_character("ena", "city_streets")
        _, event = self._commit()
        report = explain_event(self.state, event.event_id)
        by_character = {d["character_id"]: d for d in report["decisions"]}
        self.assertTrue(by_character["mizuki"]["observation_created"])
        self.assertFalse(by_character["ena"]["observation_created"])
        self.assertEqual(by_character["ena"]["reason"], "wrong_location")
        self.assertEqual(
            explain_character(self.state, event.event_id, "ena")["exposed"], False
        )
        self.assertIsNone(explain_character(self.state, event.event_id, "kanade"))

    def test_debug_data_never_reaches_a_character_history(self):
        self.world.place_character("ena", "city_streets")
        self._commit()
        for history in self.state.histories.values():
            for item in history:
                for leaked in ("wrong_location", "exposed", "reason", "event_id"):
                    self.assertNotIn(leaked, item["content"])
                self.assertEqual(set(item), {"role", "content"})

    def test_serialization_exposes_both_logs(self):
        _, event = self._commit()
        payload = self.state.to_dict()
        self.assertEqual(len(payload["observations"]["observations"]), 2)
        self.assertEqual(len(payload["exposures"]["decisions"]), 2)
        # 序列化结果是新的可变结构，改它影响不到权威状态
        payload["observations"]["observations"].clear()
        self.assertEqual(len(self.state.observations), 2)


class LegacyRecordPathTests(unittest.TestCase):
    """不经过世界模型的纯记录调用方仍然走旧的全知投影。"""

    def test_record_turn_without_observations_keeps_the_legacy_fan_out(self):
        state = SessionState(
            session_id="s1", scene="gate", characters=["mizuki", "ena"]
        )
        state.initialize_runtime("放学后的校门口")
        state.record_turn(_turn())
        self.assertEqual(len(state.histories["ena"]), 2)
        self.assertEqual(len(state.observations), 0)
