# tests/test_world2_admission.py — WORLD-2 C8a：维护入口的服务层（WorldAdmission）。
#
# 守的线（设计 v3 §3.2、§4.6、§4.8，§6.5/6/9/10/11；审查 §4 的子情形）：
#   1. 一扩展 + 四入住是五笔独立操作，各自 durable；关闭再恢复，重放互验通过，
#      第二天四人按作息起床；
#   2. 重试：同 id 同请求只查询 / 补存，不重判窗口、不重做前身检查、不重算种子；
#      同 id 不同请求拒绝；查询不补存；
#   3. 保存故障：序列化、文件 fsync、replace 失败 → committed；目录 fsync 失败 →
#      visible_not_durable；之后同 id 重试补存到 durable；已提交未保存时放弃世界，
#      恢复回到最后一次 checkpoint（没有入住）；
#   4. 前身（D7）：任一持久域里有她的实体引用就拒绝；台词里的同名字串、别人声明
#      她是室友都不算；
#   5. 窗口：不在睡眠段、地点不是作息的住宿地点、房里有清醒者 / 非声明室友、
#      声明的室友不住这里、过了出发时刻、预检过期 → 拒绝；预检给下一个可行窗口；
#      第三笔失败、第四笔照常；
#   6. 跨表日期：周五夜→周六、祝日前夜→祝日、跨午夜，窗口的结束就是作息导演让她
#      动身的那一刻。
#
# 运行: python -m unittest discover -s tests -p test_world2_admission.py
import copy
import dataclasses
import errno
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from pns.models.event import Event, EventScope, EventType  # noqa: E402
from pns.models.world_state import ActivityKind, Availability  # noqa: E402
from pns.runtime.admission import (  # noqa: E402
    COMMITTED,
    DURABLE,
    VISIBLE_NOT_DURABLE,
    AdmissionRejected,
    WorldAdmission,
    next_sleep_window,
    predecessor_records,
    sleep_window,
)
from pns.runtime.autonomy.audit import ScriptedAuditor  # noqa: E402
from pns.runtime.autonomy.seeding import ActivationCadence, seed_character_activations  # noqa: E402
from pns.runtime.event_commit import commit_session_event  # noqa: E402
from pns.runtime.formal_world import YOAKE_MAE, formal_session_state  # noqa: E402
from pns.runtime.persistence import store as store_mod  # noqa: E402
from pns.runtime.persistence.archive import WorldArchive  # noqa: E402
from pns.runtime.persistence.lifecycle import RuntimeAdapters, WorldLifecycleService  # noqa: E402
from pns.runtime.persistence.store import FileWorldStore  # noqa: E402
from pns.runtime.reload import BOUNDARY  # noqa: E402
from pns.runtime.rhythm import RhythmDirector  # noqa: E402

from tests.test_world2_content import FOUR, _old_graph  # noqa: E402
from tests.test_world2_events import MMJ  # noqa: E402

WALL = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)  # 东京 9/27（周日）19:00
CADENCE = ActivationCadence()
ROOM = "lumina_forum_mmj_room"


def _adapters(registry, *, seed=False):
    def seeding(state):
        seed_character_activations(state.scheduler, list(state.characters), CADENCE)

    return RuntimeAdapters(
        auditor=ScriptedAuditor(),
        rhythm=RhythmDirector(registry.rhythms()),
        content_revision=registry.revision,
        content=registry,
        seed=seeding if seed else None,
    )


