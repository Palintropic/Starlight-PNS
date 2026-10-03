# tests/test_content_transition.py — CONTENT-4 上线过渡（过渡设计 v2 + 修订 R2）
#
# 作息门不齐时：恢复路径终局停止运行时、不碰时钟；服务器路径存盘、关闭、释放后
# 返回 409；决定由冷维护记录，完成以重新读出的磁盘为准。验收逐条对应 ena 三轮
# 复审（kickoff/CONTENT_4_TRANSITION_REVIEW_*_FINDINGS.md）。
import ast
import itertools
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from pns.interfaces.composition import WORLD_ROOT_ENV  # noqa: E402
from pns.interfaces.content_maintenance import (  # noqa: E402
    read_conflicts,
    run_content_decisions,
)
from pns.models.world_state import ActivityKind  # noqa: E402
from pns.runtime.autonomy.coordinator import AutonomyError  # noqa: E402
from pns.runtime.formal_world import (  # noqa: E402
    ABSENT,
    rhythm_fingerprint,
    rhythm_gate,
    rhythm_subject,
)
from pns.runtime.persistence import (  # noqa: E402
    ContentNotAdopted,
    OwnershipError,
    StorageError,
    WorldAlreadyOwned,
)
from pns.runtime.persistence.store import ArchiveNotDurable, FileWorldStore  # noqa: E402
from pns.runtime.reload import BOUNDARY  # noqa: E402

from tests.test_formal_world import PlaneTestCase, _state, _with_rhythm  # noqa: E402
from tests.test_mvp_generation import CANARY  # noqa: E402

CHANGED = ("ena", "mafuyu", "mizuki")


def _other_activity(registry, character_id, *avoid):
    from datetime import datetime

    current = registry.rhythm(character_id).segment_at(datetime(2026, 9, 27, 19, 0)).activity
    return next(
        kind
        for kind in ActivityKind
        if kind not in (current, ActivityKind.UNSPECIFIED, *avoid)
    )


def _first_weekday_segment_as(registry, character_id, activity):
    """另一种改法：只换平日表第一段的活动，休息日表原样保留。"""
    import dataclasses

    from pns.world.rhythm import DailyRhythm

    content = registry.character(character_id)
    rhythm = content.rhythm
    segments = (dataclasses.replace(rhythm.segments[0], activity=activity),) + tuple(
        rhythm.segments[1:]
    )
    changed = dataclasses.replace(
        content,
        rhythm=DailyRhythm(
            character_id=rhythm.character_id,
            segments=segments,
            rest_day_segments=rhythm.rest_day_segments,
        ),
    )
    characters = dict(registry.characters)
    characters[character_id] = changed
    return dataclasses.replace(registry, revision=registry.revision + 1, characters=characters)


def _changed(registry, characters=CHANGED):
    """一份内容快照：这些人的作息各换了一版（与 CONTENT-4 上线时一样是三人）。"""
    for character_id in characters:
        registry = _with_rhythm(registry, character_id, _other_activity(registry, character_id))
    return registry


def _decide(state, conflict_id, decision):
    with state.atomic_commit():
        state.set_content(state.content.decided(conflict_id, decision, wall="w"))


def _gate(state, registry):
    with state.atomic_commit():
        return rhythm_gate(state, registry.rhythms(), registry_revision=2, wall="w")


def _SaveHook(at, action):
    """FileWorldStore.save 的替身：第 `at` 次（从 1 数）调用时改由 `action` 处理。

    是普通函数而不是可调用对象：patch 到类上之后要像方法一样拿到 store。
    """
    original = FileWorldStore.save
    calls = [0]

    def save(store, archive):
        calls[0] += 1
        if calls[0] == at:
            return action(original, store, archive)
        return original(store, archive)

    return save


def _raise(error):
    def action(original, store, archive):
        raise error

    return action


# ── 门报告 ──────────────────────────────────────────────────────────────


