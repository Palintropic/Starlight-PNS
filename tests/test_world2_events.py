# tests/test_world2_events.py — WORLD-2 C5：world.locations_extended 与 world.resident_admitted。
#
# 守的线（设计 v3 §4.2、§6.1/3/8，D6）：
#   1. 形状：世界级、无 actor / participants / 顶层锚点、scope=public、payload 白名单；
#   2. 公共提交入口（commit_event / commit_session_event）一律拒绝这两种类型，只有
#      commit_authority_event 收；
#   3. 扩展：新图换上、冻结；旧居民进不了新地点；旧地点之间路线不变；不成立的扩展
#      （指纹不符、旧→旧新边、公开新地点、在旧地点听得见、重名、非正分钟、非规范定义）拒绝；
#   4. 入住：装授予、放到作息此刻那一段、加进名单末尾；不成立的入住（已有记录、指纹不符、
#      作息属于别人、段不一致、授予进不去、室友含本人）拒绝；
#   5. 事务：外层事务在入住之后失败，名单、授予、位置、地点图、历史全部回到原样；
#   6. 零感知：曝光判定、观察、记忆、回话排期的增量全为零；单独问任何人都得到
#      authority_operation——按类型判，伪造 scope / 锚点也绕不过去。
#
# 运行: python -m unittest discover -s tests -p test_world2_events.py
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from pns.models.event import (  # noqa: E402
    AUTHORITY_EVENT_TYPES,
    Event,
    EventError,
    EventScope,
    EventType,
)
from pns.models.exposure import ExposureReason  # noqa: E402
from pns.models.location import LocationGraphError  # noqa: E402
from pns.models.observation import Observation  # noqa: E402
from pns.runtime.agency.engine import AgencyEngine  # noqa: E402
from pns.runtime.event_commit import (  # noqa: E402
    EventCommitError,
    commit_authority_event,
    commit_event,
    commit_session_event,
)
from pns.runtime.exposure.rules import evaluate_event_exposure, evaluate_exposure  # noqa: E402
from pns.runtime.formal_world import grants_fingerprint, rhythm_fingerprint  # noqa: E402
from pns.runtime.memory.encoder import MemoryEncoder  # noqa: E402
from pns.runtime.reload import BOUNDARY  # noqa: E402
from pns.runtime.world2_baseline import build_baseline  # noqa: E402
from pns.runtime.world2_seed import admission_seed  # noqa: E402
from pns.world.extension import graph_fingerprint  # noqa: E402
from pns.world.grants import encode_grants  # noqa: E402
from pns.world.locations import build_default_location_graph  # noqa: E402
from pns.world.rhythm import encode_rhythm  # noqa: E402

from tests.test_formal_world import _state  # noqa: E402
from tests.test_world2_content import FOUR, NEW, _old_graph  # noqa: E402


def _old_world_state():
    """一个 WORLD-2 之前建的正式世界：存档里是旧图。"""
    registry = BOUNDARY.active()  # 内容照常对着新冷图校验；只有建世界那一下拿旧图
    with patch("pns.runtime.content_registry.build_default_location_graph", _old_graph):
        return _state(registry)


def _baseline_or_none(state):
    """世界上第一笔 WORLD-2 操作带基准，之后的不带（C6）。"""
    if any(e.type in AUTHORITY_EVENT_TYPES for e in state.events):
        return None
    rhythms = BOUNDARY.active().rhythms()
    return build_baseline(state, {cid: rhythms[cid] for cid in state.characters})


def _extension_payload(state):
    world = state.world_state
    cold = build_default_location_graph()
    new = [location.to_dict() for location in cold if location.location_id in NEW]
    appended = [
        connection.to_dict()
        for connection in cold.get("city_streets").connections
        if connection.to_id in NEW
    ]
    from pns.world.extension import extended_graph

    after = extended_graph(world.locations, new, {"city_streets": appended})
    return {
        "operation_id": "op-ext",
        "envelope": {},
        "locations": new,
        "append_connections": {"city_streets": appended},
        "graph_before": graph_fingerprint(world.locations),
        "graph_after": graph_fingerprint(after),
        "baseline": _baseline_or_none(state),
    }


