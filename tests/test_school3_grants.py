# tests/test_school3_grants.py — SCHOOL-3：教室、校内去处与追加授予（world.grants_extended）。
#
# 设计稿 kickoff/SCHOOL_3_CLASSROOMS_DESIGN.md（v3，ena 两轮审）。阶段 A：新地图 + 新授予，作息不动。
#
# 生产现状是"SCHOOL-3 之前建的世界、MMJ 已经入住、谁都没有教室授予"。这里照着搭：
# 用一份去掉教室授予的内容快照建世界、做 MMJ 扩展和入住，再换回现行内容恢复，
# 走维护入口的公共链路（预检 → 执行 → 重试 → 查询）做 SCHOOL-3 扩展和追加授予。
#
# 运行: python -m unittest tests.test_school3_grants -v
import copy
import dataclasses
import sys
import tempfile
import unittest
from datetime import datetime, time, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from grants_support import grant_everything  # noqa: E402
from pns.models.event import Event, EventError, EventScope, EventType  # noqa: E402
from pns.models.location import Connection, Location, LocationGraph, LocationKind  # noqa: E402
from pns.models.channel import ChannelRegistry  # noqa: E402
from pns.models.world_state import WorldState  # noqa: E402
from pns.runtime.admission import DURABLE, GRANTS, AdmissionRejected, WorldAdmission, parse_operation  # noqa: E402
from pns.runtime.event_commit import EventCommitError, validate_against_world  # noqa: E402
from pns.runtime.formal_world import YOAKE_MAE, formal_session_state, grants_fingerprint  # noqa: E402
from pns.runtime.persistence.lifecycle import WorldLifecycleService  # noqa: E402
from pns.runtime.persistence.store import FileWorldStore  # noqa: E402
from pns.runtime.reload import BOUNDARY  # noqa: E402
from pns.runtime.world2_replay import WorldReplayError, verify_world2_history  # noqa: E402
from pns.world.grants import CharacterGrants, grants_in_world  # noqa: E402
from pns.world.locations import DEFAULT_LOCATIONS  # noqa: E402

from tests.test_world2_admission import CADENCE, MMJ, _adapters  # noqa: E402
from tests.test_world2_content import NEW as MMJ_NODES  # noqa: E402

WALL = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)  # 东京 9/27（周日）19:00

SCHOOL3 = frozenset(
    {
        "kamiyama_2a",
        "kamiyama_2b",
        "kamiyama_3c",
        "kamiyama_3d_evening",
        "kamiyama_rooftop",
        "miyamasuzaka_2a",
        "miyamasuzaka_2b",
        "miyamasuzaka_2d",
        "miyamasuzaka_3b",
        "miyamasuzaka_3e",
        "miyamasuzaka_courtyard",
    }
)
# 设计稿 §3.2：谁拿哪些追加。奏不加。
EXPECTED = {
    "mafuyu": {"miyamasuzaka_3b", "miyamasuzaka_courtyard"},
    "minori": {"miyamasuzaka_2d", "miyamasuzaka_courtyard"},
    "haruka": {"miyamasuzaka_2d", "miyamasuzaka_courtyard"},
    "airi": {"miyamasuzaka_3e", "miyamasuzaka_courtyard"},
    "shizuku": {"miyamasuzaka_3e", "miyamasuzaka_courtyard"},
    "mizuki": {"kamiyama_2b", "kamiyama_rooftop"},
    "ena": {"kamiyama_3d_evening", "kamiyama_rooftop"},
}


def _graph_without(*node_sets):
    drop = frozenset().union(*node_sets)
    kept = []
    for location in DEFAULT_LOCATIONS:
        if location.location_id in drop:
            continue
        connections = tuple(c for c in location.connections if c.to_id not in drop)
        kept.append(dataclasses.replace(location, connections=connections))
    return LocationGraph(kept)


def _before_world2():
    return _graph_without(MMJ_NODES, SCHOOL3)


def _before_school3():
    return _graph_without(SCHOOL3)


def _content_before_school3(registry):
    """SCHOOL-3 之前的内容：同一份快照，只是授予里没有校内节点。作息一字不差。"""
    characters = {}
    for cid, content in registry.characters.items():
        grants = content.grants
        if grants is not None:
            grants = CharacterGrants(
                character_id=grants.character_id,
                locations=tuple(p for p in grants.locations if p[0] not in SCHOOL3),
                channels=grants.channels,
            )
        characters[cid] = dataclasses.replace(content, grants=grants)
    return dataclasses.replace(registry, characters=characters)