class GateReportTests(unittest.TestCase):
    def setUp(self):
        self.registry = BOUNDARY.active()

    def test_unchanged_content_is_a_complete_gate(self):
        state = _state()
        gate = _gate(state, self.registry)
        self.assertTrue(gate.complete)
        self.assertEqual({s.reason for s in gate.subjects}, {"ok"})
        self.assertEqual(sorted(gate.accepted), ["ena", "kanade", "mafuyu", "mizuki"])

    def test_only_adopting_every_new_version_completes_the_gate(self):
        changed = _changed(self.registry)
        for combo in itertools.product(("adopted", "declined", "deferred"), repeat=3):
            with self.subTest(combo=combo):
                state = _state()
                first = _gate(state, changed)
                self.assertFalse(first.complete)
                ids = {s.character_id: s.conflict_id for s in first.blocked()}
                self.assertEqual(sorted(ids), list(CHANGED))
                for character_id, decision in zip(CHANGED, combo):
                    _decide(state, ids[character_id], decision)
                again = _gate(state, changed)
                self.assertEqual(again.complete, set(combo) == {"adopted"})
                reasons = {s.character_id: s.reason for s in again.subjects}
                for character_id, decision in zip(CHANGED, combo):
                    expected = "ok" if decision == "adopted" else decision
                    self.assertEqual(reasons[character_id], expected)
                self.assertEqual(reasons["kanade"], "ok")
                self.assertEqual(
                    len(again.accepted), 1 + combo.count("adopted"), "生效的表 = 奏 + 采用数"
                )

    def test_a_missing_definition_never_completes_the_gate(self):
        removed = dict(self.registry.rhythms())
        removed.pop("mafuyu")
        for decision in ("pending", "adopted", "declined", "deferred"):
            with self.subTest(decision=decision):
                state = _state()
                with state.atomic_commit():
                    first = rhythm_gate(state, removed, registry_revision=2, wall="w")
                (blocked,) = first.blocked()
                self.assertEqual(blocked.reason, "missing_definition")
                self.assertEqual(blocked.conflict_status, "pending")
                if decision != "pending":
                    _decide(state, blocked.conflict_id, decision)
                with state.atomic_commit():
                    again = rhythm_gate(state, removed, registry_revision=2, wall="w")
                self.assertFalse(again.complete)
                self.assertNotIn("mafuyu", again.accepted)
                (still,) = again.blocked()
                if decision == "adopted":
                    self.assertEqual(state.content.adopted_fingerprint("rhythm:mafuyu"), ABSENT)
                    self.assertEqual(still.reason, "adopted_absent")
                    self.assertIsNone(still.conflict_id, "没有可决定的记录")
                else:
                    self.assertEqual(still.reason, "missing_definition")
                    self.assertEqual(still.conflict_status, decision)

    def test_the_reported_id_targets_the_current_adopted_version(self):
        # ena R2 U5：A 提议 → B 被采用 → A 再出现。旧 A 那条针对的版本已经过期。
        a = _with_rhythm(self.registry, "mizuki", ActivityKind.DRAWING)
        # _with_rhythm 会把双表压成一张表，B 换一种改法，保证三版两两不同。
        first = self.registry.rhythm("mizuki").segments[0].activity
        b = _first_weekday_segment_as(
            self.registry, "mizuki", next(k for k in ActivityKind if k not in (first, ActivityKind.UNSPECIFIED))
        )
        self.assertEqual(
            len({rhythm_fingerprint(x.rhythm("mizuki")) for x in (self.registry, a, b)}), 3
        )
        state = _state()
        (old_a,) = _gate(state, a).blocked()
        (offer_b,) = _gate(state, b).blocked()
        _decide(state, offer_b.conflict_id, "adopted")
        (new_a,) = _gate(state, a).blocked()
        self.assertEqual(new_a.reason, "pending")
        self.assertNotEqual(new_a.conflict_id, old_a.conflict_id)
        _decide(state, new_a.conflict_id, "adopted")  # 报告给的 id 能被真的采用
        self.assertTrue(_gate(state, a).complete)

    def test_an_adopted_version_can_come_back_and_be_adopted_again(self):
        # ena IMPL-F1：原版 A → 采用 B → 采用 A → 再出现 B，必须有一条新的、能决定的
        # pending；再绕一圈也一样。同一个周期里重复打开不多记。
        a = self.registry
        first = a.rhythm("mizuki").segments[0].activity
        b = _first_weekday_segment_as(
            a, "mizuki", next(k for k in ActivityKind if k not in (first, ActivityKind.UNSPECIFIED))
        )
        state = _state()
        seen = set()
        for target in (b, a, b, a):
            (offer,) = _gate(state, target).blocked()
            self.assertEqual(offer.reason, "pending")
            self.assertNotIn(offer.conflict_id, seen, "每一轮都是一条新记录")
            seen.add(offer.conflict_id)
            count = len(state.content.conflicts)
            _gate(state, target)
            self.assertEqual(len(state.content.conflicts), count, "同一轮重复打开不多记")
            _decide(state, offer.conflict_id, "adopted")
            self.assertTrue(_gate(state, target).complete)
            self.assertEqual(
                state.content.adopted_fingerprint("rhythm:mizuki"),
                rhythm_fingerprint(target.rhythm("mizuki")),
            )

    def test_a_declined_version_stays_declined_within_its_cycle(self):
        a = self.registry
        first = a.rhythm("mizuki").segments[0].activity
        b = _first_weekday_segment_as(
            a, "mizuki", next(k for k in ActivityKind if k not in (first, ActivityKind.UNSPECIFIED))
        )
        state = _state()
        (offer,) = _gate(state, b).blocked()
        _decide(state, offer.conflict_id, "declined")
        (again,) = _gate(state, b).blocked()
        self.assertEqual((again.reason, again.conflict_id), ("declined", offer.conflict_id))

    def test_a_world_without_a_ledger_has_no_gate(self):
        from pns.models.session import SessionState

        state = SessionState("s", "gate", ["mizuki"])
        gate = rhythm_gate(state, {"mizuki": object()}, registry_revision=1, wall="w")
        self.assertTrue(gate.complete)
        self.assertEqual(gate.subjects, ())


