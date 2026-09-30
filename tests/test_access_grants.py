# tests/test_access_grants.py — 进入非公开地点、加入频道的静态授予（WORLD-1）。
#
# 守的线：
#   1. 没有授予就是拒绝：非公开地点、没写 access 的地点、非成员加入频道；
#   2. 提交边界与 Agency 前置条件用的是同一条规则，哪条路都绕不过去；
#   3. 授予只来自内容，写错在构建内容快照时就拒绝，作息表不许去没授予的地方；
#   4. 授予是世界的静态结构：跟着存档走，任何事件都改不动它。
#
# 这个文件刻意不用 tests/grants_support.py —— 那是给与授权无关的旧测试用的。
#
# 运行: python -m unittest discover -s tests -p test_access_grants.py
import unittest
from datetime import datetime
from pathlib import Path

from pns.models.action import ActionId
from pns.models.event import Event, EventScope, EventType
from pns.models.event_store import EventStore
from pns.models.world_state import WorldState, WorldStateError
from pns.runtime.agency.preconditions import legal_actions
from pns.runtime.content_registry import (
    ConfigValidationError,
    _build_character,
    build_content_registry,
)
from pns.runtime.event_commit import EventCommitError, commit_event
from pns.world.channels import build_default_channel_registry
from pns.world.grants import (
    GrantError,
    install_grants,
    parse_access_grants,
    require_rhythm_is_enterable,
)
from pns.world.locations import build_default_location_graph
from pns.world.rhythm import parse_daily_rhythm

CLOCK = datetime(2026, 9, 26, 19, 0)


def _world():
    world = WorldState(
        clock=CLOCK,
        locations=build_default_location_graph(),
        channels=build_default_channel_registry(),
    )
    world.place_character("mizuki", "kamiyama_high_gate")
    world.place_character("ena", "kamiyama_high_gate")
    return world


def _move(actor, location_id, event_id="move-1"):
    return Event(
        event_id=event_id,
        type=EventType.CHARACTER_LOCATION_CHANGED,
        occurred_at=CLOCK,
        scope=EventScope.LOCATION,
        actor_id=actor,
        location_id=location_id,
    )


def _join(actor, event_id="join-1"):
    return Event(
        event_id=event_id,
        type=EventType.PRESENCE_JOINED_CHANNEL,
        occurred_at=CLOCK,
        scope=EventScope.CHANNEL,
        actor_id=actor,
        channel_id="nightcord",
    )


def _parse(payload, character_id="mizuki"):
    return parse_access_grants(
        payload,
        character_id=character_id,
        locations=build_default_location_graph(),
        channels=build_default_channel_registry(),
    )


# ── 1. 规则本身 ─────────────────────────────────────────────────────────
class EntryRuleTests(unittest.TestCase):
    def setUp(self):
        self.world = _world()

    def test_public_locations_need_no_grant(self):
        self.assertTrue(self.world.may_enter("mizuki", "city_streets"))
        self.assertTrue(self.world.may_enter("mizuki", "clothing_store"))

    def test_a_non_public_location_without_a_grant_is_refused(self):
        for location_id in (
            "kamiyama_high",
            "clothing_store_floor",
            "ena_home_studio",
            "private_residence",
        ):
            with self.subTest(location_id=location_id):
                self.assertFalse(self.world.may_enter("mizuki", location_id))

    def test_a_location_without_access_rules_is_not_public(self):
        # tokyo 没写 access。缺省只能往拒绝那边偏。
        self.assertEqual(self.world.locations.get("tokyo").access, {})
        self.assertFalse(self.world.may_enter("mizuki", "tokyo"))

    def test_the_grant_role_must_match_the_declared_role(self):
        self.world._grant_location("mizuki", "kamiyama_high", "staff")
        self.assertFalse(self.world.may_enter("mizuki", "kamiyama_high"))
        self.world._grant_location("mizuki", "kamiyama_high", "student")
        self.assertTrue(self.world.may_enter("mizuki", "kamiyama_high"))

    def test_a_grant_belongs_to_one_character(self):
        self.world._grant_location("mizuki", "kamiyama_high", "student")
        self.assertFalse(self.world.may_enter("ena", "kamiyama_high"))

    def test_a_grant_on_a_home_does_not_cover_its_rooms(self):
        # 授予逐个地点写，不沿 parent 继承：进得了家门不等于进得了每个房间。
        self.world._grant_location("mizuki", "ena_home", "household")
        self.assertTrue(self.world.may_enter("mizuki", "ena_home"))
        self.assertFalse(self.world.may_enter("mizuki", "ena_home_studio"))

    def test_channel_membership_is_not_presence(self):
        self.assertFalse(self.world.may_join("mizuki", "nightcord"))
        self.world._grant_channel("mizuki", "nightcord")
        self.assertTrue(self.world.may_join("mizuki", "nightcord"))
        self.assertFalse(self.world.is_in_channel("mizuki", "nightcord"))
        self.assertFalse(self.world.may_join("ena", "nightcord"))

    def test_grants_must_reference_real_places(self):
        with self.assertRaises(WorldStateError):
            self.world._grant_location("mizuki", "atlantis", "student")
        with self.assertRaises(WorldStateError):
            self.world._grant_channel("mizuki", "discord")
        with self.assertRaises(WorldStateError):
            self.world._grant_location("mizuki", "kamiyama_high", "")


