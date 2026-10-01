# tests/test_nightcord_four.py — 25 時四人入住 yoake-mae（CONTENT-2 实施单 §7）。
#
# 守的线：
#   1. 一天里任意一分钟开局，四个人的位置、活动、频道都等于作息表那一段，且都有授予支撑；
#   2. 一整天一分钟一分钟推下去，每次换地方都走得到：除了路上，任何时刻每个人都在作息
#      表那一段的地点、做那一段的事、在那一段的频道里；没有一段被记成走不到；
#   3. 19:00 奏和真冬同在宵崎家吃晚饭，彼此出现在对方的"同处一地"里，也就不会被告知
#      "身边没有别人"；
#   4. 授予是闭的：真冬进不了神山高校，瑞希进不了奏的房间，奏进不了宫女；
#   5. 01:00 四人都进 Nightcord，之后按各自作息离开（真冬 02:00，其余三人 04:00）；
#   6. 真冬睡觉的时候，奏不在同一个房间里（ena 对 §3 疑点 3 的判断）；
#   7. 真冬在奏的房间作词（研究 #8），作词是独立的活动，提示词里有自己的名字。
#
# 运行: python -m unittest discover -s tests -p test_nightcord_four.py
import dataclasses
import unittest
from datetime import datetime, time, timedelta

from pns.models.activation import ActivationDue, ActivationKind
from pns.models.world_state import ActivityKind
from pns.runtime.agency.context import build_agency_context
from pns.runtime.autonomy.prompt import _ACTIVITY_LABELS
from pns.runtime.formal_world import YOAKE_MAE, formal_session_state
from pns.runtime.reload import BOUNDARY

from tests.test_formal_world import WALL, PlaneTestCase, _state

FOUR = ("mizuki", "ena", "kanade", "mafuyu")


def _expected(registry, character_id, clock):
    segment = registry.rhythm(character_id).segment_at(clock)
    channels = [segment.channel_id] if segment.channel_id is not None else []
    return segment, channels


def _due(world, character_id):
    return ActivationDue(
        activation_id=f"probe:{character_id}",
        kind=ActivationKind.CHARACTER_ACTIVATION,
        due_at=world.clock,
        fired_at=world.clock,
        sequence=0,
        character_id=character_id,
    )


class ResidentsTests(unittest.TestCase):
    def test_yoake_mae_has_the_four_of_25ji(self):
        self.assertEqual(set(YOAKE_MAE.residents), set(FOUR))


class LyricsTests(unittest.TestCase):
    def test_mafuyu_writes_lyrics_in_kanades_room(self):
        segments = [
            segment
            for segment in BOUNDARY.active().rhythm("mafuyu").segments
            if segment.activity is ActivityKind.WRITING_LYRICS
        ]
        self.assertTrue(segments, "真冬的一天里没有作词")
        for segment in segments:
            self.assertEqual(segment.location_id, "kanade_home_room")

    def test_every_activity_has_a_prompt_label(self):
        # 没有标签的活动会以英文 id 原样进提示词。
        for kind in ActivityKind:
            with self.subTest(kind=kind.value):
                self.assertIn(kind.value, _ACTIVITY_LABELS)
        self.assertEqual(_ACTIVITY_LABELS["writing_lyrics"], "作词")