class AdmissionTestCase(unittest.TestCase):
    start = time(23, 30)
    wall = WALL

    def make_registry(self):
        return BOUNDARY.active()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.registry = self.make_registry()
        self.service = WorldLifecycleService(FileWorldStore(self.root))
        self.addCleanup(self.service.release_all)
        spec = dataclasses.replace(YOAKE_MAE, start=self.start)
        with patch("pns.runtime.content_registry.build_default_location_graph", _old_graph):
            state = formal_session_state(
                spec, self.registry, session_id="yoake-mae_s1", wall=self.wall, operator="airi"
            )
        self.world = self.service.create(
            "yoake-mae", state, adapters=_adapters(self.registry, seed=True)
        )
        self.admission = WorldAdmission(self.world, cadence=CADENCE, operator="ops")

    # ── 便利 ────────────────────────────────────────────────────────────
    @property
    def state(self):
        return self.world.state

    def extend(self, operation_id="ext-1"):
        pre = self.admission.preflight({"kind": "extension"})
        self.assertTrue(pre["feasible"], pre)
        return self.admission.execute(
            {"kind": "extension", "operation_id": operation_id, **pre["fingerprints"]}
        )

    def admission_body(self, character_id="airi", operation_id=None, ordinal=None, roommates=None):
        ordinal = MMJ.index(character_id) if ordinal is None else ordinal
        roommates = [c for c in MMJ if c != character_id] if roommates is None else roommates
        pre = self.admission.preflight(
            {"kind": "admission", "character_id": character_id, "roommates": roommates, "ordinal": ordinal}
        )
        proposal = pre["proposal"]
        return pre, {
            "kind": "admission",
            "operation_id": operation_id or f"admit-{character_id}",
            "character_id": character_id,
            "location_id": proposal["location_id"] or ROOM,
            "roommates": roommates,
            "ordinal": ordinal,
            "window": proposal["window"] or {"start": "-", "end": "-"},
            "fingerprints": pre["fingerprints"],
        }

    def admit(self, character_id="airi", **kwargs):
        pre, body = self.admission_body(character_id, **kwargs)
        self.assertTrue(pre["feasible"], pre)
        return self.admission.execute(body)

    def assert_rejected(self, body, code):
        before = self.state.to_dict()
        director = self.world.runtime.rhythm
        with self.assertRaises(AdmissionRejected) as caught:
            self.admission.execute(body)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertEqual(self.state.to_dict(), before)
        self.assertIs(self.world.runtime.rhythm, director)


# ── 1. 正路 ─────────────────────────────────────────────────────────────
class HappyPathTests(AdmissionTestCase):
    def test_extension_then_four_admissions_each_durable(self):
        result = self.extend()
        self.assertEqual(result["outcome"], DURABLE)
        self.assertEqual(result["kind"], "extension")
        for cid in MMJ:
            with self.subTest(character_id=cid):
                result = self.admit(cid)
                self.assertEqual(result["outcome"], DURABLE, result)
                self.assertFalse(result["retry"])
        self.assertEqual(self.state.characters, list(FOUR) + list(MMJ))
        for cid in MMJ:
            self.assertEqual(self.state.world_state.location_of(cid), ROOM)

    def test_restore_after_admission_and_wake_up_the_next_day(self):
        self.extend()
        for cid in MMJ:
            self.admit(cid)
        self.world.close("test")
        world = self.service.restore("yoake-mae", adapters=_adapters(self.registry))
        self.assertIsNone(world.held)
        self.assertTrue(all(world.runtime.rhythm.has(cid) for cid in MMJ))
        world.runtime.advance(9 * 60)  # 23:30 → 次日（周一）08:30
        state = world.state
        for cid in MMJ:
            expected = self.registry.rhythm(cid).segment_at(state.world_state.clock)
            self.assertEqual(state.world_state.location_of(cid), expected.location_id, cid)
            self.assertIsNot(state.world_state.activity_of(cid).kind, ActivityKind.RESTING)
        # 旧四人的历史是新历史的原样前缀里的那一份：入住事件之前的事件一条没动。
        events = state.events.events()
        first = next(i for i, e in enumerate(events) if e.type is EventType.WORLD_LOCATIONS_EXTENDED)
        self.assertTrue(all(e.type is not EventType.WORLD_RESIDENT_ADMITTED for e in events[:first]))

    def test_query_reports_without_saving(self):
        self.extend()
        result = self.admit()
        self.assertEqual(self.admission.query("admit-airi"), {**result, "retry": False})
        self.assertEqual(self.admission.query("nope"), {"operation_id": "nope", "outcome": None, "found": False})