# ── 冷打开：终局停止 ────────────────────────────────────────────────────


class HeldWorldTests(PlaneTestCase):
    rate = 1.0

    def setUp(self):
        super().setUp()
        self.plane.create_formal("yoake-mae")
        self.plane.close("yoake-mae")
        self.changed = _changed(self.registry)
        self.cold = self.make_plane(registry_provider=lambda: self.changed)
        self.addCleanup(self.cold.service.release_all)

    def open_cold(self, *, start=False):
        return self.cold.service.restore(
            "yoake-mae",
            adapters=self.cold.build_adapters(self.changed),
            checkpoint_policy=self.cold.checkpoint_policy,
            clock=None,
            start=start,
        )

    def physical(self, world):
        ws = world.state.world_state
        return (
            ws.clock,
            tuple(
                (cid, ws.location_of(cid), ws.activity_of(cid), ws.availability_of(cid))
                for cid in sorted(world.state.characters)
            ),
            world.state.anchor,
        )

    def test_a_held_world_cannot_be_started_through_its_runtime(self):
        # ena R1 F1 的反例：直接 world.runtime.start() + advance。
        world = self.open_cold()
        self.assertEqual(world.status()["held"]["reason"], "content_pending")
        self.assertEqual(
            sorted(s["subject"] for s in world.status()["held"]["subjects"]),
            [rhythm_subject(c) for c in CHANGED],
        )
        before = self.physical(world)
        with self.assertRaises(AutonomyError):
            world.runtime.start()
        for call in (
            lambda: world.runtime.advance(330),
            lambda: world.runtime.advance_to_next_due(),
            lambda: world.runtime.set_quiet_time_events(True),
        ):
            with self.assertRaises(AutonomyError):
                call()
        self.assertFalse(world.runtime.running)
        self.assertEqual(self.physical(world), before)
        status = world.close("test")
        self.assertTrue(status["clean"])
        self.assertFalse(status["owned"])

    def test_asking_to_start_does_not_start_a_held_world(self):
        world = self.open_cold(start=True)
        self.assertIsNotNone(world.held)
        self.assertFalse(world.runtime.running)
        self.assertEqual(world.runtime.stop_reason, "content_pending")

    def test_a_partial_adoption_stays_held(self):
        # ena R1 F1 的第二个反例：只采用绘名和真冬，瑞希不能被冻住、别人往前走。
        world = self.open_cold()
        ids = {s["character_id"]: s["conflict_id"] for s in world.held["subjects"]}
        world.close("test")
        report = run_content_decisions(
            self.cold, "yoake-mae", [(ids["ena"], "adopted"), (ids["mafuyu"], "adopted")]
        )
        self.assertEqual(report.errors, [])
        world = self.open_cold()
        self.assertEqual([s["character_id"] for s in world.held["subjects"]], ["mizuki"])
        with self.assertRaises(AutonomyError):
            world.runtime.start()
        world.close("test")

    def test_decisions_and_checkpoints_still_work_while_held(self):
        world = self.open_cold()
        conflict_id = world.held["subjects"][0]["conflict_id"]
        world.runtime.decide_content(conflict_id, "adopted")
        world.checkpoint("content_decision")
        world.close("test")
        statuses = {v.conflict_id: v.status for v in read_conflicts(self.cold, "yoake-mae")}
        self.assertEqual(statuses[conflict_id], "adopted")


# ── 服务器路径 ──────────────────────────────────────────────────────────


