# tests/test_exposure.py — 曝光判定的不变量。
#
# 这里守的核心是一句话：**被选进会话不等于感知得到**。每条规则都要能
# 单独证伪 —— 所以每个 scope 都同时测"该给的给了"和"不该给的没给"。
#
# 运行: python -m unittest tests.test_exposure -v
import unittest
from datetime import datetime

from pns.models.channel import Channel, ChannelKind, ChannelRegistry
from pns.models.event import Event, EventScope, EventType
from pns.models.exposure import (
    ExposureDecision,
    ExposureError,
    ExposureLog,
    ExposureReason,
)
from pns.models.location import Connection, Location, LocationGraph, LocationKind
from pns.models.world_state import Availability, WorldState
from pns.runtime.exposure import (
    ExposureRuleError,
    candidate_characters,
    evaluate_event_exposure,
    evaluate_exposure,
)
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph

CLOCK = datetime(2026, 8, 20, 2, 0)


def _world(**placements) -> WorldState:
    """三个角色的默认世界；不给安排就都放在校门口。"""
    world = WorldState(
        clock=CLOCK,
        locations=build_default_location_graph(),
        channels=build_default_channel_registry(),
    )
    placements = placements or {
        "mizuki": "kamiyama_high_gate",
        "ena": "kamiyama_high_gate",
        "kanade": "kamiyama_high_gate",
    }
    for character_id, location_id in placements.items():
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


def _reasons(world, event):
    return {
        decision.character_id: decision.reason
        for decision in evaluate_event_exposure(world, event)
    }


def _exposed(world, event):
    return sorted(
        decision.character_id
        for decision in evaluate_event_exposure(world, event)
        if decision.exposed
    )


class SelfActionTests(unittest.TestCase):
    """自动作走自观察通道，不受外部感知规则约束。"""

    def test_actor_always_self_observes(self):
        world = _world(mizuki="mizuki_home_room", ena="ena_home_studio")
        decision = evaluate_exposure(
            world, _event(location_id="mizuki_home_room"), "mizuki"
        )
        self.assertEqual(decision.reason, ExposureReason.SELF_ACTION)
        self.assertTrue(decision.exposed)

    def test_self_observation_survives_a_private_scope(self):
        world = _world()
        event = _event(scope=EventScope.PRIVATE, participants=("ena",))
        self.assertEqual(
            evaluate_exposure(world, event, "mizuki").reason,
            ExposureReason.SELF_ACTION,
        )

    def test_a_world_event_without_an_actor_has_no_self_observer(self):
        world = _world()
        event = _event(
            type=EventType.WORLD_TIME_ADVANCED,
            scope=EventScope.PUBLIC,
            actor_id=None,
            location_id=None,
            payload={"minutes": 10},
        )
        for reason in _reasons(world, event).values():
            self.assertNotEqual(reason, ExposureReason.SELF_ACTION)