class EndToEndTests(AdmissionTestCase):
    """实施单 §5 的端到端：夜间窗口里扩展 + 四笔入住，关闭再恢复，跑一个模拟日。"""

    def test_a_simulated_day_after_admission(self):
        before = [e.to_dict() for e in self.state.events.events()]
        observations_before = len(self.state.observations)
        self.extend()
        for cid in MMJ:
            self.assertEqual(self.admit(cid)["outcome"], DURABLE)
        self.world.close("e2e")

        world = self.service.restore("yoake-mae", adapters=_adapters(self.registry))
        state = world.state
        start = state.world_state.clock
        visited = {cid: set() for cid in MMJ}
        for _ in range(24 * 4):  # 一个模拟日，每 15 分钟看一眼
            world.runtime.advance(15)
            for cid in MMJ:
                visited[cid].add(state.world_state.location_of(cid))
        self.assertEqual(state.world_state.clock, start + timedelta(days=1))

        # 旧世界的历史是新历史的原样前缀。
        after = [e.to_dict() for e in state.events.events()]
        self.assertEqual(after[: len(before)], before)
        # 入住与扩展没有人观察到（之后的正常生活照常被观察）。
        authority = {
            e.event_id
            for e in state.events.events()
            if e.type in (EventType.WORLD_LOCATIONS_EXTENDED, EventType.WORLD_RESIDENT_ADMITTED)
        }
        self.assertEqual(len(authority), 5)
        self.assertFalse(
            [o for o in state.observations if o.source_event_id in authority]
        )
        self.assertFalse(
            [d for d in state.exposures if d.event_id in authority]
        )
        self.assertGreater(len(state.observations), observations_before)
        # 四人都按作息动起来：周一离开宿舍去了别处，这一刻又在作息说的地方。
        for cid in MMJ:
            self.assertGreater(len(visited[cid] - {ROOM}), 0, cid)
            expected = self.registry.rhythm(cid).segment_at(state.world_state.clock)
            self.assertEqual(state.world_state.location_of(cid), expected.location_id, cid)
        world.checkpoint("e2e")
        world.close("e2e")
        # 跑过一天之后再恢复一次：重放互验仍然成立（种子已经按周期推了好几轮）。
        self.service.restore("yoake-mae", adapters=_adapters(self.registry)).close("e2e")


# ── 2. 重试 ──────────────────────────────────────────────────────────
class RetryTests(AdmissionTestCase):
    def test_same_request_returns_the_committed_one_even_after_the_window_closed(self):
        self.extend()
        pre, body = self.admission_body()
        first = self.admission.execute(body)
        events = len(self.state.events)
        self.world.runtime.advance(8 * 60)  # 窗口早过了，房里的人也醒了
        again = self.admission.execute(copy.deepcopy(body))
        self.assertTrue(again["retry"])
        self.assertEqual(again["event_id"], first["event_id"])
        self.assertEqual(again["outcome"], DURABLE)
        admitted = [e for e in self.state.events.events() if e.type is EventType.WORLD_RESIDENT_ADMITTED]
        self.assertEqual(len(admitted), 1)
        self.assertEqual(self.state.characters.count("airi"), 1)
        self.assertGreater(len(self.state.events), events)  # 时间确实走了

    def test_same_id_different_request_is_refused(self):
        self.extend()
        pre, body = self.admission_body()
        self.admission.execute(body)
        changed = dict(body, ordinal=2)
        self.assert_rejected(changed, "operation_id_conflict")
        self.assert_rejected(dict(body, kind="extension", graph_before="x", graph_after="y") | {}, "bad_request")

    def test_an_extension_id_cannot_be_reused_for_an_admission(self):
        self.extend("op-1")
        pre, body = self.admission_body(operation_id="op-1")
        self.assert_rejected(body, "operation_id_conflict")


class RaceTests(AdmissionTestCase):
    def test_concurrent_same_operation_commits_once(self):
        import threading

        self.extend()
        pre, body = self.admission_body()
        barrier = threading.Barrier(2)
        results, errors = [], []

        def run():
            barrier.wait()
            try:
                results.append(self.admission.execute(copy.deepcopy(body)))
            except Exception as e:  # pragma: no cover - 失败时给出原因
                errors.append(e)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(r["retry"] for r in results), [False, True])
        self.assertEqual(len({r["event_id"] for r in results}), 1)
        self.assertEqual(self.state.characters.count("airi"), 1)

    def test_a_closed_world_refuses(self):
        self.extend()
        pre, body = self.admission_body()
        self.world.close("test")
        with self.assertRaises(AdmissionRejected) as caught:
            self.admission.execute(body)
        self.assertEqual(caught.exception.code, "refused")
        self.assertNotIn("airi", self.state.characters)

    def test_admission_leaves_cognition_anchor_and_run_state_alone(self):
        self.extend()
        state, runtime = self.state, self.world.runtime
        before = (state.cognition, state.anchor, runtime.running, runtime.stop_reason, self.world.held)
        self.admit()
        self.assertEqual(
            (state.cognition, state.anchor, runtime.running, runtime.stop_reason, self.world.held),
            before,
        )


