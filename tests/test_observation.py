# tests/test_observation.py — 观察投影的不变量。
#
# 这里守的核心是一句话：**观察里不许出现角色感知不到的信息**。所以每条
# 测试都是从"事件里有什么"出发，去证伪"它有没有漏进观察"。
#
# 运行: python -m unittest tests.test_observation -v
import unittest
from datetime import datetime

from pns.models.event import Event, EventScope, EventType
from pns.models.exposure import ExposureDecision, ExposureReason
from pns.models.observation import Observation, ObservationError, ObservationLog
from pns.models.world_state import WorldState
from pns.runtime.exposure import (
    evaluate_event_exposure,
    evaluate_exposure,
    observation_for,
    observations_for,
)
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph

CLOCK = datetime(2026, 8, 20, 2, 0)


def _world(**placements) -> WorldState:
    world = WorldState(
        clock=CLOCK,
        locations=build_default_location_graph(),
        channels=build_default_channel_registry(),
    )
    for character_id, location_id in (
        placements or {"mizuki": "kamiyama_high_gate", "ena": "kamiyama_high_gate"}
    ).items():
        world.place_character(character_id, location_id)
    return world


def _event(**overrides) -> Event:
    payload = {
        "event_id": "e1",
        "type": EventType.DIALOGUE_SPOKEN,
        "occurred_at": CLOCK,
        "scope": EventScope.LOCATION,
        "actor_id": "mizuki",
        "location_id": "kamiyama_high_gate",
        "payload": {"text": "喵？", "char_name": "瑞希"},
    }
    payload.update(overrides)
    return Event(**payload)


def _observe(world, event, character_id):
    return observation_for(event, evaluate_exposure(world, event, character_id))


class SelfObservationTests(unittest.TestCase):
    def test_a_self_action_produces_a_self_observation(self):
        observation = _observe(_world(), _event(), "mizuki")
        self.assertIsNotNone(observation)
        self.assertTrue(observation.is_self_observation)
        self.assertEqual(observation.observer_id, "mizuki")
        self.assertEqual(observation.source_event_id, "e1")
        self.assertEqual(observation.perceived["text"], "喵？")

    def test_the_actor_perceives_both_of_their_own_anchors(self):
        world = _world(mizuki="mizuki_home_room", ena="ena_home_studio")
        world.join_channel("mizuki", "nightcord")
        world.join_channel("ena", "nightcord")
        event = _event(
            scope=EventScope.CHANNEL,
            channel_id="nightcord",
            location_id="mizuki_home_room",
        )
        observation = _observe(world, event, "mizuki")
        self.assertEqual(observation.perceived["channel_id"], "nightcord")
        self.assertEqual(observation.perceived["location_id"], "mizuki_home_room")


class RedactionTests(unittest.TestCase):
    """事件里属于系统侧的一切都不进观察。"""

    def _rich_event(self):
        return _event(
            payload={
                "text": "喵？",
                "char_name": "瑞希",
                "internal_note": "只有系统该看到的东西",
            },
            provenance={
                "kind": "generation",
                "drift_score": 8,
                "is_ooc": True,
                "generator_model": "some-model",
            },
            correlation_id="session-123",
            causation_id="e0",
        )

    def test_provenance_never_reaches_a_character(self):
        # 架构文档 §15：Router 打了几分是系统过程，不是角色经验。
        for character_id in ("mizuki", "ena"):
            observation = _observe(_world(), self._rich_event(), character_id)
            flat = observation.to_dict()["perceived"]
            for leaked in ("drift_score", "is_ooc", "generator_model", "kind"):
                self.assertNotIn(leaked, flat, f"{character_id} 看到了 {leaked}")
            self.assertNotIn("provenance", flat)

    def test_unlisted_payload_keys_are_dropped(self):
        observation = _observe(_world(), self._rich_event(), "ena")
        self.assertIn("text", observation.perceived)
        self.assertNotIn("internal_note", observation.perceived)

    def test_system_bookkeeping_ids_are_dropped(self):
        observation = _observe(_world(), self._rich_event(), "ena")
        self.assertNotIn("correlation_id", observation.perceived)
        self.assertNotIn("causation_id", observation.perceived)

    def test_a_new_event_type_leaks_nothing_by_default(self):
        # 白名单式删减的意义：没登记的类型默认什么都不透出。
        event = _event(
            type=EventType.PRESENCE_JOINED_CHANNEL,
            scope=EventScope.CHANNEL,
            channel_id="nightcord",
            location_id=None,
            payload={"invite_code": "secret"},
        )
        world = _world(mizuki="mizuki_home_room", ena="ena_home_studio")
        world.join_channel("mizuki", "nightcord")
        world.join_channel("ena", "nightcord")
        observation = _observe(world, event, "ena")
        self.assertNotIn("invite_code", observation.perceived)
        self.assertEqual(observation.perceived["type"], "presence.joined_channel")