class LaunchAnyMinuteTests(unittest.TestCase):
    def test_every_minute_of_the_day_is_a_valid_launch(self):
        registry = BOUNDARY.active()
        for minute in range(24 * 60):
            start = time(minute // 60, minute % 60)
            spec = dataclasses.replace(YOAKE_MAE, start=start)
            # formal_session_state 本身会逐人核对授予；抛错就是这一分钟开不了局。
            world = formal_session_state(
                spec, registry, session_id="s", wall=WALL
            ).world_state
            for character_id in FOUR:
                segment, channels = _expected(registry, character_id, world.clock)
                where = world.location_of(character_id)
                with self.subTest(start=start.isoformat(), character_id=character_id):
                    self.assertEqual(where, segment.location_id)
                    self.assertIs(world.activity_of(character_id).kind, segment.activity)
                    self.assertEqual(world.channels_for(character_id), channels)
                    self.assertTrue(world.may_enter(character_id, where))
                    for channel_id in channels:
                        self.assertTrue(world.may_join(character_id, channel_id))


class GrantsAreClosedTests(unittest.TestCase):
    def test_nobody_gets_into_a_place_they_have_no_grant_for(self):
        world = _state().world_state
        refused = (
            ("mafuyu", "kamiyama_high"),
            ("kanade", "kamiyama_high"),
            ("kanade", "miyamasuzaka_girls"),
            ("mizuki", "kanade_home_room"),
            ("mizuki", "kanade_home"),
            ("ena", "kanade_home_room"),
            ("mizuki", "miyamasuzaka_girls"),
            ("mafuyu", "mizuki_home_room"),
            ("mafuyu", "ena_home_studio"),
        )
        for character_id, location_id in refused:
            with self.subTest(character_id=character_id, location_id=location_id):
                self.assertFalse(world.may_enter(character_id, location_id))
        # 反例对照：该进的进得去，否则上面的"进不去"可能只是地点不存在。
        for character_id, location_id in (
            ("mafuyu", "miyamasuzaka_girls"),
            ("mafuyu", "kanade_home_room"),
            ("kanade", "kanade_home_room"),
            ("mizuki", "miyamasuzaka_gate"),
        ):
            with self.subTest(character_id=character_id, location_id=location_id):
                self.assertTrue(world.may_enter(character_id, location_id))


class DinnerAtSevenTests(unittest.TestCase):
    def assert_together_at_dinner(self, world):
        self.assertEqual(world.location_of("kanade"), "kanade_home")
        self.assertEqual(world.location_of("mafuyu"), "kanade_home")
        for me, other in (("kanade", "mafuyu"), ("mafuyu", "kanade")):
            with self.subTest(character_id=me):
                self.assertIs(world.activity_of(me).kind, ActivityKind.EATING)
                context = build_agency_context(world, me, _due(world, me))
                self.assertIn(other, context.co_located_characters)

    def test_launch_at_19_puts_them_at_the_same_table(self):
        self.assert_together_at_dinner(_state().world_state)


class DayRunTests(PlaneTestCase):
    rate = 1.0

    def test_a_whole_day_follows_the_rhythm_for_all_four(self):
        registry = self.registry
        self.plane.create_formal("yoake-mae")
        world = self.world()
        start = world.state.world_state.clock
        self.assertEqual(start.time(), time(19, 0))
        checked = 0
        # 一天多一小时：从 19:00 推到次日 20:00，覆盖跨零点和第二次晚饭。
        for _ in range(25 * 60):
            world.runtime.advance(1)
            ws = world.state.world_state
            for character_id in FOUR:
                activity = ws.activity_of(character_id).kind
                if activity is ActivityKind.COMMUTING:
                    continue
                segment, channels = _expected(registry, character_id, ws.clock)
                where = ws.location_of(character_id)
                if (
                    where != segment.location_id
                    or activity is not segment.activity
                    or ws.channels_for(character_id) != channels
                ):
                    self.fail(
                        f"{ws.clock:%H:%M} {character_id} 在 {where} 做 {activity.value}"
                        f"、频道 {ws.channels_for(character_id)}；作息是 {segment.label} "
                        f"{segment.location_id} {segment.activity.value} {channels}"
                    )
                checked += 1
                # ena 对 §3 疑点 3：真冬睡着的时候，奏不在那个房间里。
                if character_id == "mafuyu" and activity is ActivityKind.RESTING:
                    self.assertNotEqual(ws.location_of("kanade"), where, f"{ws.clock:%H:%M}")
            if ws.clock == start + timedelta(hours=24):
                DinnerAtSevenTests.assert_together_at_dinner(self, ws)
        self.assertGreater(checked, 4 * 23 * 60, "绝大多数分钟都核对到了")
        self.assertEqual(world.state.rhythm_dispositions, frozenset(), "没有一段走不到")
        self.assertEqual(self.provider.generations, [], "没按 Start，不调模型")

        # 频道进出：01:00 四人进，真冬 02:00 走，其余三人 04:00 走。
        presence = {}
        for event in world.state.events.events():
            kind = event.type.value
            if kind in ("presence.joined_channel", "presence.left_channel"):
                self.assertEqual(event.channel_id, "nightcord")
                presence.setdefault(event.actor_id, []).append(
                    (kind.rsplit(".", 1)[1], event.occurred_at.time())
                )
        self.assertEqual(
            presence,
            {
                "mizuki": [("joined_channel", time(1, 0)), ("left_channel", time(4, 0))],
                "ena": [("joined_channel", time(1, 0)), ("left_channel", time(4, 0))],
                "kanade": [("joined_channel", time(1, 0)), ("left_channel", time(4, 0))],
                "mafuyu": [("joined_channel", time(1, 0)), ("left_channel", time(2, 0))],
            },
        )


if __name__ == "__main__":
    unittest.main()