class School3TestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.registry = BOUNDARY.active()
        self.old_content = _content_before_school3(self.registry)
        self.service = WorldLifecycleService(FileWorldStore(Path(tmp.name)))
        self.addCleanup(self.service.release_all)
        spec = dataclasses.replace(YOAKE_MAE, start=time(23, 30))
        with patch("pns.runtime.content_registry.build_default_location_graph", _before_world2):
            state = formal_session_state(
                spec, self.old_content, session_id="yoake-mae_s1", wall=WALL, operator="airi"
            )
        world = self.service.create("yoake-mae", state, adapters=_adapters(self.old_content, seed=True))
        # 10-07 那次：MMJ 扩展 + 四人入住，用的还是没有教室的内容和地图。
        admission = WorldAdmission(world, cadence=CADENCE, operator="ops")
        with patch("pns.runtime.content_registry.build_default_location_graph", _before_school3):
            pre = admission.preflight({"kind": "extension"})
            admission.execute({"kind": "extension", "operation_id": "mmj-ext", **pre["fingerprints"]})
            for cid in MMJ:
                roommates = [c for c in MMJ if c != cid]
                pre = admission.preflight(
                    {"kind": "admission", "character_id": cid, "roommates": roommates, "ordinal": MMJ.index(cid)}
                )
                self.assertTrue(pre["feasible"], pre)
                proposal = pre["proposal"]
                admission.execute(
                    {
                        "kind": "admission",
                        "operation_id": f"mmj-{cid}",
                        "character_id": cid,
                        "location_id": proposal["location_id"],
                        "roommates": roommates,
                        "ordinal": MMJ.index(cid),
                        "window": proposal["window"],
                        "fingerprints": pre["fingerprints"],
                    }
                )
        world.close("before school-3")
        # 阶段 A 的内容上线：同一个世界换上现行内容恢复。作息没变，作息门照旧齐。
        self.world = self.service.restore("yoake-mae", adapters=_adapters(self.registry))
        self.assertIsNone(self.world.held)
        self.admission = WorldAdmission(self.world, cadence=CADENCE, operator="ops")

    @property
    def state(self):
        return self.world.state

    def extend(self):
        pre = self.admission.preflight({"kind": "extension"})
        self.assertTrue(pre["feasible"], pre)
        self.assertEqual(set(pre["proposal"]["locations"]), set(SCHOOL3))
        return self.admission.execute(
            {"kind": "extension", "operation_id": "school3-ext", **pre["fingerprints"]}
        )

    def grants_body(self, operation_id="school3-grants"):
        pre = self.admission.preflight({"kind": GRANTS})
        self.assertTrue(pre["feasible"], pre)
        return pre, {"kind": GRANTS, "operation_id": operation_id, "fingerprints": pre["fingerprints"]}