class LocationScopeTests(unittest.TestCase):
    def test_co_located_characters_perceive_and_others_do_not(self):
        world = _world(
            mizuki="kamiyama_high_gate",
            ena="kamiyama_high_gate",
            kanade="ena_home_studio",
        )
        reasons = _reasons(world, _event())
        self.assertEqual(reasons["mizuki"], ExposureReason.SELF_ACTION)
        self.assertEqual(reasons["ena"], ExposureReason.SAME_LOCATION)
        self.assertEqual(reasons["kanade"], ExposureReason.WRONG_LOCATION)
        self.assertEqual(_exposed(world, _event()), ["ena", "mizuki"])

    def test_containment_alone_does_not_carry_sound(self):
        # 校门口的 parent 是神山高校。人在楼里不等于听得见校门口说话 ——
        # 父子包含关系不是可闻关系。
        world = _world(
            mizuki="kamiyama_high_gate",
            ena="kamiyama_high",
            kanade="city_streets",
        )
        reasons = _reasons(world, _event())
        self.assertEqual(reasons["ena"], ExposureReason.WRONG_LOCATION)
        self.assertEqual(reasons["kanade"], ExposureReason.WRONG_LOCATION)

    def test_declared_audibility_widens_the_result(self):
        graph = LocationGraph(
            (
                Location(location_id="hall", name="大厅", kind=LocationKind.ROOM),
                Location(
                    location_id="stage",
                    name="舞台",
                    kind=LocationKind.ROOM,
                    connections=(Connection("hall", travel_minutes=1),),
                    # 台上说的话，大厅里也听得见 —— 显式声明，不靠推导。
                    perception={"audible_from": ["hall"]},
                ),
            )
        )
        world = WorldState(clock=CLOCK, locations=graph, channels=ChannelRegistry())
        world.place_character("mizuki", "stage")
        world.place_character("ena", "hall")
        reasons = _reasons(world, _event(location_id="stage"))
        self.assertEqual(reasons["ena"], ExposureReason.AUDIBLE_FROM)
        self.assertTrue(reasons["ena"].exposed)

    def test_rooms_are_closed_by_default(self):
        # 反过来：没声明 audible_from 的地点，隔壁一律听不见。
        world = _world(mizuki="ena_home_studio", ena="ena_home")
        self.assertEqual(
            _reasons(world, _event(location_id="ena_home_studio"))["ena"],
            ExposureReason.WRONG_LOCATION,
        )

    def test_an_unlocated_character_perceives_no_location_event(self):
        world = _world(mizuki="kamiyama_high_gate", ena="kamiyama_high_gate")
        world.join_channel("kanade", "nightcord")  # 只在线上，没有物理位置
        self.assertEqual(
            _reasons(world, _event())["kanade"], ExposureReason.WRONG_LOCATION
        )


class ChannelScopeTests(unittest.TestCase):
    def _channel_world(self):
        world = _world(
            mizuki="mizuki_home_room",
            ena="ena_home_studio",
            kanade="mizuki_home_room",
        )
        world.join_channel("mizuki", "nightcord")
        world.join_channel("ena", "nightcord")
        return world

    def _channel_event(self, **overrides):
        return _event(
            scope=EventScope.CHANNEL,
            channel_id="nightcord",
            location_id="mizuki_home_room",
            **overrides,
        )

    def test_membership_decides_not_physical_distance(self):
        world = self._channel_world()
        reasons = _reasons(world, self._channel_event())
        # 绘名在另一个房间，但在频道里 → 听得见
        self.assertEqual(reasons["ena"], ExposureReason.CHANNEL_MEMBER)
        # 奏和瑞希同处一室，但不在频道里 → 收不到这条频道事件
        self.assertEqual(reasons["kanade"], ExposureReason.NO_CHANNEL_ACCESS)

    def test_leaving_the_channel_stops_delivery(self):
        world = self._channel_world()
        world.leave_channel("ena", "nightcord")
        self.assertEqual(
            _reasons(world, self._channel_event())["ena"],
            ExposureReason.NO_CHANNEL_ACCESS,
        )

    def test_a_character_who_joins_later_does_not_receive_earlier_events(self):
        # 曝光在提交那一刻按当时的世界判定。这里直接证伪"事后补票"：
        # 判定的时候还没入频道，就是收不到；之后入了也不会回溯。
        world = self._channel_world()
        event = self._channel_event()
        before = evaluate_exposure(world, event, "kanade")
        self.assertEqual(before.reason, ExposureReason.NO_CHANNEL_ACCESS)

        world.join_channel("kanade", "nightcord")
        after = evaluate_exposure(world, event, "kanade")
        # 世界快照变了，重新判定的结果当然会变 —— 所以观察必须在提交时
        # 一次性落地，而不是每次要用的时候回头重算整段历史。
        self.assertEqual(after.reason, ExposureReason.CHANNEL_MEMBER)
        self.assertNotEqual(before, after)


