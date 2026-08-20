# tests/test_world_state.py — WorldState 作为权威可变世界现实的不变量。
#
# 运行: python -m unittest tests.test_world_state -v
import unittest
from datetime import datetime

from pns.models.location import Connection, Location, LocationGraph, LocationKind
from pns.models.world_state import WorldState, WorldStateError
from pns.world.channels import build_default_channel_registry
from pns.world.context import day_phase_label, render_clock, render_world_context
from pns.world.locations import build_default_location_graph


def _world(clock=datetime(2026, 8, 20, 17, 30)):
    return WorldState(
        clock=clock,
        locations=build_default_location_graph(),
        channels=build_default_channel_registry(),
    )


class WorldClockTests(unittest.TestCase):
    def test_time_advance_rolls_the_date_over_instead_of_dropping_it(self):
        world = _world(datetime(2026, 8, 20, 23, 50))
        world.advance_time(20)
        self.assertEqual(world.clock, datetime(2026, 8, 21, 0, 10))
        self.assertEqual(world.date, "2026-08-21")
        self.assertEqual(world.time, "00:10")

    def test_time_advance_spans_multiple_days(self):
        world = _world(datetime(2026, 12, 31, 22, 0))
        world.advance_time(60 * 26)
        self.assertEqual(world.clock, datetime(2027, 1, 2, 0, 0))

    def test_time_cannot_run_backwards(self):
        world = _world()
        with self.assertRaises(WorldStateError):
            world.advance_time(-1)

    def test_day_phase_is_derived_from_the_clock(self):
        world = _world(datetime(2026, 8, 20, 17, 30))
        self.assertEqual(render_clock(world.clock), "傍晚 17:30")
        world.advance_time(60 * 8)  # 跨过零点
        self.assertEqual(day_phase_label(world.clock), "深夜")


class WorldPlacementTests(unittest.TestCase):
    def setUp(self):
        self.world = _world()

    def test_placement_uses_stable_ids(self):
        self.world.place_character("mizuki", "kamiyama_high_gate")
        self.assertEqual(self.world.location_of("mizuki"), "kamiyama_high_gate")
        self.assertEqual(self.world.characters_at("kamiyama_high_gate"), ["mizuki"])

    def test_placement_rejects_unknown_locations(self):
        with self.assertRaises(WorldStateError):
            self.world.place_character("mizuki", "神山高校校门口")
        with self.assertRaises(WorldStateError):
            self.world.place_character("mizuki", "no_such_place")
        self.assertIsNone(self.world.location_of("mizuki"))

    def test_placement_rejects_non_id_character_keys(self):
        for bad in ("", None, 7):
            with self.subTest(character=bad), self.assertRaises(WorldStateError):
                self.world.place_character(bad, "kamiyama_high_gate")

    def test_characters_at_can_include_contained_locations(self):
        self.world.place_character("ena", "ena_home_studio")
        self.assertEqual(self.world.characters_at("ena_home"), [])
        self.assertEqual(
            self.world.characters_at("ena_home", include_contained=True), ["ena"]
        )
        self.assertEqual(
            self.world.characters_at("tokyo", include_contained=True), ["ena"]
        )

    def test_characters_at_rejects_unknown_locations(self):
        with self.assertRaises(WorldStateError):
            self.world.characters_at("no_such_place")

    def test_a_world_holds_agents_in_several_places_at_once(self):
        self.world.place_character("ena", "ena_home_studio")
        self.world.place_character("mizuki", "mizuki_home_room")
        self.world.place_character("kanade", "kamiyama_high_gate")
        self.assertEqual(
            sorted(set(self.world.character_locations.values())),
            ["ena_home_studio", "kamiyama_high_gate", "mizuki_home_room"],
        )

    def test_removing_a_character_clears_placement_and_channels(self):
        self.world.place_character("mizuki", "mizuki_home_room")
        self.world.join_channel("mizuki", "nightcord")
        self.world.remove_character("mizuki")
        self.assertIsNone(self.world.location_of("mizuki"))
        self.assertEqual(self.world.channel_participants("nightcord"), [])