def _unchecked_after(world, payload) -> str:
    """按 payload 原样拼出扩展后的图并取指纹，**不做任何不变量校验**。

    拒绝测试改完 payload 之后用它重算 graph_after：否则改动先撞上指纹不符，
    被测的那道检查根本走不到，测试会因为错误的原因通过。
    """
    from pns.models.location import Location, LocationGraph

    merged = []
    for location in world.locations:
        entry = location.to_dict()
        entry["connections"] = entry["connections"] + list(
            payload["append_connections"].get(location.location_id, [])
        )
        merged.append(Location.from_dict(entry))
    merged += [Location.from_dict(entry) for entry in payload["locations"]]
    return graph_fingerprint(LocationGraph(merged))


def _event(world, kind, payload, event_id="w2:evt", **overrides):
    fields = dict(
        event_id=event_id,
        type=kind,
        occurred_at=world.clock,
        scope=EventScope.PUBLIC,
        payload=payload,
    )
    fields.update(overrides)
    return Event(**fields)


MMJ = ("airi", "minori", "haruka", "shizuku")


def _admission_payload(world, character_id="airi", ordinal=None):
    registry = BOUNDARY.active()
    rhythm = registry.rhythms()[character_id]
    grants = registry.grants(character_id)
    segment = rhythm.segment_at(world.clock)
    return {
        "operation_id": f"op-admit-{character_id}",
        "envelope": {},
        "character_id": character_id,
        "location_id": segment.location_id,
        "activity": segment.activity.value,
        "channel_id": segment.channel_id,
        "roommates": [c for c in MMJ if c != character_id],
        "rhythm": encode_rhythm(rhythm),
        "grants": encode_grants(grants),
        "fingerprints": {
            "rhythm": rhythm_fingerprint(rhythm),
            "grants": grants_fingerprint(grants),
        },
        "seed": admission_seed(
            character_id,
            ordinal=MMJ.index(character_id) if ordinal is None else ordinal,
            admitted_at=world.clock,
            first_delay_minutes=5,
            stagger_minutes=5,
            interval_minutes=15,
        ),
    }


class _Injected(RuntimeError):
    pass


