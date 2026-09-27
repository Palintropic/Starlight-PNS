# tests/test_archive_segments.py — 事件分卷存档（WORLD-1 存档增长设计 §2，存档版本 3）。
#
# 盯住的东西：
#   1. 一条事件都不丢：封存前后、恢复前后，完整事件历史逐条相同。
#   2. checkpoint 只写活动段：已封存的分卷不被重写，快照不再序列化它们。
#   3. 封存与 world.json 之间崩溃：恢复用上一版，结果与没封存过一样；多出来的
#      分卷只是残留，下次封存同序号时被覆盖。
#   4. 分卷被改、缺失、截断、序号断开、清单外文件、清单名字伪造、软链：
#      加载响亮失败或不参与加载，绝不跳过、绝不猜。
#   5. 版本 2 存档原样可读，下一次保存写成版本 3。
#
# 运行: python -m unittest discover -s tests -p test_archive_segments.py -v
import json
import os
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from pns.models.event import EventType
from pns.runtime.persistence import archive as archive_mod
from pns.runtime.persistence.archive import (
    WORLD_ARCHIVE_VERSION,
    ArchiveCorrupt,
    ArchiveError,
    WorldArchive,
)
from pns.runtime.persistence.lifecycle import CheckpointError
from pns.runtime.persistence.store import FileWorldStore, StorageError
from test_world_lifecycle import WorldTestCase, _adapters


def _ids(state):
    return [event.event_id for event in state.events.events()]


class SegmentTestCase(WorldTestCase):
    def history(self, world_id="nightcord") -> Path:
        return self.root / world_id / FileWorldStore.HISTORY_DIR

    def segment(self, index=1, world_id="nightcord") -> Path:
        return self.history(world_id) / archive_mod.segment_file_name(index)

    def advance_hours(self, world, hours):
        for _ in range(hours):
            world.state.scheduler.advance_by(60)

    def sealed_world(self, hours=30):
        """一个已经封存过至少一卷的世界（开局 23:50，推 hours 小时，每小时一条时间事件）。"""
        world = self.created()
        self.advance_hours(world, hours)
        world.checkpoint()
        self.assertTrue(self.segment(1).is_file(), "推过一整天之后应当封存出第一卷")
        return world

    def reopen(self, world):
        world.close()
        return self.service.restore("nightcord", adapters=_adapters())

    def rewrite_archive(self, mutate, world_id="nightcord"):
        payload = self.archive_json(world_id)
        mutate(payload)
        self.archive_path(world_id).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