# ── 正路：阶段 A 在生产形状的世界里走完 ──────────────────────────────────
class PhaseATests(School3TestCase):
    def test_before_school3_nobody_has_a_classroom(self):
        world = self.state.world_state
        for cid in EXPECTED:
            self.assertFalse(set(world.location_grants[cid]) & SCHOOL3, cid)
        self.assertFalse(any(world.locations.has(lid) for lid in SCHOOL3))

    def test_extension_alone_grants_nothing(self):
        self.assertEqual(self.extend()["outcome"], DURABLE)
        world = self.state.world_state
        self.assertTrue(all(world.locations.has(lid) for lid in SCHOOL3))
        for cid in EXPECTED:
            self.assertFalse(set(world.location_grants[cid]) & SCHOOL3, cid)

    def test_grants_before_the_extension_are_refused_at_preflight(self):
        pre = self.admission.preflight({"kind": GRANTS})
        self.assertFalse(pre["feasible"])
        self.assertEqual(pre["reasons"][0]["code"], "no_location")

    def test_extension_then_grants_matches_the_design_table(self):
        self.extend()
        pre, body = self.grants_body()
        self.assertEqual(
            {cid: {lid for lid, _ in entries} for cid, entries in pre["proposal"].items()},
            EXPECTED,
        )
        result = self.admission.execute(body)
        self.assertEqual(result["outcome"], DURABLE, result)
        self.assertEqual(result["kind"], GRANTS)
        world = self.state.world_state
        for cid in EXPECTED:
            # 追加之后，世界授予与内容授予（地点 + 频道）完全一致。
            self.assertEqual(
                grants_fingerprint(grants_in_world(world, cid)),
                grants_fingerprint(self.registry.grants(cid)),
                cid,
            )
        self.assertFalse(set(world.location_grants.get("kanade", {})) & SCHOOL3)

    def test_nobody_perceives_or_remembers_the_operation(self):
        self.extend()
        _, body = self.grants_body()
        self.admission.execute(body)
        event = next(e for e in self.state.events if e.type is EventType.WORLD_GRANTS_EXTENDED)
        self.assertFalse([o for o in self.state.observations if o.source_event_id == event.event_id])
        self.assertFalse([d for d in self.state.exposures if d.event_id == event.event_id])

    def test_retry_and_query_on_the_public_path(self):
        self.extend()
        _, body = self.grants_body()
        first = self.admission.execute(body)
        count = len(self.state.events)
        again = self.admission.execute(copy.deepcopy(body))
        self.assertTrue(again["retry"])
        self.assertEqual(len(self.state.events), count)
        self.assertEqual(self.admission.query("school3-grants"), {**first, "retry": False})

    def test_a_second_grants_operation_has_nothing_to_add(self):
        self.extend()
        _, body = self.grants_body()
        self.admission.execute(body)
        pre = self.admission.preflight({"kind": GRANTS})
        self.assertFalse(pre["feasible"])
        self.assertEqual(pre["reasons"][0]["code"], "nothing_to_grant")

    def test_a_world_grant_the_content_lacks_is_a_conflict_not_an_addition(self):
        # 这条操作只追加：世界里多出来、或换了身份的授予，不能借它悄悄抹平。
        self.extend()
        self.state.world_state._grant_location("mafuyu", "mizuki_home", "household")
        pre = self.admission.preflight({"kind": GRANTS})
        self.assertFalse(pre["feasible"])
        self.assertEqual(pre["reasons"][0]["code"], "grants_conflict")

    def test_a_channel_difference_is_a_conflict_too(self):
        self.extend()
        self.state.world_state.channel_grants["ena"] = set()
        pre = self.admission.preflight({"kind": GRANTS})
        self.assertFalse(pre["feasible"])
        self.assertEqual(pre["reasons"][0]["code"], "grants_conflict")

    def test_preflight_runs_the_commit_checks(self):
        # ena 实现审 P3：预检不能把提交层注定拒绝的一笔（比如改变旧路线）报成可行。
        self.extend()
        with patch(
            "pns.runtime.event_commit._require_routes_unchanged",
            side_effect=EventCommitError("改变了路线"),
        ):
            pre = self.admission.preflight({"kind": GRANTS})
        self.assertFalse(pre["feasible"])
        self.assertEqual(pre["reasons"][0]["code"], "refused")

    def test_a_stale_preflight_is_refused(self):
        self.extend()
        _, body = self.grants_body()
        body["fingerprints"]["mafuyu"]["after"] = "0" * 64
        with self.assertRaises(AdmissionRejected) as caught:
            self.admission.execute(body)
        self.assertEqual(caught.exception.code, "stale_preflight")

    def test_restore_replays_and_tampered_grants_are_refused(self):
        self.extend()
        _, body = self.grants_body()
        self.admission.execute(body)
        self.world.close("phase A")
        world = self.service.restore("yoake-mae", adapters=_adapters(self.registry))
        self.assertIsNone(world.held)
        state = world.state
        verify_world2_history(state)
        for cid in EXPECTED:
            self.assertTrue(EXPECTED[cid] <= set(state.world_state.location_grants[cid]), cid)
        world.close("check")

        from pns.models.session import SessionState

        payload = state.to_dict()
        # 快照里少一条追加来的授予：重放推出来的跟快照对不上。
        broken = copy.deepcopy(payload)
        broken["world_state"]["location_grants"]["mafuyu"].pop("miyamasuzaka_3b")
        with self.assertRaises(WorldReplayError):
            verify_world2_history(SessionState.from_dict(broken))
        # 事件里多授一条：快照跟着改也没用，重放照样推得出不一致（或提交边界直接拒绝）。
        forged = copy.deepcopy(payload)
        for entry in forged["events"]["events"]:
            if entry["type"] == EventType.WORLD_GRANTS_EXTENDED.value:
                entry["payload"]["grants"]["mafuyu"].append(["miyamasuzaka_3e", "student"])
        with self.assertRaises((WorldReplayError, ValueError)):
            verify_world2_history(SessionState.from_dict(forged))


# ── 提交边界：只增不改 ───────────────────────────────────────────────────
def _grants_event(world, grants, fingerprints=None, operation_id="g"):
    if fingerprints is None:
        fingerprints = {}
        for cid, entries in grants.items():
            before = grants_in_world(world, cid)
            roles = dict(before.locations)
            roles.update({lid: role for lid, role in entries})
            after = CharacterGrants(cid, tuple(sorted(roles.items())), before.channels)
            fingerprints[cid] = {"before": grants_fingerprint(before), "after": grants_fingerprint(after)}
    return Event(
        event_id=f"world2:{operation_id}",
        type=EventType.WORLD_GRANTS_EXTENDED,
        occurred_at=world.clock,
        scope=EventScope.PUBLIC,
        payload={
            "operation_id": operation_id,
            "envelope": {},
            "grants": grants,
            "fingerprints": fingerprints,
        },
    )