class ServerRestoreTests(PlaneTestCase):
    rate = 1.0

    def setUp(self):
        super().setUp()
        self.plane.create_formal("yoake-mae")
        self.plane.close("yoake-mae")
        self.changed = _changed(self.registry)
        self.server = self.make_plane(registry_provider=lambda: self.changed)
        self.addCleanup(self.server.service.release_all)
        from fastapi.testclient import TestClient
        from pns.interfaces.app import create_app

        self.api = TestClient(create_app(self.server))
        self.disk_before = self.disk()

    def disk(self):
        # 时钟、锚点、认知时间线：搁置不许动其中任何一样（恢复时钟会给认知时间线
        # 追加 RESTORED 区间，ena R1 T4）。
        state = self.server.store.load("yoake-mae", history=False).state
        return state["world_state"]["clock"], state["anchor"], state["cognition"]

    def assert_untouched(self):
        self.assertEqual(self.disk(), self.disk_before, "时钟与锚点不动")

    def test_restore_is_a_content_409_after_a_clean_close(self):
        response = self.api.post("/api/persistent-worlds/yoake-mae/restore")
        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()["detail"]
        self.assertEqual(detail["category"], "content_not_adopted")
        self.assertEqual(
            sorted(s["subject"] for s in detail["subjects"]),
            [rhythm_subject(c) for c in CHANGED],
        )
        self.assertEqual({s["reason"] for s in detail["subjects"]}, {"pending"})
        self.assertIsNone(self.server.service.opened("yoake-mae"), "所有权已经释放")
        self.assertEqual(
            {v.status for v in read_conflicts(self.server, "yoake-mae")}, {"pending"}
        )
        self.assert_untouched()
        # 所有权真的还回去了：别的进程（这里是另一个控制面）能打开它。
        held = self.plane.service.restore(
            "yoake-mae",
            adapters=self.plane.build_adapters(self.changed),
            checkpoint_policy=self.plane.checkpoint_policy,
            clock=None,
        )
        held.close("test")

    def test_a_conflict_save_failure_is_a_storage_error_and_releases(self):
        with patch.object(FileWorldStore, "save", _SaveHook(1, _raise(StorageError("磁盘满了")))):
            response = self.api.post("/api/persistent-worlds/yoake-mae/restore")
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json()["detail"]["category"], "checkpoint_failed")
        self.assertIsNone(self.server.service.opened("yoake-mae"))
        self.assertEqual(read_conflicts(self.server, "yoake-mae"), ())

    def check_close_failure(self, error, status, category):
        # 第 1 次 save 是冲突存盘，第 2 次是关闭。
        with patch.object(FileWorldStore, "save", _SaveHook(2, _raise(error))):
            response = self.api.post("/api/persistent-worlds/yoake-mae/restore")
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["detail"]["category"], category)
        world = self.server.service.opened("yoake-mae")
        self.assertIsNotNone(world, "关闭失败：世界留在登记表里，可以再关一次")
        self.assertIsNone(world.clock_worker, "没有起时钟 worker")
        body = self.api.get("/api/persistent-worlds/yoake-mae").json()
        self.assertFalse(body["closed"])
        self.assertEqual(body["held"]["reason"], "content_pending")
        self.assertFalse(body["running"])
        retry = self.api.post("/api/persistent-worlds/yoake-mae/close")
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertTrue(retry.json()["clean"])
        self.assertIsNone(self.server.service.opened("yoake-mae"))
        self.assert_untouched()

    def test_a_close_storage_failure_keeps_its_own_error_class(self):
        self.check_close_failure(StorageError("写不下去"), 500, "checkpoint_failed")

    def test_a_close_durability_failure_keeps_its_own_error_class(self):
        def written_but_not_durable(original, store, archive):
            original(store, archive)
            raise ArchiveNotDurable("目录同步失败", revision=archive.revision, path="p")

        with patch.object(FileWorldStore, "save", _SaveHook(2, written_but_not_durable)):
            response = self.api.post("/api/persistent-worlds/yoake-mae/restore")
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json()["detail"]["category"], "archive_not_durable")
        self.assertIsNotNone(self.server.service.opened("yoake-mae"))
        retry = self.api.post("/api/persistent-worlds/yoake-mae/close")
        self.assertEqual(retry.status_code, 200, retry.text)

    def test_a_close_ownership_failure_keeps_its_own_error_class(self):
        self.check_close_failure(OwnershipError("锁不见了"), 409, "ownership_lost")

    def test_the_world_is_labelled_held_before_the_close_and_cannot_start(self):
        # ena R3 V2：登记之后、关闭之前的窗口里，它已经是标明原因的 held 世界。
        from pns.runtime.persistence.lifecycle import PersistentWorld

        entered, release = threading.Event(), threading.Event()
        real_close = PersistentWorld.close

        def pausing_close(world, *args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(10))
            return real_close(world, *args, **kwargs)

        outcome = {}

        def restore():
            try:
                self.server.restore("yoake-mae")
            except Exception as e:  # noqa: BLE001
                outcome["error"] = e

        with patch.object(PersistentWorld, "close", pausing_close):
            thread = threading.Thread(target=restore)
            thread.start()
            self.assertTrue(entered.wait(10))
            try:
                body = self.api.get("/api/persistent-worlds/yoake-mae").json()
                self.assertEqual(body["held"]["reason"], "content_pending")
                self.assertFalse(body["running"])
                start = self.api.post("/api/persistent-worlds/yoake-mae/autonomy/start")
                self.assertGreaterEqual(start.status_code, 400, start.text)
                activity = self.api.post(
                    "/api/persistent-worlds/yoake-mae/activity",
                    json={"character_id": "mizuki", "activity": "drawing"},
                )
                self.assertGreaterEqual(activity.status_code, 400, activity.text)
                world = self.server.service.opened("yoake-mae")
                self.assertIsNone(world.clock_worker)
                self.assertFalse(world.runtime.running)
            finally:
                release.set()
                thread.join(10)
        self.assertIsInstance(outcome.get("error"), ContentNotAdopted)
        self.assertIsNone(self.server.service.opened("yoake-mae"))
        self.assert_untouched()

    def test_a_complete_gate_restores_exactly_as_before(self):
        # 门齐：照旧恢复、照旧起 worker（ena R3 V4 / U6）。
        status = self.plane.restore("yoake-mae")
        self.assertTrue(status["running"])
        self.assertIsNone(status["held"])
        world = self.world()
        self.assertIsNotNone(world.clock_worker)

    def test_a_complete_gate_without_start_keeps_the_existing_behaviour(self):
        world = self.plane.service.restore(
            "yoake-mae",
            adapters=self.plane.build_adapters(self.registry),
            checkpoint_policy=self.plane.checkpoint_policy,
            clock=self.plane.autonomy.clock,
            start=False,
        )
        self.assertIsNone(world.held)
        self.assertFalse(world.runtime.running)
        self.assertIsNotNone(world.clock_worker, "门齐的 start=False + clock 仍起 worker")
        self.assertIsNone(world.runtime.stop_reason)