# ── 1. 不丢、不重写 ─────────────────────────────────────────────────────
class SealingTests(SegmentTestCase):
    def test_a_full_day_is_sealed_and_restores_event_for_event(self):
        world = self.sealed_world()
        before = _ids(world.state)
        payload = self.archive_json()
        self.assertEqual(payload["version"], WORLD_ARCHIVE_VERSION)
        manifest = payload["segments"]
        self.assertEqual(len(manifest), 1)
        active = payload["state"]["events"]["events"]
        # 活动段紧接着分卷，两者合起来就是全部。
        self.assertEqual(active[0]["sequence"], manifest[0]["last_sequence"] + 1)
        self.assertEqual(manifest[0]["count"] + len(active), len(before))
        restored = self.reopen(world)
        self.assertEqual(_ids(restored.state), before)

    def test_a_segment_is_one_simulated_day_at_most(self):
        world = self.sealed_world(hours=80)
        manifest = self.archive_json()["segments"]
        self.assertGreaterEqual(len(manifest), 3)
        for item in manifest:
            span = archive_mod.datetime.fromisoformat(
                item["last_at"]
            ) - archive_mod.datetime.fromisoformat(item["first_at"])
            self.assertLess(span, archive_mod.SEGMENT_SPAN)
        self.assertEqual(_ids(self.reopen(world).state)[-1], _ids(world.state)[-1])

    def test_the_event_cap_splits_a_busy_day(self):
        world = self.created()
        self.advance_hours(world, 12)
        with patch.object(archive_mod, "SEGMENT_MAX_EVENTS", 5):
            world.checkpoint()
        manifest = self.archive_json()["segments"]
        self.assertGreaterEqual(len(manifest), 2)
        self.assertTrue(all(item["count"] <= 5 for item in manifest))
        before = _ids(world.state)
        self.assertEqual(_ids(self.reopen(world).state), before)

    def test_a_growing_tail_is_not_sealed(self):
        world = self.created()
        self.advance_hours(world, 20)
        world.checkpoint()
        self.assertFalse(self.history().exists())
        self.assertEqual(self.archive_json()["segments"], [])

    def test_a_sealed_segment_is_never_rewritten(self):
        world = self.sealed_world()
        first = self.segment(1).stat()
        replaced = []
        real = os.replace

        def spy(src, dst):
            replaced.append(Path(dst).name)
            return real(src, dst)

        self.advance_hours(world, 30)
        with patch.object(os, "replace", side_effect=spy):
            world.checkpoint()
        self.assertNotIn(archive_mod.segment_file_name(1), replaced)
        self.assertIn(archive_mod.segment_file_name(2), replaced)
        again = self.segment(1).stat()
        self.assertEqual((first.st_ino, first.st_mtime_ns), (again.st_ino, again.st_mtime_ns))

    def test_a_checkpoint_only_serializes_the_active_tail(self):
        world = self.sealed_world()
        sealed = self.archive_json()["segments"][-1]["last_sequence"] + 1
        calls = []
        real = type(world.state.events).to_dict

        def spy(store, start=0):
            calls.append(start)
            return real(store, start)

        world.state.scheduler.advance_by(1)
        with patch.object(type(world.state.events), "to_dict", spy):
            world.checkpoint()
        self.assertEqual(calls, [sealed])

    def test_status_reports_the_archive_footprint(self):
        world = self.sealed_world()
        report = world.status()["archive"]
        manifest = self.archive_json()["segments"]
        self.assertEqual(report["segments"], len(manifest))
        self.assertEqual(report["sealed_bytes"], sum(i["bytes"] for i in manifest))
        self.assertEqual(report["world_bytes"], self.archive_path().stat().st_size)
        self.assertEqual(report["total_bytes"], report["world_bytes"] + report["sealed_bytes"])
        self.assertEqual(
            report["sealed_events"] + report["active_events"], len(world.state.events)
        )
        world.close()
        disk = self.service.status("nightcord")
        self.assertIsNone(disk["error"])
        self.assertEqual(disk["archive"]["segments"], len(manifest))
        self.assertEqual(disk["archive"]["total_bytes"], report["total_bytes"])