class ParticipantAndPrivacyTests(unittest.TestCase):
    def test_participant_scope_reaches_named_characters_only(self):
        world = _world()
        event = _event(scope=EventScope.PARTICIPANT, participants=("mizuki", "ena"))
        reasons = _reasons(world, event)
        self.assertEqual(reasons["ena"], ExposureReason.EXPLICIT_PARTICIPANT)
        self.assertEqual(reasons["kanade"], ExposureReason.NOT_A_PARTICIPANT)

    def test_private_events_do_not_leak_through_co_location(self):
        # 三个人在同一个地点。私密事件只给被点名的人 —— 同处一地不构成
        # 任何豁免。
        world = _world()
        event = _event(scope=EventScope.PRIVATE, participants=("ena",))
        reasons = _reasons(world, event)
        self.assertEqual(reasons["ena"], ExposureReason.EXPLICIT_PARTICIPANT)
        self.assertEqual(reasons["kanade"], ExposureReason.PRIVATE_SCOPE_DENIED)
        self.assertEqual(_exposed(world, event), ["ena", "mizuki"])

    def test_an_ambient_participant_snapshot_is_not_an_exposure_grant(self):
        # P5 会把"提交时在场的人"记进 participants。那是历史事实，不是曝光
        # 依据：绘名已经走了，location 档必须回世界状态现算，判它听不见。
        world = _world(
            mizuki="kamiyama_high_gate",
            ena="city_streets",
            kanade="kamiyama_high_gate",
        )
        event = _event(participants=("mizuki", "ena", "kanade"))
        reasons = _reasons(world, event)
        self.assertEqual(reasons["ena"], ExposureReason.WRONG_LOCATION)
        self.assertEqual(reasons["kanade"], ExposureReason.SAME_LOCATION)


class PublicScopeTests(unittest.TestCase):
    """公开 ≠ 自动知道：本阶段只给当下就在感知范围内的角色。"""

    def test_public_event_reaches_only_characters_currently_in_range(self):
        world = _world(
            mizuki="kamiyama_high_gate",
            ena="kamiyama_high_gate",
            kanade="city_streets",
        )
        event = _event(scope=EventScope.PUBLIC)
        reasons = _reasons(world, event)
        self.assertEqual(reasons["ena"], ExposureReason.PUBLIC_VISIBLE)
        self.assertEqual(reasons["kanade"], ExposureReason.PUBLIC_NOT_PERCEIVED)

    def test_public_event_without_any_anchor_reaches_nobody_yet(self):
        world = _world()
        event = _event(
            type=EventType.WORLD_TIME_ADVANCED,
            scope=EventScope.PUBLIC,
            actor_id=None,
            location_id=None,
            payload={"minutes": 5},
        )
        self.assertEqual(_exposed(world, event), [])
        for reason in _reasons(world, event).values():
            self.assertEqual(reason, ExposureReason.PUBLIC_NOT_PERCEIVED)

    def test_a_public_channel_event_uses_channel_membership(self):
        world = _world(mizuki="mizuki_home_room", ena="ena_home_studio")
        world.join_channel("mizuki", "nightcord")
        world.join_channel("ena", "nightcord")
        world.place_character("kanade", "city_streets")
        event = _event(
            scope=EventScope.PUBLIC, channel_id="nightcord", location_id=None
        )
        reasons = _reasons(world, event)
        self.assertEqual(reasons["ena"], ExposureReason.PUBLIC_VISIBLE)
        self.assertEqual(reasons["kanade"], ExposureReason.PUBLIC_NOT_PERCEIVED)


