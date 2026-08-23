# tests/test_scene_compat.py — 遗留 scene → 初始 WorldState 的兼容边界。
#
# 这是唯一允许从 SCENES 推导世界状态的地方，所以映射必须是确定性的、
# 显式的，映射不到时必须清楚地失败。
#
# 运行: python -m unittest tests.test_scene_compat -v
import unittest
from datetime import date, datetime

from pns.world.context import render_session_location, render_world_context
from pns.models.world_state import ActivityKind
from pns.world.scene_compat import (
    SCENE_WORLD_MAP,
    SceneMappingError,
    build_initial_world_state,
    get_scene_mapping,
)
from pns.world.scenes import SCENES

_DAY = date(2026, 8, 20)


def _build(scene_id, characters=("mizuki", "ena")):
    return build_initial_world_state(
        SCENES[scene_id], list(characters), start_date=_DAY
    )


class SceneMappingCoverageTests(unittest.TestCase):
    def test_every_shipped_scene_has_an_explicit_mapping(self):
        self.assertEqual(set(SCENES), set(SCENE_WORLD_MAP))

    def test_every_shipped_scene_places_all_characters(self):
        for scene_id in SCENES:
            with self.subTest(scene=scene_id):
                world = _build(scene_id)
                self.assertEqual(sorted(world.character_locations), ["ena", "mizuki"])

    def test_mapping_is_deterministic(self):
        first = _build("gate").to_dict()
        second = _build("gate").to_dict()
        self.assertEqual(first, second)

    def test_unknown_scene_id_fails_with_an_actionable_error(self):
        with self.assertRaises(SceneMappingError) as ctx:
            get_scene_mapping("no_such_scene")
        self.assertIn("no_such_scene", str(ctx.exception))
        self.assertIn("SCENE_WORLD_MAP", str(ctx.exception))

    def test_scene_without_id_fails_clearly(self):
        with self.assertRaises(SceneMappingError):
            build_initial_world_state({"label": "无 id"}, ["mizuki", "ena"])

    def test_unmapped_scene_never_silently_lands_somewhere_else(self):
        custom_scene = dict(SCENES["gate"], id="user_authored_scene")
        with self.assertRaises(SceneMappingError):
            build_initial_world_state(custom_scene, ["mizuki", "ena"])


class SceneInitializationTests(unittest.TestCase):
    def test_gate_puts_everyone_at_the_school_gate(self):
        world = _build("gate")
        self.assertEqual(world.clock, datetime(2026, 8, 20, 17, 30))
        self.assertEqual(world.characters_at("kamiyama_high_gate"), ["ena", "mizuki"])
        self.assertEqual(world.channel_members, {})
        self.assertEqual(
            world.environment_of("kamiyama_high_gate"), {"weather": "晴，微风"}
        )

    def test_nightcord_separates_physical_rooms_from_the_online_channel(self):
        world = _build("nightcord")
        self.assertEqual(world.location_of("ena"), "ena_home_studio")
        self.assertEqual(world.location_of("mizuki"), "mizuki_home_room")
        self.assertEqual(world.channel_participants("nightcord"), ["ena", "mizuki"])
        self.assertFalse(world.locations.has("nightcord"))
        self.assertIs(
            world.activity_of("mizuki").kind, ActivityKind.ONLINE_CHATTING
        )
        self.assertIs(world.activity_of("ena").kind, ActivityKind.ONLINE_CHATTING)

    def test_characters_without_a_modelled_home_fall_back_to_the_placeholder(self):
        world = _build("nightcord", characters=("mizuki", "kanade"))
        self.assertEqual(world.location_of("mizuki"), "mizuki_home_room")
        self.assertEqual(world.location_of("kanade"), "private_residence")
        self.assertTrue(world.is_in_channel("kanade", "nightcord"))

    def test_origin_metadata_is_provenance_only(self):
        world = _build("ena_room")
        origin = world.metadata["origin"]
        self.assertEqual(origin["kind"], "legacy_scene")
        self.assertEqual(origin["scene_id"], "ena_room")
        # trigger/auto_next/auto_turns 不进入世界模型本身
        self.assertNotIn("auto_next", world.to_dict())
        self.assertNotIn("auto_turns", world.to_dict())

    def test_start_date_is_injectable_and_the_world_carries_a_real_date(self):
        world = build_initial_world_state(
            SCENES["nightcord"], ["mizuki", "ena"], start_date=date(2027, 3, 1)
        )
        self.assertEqual(world.clock, datetime(2027, 3, 1, 2, 0))
        self.assertEqual(world.date, "2027-03-01")

    def test_legacy_scene_time_and_weather_remain_effective_inputs(self):
        scene = dict(SCENES["gate"], time="晚上 21:45", weather="小雨")
        world = build_initial_world_state(scene, ["mizuki", "ena"], start_date=_DAY)

        self.assertEqual(world.clock, datetime(2026, 8, 20, 21, 45))
        self.assertEqual(
            world.environment_of("kamiyama_high_gate"), {"weather": "小雨"}
        )

    def test_invalid_legacy_scene_time_and_weather_fail_clearly(self):
        with self.assertRaises(SceneMappingError):
            build_initial_world_state(
                dict(SCENES["gate"], time="tomorrow afternoon"),
                ["mizuki", "ena"],
            )
        with self.assertRaises(SceneMappingError):
            build_initial_world_state(
                dict(SCENES["gate"], weather={"kind": "rain"}),
                ["mizuki", "ena"],
            )


class ScenePromptProjectionTests(unittest.TestCase):
    """位置在场景里的散文语义必须由世界状态重新投影出来，而不是抄 scene 字段。"""

    def test_colocated_scenes_reproduce_the_legacy_prompt_context(self):
        for scene_id in ("gate", "ena_room", "clothes_shop"):
            with self.subTest(scene=scene_id):
                scene = SCENES[scene_id]
                legacy = (
                    f"时间：{scene['time']}，地点：{scene['location']}，"
                    f"天气/环境：{scene['weather']}"
                )
                world = _build(scene_id)
                self.assertEqual(render_world_context(world, "mizuki"), legacy)

    def test_nightcord_projection_is_per_character_instead_of_one_scene_string(self):
        world = _build("nightcord")
        self.assertIn("绘名家", render_world_context(world, "ena"))
        self.assertIn("瑞希的房间", render_world_context(world, "mizuki"))
        for character in ("ena", "mizuki"):
            self.assertIn(
                "在线频道：Nightcord 语音频道",
                render_world_context(world, character),
            )

    def test_session_summary_lists_split_locations_and_channels(self):
        summary = render_session_location(_build("nightcord"))
        self.assertIn("绘名家·画室", summary)
        self.assertIn("瑞希家·房间", summary)
        self.assertIn("Nightcord 语音频道", summary)

    def test_projection_follows_state_changes_not_the_scene_fixture(self):
        world = _build("gate")
        world.advance_time(60 * 9)  # 跨过零点
        world.place_character("mizuki", "mizuki_home_room")

        context = render_world_context(world, "mizuki")
        self.assertIn("深夜 02:30", context)
        self.assertIn("瑞希的房间", context)
        self.assertNotIn(SCENES["gate"]["location"], context)


if __name__ == "__main__":
    unittest.main()