# ── 3. 保存故障 ─────────────────────────────────────────────────────────
def _fsync_failing(error, *, directory):
    """只让目录（或只让文件）那一次 fsync 失败；同 tests/test_world_lifecycle._dir_fsync。"""
    real = os.fsync

    def patched(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) == directory:
            raise error
        return real(fd)

    return patched


def _dir_fsync(error):
    return _fsync_failing(error, directory=True)


def _file_fsync(error):
    return _fsync_failing(error, directory=False)


class SaveFaultTests(AdmissionTestCase):
    def faulty(self, fault, expected):
        self.extend()
        pre, body = self.admission_body()
        with fault:
            result = self.admission.execute(body)
        self.assertEqual(result["outcome"], expected, result)
        self.assertIn("airi", self.state.characters)
        # 查询不补存。
        self.assertEqual(self.admission.query("admit-airi")["outcome"], expected)
        # 同 id 同请求的重试补存。
        again = self.admission.execute(body)
        self.assertTrue(again["retry"])
        self.assertEqual(again["outcome"], DURABLE)
        self.assertEqual(again["sequence"], result["sequence"])

    def test_serialization_failure(self):
        self.faulty(patch.object(WorldArchive, "to_dict", side_effect=TypeError("boom")), COMMITTED)

    def test_file_fsync_failure(self):
        self.faulty(patch.object(store_mod.os, "fsync", _file_fsync(OSError(errno.EIO, "io"))), COMMITTED)

    def test_replace_failure(self):
        self.faulty(patch.object(store_mod.os, "replace", side_effect=OSError("disk full")), COMMITTED)

    def test_directory_fsync_failure(self):
        self.faulty(
            patch.object(store_mod.os, "fsync", _dir_fsync(OSError(errno.EIO, "io"))),
            VISIBLE_NOT_DURABLE,
        )

    def test_a_platform_without_directory_sync_is_honestly_not_proven_durable(self):
        # 保存照常成功，但目录同步这一层"这里没有"：证实不了耐久，重试也变不成 durable。
        self.extend()
        pre, body = self.admission_body()
        with patch.object(store_mod.os, "fsync", _dir_fsync(OSError(errno.EINVAL, "unsupported"))):
            result = self.admission.execute(body)
            self.assertEqual(result["outcome"], VISIBLE_NOT_DURABLE)
            again = self.admission.execute(body)
        self.assertTrue(again["retry"])
        self.assertEqual(again["outcome"], VISIBLE_NOT_DURABLE)

    def test_abandoned_before_any_save_restores_without_her(self):
        self.extend()
        pre, body = self.admission_body()
        with patch.object(store_mod.os, "replace", side_effect=OSError("disk full")):
            result = self.admission.execute(body)
            self.assertEqual(result["outcome"], COMMITTED)
            self.world.close("crash", force=True)
        world = self.service.restore("yoake-mae", adapters=_adapters(self.registry))
        self.assertNotIn("airi", world.state.characters)
        self.assertIsNone(WorldAdmission(world, cadence=CADENCE, operator="ops").query("admit-airi")["outcome"])

    def test_visible_but_not_durable_restores_with_her(self):
        self.extend()
        pre, body = self.admission_body()
        with patch.object(store_mod.os, "fsync", _dir_fsync(OSError(errno.EIO, "io"))):
            self.admission.execute(body)
        self.world.close("crash", force=True)
        world = self.service.restore("yoake-mae", adapters=_adapters(self.registry))
        self.assertIn("airi", world.state.characters)
        # 恢复出来的句柄没有耐久性证据：如实说证实不了。
        found = WorldAdmission(world, cadence=CADENCE, operator="ops").query("admit-airi")
        self.assertEqual(found["outcome"], VISIBLE_NOT_DURABLE)


