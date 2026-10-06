# tests/test_world2_content.py — WORLD-2 C2：LUMINA FORUM 冷图与 MMJ 四人的作息 / 授予。
#
# 守的线（实施单 §4.1）：
#   1. 冷图只加不改：旧节点逐字段不变，街道只在连接末尾追加；任意两个旧节点之间的完整
#      路线（节点、每跳耗时、方式）不变；
#   2. 旧居民一个新授予都没有，进不了任何新地点；
#   3. 新开的世界名单仍是原四人，MMJ 不在场、账本里没有 MMJ 的作息；
#   4. 旧世界（存档里是旧图）用这份内容恢复：图原样来自存档，账本不变，作息门不把 MMJ
#      记成待决，世界照常运行——冷图变大碰不到已经存在的世界；
#   5. MMJ 四人每晚 23:00–06:30 都在共享寝室睡着（入住窗口，设计 §3.2），平日、休息日
#      两张表都是。
#
# 运行: python -m unittest discover -s tests -p test_world2_content.py
import itertools
import sys
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from pns.interfaces.content_maintenance import read_conflicts
from pns.models.location import LocationGraph
from pns.models.world_state import ActivityKind
from pns.runtime.formal_world import rhythm_subject
from pns.runtime.reload import BOUNDARY
from pns.world.locations import DEFAULT_LOCATIONS, build_default_location_graph
from pns.world.routing import plan_route

from tests.test_formal_world import PlaneTestCase, _state

FOUR = ("mizuki", "ena", "kanade", "mafuyu")
MMJ = ("airi", "minori", "haruka", "shizuku")
ROOM = "lumina_forum_mmj_room"
NEW = frozenset(
    {
        "lumina_forum",
        ROOM,
        "lumina_forum_lesson_room",
        "lumina_forum_training_room",
        "lumina_forum_dining_room",
        "lumina_forum_meeting_room",
        "mmj_minori_worksite",
        "mmj_haruka_worksite",
        "mmj_airi_worksite",
        "mmj_shizuku_worksite",
    }
)


def _old_graph() -> LocationGraph:
    """WORLD-2 之前的冷图：去掉新节点和街道上追加的连接。"""
    import dataclasses

    old = []
    for location in DEFAULT_LOCATIONS:
        if location.location_id in NEW:
            continue
        kept = tuple(c for c in location.connections if c.to_id not in NEW)
        old.append(dataclasses.replace(location, connections=kept))
    return LocationGraph(old)


class ColdGraphTests(unittest.TestCase):
    def setUp(self):
        self.new = build_default_location_graph()
        self.old = _old_graph()
        self.new_dict = self.new.to_dict()
        self.old_dict = self.old.to_dict()

    def test_exactly_ten_new_nodes(self):
        self.assertEqual(set(self.new_dict) - set(self.old_dict), set(NEW))
        self.assertEqual(set(self.old_dict) - set(self.new_dict), set())

    def test_old_nodes_are_unchanged_except_appended_street_edges(self):
        for location_id, old in self.old_dict.items():
            with self.subTest(location_id=location_id):
                new = dict(self.new_dict[location_id])
                if location_id == "city_streets":
                    count = len(old["connections"])
                    self.assertEqual(new["connections"][:count], old["connections"])
                    self.assertEqual(
                        {c["to_id"] for c in new["connections"][count:]},
                        {"lumina_forum"} | {f"mmj_{c}_worksite" for c in MMJ},
                    )
                    new["connections"] = new["connections"][:count]
                self.assertEqual(new, old)

    def test_routes_between_old_nodes_are_identical(self):
        # 全通行：最宽松的权限下也不能有旧节点之间的新捷径。
        for a, b in itertools.permutations(sorted(self.old_dict), 2):
            with self.subTest(route=(a, b)):
                self.assertEqual(
                    plan_route(self.new, lambda _: True, a, b),
                    plan_route(self.old, lambda _: True, a, b),
                )

    def test_worksite_names_carry_no_research_markers(self):
        # 地点名会进生成提示；"（未细化）"是研究标注，不能漏给模型。
        for location_id in NEW:
            with self.subTest(location_id=location_id):
                location = self.new_dict[location_id]
                self.assertNotIn("未细化", location["name"])
                self.assertNotIn("未细化", location["description"])


class NewWorldTests(unittest.TestCase):
    def setUp(self):
        self.registry = BOUNDARY.active()
        self.state = _state(self.registry)
        self.world = self.state.world_state

    def test_roster_is_still_the_four(self):
        self.assertEqual(tuple(self.state.characters), FOUR)
        for character_id in MMJ:
            with self.subTest(character_id=character_id):
                self.assertIsNone(self.world.location_of(character_id))

    def test_ledger_has_no_mmj_subjects(self):
        subjects = {subject for subject, _ in self.state.content.adopted}
        self.assertEqual(subjects, {rhythm_subject(c) for c in FOUR})

    def test_old_residents_cannot_enter_any_new_location(self):
        for character_id, location_id in itertools.product(FOUR, sorted(NEW)):
            with self.subTest(character_id=character_id, location_id=location_id):
                self.assertFalse(self.world.may_enter(character_id, location_id))


class MmjContentTests(unittest.TestCase):
    def setUp(self):
        self.registry = BOUNDARY.active()

    def test_every_mmj_member_has_rhythm_and_grants(self):
        rhythms = self.registry.rhythms()
        for character_id in MMJ:
            with self.subTest(character_id=character_id):
                self.assertIn(character_id, rhythms)
                self.assertIsNotNone(self.registry.grants(character_id))

    def test_all_four_sleep_in_the_shared_room_23_to_0630(self):
        rhythms = self.registry.rhythms()
        # 周五夜 → 周六（平日表跨进休息日表）、周日夜 → 10-12 祝日、祝日夜 → 平日。
        nights = (date(2026, 10, 9), date(2026, 10, 11), date(2026, 10, 12))
        for night in nights:
            start = datetime.combine(night, time(23, 0))
            for minute in range(0, 7 * 60 + 30, 5):
                clock = start + timedelta(minutes=minute)
                for character_id in MMJ:
                    with self.subTest(at=clock.isoformat(), character_id=character_id):
                        segment = rhythms[character_id].segment_at(clock)
                        self.assertIs(segment.activity, ActivityKind.RESTING)
                        self.assertEqual(segment.location_id, ROOM)


class OldWorldRestoreTests(PlaneTestCase):
    rate = 1.0

    def test_an_old_world_restores_with_its_own_graph(self):
        # 存档是 WORLD-2 之前建的：建局那一刻冷图还是旧图。
        with patch(
            "pns.runtime.content_registry.build_default_location_graph", _old_graph
        ):
            self.plane.create_formal("yoake-mae")
        self.plane.close("yoake-mae")
        before = self.plane.store.load("yoake-mae", history=False).state
        old_ids = set(before["world_state"]["locations"])
        self.assertFalse(old_ids & NEW, "存档里本来就没有新节点")

        status = self.plane.restore("yoake-mae")
        self.assertTrue(status["running"], status)
        state = self.world().state
        self.assertEqual(set(state.world_state.locations.to_dict()), old_ids)
        self.assertEqual(state.content.to_dict(), before["content"])
        self.assertEqual(read_conflicts(self.plane, "yoake-mae"), ())
        self.assertEqual(tuple(state.characters), FOUR)


if __name__ == "__main__":
    unittest.main()