class WorldChannelTests(unittest.TestCase):
    def setUp(self):
        self.world = _world(datetime(2026, 8, 20, 2, 0))
        self.world.place_character("ena", "ena_home_studio")
        self.world.place_character("mizuki", "mizuki_home_room")

    def test_physical_location_and_channel_presence_are_independent(self):
        self.world.join_channel("ena", "nightcord")
        self.world.join_channel("mizuki", "nightcord")

        # 两个人物理上在不同房间……
        self.assertNotEqual(
            self.world.location_of("ena"), self.world.location_of("mizuki")
        )
        # ……但同时在同一个线上频道里。
        self.assertEqual(
            self.world.channel_participants("nightcord"), ["ena", "mizuki"]
        )
        self.assertEqual(self.world.channels_for("ena"), ["nightcord"])
        self.assertTrue(self.world.is_in_channel("ena", "nightcord"))

    def test_leaving_a_channel_does_not_move_the_character(self):
        self.world.join_channel("ena", "nightcord")
        self.world.leave_channel("ena", "nightcord")
        self.assertEqual(self.world.channels_for("ena"), [])
        self.assertEqual(self.world.location_of("ena"), "ena_home_studio")

    def test_unknown_channels_are_rejected(self):
        with self.assertRaises(WorldStateError):
            self.world.join_channel("ena", "no_such_channel")
        with self.assertRaises(WorldStateError):
            self.world.channel_participants("no_such_channel")

    def test_channel_membership_shows_up_in_the_character_projection(self):
        self.world.set_environment("ena_home_studio", {"weather": "室内"})
        self.world.join_channel("ena", "nightcord")
        context = render_world_context(self.world, "ena")
        self.assertEqual(
            context,
            "时间：深夜 02:00，地点：绘名家，她的画室，台灯开着，"
            "天气/环境：室内，在线频道：Nightcord 语音频道",
        )
        self.assertNotIn("在线频道", render_world_context(self.world, "mizuki"))


class WorldEnvironmentTests(unittest.TestCase):
    def test_environment_is_stored_per_location(self):
        world = _world()
        world.set_environment("kamiyama_high_gate", {"weather": "晴，微风"})
        self.assertEqual(
            world.environment_of("kamiyama_high_gate"), {"weather": "晴，微风"}
        )
        self.assertEqual(world.environment_of("ena_home_studio"), {})

    def test_environment_rejects_unknown_locations(self):
        with self.assertRaises(WorldStateError):
            _world().set_environment("no_such_place", {"weather": "晴"})

    def test_environment_accessor_returns_a_copy(self):
        world = _world()
        world.set_environment("kamiyama_high_gate", {"weather": "晴"})
        world.environment_of("kamiyama_high_gate")["weather"] = "雨"
        self.assertEqual(world.environment_of("kamiyama_high_gate"), {"weather": "晴"})