# ── 4. 前身 ─────────────────────────────────────────────────────────────
class PredecessorTests(AdmissionTestCase):
    def setUp(self):
        super().setUp()
        self.extend()

    def test_an_event_mentioning_her_as_a_participant(self):
        world = self.state.world_state
        # 一条把她写成参与者的旧事件（低分辨率记录，比如遗留场景里的一句话）。
        # 现行提交边界不收不认识的参与者，所以直接放进历史，模拟存档里本来就有。
        event = Event(
            event_id="legacy:airi",
            type=EventType.DIALOGUE_SPOKEN,
            occurred_at=world.clock,
            scope=EventScope.PARTICIPANT,
            actor_id="mizuki",
            participants=("mizuki", "airi"),
            location_id="mizuki_home_room",
            payload={"text": "早点睡"},
        )
        with self.state.atomic_commit():
            self.state.events._append(event)
        self.assertTrue(predecessor_records(self.state, "airi"))
        pre, body = self.admission_body()
        self.assertFalse(pre["feasible"])
        self.assertEqual(pre["reasons"][0]["code"], "predecessor")
        self.assert_rejected(body, "predecessor")

    def test_a_queue_entry_for_her(self):
        from pns.models.activation import ActivationKind, ScheduledActivation

        with self.state.atomic_commit():
            self.state.activations._append(
                ScheduledActivation(
                    activation_id="legacy.ping",
                    kind=ActivationKind.CHARACTER_ACTIVATION,
                    due_at=self.state.world_state.clock + timedelta(hours=1),
                    character_id="airi",
                )
            )
        pre, body = self.admission_body()
        self.assert_rejected(body, "predecessor")

    def test_her_name_in_free_text_does_not_count(self):
        state = self.state
        record = {"text": "airi", "roommates": ["airi"], "line": "airi"}
        state.metadata["note"] = record
        self.assertEqual(predecessor_records(state, "airi"), [])
        state.metadata["who"] = "airi"
        self.assertEqual(predecessor_records(state, "airi"), ["state.metadata.who"])

    def test_being_declared_a_roommate_is_not_a_record(self):
        self.admit("minori")  # minori 的 payload 声明了 airi 是室友
        self.assertEqual(predecessor_records(self.state, "airi"), [])
        self.assertEqual(self.admit("airi")["outcome"], DURABLE)