class AvailabilityTests(unittest.TestCase):
    """三档可用性的后果各不相同 —— 这正是它们值得分开的原因。"""

    def test_busy_does_not_block_perception(self):
        world = _world()
        world.set_availability("ena", Availability.BUSY)
        decision = evaluate_exposure(world, _event(), "ena")
        self.assertEqual(decision.reason, ExposureReason.SAME_LOCATION)
        self.assertTrue(decision.exposed)

    def test_asleep_blocks_perception_everywhere(self):
        world = _world(mizuki="mizuki_home_room", ena="ena_home_studio")
        world.join_channel("mizuki", "nightcord")
        world.join_channel("ena", "nightcord")
        world.set_availability("ena", Availability.ASLEEP)
        channel_event = _event(
            scope=EventScope.CHANNEL,
            channel_id="nightcord",
            location_id="mizuki_home_room",
        )
        self.assertEqual(
            evaluate_exposure(world, channel_event, "ena").reason,
            ExposureReason.UNAVAILABLE,
        )

    def test_asleep_does_not_block_your_own_action(self):
        world = _world()
        world.set_availability("mizuki", Availability.ASLEEP)
        self.assertEqual(
            evaluate_exposure(world, _event(), "mizuki").reason,
            ExposureReason.SELF_ACTION,
        )

    def test_offline_from_a_channel_is_membership_not_availability(self):
        world = _world(mizuki="mizuki_home_room", ena="ena_home_studio")
        world.join_channel("mizuki", "nightcord")
        event = _event(
            scope=EventScope.CHANNEL,
            channel_id="nightcord",
            location_id="mizuki_home_room",
        )
        decision = evaluate_exposure(world, event, "ena")
        self.assertEqual(decision.reason, ExposureReason.NO_CHANNEL_ACCESS)
        self.assertEqual(decision.detail["availability"], "available")

    def test_a_dirty_availability_value_fails_loudly(self):
        # 绕过 set_availability 塞脏值会让"睡着了感知不到"静默失效。
        # 这类失败必须响亮，不能 fail-open。
        world = _world()
        world.character_availability["ena"] = "asleep-ish"
        with self.assertRaises(Exception):
            world.validate()

    def test_availability_survives_serialization(self):
        world = _world()
        world.set_availability("ena", Availability.ASLEEP)
        restored = WorldState.from_dict(world.to_dict())
        self.assertEqual(restored.availability_of("ena"), Availability.ASLEEP)
        self.assertEqual(restored.availability_of("mizuki"), Availability.AVAILABLE)


class CandidateTests(unittest.TestCase):
    def test_candidates_come_from_the_world_not_from_a_session_roster(self):
        world = _world(mizuki="kamiyama_high_gate", ena="kamiyama_high_gate")
        self.assertEqual(candidate_characters(world, _event()), ("ena", "mizuki"))

    def test_named_participants_are_always_evaluated(self):
        world = _world(mizuki="kamiyama_high_gate", ena="kamiyama_high_gate")
        event = _event(scope=EventScope.PARTICIPANT, participants=("mizuki", "ena"))
        self.assertIn("ena", candidate_characters(world, event))

    def test_a_character_the_world_does_not_know_is_denied(self):
        world = _world(mizuki="kamiyama_high_gate", ena="kamiyama_high_gate")
        decision = evaluate_exposure(world, _event(), "kanade")
        self.assertEqual(decision.reason, ExposureReason.UNKNOWN_CHARACTER)
        self.assertFalse(decision.exposed)


class DeterminismTests(unittest.TestCase):
    def test_identical_inputs_produce_identical_decisions(self):
        world = _world()
        event = _event()
        first = evaluate_event_exposure(world, event)
        second = evaluate_event_exposure(world, event)
        self.assertEqual(first, second)
        for a, b in zip(first, second):
            self.assertEqual(a.reason, b.reason)
            self.assertEqual(a.detail, b.detail)
            self.assertEqual(hash(a.reason), hash(b.reason))

    def test_evaluation_time_is_the_simulation_clock(self):
        world = _world()
        self.assertEqual(evaluate_exposure(world, _event(), "ena").evaluated_at, CLOCK)
        world.advance_time(30)
        self.assertEqual(
            evaluate_exposure(world, _event(), "ena").evaluated_at, world.clock
        )

    def test_decision_order_is_stable(self):
        world = _world()
        ids = [d.character_id for d in evaluate_event_exposure(world, _event())]
        self.assertEqual(ids, sorted(ids))

    def test_rules_reject_arguments_they_cannot_judge(self):
        world = _world()
        for args in (
            (None, _event(), "ena"),
            (world, "not-an-event", "ena"),
            (world, _event(), ""),
        ):
            with self.subTest(args=args), self.assertRaises(ExposureRuleError):
                evaluate_exposure(*args)