# ── 冷维护 ──────────────────────────────────────────────────────────────


class MaintenanceTests(PlaneTestCase):
    rate = 1.0

    def setUp(self):
        super().setUp()
        self.plane.create_formal("yoake-mae")
        self.plane.close("yoake-mae")
        self.changed = _changed(self.registry)
        self.cold = self.make_plane(registry_provider=lambda: self.changed)
        self.addCleanup(self.cold.service.release_all)
        with self.assertRaises(ContentNotAdopted) as caught:
            self.cold.restore("yoake-mae")
        self.ids = [s.conflict_id for s in caught.exception.gate.blocked()]

    def adopt_all(self):
        return run_content_decisions(self.cold, "yoake-mae", [(i, "adopted") for i in self.ids])

    def next_seq(self):
        return self.cold.store.load("yoake-mae", history=False).state["content"]["next_seq"]

    def test_adopting_all_three_completes_and_the_world_then_runs(self):
        report = self.adopt_all()
        self.assertTrue(report.complete, report.to_dict())
        self.assertEqual(report.exit_code, 0)
        self.assertEqual({o.result for o in report.outcomes}, {"recorded"})
        status = self.cold.restore("yoake-mae")
        self.assertTrue(status["running"])
        self.assertEqual(
            self.cold.service.opened("yoake-mae").runtime.rhythm.characters(),
            ("ena", "kanade", "mafuyu", "mizuki"),
        )

    def test_a_rerun_records_nothing_twice(self):
        self.adopt_all()
        seq = self.next_seq()
        again = self.adopt_all()
        self.assertTrue(again.complete)
        self.assertEqual({o.result for o in again.outcomes}, {"already"})
        self.assertEqual(self.next_seq(), seq, "决定序号不再增加")

    def test_the_maintenance_refuses_while_the_server_holds_the_world(self):
        held = self.cold.service.restore(
            "yoake-mae",
            adapters=self.cold.build_adapters(self.changed),
            checkpoint_policy=self.cold.checkpoint_policy,
            clock=None,
        )
        other = self.make_plane(registry_provider=lambda: self.changed)
        with self.assertRaises(WorldAlreadyOwned):
            run_content_decisions(other, "yoake-mae", [(self.ids[0], "adopted")])
        held.close("test")
        self.assertEqual({v.status for v in read_conflicts(self.cold, "yoake-mae")}, {"pending"})

    def test_the_server_cannot_restore_while_the_maintenance_holds_the_world(self):
        from pns.runtime.autonomy.coordinator import AutonomousRuntime

        entered, release = threading.Event(), threading.Event()
        real = AutonomousRuntime.decide_content

        def pausing(runtime, *args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(10))
            return real(runtime, *args, **kwargs)

        with patch.object(AutonomousRuntime, "decide_content", pausing):
            thread = threading.Thread(
                target=run_content_decisions,
                args=(self.cold, "yoake-mae", [(self.ids[0], "adopted")]),
            )
            thread.start()
            self.assertTrue(entered.wait(10))
            try:
                server = self.make_plane(registry_provider=lambda: self.changed)
                with self.assertRaises(WorldAlreadyOwned):
                    server.restore("yoake-mae")
            finally:
                release.set()
                thread.join(10)
        self.assertEqual(
            {v.conflict_id: v.status for v in read_conflicts(self.cold, "yoake-mae")}[self.ids[0]],
            "adopted",
        )

    def test_a_failed_decision_save_is_not_complete_even_if_close_repairs_it(self):
        # 第 1 条存盘成功，第 2 条存盘失败；后面的不再记；关闭把第 2 条补存。
        with patch.object(FileWorldStore, "save", _SaveHook(2, _raise(StorageError("写不下去")))):
            report = self.adopt_all()
        self.assertNotEqual(report.exit_code, 0)
        self.assertTrue(report.errors)
        self.assertEqual(
            [o.result for o in report.outcomes], ["recorded", "failed", "not_attempted"]
        )
        self.assertEqual(report.disk_status(self.ids[1]), "adopted", "关闭补存了第 2 条")
        self.assertEqual(report.disk_status(self.ids[2]), "pending")
        rerun = self.adopt_all()
        self.assertTrue(rerun.complete, rerun.to_dict())
        self.assertEqual(
            [o.result for o in rerun.outcomes], ["already", "already", "recorded"]
        )

    def test_an_error_this_run_is_not_complete_even_when_the_disk_ends_up_right(self):
        # ena R2 U4：只要求两条，第 2 条存盘失败、关闭把它补上。磁盘最终是对的，
        # 但这次运行报过错，退出码仍然非 0；下一次无错重跑才是 0。
        wanted = [(self.ids[0], "adopted"), (self.ids[1], "adopted")]
        with patch.object(FileWorldStore, "save", _SaveHook(2, _raise(StorageError("写不下去")))):
            report = run_content_decisions(self.cold, "yoake-mae", wanted)
        self.assertTrue(report.on_disk, "关闭补齐了磁盘")
        self.assertTrue(report.strongly_durable)
        self.assertTrue(report.errors)
        self.assertNotEqual(report.exit_code, 0)
        rerun = run_content_decisions(self.cold, "yoake-mae", wanted)
        self.assertEqual(rerun.exit_code, 0, rerun.to_dict())

    def test_unproven_durability_is_not_complete_and_a_rerun_repairs_it(self):
        def written_but_not_durable(original, store, archive):
            original(store, archive)
            raise ArchiveNotDurable("目录同步失败", revision=archive.revision, path="p")

        with patch.object(FileWorldStore, "save", _SaveHook(1, written_but_not_durable)):
            report = self.adopt_all()
        self.assertNotEqual(report.exit_code, 0)
        self.assertEqual(report.disk_status(self.ids[0]), "adopted", "已写入，耐久未证实")
        rerun = self.adopt_all()
        self.assertTrue(rerun.complete, rerun.to_dict())

    def test_an_unsupported_directory_sync_is_not_strong_durability(self):
        with patch.object(FileWorldStore, "_sync_dir", classmethod(lambda cls, d, a: False)):
            report = self.adopt_all()
        self.assertTrue(report.on_disk)
        self.assertFalse(report.strongly_durable)
        self.assertNotEqual(report.exit_code, 0)

    def test_a_different_earlier_decision_is_never_overwritten(self):
        run_content_decisions(self.cold, "yoake-mae", [(self.ids[0], "declined")])
        report = self.adopt_all()
        self.assertNotEqual(report.exit_code, 0)
        self.assertEqual(report.outcomes[0].result, "failed")
        self.assertEqual(report.disk_status(self.ids[0]), "declined")

    def test_an_ownership_failure_after_a_decision_still_reports_the_disk(self):
        # ena IMPL-F2：决定之后的 checkpoint 抛 OwnershipError，不能逃出报告。
        from pns.runtime.persistence.lifecycle import PersistentWorld

        real = PersistentWorld.checkpoint

        def checkpoint(world, reason="manual"):
            if reason == "content_decision":
                raise OwnershipError("锁不见了")
            return real(world, reason)

        with patch.object(PersistentWorld, "checkpoint", checkpoint):
            report = self.adopt_all()
        self.assertNotEqual(report.exit_code, 0)
        self.assertTrue(any("OwnershipError" in error for error in report.errors), report.errors)
        self.assertEqual(
            [o.result for o in report.outcomes], ["failed", "not_attempted", "not_attempted"]
        )
        self.assertIsNotNone(report.close, "照常关闭")
        self.assertIsNotNone(report.disk, "照常从磁盘读回")
        self.assertEqual(report.disk_status(self.ids[0]), "adopted", "关闭把内存里的决定存下了")
        self.assertEqual(report.disk_status(self.ids[1]), "pending", "后面的决定没有执行")
        self.assertIsNone(self.cold.service.opened("yoake-mae"))

    def test_a_historical_record_is_not_this_rounds_decision(self):
        # ena IMPL-F1 的维护面：采用 B → 采用回原版 A → B 再出现。拿第一轮 B 的旧 id
        # 来"采用"不能报"已是此状态"；新一轮给的是新 id，用它才能让门齐。
        self.assertTrue(self.adopt_all().complete)
        back = self.make_plane(registry_provider=lambda: self.registry)
        with self.assertRaises(ContentNotAdopted) as to_a:
            back.restore("yoake-mae")
        ids_a = [s.conflict_id for s in to_a.exception.gate.blocked()]
        self.assertTrue(
            run_content_decisions(back, "yoake-mae", [(i, "adopted") for i in ids_a]).complete
        )
        with self.assertRaises(ContentNotAdopted) as to_b:
            self.cold.restore("yoake-mae")
        new_ids = [s.conflict_id for s in to_b.exception.gate.blocked()]
        self.assertEqual({s.reason for s in to_b.exception.gate.blocked()}, {"pending"})
        self.assertFalse(set(new_ids) & set(self.ids), "新一轮是新记录")

        stale = run_content_decisions(self.cold, "yoake-mae", [(self.ids[0], "adopted")])
        self.assertNotEqual(stale.exit_code, 0)
        self.assertEqual(stale.outcomes[0].result, "failed")
        self.assertIn("历史记录", stale.outcomes[0].error)

        fresh = run_content_decisions(self.cold, "yoake-mae", [(i, "adopted") for i in new_ids])
        self.assertTrue(fresh.complete, fresh.to_dict())
        self.assertEqual(fresh.still_blocked, [])
        self.assertTrue(self.cold.restore("yoake-mae")["running"])

    def test_the_report_says_when_the_gate_is_still_blocked(self):
        report = run_content_decisions(self.cold, "yoake-mae", [(self.ids[0], "adopted")])
        self.assertTrue(report.complete)
        self.assertEqual(
            sorted(s["conflict_id"] for s in report.still_blocked), sorted(self.ids[1:])
        )
        self.assertEqual(self.adopt_all().still_blocked, [])

    def test_an_interrupted_run_resumes_from_disk(self):
        # 进程在决定与 checkpoint 之间死掉：只能依赖最后一次成功的 checkpoint。
        # 这里用"不关闭、直接放掉所有权"模拟进程退出时内核释放锁。
        world = self.cold.service.restore(
            "yoake-mae",
            adapters=self.cold.build_adapters(self.changed),
            checkpoint_policy=self.cold.checkpoint_policy,
            clock=None,
        )
        world.runtime.decide_content(self.ids[0], "adopted")
        world.checkpoint("content_decision")
        world.runtime.decide_content(self.ids[1], "adopted")
        world.release("simulated crash")
        statuses = {v.conflict_id: v.status for v in read_conflicts(self.cold, "yoake-mae")}
        self.assertEqual(statuses[self.ids[0]], "adopted")
        self.assertEqual(statuses[self.ids[1]], "pending", "没存下去的决定不存在")
        seq = self.next_seq()
        report = self.adopt_all()
        self.assertTrue(report.complete, report.to_dict())
        self.assertEqual([o.result for o in report.outcomes], ["already", "recorded", "recorded"])
        self.assertEqual(self.next_seq(), seq + 2)