# ── 2. 提交边界 ─────────────────────────────────────────────────────────
class CommitBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.world = _world()
        self.store = EventStore()

    def test_an_ungranted_move_is_refused_and_changes_nothing(self):
        before = self.world.to_dict()
        with self.assertRaises(EventCommitError) as caught:
            commit_event(self.world, self.store, _move("mizuki", "kamiyama_high"))
        self.assertIn("授予", str(caught.exception))
        self.assertEqual(self.world.to_dict(), before)
        self.assertEqual(len(self.store), 0)

    def test_a_granted_move_commits(self):
        self.world._grant_location("mizuki", "kamiyama_high", "student")
        commit_event(self.world, self.store, _move("mizuki", "kamiyama_high"))
        self.assertEqual(self.world.location_of("mizuki"), "kamiyama_high")

    def test_a_non_member_cannot_join(self):
        with self.assertRaises(EventCommitError) as caught:
            commit_event(self.world, self.store, _join("mizuki"))
        self.assertIn("成员", str(caught.exception))
        self.assertFalse(self.world.is_in_channel("mizuki", "nightcord"))

    def test_a_member_can_join(self):
        self.world._grant_channel("mizuki", "nightcord")
        commit_event(self.world, self.store, _join("mizuki"))
        self.assertTrue(self.world.is_in_channel("mizuki", "nightcord"))

    def test_a_member_can_leave(self):
        self.world._grant_channel("mizuki", "nightcord")
        self.world.join_channel("mizuki", "nightcord")
        commit_event(
            self.world,
            self.store,
            Event(
                event_id="leave-1",
                type=EventType.PRESENCE_LEFT_CHANNEL,
                occurred_at=CLOCK,
                scope=EventScope.CHANNEL,
                actor_id="mizuki",
                channel_id="nightcord",
            ),
        )
        self.assertFalse(self.world.is_in_channel("mizuki", "nightcord"))

    def test_no_event_changes_the_grants(self):
        self.world._grant_location("mizuki", "kamiyama_high", "student")
        self.world._grant_channel("mizuki", "nightcord")
        before = (
            self.world.to_dict()["location_grants"],
            self.world.to_dict()["channel_grants"],
        )
        commit_event(self.world, self.store, _move("mizuki", "kamiyama_high"))
        commit_event(self.world, self.store, _join("mizuki"))
        after = (
            self.world.to_dict()["location_grants"],
            self.world.to_dict()["channel_grants"],
        )
        self.assertEqual(before, after)


# ── 3. Agency 前置条件与提交边界同一口径 ─────────────────────────────────
class AgencyUsesTheSameRuleTests(unittest.TestCase):
    def _legal(self, world, actor):
        found, _truncated = legal_actions(world, actor)
        return {(legal.action_id, legal.target_id) for legal in found}

    def test_an_ungranted_neighbour_is_not_a_legal_move(self):
        world = _world()
        legal = self._legal(world, "mizuki")
        self.assertIn((ActionId.MOVE_TO, "city_streets"), legal)
        self.assertNotIn((ActionId.MOVE_TO, "kamiyama_high"), legal)
        world._grant_location("mizuki", "kamiyama_high", "student")
        self.assertIn((ActionId.MOVE_TO, "kamiyama_high"), self._legal(world, "mizuki"))

    def test_a_non_member_has_no_join_action(self):
        world = _world()
        self.assertNotIn((ActionId.JOIN_CHANNEL, "nightcord"), self._legal(world, "ena"))
        world._grant_channel("ena", "nightcord")
        self.assertIn((ActionId.JOIN_CHANNEL, "nightcord"), self._legal(world, "ena"))

    def test_every_legal_move_or_join_actually_commits(self):
        # 枚举出来的每一条都必须过得了提交边界，反过来也一样。
        world = _world()
        world._grant_location("mizuki", "kamiyama_high", "student")
        world._grant_channel("mizuki", "nightcord")
        for action_id, target in sorted(self._legal(world, "mizuki")):
            if action_id is ActionId.MOVE_TO:
                event = _move("mizuki", target, f"m-{target}")
            elif action_id is ActionId.JOIN_CHANNEL:
                event = _join("mizuki", f"j-{target}")
            else:
                continue
            with self.subTest(action=action_id, target=target):
                trial = WorldState.from_dict(world.to_dict())
                commit_event(trial, EventStore(), event)


