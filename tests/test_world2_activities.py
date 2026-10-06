# tests/test_world2_activities.py — WORLD-2 C1：MMJ 入住需要的五个新活动。
#
# 守的线：
#   1. 活动是闭集，闭集的每一个消费点都认得新成员：生成提示的中文标签、dashboard 的显示
#      标签、docs/API.md 列出的合法值。漏一处，那一处就会把英文 id 原样露给模型或用户；
#   2. 新活动全是清醒的活动：可用性不会被推成 asleep（只有 resting 会）；
#   3. 新活动能经公共的活动接口设上、读回，并作为 character.activity_changed 的合法值。
#
# 运行: python -m unittest discover -s tests -p test_world2_activities.py
import re
import unittest
from datetime import datetime
from pathlib import Path

from pns.models.location import Location, LocationGraph
from pns.models.world_state import ActivityKind, Availability, WorldState
from pns.runtime.autonomy.prompt import _ACTIVITY_LABELS

ROOT = Path(__file__).resolve().parent.parent
NEW = (
    ActivityKind.IDOL_PRACTICE,
    ActivityKind.PHYSICAL_TRAINING,
    ActivityKind.STAGE_PLANNING,
    ActivityKind.PERFORMANCE_REVIEW,
    ActivityKind.IDOL_WORK,
)


def _dashboard_labels() -> set:
    source = (ROOT / "dashboard" / "src" / "WorldOverview.tsx").read_text(encoding="utf-8")
    block = re.search(r"const ACTIVITY_LABEL[^{]*\{(.*?)\};", source, re.S)
    assert block, "WorldOverview.tsx 里找不到 ACTIVITY_LABEL"
    return set(re.findall(r"^\s*([a-z_]+)\s*:", block.group(1), re.M))


def _documented_activities() -> set:
    source = (ROOT / "docs" / "API.md").read_text(encoding="utf-8")
    sentence = re.search(r"`activity` 是服务器声明的闭集：(.*?)。", source, re.S)
    assert sentence, "docs/API.md 里找不到活动闭集那一句"
    return set(re.findall(r"`([a-z_]+)`", sentence.group(1)))


class ClosedSetConsumerTests(unittest.TestCase):
    def test_new_activities_exist(self):
        self.assertEqual(
            [kind.value for kind in NEW],
            ["idol_practice", "physical_training", "stage_planning",
             "performance_review", "idol_work"],
        )

    def test_every_activity_has_a_prompt_label(self):
        for kind in ActivityKind:
            with self.subTest(kind=kind.value):
                self.assertIn(kind.value, _ACTIVITY_LABELS)
                self.assertNotEqual(_ACTIVITY_LABELS[kind.value], kind.value)

    def test_every_activity_has_a_dashboard_label(self):
        # dashboard 对没列出的活动回退成原始 id；那是兜底，不是该走的路。
        labels = _dashboard_labels()
        for kind in ActivityKind:
            with self.subTest(kind=kind.value):
                self.assertIn(kind.value, labels)

    def test_api_doc_lists_exactly_the_closed_set(self):
        self.assertEqual(_documented_activities(), {kind.value for kind in ActivityKind})


class NewActivitiesAreAwakeTests(unittest.TestCase):
    def _world(self) -> WorldState:
        world = WorldState(
            clock=datetime(2026, 10, 6, 10, 0),
            locations=LocationGraph([Location("room", "room", access={"public": True})]),
        )
        world.place_character("airi", "room")
        return world

    def test_new_activities_do_not_put_anyone_to_sleep(self):
        for kind in NEW:
            with self.subTest(kind=kind.value):
                world = self._world()
                world.set_activity("airi", kind)
                self.assertIs(world.activity_of("airi").kind, kind)
                self.assertIsNot(world.availability_of("airi"), Availability.ASLEEP)

    def test_resting_is_still_the_only_sleeping_activity(self):
        world = self._world()
        world.set_activity("airi", ActivityKind.RESTING)
        self.assertIs(world.availability_of("airi"), Availability.ASLEEP)

    def test_activity_round_trips_through_the_snapshot(self):
        for kind in NEW:
            with self.subTest(kind=kind.value):
                world = self._world()
                world.set_activity("airi", kind)
                restored = WorldState.from_dict(world.to_dict())
                self.assertIs(restored.activity_of("airi").kind, kind)


if __name__ == "__main__":
    unittest.main()