# ── 5. 窗口 ─────────────────────────────────────────────────────────────
class WindowTests(AdmissionTestCase):
    def setUp(self):
        super().setUp()
        self.extend()

    def test_preflight_gives_the_window_and_fingerprints(self):
        pre, body = self.admission_body()
        self.assertTrue(pre["feasible"])
        rhythm = self.registry.rhythm("airi")
        start, end, segment = sleep_window(
            rhythm, self.registry.grants("airi"), self.state.world_state.locations, self.state.world_state.clock
        )
        self.assertEqual(pre["proposal"]["window"], {"start": start.isoformat(), "end": end.isoformat()})
        self.assertEqual(pre["proposal"]["location_id"], ROOM)
        self.assertLessEqual(start, self.state.world_state.clock)
        self.assertGreater(end, self.state.world_state.clock)

    def test_not_in_a_sleep_segment(self):
        pre, body = self.admission_body()
        self.world.runtime.advance(8 * 60)  # 次日 07:30
        self.assert_rejected(body, "not_asleep")
        pre = self.admission.preflight(
            {"kind": "admission", "character_id": "airi", "roommates": list(MMJ[1:]), "ordinal": 0}
        )
        self.assertFalse(pre["feasible"])
        self.assertGreater(datetime.fromisoformat(pre["next_window"]["start"]), self.state.world_state.clock)

    def test_wrong_lodging(self):
        pre, body = self.admission_body()
        self.assert_rejected(dict(body, location_id="lumina_forum_lesson_room"), "not_lodging")

    def test_stale_window_or_content(self):
        pre, body = self.admission_body()
        self.assert_rejected(dict(body, window={"start": body["window"]["start"], "end": "2026-09-28T06:00:00"}), "stale_preflight")
        self.assert_rejected(dict(body, fingerprints={**body["fingerprints"], "rhythm": "0" * 64}), "stale_preflight")
        self.assert_rejected(dict(body, fingerprints={**body["fingerprints"], "grants": "0" * 64}), "stale_preflight")

    def test_a_roommate_who_does_not_live_here(self):
        pre, body = self.admission_body(roommates=["minori", "mizuki"])
        self.assert_rejected(body, "bad_roommate")

    def test_an_undeclared_occupant(self):
        self.admit("minori")
        pre, body = self.admission_body("airi", roommates=["haruka", "shizuku"])
        self.assert_rejected(body, "occupied")

    def test_an_awake_roommate(self):
        self.admit("minori")
        world = self.state.world_state
        with self.state.atomic_commit():
            world.set_availability("minori", Availability.AVAILABLE)
            world.set_activity("minori", ActivityKind.IDLE)
        pre, body = self.admission_body("airi")
        self.assert_rejected(body, "awake")

    def test_the_window_ends_exactly_when_the_director_moves_her(self):
        pre, body = self.admission_body()
        self.admission.execute(body)
        end = datetime.fromisoformat(body["window"]["end"])
        runtime, world = self.world.runtime, self.state.world_state
        runtime.advance(int((end - world.clock).total_seconds() // 60) - 1)
        self.assertIs(world.activity_of("airi").kind, ActivityKind.RESTING)
        mine = [e for e in self.state.events.events() if e.actor_id == "airi"]
        self.assertEqual(mine, [])
        runtime.advance(1)
        moved = [e for e in self.state.events.events() if e.actor_id == "airi"]
        self.assertTrue(moved)
        self.assertEqual(moved[0].occurred_at, end)

    def test_a_failed_third_does_not_block_the_fourth(self):
        self.admit("airi")
        self.admit("minori")
        pre, body = self.admission_body("haruka", roommates=["airi", "minori", "mizuki"])
        with self.assertRaises(AdmissionRejected):
            self.admission.execute(body)
        result = self.admit("shizuku")
        self.assertEqual(result["outcome"], DURABLE)
        seed = self.state.activations.get("seed.activation:shizuku")
        admitted = self.state.events.get("world2:admit-shizuku").occurred_at
        self.assertEqual(seed.due_at, admitted + timedelta(minutes=5 + 5 * 3))
        self.assertEqual(self.admit("haruka")["outcome"], DURABLE)


class MoreRefusalTests(AdmissionTestCase):
    def setUp(self):
        super().setUp()
        self.extend()

    def test_an_unknown_location(self):
        pre, body = self.admission_body()
        self.assert_rejected(dict(body, location_id="nowhere"), "not_lodging")

    def test_a_resident_with_unknown_whereabouts(self):
        pre, body = self.admission_body()
        with self.state.atomic_commit():
            self.state.world_state.remove_character("ena")
        self.assert_rejected(body, "unknown_occupancy")

    def test_her_own_id_among_roommates(self):
        pre, body = self.admission_body()
        self.assert_rejected(dict(body, roommates=["airi", "minori"]), "bad_request")

    def test_malformed_requests(self):
        pre, body = self.admission_body()
        for broken in (
            dict(body, operation_id="bad id!"),
            dict(body, extra=1),
            dict(body, ordinal=True),
            dict(body, roommates="minori"),
            dict(body, window={"start": "x"}),
            dict(body, kind="promotion"),
            [],
        ):
            with self.subTest(body=str(broken)[:60]):
                self.assert_rejected(broken, "bad_request")

    def test_nothing_left_to_extend(self):
        pre = self.admission.preflight({"kind": "extension"})
        self.assertFalse(pre["feasible"])
        self.assertEqual(pre["reasons"][0]["code"], "nothing_to_extend")

    def test_a_world_without_a_content_snapshot(self):
        world = self.world
        with patch.object(type(world), "content", property(lambda self: None)):
            with self.assertRaises(AdmissionRejected) as caught:
                WorldAdmission(world, cadence=CADENCE, operator="ops")
        self.assertEqual(caught.exception.code, "no_content")


class ExtensionRefusalTests(AdmissionTestCase):
    def test_a_stale_extension_preflight(self):
        pre = self.admission.preflight({"kind": "extension"})
        body = {"kind": "extension", "operation_id": "ext", **pre["fingerprints"], "graph_after": "0" * 64}
        self.assert_rejected(body, "stale_preflight")

    def test_a_cold_graph_that_changes_an_old_location(self):
        real = type(self.registry).new_location_graph

        def changed(registry):
            graph = real(registry)
            from pns.models.location import Location, LocationGraph

            entries = []
            for location in graph:
                entry = location.to_dict()
                if entry["location_id"] == "kanade_home":
                    entry["name"] = entry["name"] + "（改）"
                entries.append(Location.from_dict(entry))
            return LocationGraph(entries)

        with patch.object(type(self.registry), "new_location_graph", changed):
            pre = self.admission.preflight({"kind": "extension"})
        self.assertFalse(pre["feasible"])
        self.assertEqual(pre["reasons"][0]["code"], "cold_graph_conflict")

    def test_only_the_first_extension_carries_a_baseline(self):
        self.extend()
        first = self.state.events.get("world2:ext-1")
        self.assertIsNotNone(first.payload["baseline"])


class PastDepartureTests(AdmissionTestCase):
    """下一段在别处：出发时刻 = 下一段开始 − 路程。过了出发时刻就不能入住。"""

    def make_registry(self):
        registry = BOUNDARY.active()
        content = registry.character("airi")
        rhythm = content.rhythm
        clock = datetime(2026, 9, 27, 3, 0)  # 周日（休息日表）凌晨，还在睡
        _, start = rhythm.occurrence_at(clock)
        following, next_start = rhythm.next_after(start)
        self.next_start = next_start
        # 世界开在"已过出发、还没到下一段"的那一分钟。
        self.start = (next_start - timedelta(minutes=2)).time()
        tables = []
        for table in rhythm.tables:
            tables.append(
                tuple(
                    dataclasses.replace(
                        segment, activity=ActivityKind.IDOL_PRACTICE, location_id="lumina_forum_lesson_room"
                    )
                    if segment is following
                    else segment
                    for segment in table
                )
            )
        changed = dataclasses.replace(
            rhythm, segments=tables[0], **({"rest_day_segments": tables[1]} if len(tables) > 1 else {})
        )
        characters = dict(registry.characters)
        characters["airi"] = dataclasses.replace(content, rhythm=changed)
        return dataclasses.replace(registry, revision=registry.revision + 1, characters=characters)

    def test_past_departure_is_refused(self):
        self.extend()
        rhythm, grants = self.registry.rhythm("airi"), self.registry.grants("airi")
        graph = self.state.world_state.locations
        start, end, _ = sleep_window(rhythm, grants, graph, datetime(2026, 9, 27, 3, 0))
        self.assertLess(end, self.next_start)  # 要走路过去：出发早于下一段开始
        self.assertLessEqual(end, self.state.world_state.clock)
        pre = self.admission.preflight(
            {"kind": "admission", "character_id": "airi", "roommates": list(MMJ[1:]), "ordinal": 0}
        )
        self.assertFalse(pre["feasible"])
        pre, body = self.admission_body()
        body = dict(body, location_id=ROOM, window={"start": start.isoformat(), "end": end.isoformat()})
        self.assert_rejected(body, "past_departure")


class DepartureTests(unittest.TestCase):
    """窗口的结束 = 作息导演让她动身的那一刻（跨表、跨午夜都一致）。"""

    def test_window_end_is_when_the_director_moves_her(self):
        registry = BOUNDARY.active()
        graph = registry.new_location_graph()
        cases = {
            "friday_night": datetime(2026, 10, 2, 23, 30),  # 周五夜 → 周六
            "holiday_eve": datetime(2026, 10, 11, 23, 30),  # 祝日（10/12 体育の日）前夜
            "past_midnight": datetime(2026, 9, 29, 2, 0),  # 跨午夜、未到本日首段
        }
        for name, clock in cases.items():
            for cid in MMJ:
                with self.subTest(case=name, character_id=cid):
                    rhythm, grants = registry.rhythm(cid), registry.grants(cid)
                    window = sleep_window(rhythm, grants, graph, clock)
                    self.assertIsNotNone(window)
                    start, end, segment = window
                    self.assertEqual(segment.location_id, ROOM)
                    self.assertLessEqual(start, clock)
                    self.assertGreater(end, clock)
                    # 窗口里每一刻她都按作息睡在这间房里（零点切开的那一段并进来）；
                    # 窗口一结束，作息就不再是"在这里睡"。
                    probe = start
                    while probe < end:
                        here = rhythm.segment_at(probe)
                        self.assertIs(here.activity, ActivityKind.RESTING)
                        self.assertEqual(here.location_id, ROOM)
                        probe += timedelta(minutes=10)
                    after = rhythm.segment_at(end)
                    self.assertFalse(
                        after.activity is ActivityKind.RESTING and after.location_id == ROOM
                    )
                    self.assertEqual(
                        next_sleep_window(rhythm, grants, graph, clock), (start, end)
                    )


if __name__ == "__main__":
    unittest.main()