class ShapeTests(unittest.TestCase):
    def setUp(self):
        self.state = _old_world_state()
        self.world = self.state.world_state
        self.payload = _extension_payload(self.state)

    def test_accepts_the_canonical_shape(self):
        _event(self.world, EventType.WORLD_LOCATIONS_EXTENDED, self.payload)

    def test_rejects_anchors_actor_and_scope(self):
        kind = EventType.WORLD_LOCATIONS_EXTENDED
        for overrides in (
            {"actor_id": "mizuki"},
            {"participants": ("mizuki",)},
            {"location_id": "city_streets"},
            {"channel_id": "nightcord"},
            {"scope": EventScope.LOCATION, "location_id": "city_streets"},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(EventError):
                    _event(self.world, kind, self.payload, **overrides)

    def test_rejects_payload_key_drift(self):
        for mutate in (
            lambda p: p.pop("graph_after"),
            lambda p: p.update(effects={}),
            lambda p: p.update(operation_id=""),
            lambda p: p.update(envelope=[]),
        ):
            payload = copy.deepcopy(self.payload)
            mutate(payload)
            with self.subTest(keys=sorted(payload)):
                with self.assertRaises(EventError):
                    _event(self.world, EventType.WORLD_LOCATIONS_EXTENDED, payload)


class EntryPointTests(unittest.TestCase):
    def setUp(self):
        self.state = _old_world_state()
        self.world = self.state.world_state
        self.event = _event(
            self.world, EventType.WORLD_LOCATIONS_EXTENDED, _extension_payload(self.state)
        )

    def test_public_entries_refuse_authority_events(self):
        events_before = len(self.state.events)
        graph_before = self.world.locations
        with self.assertRaises(EventCommitError):
            commit_session_event(self.state, self.event)
        with self.assertRaises(EventCommitError):
            commit_event(self.world, self.state.events, self.event)
        self.assertEqual(len(self.state.events), events_before)
        self.assertIs(self.world.locations, graph_before)

    def test_authority_entry_refuses_ordinary_events(self):
        ordinary = Event(
            event_id="w2:ordinary",
            type=EventType.CHARACTER_ACTIVITY_CHANGED,
            occurred_at=self.world.clock,
            scope=EventScope.PRIVATE,
            actor_id="mizuki",
            payload={"activity": "idle"},
        )
        with self.assertRaises(EventCommitError):
            commit_authority_event(self.state, ordinary)


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.state = _old_world_state()
        self.world = self.state.world_state
        self.old_graph = self.world.locations
        self.payload = _extension_payload(self.state)

    def _commit(self, payload):
        return commit_authority_event(
            self.state, _event(self.world, EventType.WORLD_LOCATIONS_EXTENDED, payload)
        )

    def test_extension_swaps_in_a_frozen_extended_graph(self):
        self._commit(self.payload)
        graph = self.world.locations
        self.assertIsNot(graph, self.old_graph)
        self.assertEqual(set(graph.ids()) - set(self.old_graph.ids()), set(NEW))
        self.assertEqual(graph_fingerprint(graph), self.payload["graph_after"])
        with self.assertRaises(LocationGraphError):
            graph.add(graph.get("lumina_forum"))
        for character_id in FOUR:
            for location_id in NEW:
                self.assertFalse(self.world.may_enter(character_id, location_id))

    def assert_refused(self, mutate, recompute=True):
        payload = copy.deepcopy(self.payload)
        mutate(payload)
        if recompute:
            try:
                payload["graph_after"] = _unchecked_after(self.world, payload)
            except (LocationGraphError, KeyError, ValueError):
                pass  # 拼都拼不出来的扩展（重名、缺字段）：让提交边界自己拒
        events_before = len(self.state.events)
        with self.assertRaises(EventCommitError):
            self._commit(payload)
        self.assertIs(self.world.locations, self.old_graph)
        self.assertEqual(len(self.state.events), events_before)

    def test_refuses_a_stale_graph_before(self):
        self.assert_refused(lambda p: p.update(graph_before="0" * 64), recompute=False)

    def test_refuses_a_wrong_graph_after(self):
        self.assert_refused(lambda p: p.update(graph_after="0" * 64), recompute=False)

    def test_refuses_a_new_edge_between_old_locations(self):
        self.assert_refused(
            lambda p: p["append_connections"]["city_streets"].append(
                {"to_id": "kanade_home", "travel_minutes": 1, "mode": "walk"}
            )
        )

    def test_refuses_a_slow_new_edge_between_old_locations(self):
        # 不抄近路的旧→旧新边：路线比较抓不到它，只能靠"追加连接只能连向新地点"。
        self.assert_refused(
            lambda p: p["append_connections"]["city_streets"].append(
                {"to_id": "kanade_home", "travel_minutes": 99, "mode": "walk"}
            )
        )

    def _set_location(self, location_id, **fields):
        def mutate(payload):
            for entry in payload["locations"]:
                if entry["location_id"] == location_id:
                    entry.update(fields)

        return mutate

    def test_refuses_a_public_new_location(self):
        self.assert_refused(self._set_location("lumina_forum", access={"public": True}))

    def test_refuses_a_new_location_audible_from_an_old_one(self):
        self.assert_refused(
            self._set_location(
                "lumina_forum", perception={"indoor": True, "audible_from": ["city_streets"]}
            )
        )

    def test_refuses_a_shortcut_through_new_locations(self):
        # 新地点同时连着两个旧地点、而且比原路近：旧地点之间的路线就变了。
        def mutate(payload):
            for entry in payload["locations"]:
                if entry["location_id"] == "lumina_forum":
                    entry["connections"].append(
                        {"to_id": "kanade_home", "travel_minutes": 1, "mode": "walk"}
                    )
            payload["append_connections"]["mizuki_home"] = [
                {"to_id": "lumina_forum", "travel_minutes": 1, "mode": "walk"}
            ]

        self.assert_refused(mutate)

    def test_refuses_a_name_clash_with_an_old_location(self):
        self.assert_refused(lambda p: p["locations"][0].update(location_id="city_streets"))

    def test_refuses_non_positive_minutes(self):
        self.assert_refused(
            lambda p: p["append_connections"]["city_streets"][0].update(travel_minutes=0)
        )

    def test_refuses_a_non_canonical_definition(self):
        self.assert_refused(lambda p: p["locations"][0].pop("perception"))


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.state = _old_world_state()
        self.world = self.state.world_state
        commit_authority_event(
            self.state,
            _event(
                self.world,
                EventType.WORLD_LOCATIONS_EXTENDED,
                _extension_payload(self.state),
                event_id="w2:ext",
            ),
        )

    def _admit(self, payload, event_id="w2:admit"):
        return commit_authority_event(
            self.state, _event(self.world, EventType.WORLD_RESIDENT_ADMITTED, payload, event_id)
        )

    def test_admission_installs_places_and_rosters(self):
        payload = _admission_payload(self.world)
        self._admit(payload)
        self.assertEqual(tuple(self.state.characters), FOUR + ("airi",))
        self.assertEqual(self.world.location_of("airi"), payload["location_id"])
        self.assertEqual(self.world.activity_of("airi").kind.value, payload["activity"])
        self.assertTrue(self.world.may_enter("airi", "lumina_forum_mmj_room"))
        self.assertFalse(self.world.may_enter("airi", "mmj_minori_worksite"))
        self.assertIn("airi", self.world.known_characters())

    def assert_refused(self, payload):
        before = (list(self.state.characters), len(self.state.events))
        with self.assertRaises(EventCommitError):
            self._admit(payload)
        self.assertEqual((list(self.state.characters), len(self.state.events)), before)
        self.assertNotIn("airi", self.world.known_characters())
        self.assertNotIn("airi", self.world.location_grants)

    def test_refuses_a_second_admission(self):
        self._admit(_admission_payload(self.world))
        with self.assertRaises(EventCommitError):
            self._admit(_admission_payload(self.world), event_id="w2:admit2")

    def test_refuses_mismatched_fingerprints(self):
        payload = _admission_payload(self.world)
        payload["fingerprints"]["rhythm"] = "0" * 64
        self.assert_refused(payload)

    def test_refuses_someone_elses_rhythm(self):
        payload = _admission_payload(self.world)
        payload["rhythm"] = _admission_payload(self.world, "minori")["rhythm"]
        self.assert_refused(payload)

    def test_refuses_a_state_that_is_not_the_current_segment(self):
        payload = _admission_payload(self.world)
        payload["location_id"] = (
            "lumina_forum_lesson_room"
            if payload["location_id"] != "lumina_forum_lesson_room"
            else "lumina_forum_meeting_room"
        )
        self.assert_refused(payload)

    def test_refuses_grants_that_do_not_admit_the_location(self):
        payload = _admission_payload(self.world)
        payload["grants"]["locations"] = [
            pair for pair in payload["grants"]["locations"] if pair[0] != payload["location_id"]
        ]
        registry_grants = BOUNDARY.active().grants("airi")
        import dataclasses

        stripped = dataclasses.replace(
            registry_grants,
            locations=tuple(p for p in registry_grants.locations if p[0] != payload["location_id"]),
        )
        payload["grants"] = encode_grants(stripped)
        payload["fingerprints"]["grants"] = grants_fingerprint(stripped)
        self.assert_refused(payload)

    def test_refuses_roommates_that_include_herself(self):
        payload = _admission_payload(self.world)
        payload["roommates"] = payload["roommates"] + ["airi"]
        self.assert_refused(payload)

    def test_refuses_before_the_extension(self):
        state = _old_world_state()
        payload = _admission_payload(state.world_state)
        with self.assertRaises(EventCommitError):
            commit_authority_event(
                state,
                _event(state.world_state, EventType.WORLD_RESIDENT_ADMITTED, payload),
            )
        self.assertEqual(tuple(state.characters), FOUR)


class RollbackTests(unittest.TestCase):
    def test_outer_failure_after_extension_and_admission_restores_everything(self):
        state = _old_world_state()
        world = state.world_state
        graph = world.locations
        grants = copy.deepcopy(world.location_grants)
        events = len(state.events)
        with self.assertRaises(_Injected):
            with state.atomic_commit():
                commit_authority_event(
                    state,
                    _event(world, EventType.WORLD_LOCATIONS_EXTENDED, _extension_payload(state), "e"),
                )
                commit_authority_event(
                    state,
                    _event(world, EventType.WORLD_RESIDENT_ADMITTED, _admission_payload(world), "a"),
                )
                raise _Injected()
        self.assertIs(world.locations, graph)
        self.assertEqual(world.location_grants, grants)
        self.assertEqual(tuple(state.characters), FOUR)
        self.assertEqual(len(state.events), events)
        self.assertIsNone(world.location_of("airi"))


class ZeroPerceptionTests(unittest.TestCase):
    def setUp(self):
        self.state = _old_world_state()
        self.world = self.state.world_state

    def test_no_exposure_observation_memory_or_reply(self):
        state = self.state
        def snapshot():
            return (
                len(state.exposures),
                len(state.observations),
                len(state.memories),
                state.activations.entries(),
                state.activation_outbox._snapshot(),
            )

        before = snapshot()
        commit_authority_event(
            state,
            _event(self.world, EventType.WORLD_LOCATIONS_EXTENDED, _extension_payload(self.state), "e"),
        )
        commit_authority_event(
            state,
            _event(self.world, EventType.WORLD_RESIDENT_ADMITTED, _admission_payload(self.world), "a"),
        )
        after = snapshot()
        # 排期唯一的增量是她自己的种子（C7）；旧居民的条目原样、顺序不变。
        old_entries = tuple(e for e in after[3] if e[1].character_id != "airi")
        self.assertEqual(old_entries, before[3])
        self.assertEqual(
            [e[1].activation_id for e in after[3] if e[1].character_id == "airi"],
            ["seed.activation:airi"],
        )
        self.assertEqual(after[:3] + after[4:], before[:3] + before[4:])

    def test_everyone_gets_authority_operation_even_with_forged_anchors(self):
        payload = _extension_payload(self.state)
        event = _event(self.world, EventType.WORLD_LOCATIONS_EXTENDED, payload)
        self.assertEqual(evaluate_event_exposure(self.world, event), ())
        for character_id in FOUR:
            with self.subTest(character_id=character_id):
                decision = evaluate_exposure(self.world, event, character_id)
                self.assertIs(decision.reason, ExposureReason.AUTHORITY_OPERATION)
                self.assertFalse(decision.reason.exposed)

    def test_encoder_skips_authority_observations_by_type(self):
        # 正常走不到（没有观察）；伪造一条也记不住。
        observation = Observation(
            source_event_id="w2:forged",
            observer_id="mizuki",
            reason=ExposureReason.SAME_LOCATION,
            observed_at=self.world.clock,
            perceived={"type": EventType.WORLD_RESIDENT_ADMITTED.value, "actor_id": None},
        )
        encoder = MemoryEncoder(self.state)
        decisions = encoder._encode_one(observation, self.world.clock)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].detail["reason"], "authority_operation")

    def test_reply_offers_skip_authority_events_by_type(self):
        # 一个什么属性都没有的"引擎"：守卫不在，读 _budget 就会炸。
        event = _event(self.world, EventType.WORLD_LOCATIONS_EXTENDED, _extension_payload(self.state))
        AgencyEngine._offer_replies(SimpleNamespace(), event)


if __name__ == "__main__":
    unittest.main()