# ── 4. 内容侧 ───────────────────────────────────────────────────────────
class ContentGrantParsingTests(unittest.TestCase):
    def test_no_grants_is_normal(self):
        self.assertIsNone(_parse(None))

    def test_a_valid_declaration(self):
        grants = _parse(
            {
                "locations": [
                    {"location_id": "kamiyama_high", "role": "student"},
                    {"location_id": "mizuki_home_room", "role": "household"},
                ],
                "channels": ["nightcord"],
            }
        )
        self.assertEqual(
            dict(grants.locations),
            {"kamiyama_high": "student", "mizuki_home_room": "household"},
        )
        self.assertEqual(grants.channels, frozenset({"nightcord"}))

    def test_bad_declarations_are_rejected(self):
        cases = {
            "unknown location": {"locations": [{"location_id": "atlantis", "role": "x"}]},
            "public location": {
                "locations": [{"location_id": "city_streets", "role": "x"}]
            },
            "role mismatch": {
                "locations": [{"location_id": "kamiyama_high", "role": "staff"}]
            },
            "missing role": {"locations": [{"location_id": "kamiyama_high"}]},
            "prose rides along": {
                "locations": [
                    {
                        "location_id": "kamiyama_high",
                        "role": "student",
                        "because": "关系很好",
                    }
                ]
            },
            "duplicate": {
                "locations": [
                    {"location_id": "kamiyama_high", "role": "student"},
                    {"location_id": "kamiyama_high", "role": "student"},
                ]
            },
            "unknown channel": {"channels": ["discord"]},
            "channels not a list": {"channels": "nightcord"},
            "unknown top key": {"trust": ["ena"]},
            "not a mapping": ["kamiyama_high"],
        }
        for label, payload in cases.items():
            with self.subTest(label):
                with self.assertRaises(GrantError):
                    _parse(payload)

    def test_a_rhythm_may_not_send_a_character_where_it_has_no_grant(self):
        locations = build_default_location_graph()
        rhythm = parse_daily_rhythm(
            [
                {
                    "at": "08:00",
                    "activity": "studying",
                    "location_id": "kamiyama_high",
                    "source": "inferred",
                },
                {"at": "15:00", "activity": "idle", "source": "inferred"},
            ],
            character_id="mizuki",
            locations=locations,
        )
        with self.assertRaises(GrantError):
            require_rhythm_is_enterable(rhythm, None, locations)
        grants = _parse({"locations": [{"location_id": "kamiyama_high", "role": "student"}]})
        require_rhythm_is_enterable(rhythm, grants, locations)

    def test_the_character_build_fails_on_an_unenterable_rhythm(self):
        with self.assertRaises(ConfigValidationError):
            _build_character(
                "mizuki",
                {
                    "unit": "25ji",
                    "status": "draft",
                    "daily_rhythm": [
                        {
                            "at": "08:00",
                            "activity": "studying",
                            "location_id": "kamiyama_high",
                            "source": "inferred",
                        },
                        {"at": "15:00", "activity": "idle", "source": "inferred"},
                    ],
                },
                Path("."),
                build_default_location_graph(),
                build_default_channel_registry(),
            )