# ── 维护脚本（真的子进程） ──────────────────────────────────────────────


class MaintenanceScriptTests(PlaneTestCase):
    rate = 1.0

    def setUp(self):
        super().setUp()
        self.plane.create_formal("yoake-mae")
        self.plane.close("yoake-mae")

    def run_script(self, *args):
        env = dict(os.environ)
        env[WORLD_ROOT_ENV] = str(self.root)
        env[self.registry.models.key_name] = CANARY
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "content_decisions.py"), "yoake-mae", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def test_listing_writes_nothing(self):
        path = self.root / "yoake-mae" / "world.json"
        before = path.read_bytes()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("没有内容冲突记录", result.stdout)
        self.assertEqual(path.read_bytes(), before)

    def make_pending(self):
        changed = _changed(self.registry)
        cold = self.make_plane(registry_provider=lambda: changed)
        with self.assertRaises(ContentNotAdopted) as caught:
            cold.restore("yoake-mae")
        return changed, [s.conflict_id for s in caught.exception.gate.blocked()]

    def run_with(self, changed, *args, hook=None):
        """子进程里的内容包换成 `_changed(原内容)`（与 make_pending 相同、确定性的）。

        用 sitecustomize 在解释器启动时换掉 BOUNDARY.active，可选再装一个故障钩子。
        """
        import tempfile

        del changed  # 子进程自己按同一个确定性函数重建，不跨进程传对象
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        site = [
            "from pns.runtime.reload import BOUNDARY",
            "_original = BOUNDARY.active",
            "_cache = []",
            "def _active():",
            "    if not _cache:",
            "        from tests.test_content_transition import _changed",
            "        _cache.append(_changed(_original()))",
            "    return _cache[0]",
            "BOUNDARY.active = _active",
        ]
        if hook:
            site.append(hook)
        with open(os.path.join(tmp.name, "sitecustomize.py"), "w") as handle:
            handle.write("\n".join(site) + "\n")
        env = dict(os.environ)
        env[WORLD_ROOT_ENV] = str(self.root)
        env[self.registry.models.key_name] = CANARY
        env["PYTHONPATH"] = os.pathsep.join(
            [tmp.name, str(REPO_ROOT), str(REPO_ROOT / "scripts")]
        )
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "content_decisions.py"), "yoake-mae", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def test_the_script_records_decisions_and_says_the_gate_is_complete(self):
        changed, ids = self.make_pending()
        result = self.run_with(changed, "--adopt", *ids)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("作息门齐了", result.stdout)
        again = self.run_with(changed, "--json", "--adopt", *ids)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        import json

        body = json.loads(again.stdout)
        self.assertEqual({o["result"] for o in body["outcomes"]}, {"already"})

    def test_the_script_reports_an_ownership_failure_with_the_disk_state(self):
        changed, ids = self.make_pending()
        hook = "\n".join(
            [
                "from pns.runtime.persistence.lifecycle import PersistentWorld",
                "from pns.runtime.persistence import OwnershipError",
                "_real = PersistentWorld.checkpoint",
                "def _cp(world, reason='manual'):",
                "    if reason == 'content_decision':",
                "        raise OwnershipError('injected')",
                "    return _real(world, reason)",
                "PersistentWorld.checkpoint = _cp",
            ]
        )
        result = self.run_with(changed, "--json", "--adopt", *ids, hook=hook)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        import json

        body = json.loads(result.stdout)
        self.assertFalse(body["complete"])
        self.assertTrue(any("OwnershipError" in e for e in body["errors"]))
        self.assertEqual(
            [o["result"] for o in body["outcomes"]], ["failed", "not_attempted", "not_attempted"]
        )
        self.assertTrue(body["close"]["clean"])
        statuses = {d["conflict_id"]: d["status"] for d in body["disk"]}
        self.assertEqual(statuses[ids[0]], "adopted")
        self.assertEqual(statuses[ids[1]], "pending")

    def test_the_script_refuses_while_the_world_is_open(self):
        self.plane.restore("yoake-mae")
        result = self.run_script("--adopt", "rhythm:mizuki#0")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("先在 dashboard 关闭世界", result.stderr)