def _room(location_id, links, *, public=False, role="student"):
    return Location(
        location_id=location_id,
        name=location_id,
        kind=LocationKind.ROOM,
        connections=tuple(Connection(to, travel_minutes=m) for to, m in links),
        access={"public": True} if public else {"public": False, "role": role},
    )


class CommitBoundaryTests(unittest.TestCase):
    def setUp(self):
        # a 公开；b、x、y 要授予。a—b 走 10 分钟，a—x—b 共 2 分钟（x 是一条捷径走廊）。
        graph = LocationGraph(
            (
                _room("a", (("b", 10), ("x", 1)), public=True),
                _room("b", (("a", 10), ("x", 1))),
                _room("x", (("a", 1), ("b", 1))),
                _room("y", ()),
                _room("hall", (), public=True),
            )
        )
        self.world = WorldState(
            clock=datetime(2026, 8, 21, 8, 0), locations=graph, channels=ChannelRegistry()
        )
        self.world._grant_location("mizuki", "b", "student")
        self.world.place_character("mizuki", "a")

    def refused(self, grants, **kwargs):
        with self.assertRaises(EventCommitError):
            validate_against_world(self.world, _grants_event(self.world, grants, **kwargs))

    def test_a_leaf_is_granted(self):
        validate_against_world(self.world, _grants_event(self.world, {"mizuki": [["y", "student"]]}))

    def test_a_shortcut_that_changes_an_old_route_is_refused(self):
        # 这是本操作的产品限制：正当的走廊授予也拒（设计 §4.2 第 5 条）。
        self.refused({"mizuki": [["x", "student"]]})

    def test_role_change_and_duplicates_are_refused(self):
        self.refused({"mizuki": [["b", "staff"]]})
        self.refused({"mizuki": [["y", "student"], ["y", "student"]]})

    def test_public_unknown_and_wrong_role_locations_are_refused(self):
        self.refused({"mizuki": [["hall", "student"]]})
        self.refused({"mizuki": [["nowhere", "student"]]})
        self.refused({"mizuki": [["y", "teacher"]]})

    def test_a_newcomer_cannot_be_admitted_this_way(self):
        self.refused({"ena": [["y", "student"]]})

    def test_fingerprints_must_match_and_cover_exactly_the_same_people(self):
        good = _grants_event(self.world, {"mizuki": [["y", "student"]]})
        self.refused({"mizuki": [["y", "student"]]}, fingerprints={})
        self.refused(
            {"mizuki": [["y", "student"]]},
            fingerprints={"mizuki": {"before": "0" * 64, "after": good.payload["fingerprints"]["mizuki"]["after"]}},
        )

    def test_one_bad_entry_refuses_the_whole_operation(self):
        self.world._grant_location("kanade", "b", "student")
        self.world.place_character("kanade", "a")
        before = copy.deepcopy(self.world.location_grants)
        self.refused({"mizuki": [["y", "student"]], "kanade": [["x", "student"]]})
        self.assertEqual(self.world.location_grants, before)

    def test_it_cannot_be_the_first_world2_operation(self):
        # 由 _check_baseline_order 拒；在会话层验（提交前世界上没有任何 WORLD-2 操作）。
        from pns.models.session import SessionState
        from pns.runtime.event_commit import commit_authority_event

        state = SessionState(session_id="s", scene="gate", characters=["mizuki"])
        state.attach_world_state(self.world)
        state.initialize_runtime("开场")
        with self.assertRaises(EventCommitError):
            commit_authority_event(state, _grants_event(self.world, {"mizuki": [["y", "student"]]}))

    def test_the_payload_shape_is_fixed(self):
        event = _grants_event(self.world, {"mizuki": [["y", "student"]]})
        fields = dict(
            event_id=event.event_id,
            type=event.type,
            occurred_at=event.occurred_at,
            scope=EventScope.PUBLIC,
        )
        with self.assertRaises(EventError):
            Event(**fields, payload={**dict(event.payload), "baseline": None})
        with self.assertRaises(EventError):
            Event(**{**fields, "actor_id": "mizuki"}, payload=dict(event.payload))


class RequestParsingTests(unittest.TestCase):
    def test_the_grants_request_is_strict(self):
        good = {"kind": GRANTS, "operation_id": "g1", "fingerprints": {"mizuki": {"before": "a", "after": "b"}}}
        self.assertEqual(parse_operation(good).canonical(), good)
        for broken in (
            {**good, "grants": {}},
            {**good, "fingerprints": {}},
            {**good, "fingerprints": {"mizuki": {"before": "a"}}},
            {"kind": "nope", "operation_id": "g1"},
        ):
            with self.assertRaises(AdmissionRejected):
                parse_operation(broken)


if __name__ == "__main__":
    unittest.main()