class PackGrantTests(unittest.TestCase):
    """用的是真正的角色包，不是测试专用数据。"""

    @classmethod
    def setUpClass(cls):
        cls.registry = build_content_registry()

    def test_the_content_2_residents_declare_their_grants(self):
        kanade = self.registry.grants("kanade")
        mafuyu = self.registry.grants("mafuyu")
        self.assertEqual(
            dict(kanade.locations),
            {"kanade_home": "household", "kanade_home_room": "household"},
        )
        # 真冬暂住宵崎家，与奏同属 household；学校只给她。
        self.assertEqual(
            dict(mafuyu.locations),
            {
                "kanade_home": "household",
                "kanade_home_room": "household",
                "miyamasuzaka_girls": "student",
            },
        )
        self.assertEqual(kanade.channels, frozenset({"nightcord"}))
        self.assertEqual(mafuyu.channels, frozenset({"nightcord"}))

    def test_the_two_world_1_residents_declare_their_grants(self):
        mizuki = self.registry.grants("mizuki")
        ena = self.registry.grants("ena")
        self.assertEqual(
            dict(mizuki.locations),
            {
                "clothing_store_floor": "staff",
                "kamiyama_high": "student",
                "mizuki_home": "household",
                "mizuki_home_room": "household",
            },
        )
        self.assertEqual(
            dict(ena.locations),
            {
                "ena_home": "household",
                "ena_home_studio": "household",
                "kamiyama_high": "student",
            },
        )
        self.assertEqual(mizuki.channels, frozenset({"nightcord"}))
        self.assertEqual(ena.channels, frozenset({"nightcord"}))

    def test_characters_without_content_get_nothing_by_default(self):
        # 25 時以外的角色还没有世界侧内容。
        for character_id in ("ichika", "airi"):
            with self.subTest(character_id=character_id):
                self.assertIsNone(self.registry.grants(character_id))

    def test_a_new_world_carries_exactly_the_content_grants(self):
        # gate 场景在公开地点、不带频道：世界里只有内容包声明的授予。
        world = self.registry.new_world_state(
            self.registry.scene("gate"), ["mizuki", "ena", "ichika"]
        )
        self.assertEqual(world.metadata["origin"]["scene_grants"], [])
        self.assertTrue(world.may_enter("mizuki", "clothing_store_floor"))
        self.assertFalse(world.may_enter("ena", "clothing_store_floor"))
        self.assertFalse(world.may_enter("ena", "mizuki_home_room"))
        self.assertTrue(world.may_join("ena", "nightcord"))
        self.assertFalse(world.may_join("ichika", "nightcord"))


class PresenceInvariantTests(unittest.TestCase):
    """EC-1/EC-2：人在哪里、在哪个频道，任何时刻都必须有授予支撑。"""

    def test_placing_or_joining_without_a_grant_is_refused(self):
        world = _world()
        with self.assertRaises(WorldStateError):
            world.place_character("mizuki", "kamiyama_high")
        with self.assertRaises(WorldStateError):
            world.join_channel("mizuki", "nightcord")
        self.assertEqual(world.location_of("mizuki"), "kamiyama_high_gate")
        self.assertFalse(world.is_in_channel("mizuki", "nightcord"))

    def test_an_archive_with_ungranted_presence_is_refused(self):
        registry = build_content_registry()
        world = registry.new_world_state(registry.scene("gate"), ["mizuki", "ena"])
        for mutate in (
            lambda p: p["character_locations"].update(ena="mizuki_home_room"),
            lambda p: p["channel_members"].setdefault("nightcord", []).append("ichika"),
            lambda p: p["location_grants"].pop("mizuki")
            and p["character_locations"].update(mizuki="mizuki_home_room"),
        ):
            payload = world.to_dict()
            mutate(payload)
            with self.subTest(payload=payload["character_locations"]):
                with self.assertRaises(WorldStateError):
                    WorldState.from_dict(payload)

    def test_a_grant_installed_inside_a_failed_transaction_rolls_back(self):
        world = _world()
        snapshot = world.snapshot_mutable_state()
        world._grant_channel("ena", "nightcord")
        world._restore_mutable_state(snapshot)
        self.assertFalse(world.may_join("ena", "nightcord"))


class GrantPersistenceTests(unittest.TestCase):
    def test_grants_survive_a_round_trip(self):
        world = _world()
        install_grants(
            world,
            _parse(
                {
                    "locations": [{"location_id": "kamiyama_high", "role": "student"}],
                    "channels": ["nightcord"],
                }
            ),
        )
        restored = WorldState.from_dict(world.to_dict())
        self.assertTrue(restored.may_enter("mizuki", "kamiyama_high"))
        self.assertTrue(restored.may_join("mizuki", "nightcord"))
        self.assertFalse(restored.may_enter("ena", "kamiyama_high"))

    def test_a_tampered_grant_to_an_unknown_place_is_refused_on_load(self):
        payload = _world().to_dict()
        payload["location_grants"] = {"mizuki": {"atlantis": "student"}}
        with self.assertRaises(WorldStateError):
            WorldState.from_dict(payload)
        payload = _world().to_dict()
        payload["channel_grants"] = {"mizuki": ["discord"]}
        with self.assertRaises(WorldStateError):
            WorldState.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