class ExposureDecisionModelTests(unittest.TestCase):
    def test_exposed_is_derived_from_the_reason_code(self):
        # 不另存一个可能跟理由码对不上的布尔。
        for reason in ExposureReason:
            decision = ExposureDecision("e1", "ena", reason, CLOCK)
            self.assertEqual(decision.exposed, reason.exposed)

    def test_detail_is_frozen_against_the_caller(self):
        detail = {"scope": "location"}
        decision = ExposureDecision(
            "e1", "ena", ExposureReason.SAME_LOCATION, CLOCK, detail
        )
        detail["scope"] = "public"
        self.assertEqual(decision.detail["scope"], "location")
        with self.assertRaises(TypeError):
            decision.detail["scope"] = "public"

    def test_invalid_decisions_are_rejected(self):
        for kwargs in (
            {"event_id": ""},
            {"character_id": ""},
            {"reason": "definitely_not_a_reason"},
            {"evaluated_at": "2026-08-20"},
            {"detail": ["not", "a", "dict"]},
            {"detail": {"bad": object()}},
        ):
            base = {
                "event_id": "e1",
                "character_id": "ena",
                "reason": ExposureReason.SAME_LOCATION,
                "evaluated_at": CLOCK,
            }
            base.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ExposureError):
                ExposureDecision(**base)

    def test_decisions_are_hashable_by_identity(self):
        # detail 冻结后不可哈希；身份是 (事件, 角色)，放进 set() 不该炸。
        first = ExposureDecision(
            "e1", "ena", ExposureReason.SAME_LOCATION, CLOCK, {"scope": "location"}
        )
        second = ExposureDecision(
            "e1", "ena", ExposureReason.SAME_LOCATION, CLOCK, {"scope": "location"}
        )
        self.assertEqual(len({first, second}), 1)

    def test_round_trips_through_a_dict(self):
        decision = ExposureDecision(
            "e1", "ena", ExposureReason.SAME_LOCATION, CLOCK, {"scope": "location"}
        )
        self.assertEqual(ExposureDecision.from_dict(decision.to_dict()), decision)
        self.assertTrue(decision.to_dict()["exposed"])


class ExposureLogTests(unittest.TestCase):
    def _log(self):
        return ExposureLog(
            (
                ExposureDecision("e1", "ena", ExposureReason.SAME_LOCATION, CLOCK),
                ExposureDecision("e1", "kanade", ExposureReason.WRONG_LOCATION, CLOCK),
                ExposureDecision("e2", "ena", ExposureReason.NO_CHANNEL_ACCESS, CLOCK),
            )
        )

    def test_explain_finds_a_single_decision(self):
        log = self._log()
        self.assertEqual(
            log.explain("e1", "kanade").reason, ExposureReason.WRONG_LOCATION
        )
        self.assertIsNone(log.explain("e9", "ena"))

    def test_denied_decisions_are_kept_for_debugging(self):
        log = self._log()
        self.assertEqual(len(log.for_event("e1")), 2)
        self.assertEqual(len(log.for_character("ena")), 2)

    def test_rollback_and_type_checks(self):
        log = self._log()
        log._rollback_to(1)
        self.assertEqual(len(log), 1)
        with self.assertRaises(ExposureError):
            log._rollback_to(5)
        with self.assertRaises(ExposureError):
            log._append("not a decision")

    def test_round_trips_through_a_dict(self):
        log = self._log()
        self.assertEqual(
            ExposureLog.from_dict(log.to_dict()).decisions(), log.decisions()
        )


if __name__ == "__main__":
    unittest.main()