class WorldSerializationTests(unittest.TestCase):
    def setUp(self):
        self.world = _world(datetime(2026, 8, 20, 2, 0))
        self.world.place_character("ena", "ena_home_studio")
        self.world.place_character("mizuki", "mizuki_home_room")
        self.world.join_channel("ena", "nightcord")
        self.world.join_channel("mizuki", "nightcord")
        self.world.set_environment("ena_home_studio", {"weather": "室内"})
        self.world.metadata["origin"] = {
            "kind": "legacy_scene",
            "scene_id": "nightcord",
        }

    def test_public_shape(self):
        payload = self.world.to_dict()
        self.assertEqual(
            set(payload),
            {
                "clock",
                "date",
                "time",
                "locations",
                "channels",
                "character_locations",
                "channel_members",
                "location_state",
                "metadata",
            },
        )
        self.assertEqual(payload["clock"], "2026-08-20T02:00:00")
        self.assertEqual(payload["date"], "2026-08-20")
        self.assertEqual(payload["time"], "02:00")
        self.assertEqual(
            payload["character_locations"],
            {"ena": "ena_home_studio", "mizuki": "mizuki_home_room"},
        )
        self.assertEqual(payload["channel_members"], {"nightcord": ["ena", "mizuki"]})
        # 没有 current_scene / active_characters —— 世界不再围绕一个"当前场景"。
        self.assertNotIn("current_scene", payload)
        self.assertNotIn("active_characters", payload)

    def test_restores_the_same_public_shape(self):
        restored = WorldState.from_dict(self.world.to_dict())
        self.assertEqual(restored.to_dict(), self.world.to_dict())
        self.assertEqual(restored.location_of("ena"), "ena_home_studio")
        self.assertTrue(restored.is_in_channel("mizuki", "nightcord"))
        self.assertTrue(restored.locations.has("ena_home_studio"))

    def test_serialization_does_not_leak_mutable_internal_references(self):
        payload = self.world.to_dict()
        payload["character_locations"]["ena"] = "kamiyama_high_gate"
        payload["channel_members"]["nightcord"].append("kanade")
        payload["location_state"]["ena_home_studio"]["weather"] = "暴雨"
        payload["metadata"]["origin"]["scene_id"] = "gate"
        payload["locations"]["ena_home_studio"]["description"] = "被改掉了"

        self.assertEqual(self.world.location_of("ena"), "ena_home_studio")
        self.assertEqual(
            self.world.channel_participants("nightcord"), ["ena", "mizuki"]
        )
        self.assertEqual(
            self.world.environment_of("ena_home_studio"), {"weather": "室内"}
        )
        self.assertEqual(self.world.metadata["origin"]["scene_id"], "nightcord")
        self.assertEqual(
            self.world.locations.get("ena_home_studio").description,
            "绘名家，她的画室，台灯开着",
        )

    def test_restore_rejects_unknown_location_references(self):
        payload = self.world.to_dict()
        payload["character_locations"]["ena"] = "missing_location"
        with self.assertRaises(WorldStateError):
            WorldState.from_dict(payload)

        payload = self.world.to_dict()
        payload["location_state"]["missing_location"] = {"weather": "雨"}
        with self.assertRaises(WorldStateError):
            WorldState.from_dict(payload)

    def test_restore_rejects_unknown_channel_and_invalid_character_ids(self):
        payload = self.world.to_dict()
        payload["channel_members"]["missing_channel"] = ["ena"]
        with self.assertRaises(WorldStateError):
            WorldState.from_dict(payload)

        payload = self.world.to_dict()
        payload["channel_members"]["nightcord"] = [""]
        with self.assertRaises(WorldStateError):
            WorldState.from_dict(payload)

    def test_constructor_owns_supplied_mutable_state(self):
        character_locations = {"ena": "ena_home_studio"}
        channel_members = {"nightcord": {"ena"}}
        location_state = {"ena_home_studio": {"weather": "室内"}}
        world = WorldState(
            clock=datetime(2026, 8, 20, 2, 0),
            locations=build_default_location_graph(),
            channels=build_default_channel_registry(),
            character_locations=character_locations,
            channel_members=channel_members,
            location_state=location_state,
        )

        character_locations["ena"] = "kamiyama_high_gate"
        channel_members["nightcord"].add("mizuki")
        location_state["ena_home_studio"]["weather"] = "暴雨"

        self.assertEqual(world.location_of("ena"), "ena_home_studio")
        self.assertEqual(world.channel_participants("nightcord"), ["ena"])
        self.assertEqual(
            world.environment_of("ena_home_studio"), {"weather": "室内"}
        )


class WorldStateAcceptsCustomGraphTests(unittest.TestCase):
    def test_world_state_is_not_bound_to_the_default_pack_content(self):
        graph = LocationGraph(
            [
                Location("hub", "Hub", LocationKind.REGION),
                Location(
                    "cell",
                    "Cell",
                    LocationKind.ROOM,
                    parent_id="hub",
                    connections=(Connection("hub", travel_minutes=2),),
                ),
            ]
        )
        world = WorldState(clock=datetime(2026, 1, 1, 9, 0), locations=graph)
        world.place_character("someone", "cell")
        self.assertEqual(world.characters_at("cell"), ["someone"])


if __name__ == "__main__":
    unittest.main()
