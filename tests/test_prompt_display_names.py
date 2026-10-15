"""生成提示词里只出现显示名，不出现角色 / 地点 / 频道 ID。

生产 10-09 起台词里冒出 "minori""mizuki"：召回行把记忆里存的 ID 原样拼进了
提示词，模型照着念。记忆内容里继续存 ID（要能从观察重新推导），显示名只在
渲染时换上 —— 所以担保落在两处：渲染层用名字表，live composition 给全名字表。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from pns.interfaces.composition import WorldControlPlane  # noqa: E402
from pns.models.event import Event, EventScope, EventType  # noqa: E402
from pns.runtime.memory import MemoryRecall, prompt_projection, recalled_lines  # noqa: E402
from pns.runtime.reload import BOUNDARY  # noqa: E402

from test_memory import _move, _rig  # noqa: E402

NAMES = {"ena": "绘名", "mizuki": "瑞希"}
LOCATIONS = {"mizuki_home_room": "瑞希的房间"}
CHANNELS = {"nightcord": "25時、ナイトコードで。"}
IDS = ("ena", "mizuki_home_room", "nightcord")


class WorldFactMemoriesUseDisplayNamesTests(unittest.TestCase):
    """走公开的编码 → 召回路径：移动、下线长出来的每一类记忆都用名字。"""

    def setUp(self):
        self.state, encoder = _rig()
        encoder.commit_and_encode(_move(self.state, "ena", "mizuki_home_room", "m1"))
        self.state.world_state.advance_time(1)
        world = self.state.world_state
        encoder.commit_and_encode(
            Event(
                event_id="p1",
                type=EventType.PRESENCE_LEFT_CHANNEL,
                occurred_at=world.clock,
                scope=EventScope.CHANNEL,
                actor_id="ena",
                channel_id="nightcord",
                participants=world.channel_participants("nightcord"),
            )
        )
        self.result = MemoryRecall(self.state).recall_for("mizuki")

    def _lines(self):
        return recalled_lines(
            self.result,
            names=NAMES,
            location_names=LOCATIONS,
            channel_names=CHANNELS,
        )

    def test_every_recalled_line_names_people_places_and_channels(self):
        lines = self._lines()
        # 世界事实和短时痕迹各两条：在哪、去了哪、在不在频道、离开了频道。
        self.assertEqual(len(lines), 4, lines)
        for line in lines:
            for entity_id in IDS:
                self.assertNotIn(entity_id, line)
        joined = "\n".join(lines)
        self.assertIn("绘名 在 瑞希的房间", joined)
        self.assertIn("绘名 去了 瑞希的房间", joined)
        self.assertIn("绘名 离开了频道 25時、ナイトコードで。", joined)
        self.assertIn("绘名 不在频道 25時、ナイトコードで。 里了", joined)

    def test_the_stored_memories_still_hold_ids(self):
        self._lines()
        stored = [dict(r.content) for r in self.state.memories.for_owner("mizuki")]
        self.assertIn("去了 mizuki_home_room", [c.get("summary") for c in stored])
        self.assertIn("mizuki_home_room", [c.get("value") for c in stored])

    def test_the_debug_projection_renders_the_same_lines(self):
        projection = prompt_projection(
            self.result,
            names=NAMES,
            location_names=LOCATIONS,
            channel_names=CHANNELS,
        )
        self.assertEqual(tuple(projection["lines"]), self._lines())


class QuotedSpeechIsNotRewrittenTests(unittest.TestCase):
    def test_a_line_that_happens_to_say_an_id_keeps_its_words(self):
        """只换固定句式里的 ID：引文里出现同一个词，原话照旧。"""
        state, encoder = _rig()
        world = state.world_state
        encoder.commit_and_encode(
            Event(
                event_id="e1",
                type=EventType.MESSAGE_SENT,
                occurred_at=world.clock,
                scope=EventScope.CHANNEL,
                actor_id="ena",
                participants=world.channel_participants("nightcord"),
                channel_id="nightcord",
                payload={"text": "去了 nightcord 再说吧", "char_name": "ena"},
            )
        )
        lines = recalled_lines(
            MemoryRecall(state).recall_for("mizuki"),
            names=NAMES,
            channel_names=CHANNELS,
        )
        self.assertTrue(lines)
        joined = "\n".join(lines)
        self.assertIn("「去了 nigh", joined)
        self.assertNotIn(CHANNELS["nightcord"], joined)


class LiveCompositionGivesFullNameTablesTests(unittest.TestCase):
    """生产装配出来的策略，名字表覆盖内容包里的每一个角色、地点、频道。"""

    def setUp(self):
        registry = BOUNDARY.active()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {registry.models.key_name: "sk-names-0123456789"})
        env.start()
        self.addCleanup(env.stop)
        plane = WorldControlPlane(
            root=Path(tmp.name) / "worlds", client_factory=lambda *a, **k: object()
        )
        adapters = plane.build_adapters(registry, world_id="w")
        state = plane.new_session_state(
            world_id="w",
            scene_id=registry.default_scene,
            character_ids=["kanade"],
            registry=registry,
        )
        self.policy = adapters.policy_factory(state)
        self.registry = registry

    def test_every_character_has_a_display_name(self):
        self.assertEqual(set(self.policy._names), set(self.registry.characters))

    def test_every_location_and_channel_has_a_display_name(self):
        locations = {loc.location_id for loc in self.registry.new_location_graph()}
        channels = {ch.channel_id for ch in self.registry.new_channel_registry()}
        self.assertTrue(locations)
        self.assertTrue(channels)
        self.assertEqual(set(self.policy._location_names), locations)
        self.assertEqual(set(self.policy._channel_names), channels)
        for table in (self.policy._location_names, self.policy._channel_names):
            for entity_id, display in table.items():
                self.assertTrue(display, entity_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