# ── 2. 封存中途崩溃 ─────────────────────────────────────────────────────
class InterruptedSealTests(SegmentTestCase):
    def test_a_crash_between_seal_and_save_restores_the_previous_version(self):
        world = self.created()
        self.advance_hours(world, 30)
        with patch.object(FileWorldStore, "save", side_effect=StorageError("掉电")):
            with self.assertRaises(CheckpointError):
                world.checkpoint()
        # 分卷已经写下去了，但 world.json 仍是第 1 版、不认识它。
        self.assertTrue(self.segment(1).is_file())
        self.assertEqual(self.archive_json()["revision"], 1)
        self.assertEqual(self.archive_json()["segments"], [])
        self.assertIn(str(self.segment(1)), world.status()["residue"])
        # 模拟进程死亡：不存，直接放手，然后恢复。
        self.service.release_all()
        restored = self.service.restore("nightcord", adapters=_adapters())
        self.assertEqual(restored.revision, 1)
        self.assertEqual(restored.status()["archive"]["segments"], 0)

    def test_the_next_seal_overwrites_the_orphan_and_takes_its_number(self):
        world = self.created()
        self.advance_hours(world, 30)
        with patch.object(FileWorldStore, "save", side_effect=StorageError("掉电")):
            with self.assertRaises(CheckpointError):
                world.checkpoint()
        self.segment(1).write_bytes(b"garbage from a dead process\n")
        self.advance_hours(world, 1)
        world.checkpoint()
        manifest = self.archive_json()["segments"]
        self.assertEqual(manifest[0]["file"], archive_mod.segment_file_name(1))
        self.assertEqual(world.status()["residue"], [])
        before = _ids(world.state)
        self.assertEqual(_ids(self.reopen(world).state), before)

    def test_a_failed_segment_write_leaves_world_json_untouched(self):
        world = self.created()
        self.advance_hours(world, 30)
        before = self.archive_path().read_bytes()
        real = os.replace

        def fail_segment(src, dst):
            if Path(dst).name.startswith("events-"):
                raise OSError(28, "No space left on device")
            return real(src, dst)

        with patch.object(os, "replace", side_effect=fail_segment):
            with self.assertRaises(CheckpointError):
                world.checkpoint()
        self.assertEqual(self.archive_path().read_bytes(), before)
        self.assertEqual(world.revision, 1)
        self.assertEqual(list(self.history().iterdir()), [])

    def test_a_history_directory_sync_that_really_fails_fails_the_checkpoint(self):
        world = self.created()
        self.advance_hours(world, 30)
        real_open, real_fsync = os.open, os.fsync
        history = str(self.history())
        history_fds = set()

        def opening(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if str(path) == history:
                history_fds.add(fd)
            return fd

        def syncing(fd):
            if fd in history_fds:
                raise OSError(5, "Input/output error")
            return real_fsync(fd)

        with patch.object(os, "open", side_effect=opening), patch.object(
            os, "fsync", side_effect=syncing
        ):
            with self.assertRaises(CheckpointError):
                world.checkpoint()
        # 分卷目录没能证实耐久，world.json 就不许指着它。
        self.assertEqual(self.archive_json()["revision"], 1)
        self.assertEqual(self.archive_json()["segments"], [])


    def test_an_error_after_the_replace_is_booked_as_written_not_durable(self):
        # 第三方反向测试 A-2：replace 之后关闭目录句柄报 EIO。
        world = self.sealed_world()  # history/ 已经在了，下一次只有 save 会打开世界目录
        self.advance_hours(world, 30)
        real_open, real_close = os.open, os.close
        world_dir = str(self.root / "nightcord")
        fds = set()

        def opening(path, flags, *a, **k):
            fd = real_open(path, flags, *a, **k)
            if str(path) == world_dir:
                fds.add(fd)
            return fd

        def closing(fd):
            real_close(fd)
            if fd in fds:
                fds.discard(fd)
                raise OSError(5, "Input/output error")

        with patch.object(os, "open", side_effect=opening), patch.object(
            os, "close", side_effect=closing
        ):
            with self.assertRaises(CheckpointError):
                world.checkpoint()
        status = world.status()
        self.assertEqual(status["revision"], self.archive_json()["revision"])
        self.assertFalse(status["durable"])
        self.assertEqual(status["archive"]["segments"], len(self.archive_json()["segments"]))
        listed = [self.segment(i).stat().st_ino for i in (1, 2)]
        world.checkpoint()
        self.assertEqual(
            [self.segment(i).stat().st_ino for i in (1, 2)], listed, "清单里的分卷不许被重写"
        )

    def test_an_interrupt_after_the_replace_is_reconciled_with_the_disk(self):
        world = self.created()
        self.advance_hours(world, 30)
        real = FileWorldStore._sync_dir

        def interrupted(directory, archive):
            raise KeyboardInterrupt("SIGINT during dir fsync")

        with patch.object(FileWorldStore, "_sync_dir", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                world.checkpoint()
        self.assertEqual(world.revision, self.archive_json()["revision"])
        self.assertFalse(world.status()["durable"])
        del real

    def test_an_unsupported_history_sync_is_not_reported_as_synced(self):
        world = self.created()
        self.advance_hours(world, 30)
        real_open, real_fsync = os.open, os.fsync
        history = str(self.history())
        fds = set()

        def opening(path, flags, *a, **k):
            fd = real_open(path, flags, *a, **k)
            if str(path) == history:
                fds.add(fd)
            return fd

        real_close = os.close

        def syncing(fd):
            if fd in fds:
                raise OSError(22, "Invalid argument")  # EINVAL：平台不支持
            return real_fsync(fd)

        def closing(fd):
            fds.discard(fd)
            return real_close(fd)

        with patch.object(os, "open", side_effect=opening), patch.object(
            os, "fsync", side_effect=syncing
        ), patch.object(os, "close", side_effect=closing):
            world.checkpoint()
        self.assertFalse(world.status()["directory_synced"])

    def test_a_non_positive_span_is_refused(self):
        world = self.created()
        _ = world
        archive = WorldArchive.from_state_payload(
            "nightcord", world.state.to_dict(), revision=2
        )
        for span in (archive_mod.timedelta(0), archive_mod.timedelta(minutes=-1)):
            with self.assertRaises(ArchiveError):
                archive.seal_plan(span=span)


# ── 3. 损坏的分卷 ───────────────────────────────────────────────────────
class DamagedHistoryTests(SegmentTestCase):
    def closed_sealed_world(self):
        world = self.sealed_world()
        world.close()

    def assertRestoreFails(self, error=ArchiveError, text=None):
        with self.assertRaises(error) as caught:
            self.service.restore("nightcord", adapters=_adapters())
        if text is not None:
            self.assertIn(text, str(caught.exception))
        # 失败的恢复把所有权还回去。
        self.assertIsNone(self.service.opened("nightcord"))

    def test_a_modified_segment_is_refused_and_named(self):
        self.closed_sealed_world()
        lines = self.segment(1).read_bytes().split(b"\n")
        entry = json.loads(lines[5])
        # 改中间一条的 event_id 最后一个字符：长度、序号、首尾时刻都不变，
        # 只能靠哈希发现。
        entry["event_id"] = entry["event_id"][:-1] + (
            "x" if entry["event_id"][-1] != "x" else "y"
        )
        lines[5] = json.dumps(entry, ensure_ascii=False, sort_keys=True).encode("utf-8")
        blob = b"\n".join(lines)
        self.assertEqual(len(blob), self.segment(1).stat().st_size)
        self.segment(1).write_bytes(blob)
        self.assertRestoreFails(ArchiveCorrupt, "events-000001.jsonl")

    def test_a_truncated_segment_is_refused_and_status_says_so(self):
        self.closed_sealed_world()
        blob = self.segment(1).read_bytes()
        self.segment(1).write_bytes(blob[:-10])
        self.assertRestoreFails(ArchiveCorrupt, "events-000001.jsonl")
        self.assertIn("events-000001.jsonl", self.service.status("nightcord")["error"])

    def test_a_missing_segment_is_refused_not_skipped(self):
        self.closed_sealed_world()
        self.segment(1).unlink()
        self.assertRestoreFails(ArchiveCorrupt, "不见了")
        self.assertIn("不见了", self.service.status("nightcord")["error"])

    def test_a_segment_whose_manifest_was_rewritten_to_match_is_still_checked_line_by_line(self):
        # 攻击：改分卷内容，同时把清单的 sha256 / bytes 也改得对得上。
        # 哈希挡不住它了，但序号与首尾时刻仍然要核对。
        self.closed_sealed_world()
        lines = self.segment(1).read_bytes().split(b"\n")
        del lines[1]  # 抽掉第二条事件
        blob = b"\n".join(lines)
        self.segment(1).write_bytes(blob)
        import hashlib

        def forge(payload):
            item = payload["segments"][0]
            item["bytes"] = len(blob)
            item["sha256"] = hashlib.sha256(blob).hexdigest()

        self.rewrite_archive(forge)
        self.assertRestoreFails(ArchiveCorrupt, "序号")

    def test_a_sequence_gap_between_segment_and_active_tail_is_refused(self):
        self.closed_sealed_world()

        def gap(payload):
            payload["state"]["events"]["events"].pop(0)

        self.rewrite_archive(gap)
        self.assertRestoreFails(ArchiveError, "活动段应当从序号")

    def test_a_manifest_that_does_not_start_at_zero_is_refused(self):
        self.closed_sealed_world()

        def shift(payload):
            item = payload["segments"][0]
            item["first_sequence"] += 1
            item["count"] -= 1

        self.rewrite_archive(shift)
        self.assertRestoreFails(ArchiveError, "序号断开")

    def test_a_forged_segment_name_is_refused_before_any_path_is_built(self):
        self.closed_sealed_world()
        for name in ("../world.json", "events-1.jsonl", "/etc/passwd", "events-000002.jsonl"):
            with self.subTest(name=name):

                def forge(payload, name=name):
                    payload["segments"][0]["file"] = name

                self.rewrite_archive(forge)
                self.assertRestoreFails(ArchiveError, "文件名应当是")
                self.rewrite_archive(
                    lambda p: p["segments"][0].__setitem__("file", "events-000001.jsonl")
                )

    def test_a_file_outside_the_manifest_is_ignored_and_reported(self):
        self.closed_sealed_world()
        stray = self.history() / archive_mod.segment_file_name(9)
        stray.write_bytes(b'{"not": "an event"}\n')
        world = self.service.restore("nightcord", adapters=_adapters())
        self.assertIn(str(stray), world.status()["residue"])
        world.close()
        self.assertIn(str(stray), self.service.status("nightcord")["residue"])

    def test_a_symlinked_segment_is_refused(self):
        self.closed_sealed_world()
        real = self.root / "elsewhere.jsonl"
        real.write_bytes(self.segment(1).read_bytes())
        self.segment(1).unlink()
        self.segment(1).symlink_to(real)
        self.assertRestoreFails(StorageError, "软链")

    def test_a_symlinked_history_directory_is_refused(self):
        self.closed_sealed_world()
        moved = self.root / "moved-history"
        self.history().rename(moved)
        self.history().symlink_to(moved, target_is_directory=True)
        self.assertRestoreFails(StorageError, "软链")

    def test_an_archive_with_segments_but_no_history_loaded_cannot_be_restored(self):
        self.closed_sealed_world()
        archive = self.store.load("nightcord", history=False)
        with self.assertRaises(ArchiveError):
            archive.restore_state()


# ── 4. 版本 ─────────────────────────────────────────────────────────────
class VersionTests(SegmentTestCase):
    def test_a_version_2_archive_is_readable_and_is_next_written_as_version_3(self):
        world = self.created()
        self.advance_hours(world, 30)
        before = _ids(world.state)
        # 关闭时不封存，于是全部事件都在 world.json 里 —— 这时版本 3 与版本 2
        # 只差 version 与 segments 两个字段，手工还原成版本 2。
        with patch.object(archive_mod, "SEGMENT_SPAN", archive_mod.timedelta(days=365)):
            world.close()
        self.assertEqual(self.archive_json()["segments"], [])

        def downgrade(payload):
            payload["version"] = 2
            del payload["segments"]

        self.rewrite_archive(downgrade)
        restored = self.service.restore("nightcord", adapters=_adapters())
        self.assertEqual(_ids(restored.state), before)
        restored.checkpoint()
        payload = self.archive_json()
        self.assertEqual(payload["version"], 3)
        self.assertEqual(len(payload["segments"]), 1)
        self.assertEqual(_ids(self.reopen(restored).state), before)

    def test_a_version_2_archive_with_a_manifest_is_refused(self):
        self.created().close()
        self.rewrite_archive(lambda p: p.__setitem__("version", 2))
        with self.assertRaises(ArchiveError):
            self.service.restore("nightcord", adapters=_adapters())

    def test_a_version_3_archive_without_a_manifest_is_refused(self):
        self.created().close()
        self.rewrite_archive(lambda p: p.pop("segments"))
        with self.assertRaises(ArchiveError):
            self.service.restore("nightcord", adapters=_adapters())

    def test_an_envelope_round_trips_its_manifest(self):
        world = self.sealed_world()
        world.close()
        loaded = self.store.load("nightcord")
        again = WorldArchive.from_dict(json.loads(json.dumps(loaded.to_dict())))
        self.assertEqual(again, loaded)
        self.assertEqual(again.segments, loaded.segments)

    def test_the_time_events_are_all_there(self):
        # 阶段一的承诺是全量：每一次推进都有一条 time_advanced。
        world = self.sealed_world(hours=50)
        restored = self.reopen(world)
        advanced = restored.state.events.by_type(EventType.WORLD_TIME_ADVANCED)
        self.assertEqual(len(advanced), 50)


if __name__ == "__main__":
    unittest.main()