# ── 静态边界（ena R2-F1 / R3 V1） ───────────────────────────────────────


def _direct_imports(path: Path):
    """一个文件直接 import 了什么：(\"module\", 全名) 是整个模块，(\"name\", 全名)
    是 from-import 进来的名字（相对导入换算成绝对名，别名不影响）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = path.relative_to(REPO_ROOT).with_suffix("").parts[:-1]
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(("module", alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            parts = list(package[: len(package) - node.level + 1]) if node.level else []
            if node.module:
                parts.append(node.module)
            base = ".".join(parts)
            found.add(("module", base))
            found.update(("name", f"{base}.{alias.name}") for alias in node.names)
    return found


class CommitBoundaryTests(unittest.TestCase):
    """held 的保证只覆盖经协调器准入的路径。直接提交会话事件的原语不在保证里，
    所以它的调用方集合要被盯住：新增一个调用方，这里就要失败，逼人重新审查。"""

    CALLERS = {
        "pns/runtime/autonomy/coordinator.py",
        "pns/runtime/agency/engine.py",
        "pns/runtime/memory/encoder.py",
        "pns/runtime/scheduler.py",
    }

    def test_only_the_known_modules_import_the_session_event_commit(self):
        importers = set()
        for root in ("pns", "scripts"):
            for path in sorted((REPO_ROOT / root).rglob("*.py")):
                rel = path.relative_to(REPO_ROOT).as_posix()
                if rel == "pns/runtime/event_commit.py":
                    continue
                names = _direct_imports(path)
                # 直接拿到这个函数，或者把整个模块拿到手（之后能调它的任何东西）。
                if names & {
                    ("name", "pns.runtime.event_commit.commit_session_event"),
                    ("name", "pns.runtime.event_commit"),
                } or (
                    ("module", "pns.runtime.event_commit") in names
                    and not any(
                        kind == "name" and n.startswith("pns.runtime.event_commit.")
                        for kind, n in names
                    )
                ):
                    importers.add(rel)
        self.assertEqual(importers, self.CALLERS)

    def test_the_maintenance_path_imports_no_direct_writer(self):
        banned = (
            "pns.runtime.event_commit",
            "pns.runtime.scheduler",
            "pns.runtime.agency",
            "pns.runtime.memory",
        )
        for rel in ("scripts/content_decisions.py", "pns/interfaces/content_maintenance.py"):
            names = _direct_imports(REPO_ROOT / rel)
            offenders = sorted(n for _, n in names if n.startswith(banned))
            self.assertEqual(offenders, [], rel)


if __name__ == "__main__":
    unittest.main()