class AnchorRedactionTests(unittest.TestCase):
    """角色只感知得到自己那条通道。"""

    def test_a_channel_listener_does_not_learn_the_speakers_room(self):
        world = _world(mizuki="mizuki_home_room", ena="ena_home_studio")
        world.join_channel("mizuki", "nightcord")
        world.join_channel("ena", "nightcord")
        event = _event(
            scope=EventScope.CHANNEL,
            channel_id="nightcord",
            location_id="mizuki_home_room",
        )
        observation = _observe(world, event, "ena")
        self.assertEqual(observation.perceived["channel_id"], "nightcord")
        self.assertNotIn("location_id", observation.perceived)

    def test_a_co_located_listener_does_not_learn_the_channel(self):
        world = _world()
        event = _event(channel_id="nightcord")
        observation = _observe(world, event, "ena")
        self.assertEqual(observation.perceived["location_id"], "kamiyama_high_gate")
        self.assertNotIn("channel_id", observation.perceived)

    def test_an_ambient_roster_is_not_handed_to_bystanders(self):
        # channel / location 档的 participants 是在场快照。把它给出去等于
        # 告诉一个旁观者整个频道的成员表。
        world = _world(mizuki="kamiyama_high_gate", ena="kamiyama_high_gate")
        world.place_character("kanade", "kamiyama_high_gate")
        event = _event(participants=("mizuki", "ena", "kanade"))
        for character_id in ("mizuki", "ena", "kanade"):
            observation = _observe(world, event, character_id)
            self.assertNotIn("participants", observation.perceived, character_id)

    def test_a_named_recipient_does_see_who_else_was_named(self):
        # private / participant 档里 participants 表示"被点名的人"，收件人
        # 有资格知道同批收件人是谁。
        world = _world(mizuki="kamiyama_high_gate", ena="kamiyama_high_gate")
        event = _event(scope=EventScope.PARTICIPANT, participants=("mizuki", "ena"))
        observation = _observe(world, event, "ena")
        # 冻结之后列表是元组，序列化出去仍是列表。
        self.assertEqual(observation.perceived["participants"], ("mizuki", "ena"))
        self.assertEqual(
            observation.to_dict()["perceived"]["participants"], ["mizuki", "ena"]
        )


class ProjectionContractTests(unittest.TestCase):
    def test_a_denied_decision_produces_no_observation(self):
        world = _world(mizuki="kamiyama_high_gate", ena="city_streets")
        self.assertIsNone(_observe(world, _event(), "ena"))

    def test_observations_for_only_returns_the_exposed_ones(self):
        world = _world(mizuki="kamiyama_high_gate", ena="city_streets")
        world.place_character("kanade", "kamiyama_high_gate")
        event = _event()
        decisions = evaluate_event_exposure(world, event)
        observations = observations_for(event, decisions)
        self.assertEqual(
            sorted(o.observer_id for o in observations), ["kanade", "mizuki"]
        )

    def test_a_decision_from_another_event_is_refused(self):
        decision = ExposureDecision("other", "ena", ExposureReason.SAME_LOCATION, CLOCK)
        with self.assertRaises(ValueError):
            observation_for(_event(), decision)


class ObservationModelTests(unittest.TestCase):
    def _observation(self, **overrides):
        base = {
            "source_event_id": "e1",
            "observer_id": "ena",
            "reason": ExposureReason.SAME_LOCATION,
            "observed_at": CLOCK,
            "perceived": {"text": "喵？", "char_name": "瑞希"},
        }
        base.update(overrides)
        return Observation(**base)

    def test_a_denied_reason_cannot_become_an_observation(self):
        # 这一层最严重的错误就是"没曝光却生成了观察"，在类型层面直接拦死。
        for reason in (
            ExposureReason.WRONG_LOCATION,
            ExposureReason.PRIVATE_SCOPE_DENIED,
            ExposureReason.UNAVAILABLE,
        ):
            with self.subTest(reason=reason), self.assertRaises(ObservationError):
                self._observation(reason=reason)

    def test_invalid_observations_are_rejected(self):
        for kwargs in (
            {"source_event_id": ""},
            {"observer_id": ""},
            {"reason": "definitely_not_a_reason"},
            {"observed_at": "2026-08-20"},
            {"perceived": ["not", "a", "dict"]},
            {"perceived": {"bad": object()}},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ObservationError):
                self._observation(**kwargs)

    def test_perceived_is_frozen_against_the_caller(self):
        perceived = {"text": "喵？"}
        observation = self._observation(perceived=perceived)
        perceived["text"] = "改了"
        self.assertEqual(observation.perceived["text"], "喵？")
        with self.assertRaises(TypeError):
            observation.perceived["text"] = "改了"

    def test_render_line_matches_the_legacy_history_shape(self):
        self.assertEqual(self._observation().render_line(), "瑞希：喵？")

    def test_a_non_dialogue_observation_has_no_legacy_line(self):
        observation = self._observation(perceived={"type": "presence.joined_channel"})
        self.assertIsNone(observation.render_line())

    def test_observations_are_hashable_by_identity(self):
        self.assertEqual(len({self._observation(), self._observation()}), 1)

    def test_round_trips_through_a_dict(self):
        observation = self._observation()
        self.assertEqual(Observation.from_dict(observation.to_dict()), observation)


class ObservationLogTests(unittest.TestCase):
    def _log(self):
        return ObservationLog(
            (
                Observation("e1", "mizuki", ExposureReason.SELF_ACTION, CLOCK),
                Observation("e1", "ena", ExposureReason.SAME_LOCATION, CLOCK),
                Observation("e2", "ena", ExposureReason.CHANNEL_MEMBER, CLOCK),
            )
        )

    def test_per_character_streams_preserve_order(self):
        log = self._log()
        self.assertEqual(
            [o.source_event_id for o in log.for_character("ena")], ["e1", "e2"]
        )
        self.assertEqual(len(log.for_character("mizuki")), 1)
        self.assertEqual(log.for_character("kanade"), ())

    def test_observers_of_an_event(self):
        self.assertEqual(self._log().observers_of("e1"), ("mizuki", "ena"))

    def test_rollback_and_type_checks(self):
        log = self._log()
        log._rollback_to(1)
        self.assertEqual(len(log), 1)
        with self.assertRaises(ObservationError):
            log._rollback_to(9)
        with self.assertRaises(ObservationError):
            log._append("not an observation")

    def test_round_trips_through_a_dict(self):
        log = self._log()
        self.assertEqual(
            ObservationLog.from_dict(log.to_dict()).observations(), log.observations()
        )


if __name__ == "__main__":
    unittest.main()
