# tests/test_world_lifecycle.py — P12 持久世界生命周期的不变量。
#
# 盯住的东西按"错了会怎样"排：
#   1. 存档是**原子**的：任何一刻的磁盘要么是上一份完整存档，要么是新的
#      那一份完整存档。写到一半的进程死亡不产生第三种。
#   2. 一个世界同一时刻只有一个拥有者：同进程两个句柄不行，两个进程也不行。
#      而且抢锁绝不夺走一个**活着**的拥有者。
#   3. checkpoint 只在 P11 那条线性化边界上取快照：绝不会存下一份"事件写了
#      一半、观察还没落地"的世界，也绝不会存下一份回滚掉的事务。
#   4. 恢复走既有构造函数和跨段校验：损坏、版本不对、身份不对、时钟对不上、
#      缺段，一律响亮失败，没有"安静地恢复成一个空世界"这条路。
#   5. 关闭顺序固定：停准入 → 等事务落定 → 最后一次 checkpoint → 标记关闭
#      → 释放所有权。checkpoint 失败不许假装干净关闭、也不许释放所有权。
#   6. 路径只在配置的存档根之下：绝对路径、穿越、软链逃逸、会归一成同一个
#      目录的 ID，一律拒。
#   7. 崩溃恢复的边界是**最后一次成功的 checkpoint**，不多不少。之后的内存
#      工作会丢，这件事必须如实说出来，不许暗示 WAL 级别的保证。
#   8. /ws/run 研究会话一点没变：不拿世界锁、不写存档根、不 import 这一层。
#
# 运行: python -m unittest tests.test_world_lifecycle -v
import ast
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from pns.models.activation import ActivationKind, ScheduledActivation
from pns.models.activation_outbox import ActivationOutbox
from pns.models.event import EventType
from pns.models.session import SessionState
from pns.models.world_state import WorldState
from pns.runtime.autonomy.audit import ScriptedAuditor
from pns.runtime.autonomy.coordinator import AutonomousRuntime, AutonomyError
from pns.runtime.autonomy.generation import AuthoredLinePolicy, ScriptedLineGenerator
from pns.runtime.memory.encoder import MemoryEncoder
from pns.runtime.memory.recall import MemoryRecall
from pns.runtime.persistence import archive as archive_mod
from pns.runtime.persistence import ownership as ownership_mod
from pns.runtime.persistence import store as store_mod
from pns.runtime.persistence.archive import (
    WORLD_ARCHIVE_VERSION,
    ArchiveCorrupt,
    ArchiveError,
    WorldArchive,
)
from pns.runtime.persistence.lifecycle import (
    CheckpointError,
    CheckpointPolicy,
    LifecycleError,
    RuntimeAdapters,
    WorldLifecycleService,
)
from pns.runtime.persistence.ownership import (
    OwnershipError,
    WorldAlreadyOwned,
    acquire_world,
    owned_world_paths,
)
from pns.runtime.persistence.store import (
    ArchiveNotFound,
    FileWorldStore,
    StorageError,
    WorldIdError,
    WorldStore,
)
from pns.runtime.scheduler import PersistentScheduler
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph

CLOCK = datetime(2026, 8, 22, 23, 50)
REPO_ROOT = Path(__file__).resolve().parent.parent
PERSISTENCE_DIR = Path(archive_mod.__file__).resolve().parent


# ── 夹具 ────────────────────────────────────────────────────────────────
def _world(clock=CLOCK):
    world = WorldState(
        clock=clock,
        locations=build_default_location_graph(),
        channels=build_default_channel_registry(),
    )
    world.place_character("mizuki", "mizuki_home_room")
    world.place_character("ena", "ena_home_studio")
    world.join_channel("mizuki", "nightcord")
    world.join_channel("ena", "nightcord")
    return world


def _cold_state(session_id="s1", clock=CLOCK):
    """一份还没绑过任何运行时服务的权威状态（create() 要的就是这个）。"""
    state = SessionState(
        session_id=session_id, scene="gate", characters=["mizuki", "ena"]
    )
    state.attach_world_state(_world(clock))
    state.initialize_runtime("开场")
    return state


def _adapters(lines=None, **kwargs):
    """调用方提供的冷适配器。里面没有任何一个东西是从存档里恢复出来的。"""
    return RuntimeAdapters(
        auditor=ScriptedAuditor(),
        policy_factory=lambda state: AuthoredLinePolicy(
            ScriptedLineGenerator(
                lines if lines is not None else {"mizuki": "在的哦", "ena": "……嗯"}
            ),
            recall=MemoryRecall(state),
        ),
        **kwargs,
    )


def _due(scheduler, activation_id="wake", *, character_id="mizuki", minutes=10):
    scheduler.schedule(
        ScheduledActivation(
            activation_id=activation_id,
            kind=ActivationKind.CHARACTER_ACTIVATION,
            due_at=scheduler.clock + timedelta(minutes=minutes),
            character_id=character_id,
        )
    )
    return scheduler.advance_by(minutes).due[0]


class WorldTestCase(unittest.TestCase):
    """每个用例一个干净的存档根，而且退出时保证不留下被本进程持有的锁。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "worlds"
        self.store = FileWorldStore(self.root)
        self.service = WorldLifecycleService(self.store)
        self.addCleanup(self.service.release_all)

    def created(self, world_id="nightcord", *, session_id="s1", adapters=None, **kw):
        return self.service.create(
            world_id,
            _cold_state(session_id=session_id),
            adapters=adapters if adapters is not None else _adapters(),
            **kw,
        )

    def hold_in_another_process(self, world_id="nightcord", release=False):
        """起一个持有该世界的子进程，并保证用例结束时它被收干净。"""
        child = _child_hold(self.root, world_id=world_id, release=release)

        def cleanup():
            if child.poll() is None:
                child.kill()
            child.communicate()

        self.addCleanup(cleanup)
        return child

    def archive_path(self, world_id="nightcord") -> Path:
        return self.root / world_id / FileWorldStore.ARCHIVE_NAME

    def archive_json(self, world_id="nightcord") -> dict:
        return json.loads(self.archive_path(world_id).read_text(encoding="utf-8"))

    @staticmethod
    def _sent(payload: dict) -> list:
        """存档里的台词事件。推时钟本身也是一条事件，按类型挑出来数。"""
        return [
            event
            for event in payload["state"]["events"]["events"]
            if event["type"] == EventType.MESSAGE_SENT.value
        ]


# ── AC1 存档信封 ────────────────────────────────────────────────────────
class ArchiveEnvelopeTests(WorldTestCase):
    """信封必须带着身份、版本、修订号和那一刻的世界时钟，而且能原样回来。"""

    def test_a_captured_envelope_round_trips_through_json(self):
        state = _cold_state()
        captured = WorldArchive.capture("nightcord", state, revision=1)
        payload = json.loads(json.dumps(captured.to_dict(), ensure_ascii=False))
        restored = WorldArchive.from_dict(payload)
        self.assertEqual(restored.world_id, "nightcord")
        self.assertEqual(restored.session_id, "s1")
        self.assertEqual(restored.revision, 1)
        self.assertEqual(restored.clock, CLOCK)
        self.assertEqual(restored.version, WORLD_ARCHIVE_VERSION)
        self.assertEqual(restored.state, captured.state)

    def test_restoring_goes_through_the_real_constructor(self):
        world = self.created()
        _due(world.runtime.scheduler)
        world.runtime.process_pending()
        world.checkpoint()
        restored = self.store.load("nightcord").restore_state()
        self.assertIsInstance(restored, SessionState)
        self.assertEqual(
            restored.events.to_dict(), world.state.events.to_dict()
        )
        self.assertEqual(
            restored.memories.to_dict(), world.state.memories.to_dict()
        )
        self.assertEqual(
            restored.agency.to_dict(), world.state.agency.to_dict()
        )
        # 恢复出来的是一份**没有**服务的冷状态：服务绑定是另一步。
        self.assertIsNone(restored.scheduler)
        self.assertIsNone(restored.agency_engine)
        self.assertIsNone(restored.memory_encoder)
        self.assertIsNone(restored.autonomy)

    def test_an_archive_carries_no_live_service_object(self):
        world = self.created()
        blob = json.dumps(self.store.load("nightcord").to_dict(), ensure_ascii=False)
        for forbidden in ("scheduler_instance", "api_key", "<object", "<function"):
            self.assertNotIn(forbidden, blob)
        payload = self.archive_json()["state"]
        for service_field in ("scheduler_object", "agency_engine", "memory_encoder", "autonomy"):
            self.assertNotIn(service_field, payload)

    def test_capturing_a_state_that_smuggled_a_live_object_is_refused(self):
        # metadata 是自由字典 —— 有人往里塞一个模型客户端，存档就带着一个
        # 活对象。这必须在**捕获**那一刻响亮失败，而不是等到 json.dumps。
        state = _cold_state()
        state.metadata["client"] = object()
        with self.assertRaises(ArchiveError):
            WorldArchive.capture("nightcord", state, revision=1)

    def test_capturing_a_state_that_smuggled_a_callable_is_refused(self):
        state = _cold_state()
        state.metadata["policy"] = lambda: None
        with self.assertRaises(ArchiveError):
            WorldArchive.capture("nightcord", state, revision=1)

    def test_capturing_a_value_that_is_not_legal_json_is_refused(self):
        # NaN / Infinity 是合法的 Python float、**不是**合法的 JSON。
        # json.dumps 默认还会把它们写出去，于是存档在别的解析器眼里是坏的。
        for smuggled in (float("nan"), float("inf"), -float("inf")):
            state = _cold_state()
            state.metadata["score"] = smuggled
            with self.assertRaises(ArchiveError):
                WorldArchive.capture("nightcord", state, revision=1)

    def test_capturing_a_non_string_key_is_refused(self):
        state = _cold_state()
        state.metadata[7] = "七"
        with self.assertRaises(ArchiveError):
            WorldArchive.capture("nightcord", state, revision=1)

    def test_an_envelope_dict_is_a_fresh_structure(self):
        state = _cold_state()
        captured = WorldArchive.capture("nightcord", state, revision=1)
        payload = captured.to_dict()
        payload["state"]["metadata"]["injected"] = True
        payload["world_id"] = "other"
        self.assertNotIn("injected", captured.to_dict()["state"]["metadata"])
        self.assertEqual(captured.world_id, "nightcord")
        # 也不许是活状态的引用。
        self.assertNotIn("injected", state.metadata)

    def test_revisions_must_be_positive_integers(self):
        state = _cold_state()
        for bad in (0, -1, 1.5, True, "1", None):
            with self.assertRaises(ArchiveError):
                WorldArchive.capture("nightcord", state, revision=bad)

    def test_an_unsupported_version_is_named_not_guessed(self):
        state = _cold_state()
        payload = WorldArchive.capture("nightcord", state, revision=1).to_dict()
        payload["version"] = WORLD_ARCHIVE_VERSION + 99
        with self.assertRaises(ArchiveError) as caught:
            WorldArchive.from_dict(payload)
        self.assertIn(str(WORLD_ARCHIVE_VERSION + 99), str(caught.exception))

    def test_an_envelope_whose_session_disagrees_with_its_state_is_refused(self):
        state = _cold_state()
        payload = WorldArchive.capture("nightcord", state, revision=1).to_dict()
        payload["session_id"] = "somebody-else"
        with self.assertRaises(ArchiveError):
            WorldArchive.from_dict(payload)

    def test_an_envelope_whose_clock_disagrees_with_its_world_is_refused(self):
        # 信封里的时钟和世界状态里的时钟必须是同一刻。不然"这份存档是哪一
        # 刻的"有两个都自称权威的答案。
        state = _cold_state()
        payload = WorldArchive.capture("nightcord", state, revision=1).to_dict()
        payload["clock"] = (CLOCK + timedelta(minutes=1)).isoformat()
        with self.assertRaises(ArchiveError):
            WorldArchive.from_dict(payload)

    def test_missing_envelope_fields_are_refused_one_by_one(self):
        state = _cold_state()
        complete = WorldArchive.capture("nightcord", state, revision=1).to_dict()
        for field in ("world_id", "session_id", "revision", "clock", "state", "version"):
            payload = dict(complete)
            payload.pop(field)
            with self.assertRaises(ArchiveError, msg=field):
                WorldArchive.from_dict(payload)

    def test_a_state_missing_a_section_is_refused_at_restore(self):
        # 缺段的存档不许安静地恢复成"什么都没记住"。
        state = _cold_state()
        payload = WorldArchive.capture("nightcord", state, revision=1).to_dict()
        for section in ("memory", "scheduler", "agency"):
            broken = json.loads(json.dumps(payload))
            broken["state"].pop(section)
            with self.assertRaises(ArchiveError, msg=section):
                WorldArchive.from_dict(broken).restore_state()


# ── AC2 原子保存与残留 ──────────────────────────────────────────────────
class AtomicSaveTests(WorldTestCase):
    """磁盘上只有两种画面：上一份完整存档，或者新的那份完整存档。"""

    def _archive(self, revision, *, world_id="nightcord", clock=CLOCK):
        return WorldArchive.capture(
            world_id, _cold_state(clock=clock), revision=revision
        )

    def test_a_save_writes_through_a_temp_file_and_replaces(self):
        seen = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append((str(src), str(dst)))
            return real_replace(src, dst)

        with patch.object(store_mod.os, "replace", spy):
            result = self.store.save(self._archive(1))
        self.assertEqual(len(seen), 1)
        src, dst = seen[0]
        self.assertTrue(src.endswith(FileWorldStore.TMP_SUFFIX), src)
        self.assertEqual(Path(src).parent, Path(dst).parent)
        self.assertEqual(Path(dst), self.archive_path())
        self.assertEqual(result.revision, 1)
        self.assertEqual(self.archive_json()["revision"], 1)

    def test_the_payload_is_fsynced_before_it_is_replaced(self):
        order = []
        real_fsync, real_replace = os.fsync, os.replace

        with patch.object(
            store_mod.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1]
        ), patch.object(
            store_mod.os,
            "replace",
            lambda s, d: (order.append("replace"), real_replace(s, d))[1],
        ):
            self.store.save(self._archive(1))
        self.assertIn("fsync", order)
        self.assertLess(order.index("fsync"), order.index("replace"))

    def test_a_failed_replace_keeps_the_previous_complete_archive(self):
        self.store.save(self._archive(1))
        newer = self._archive(2, clock=CLOCK + timedelta(minutes=30))
        with patch.object(
            store_mod.os, "replace", side_effect=OSError("disk full")
        ):
            with self.assertRaises(StorageError):
                self.store.save(newer)
        self.assertEqual(self.archive_json()["revision"], 1)
        self.assertEqual(self.store.load("nightcord").revision, 1)
        # 临时文件收拾干净了，没有残留需要人来处理。
        self.assertEqual(self.store.residue("nightcord"), ())

    def test_a_failed_write_keeps_the_previous_complete_archive(self):
        self.store.save(self._archive(1))
        with patch.object(store_mod.os, "fsync", side_effect=OSError("io error")):
            with self.assertRaises(StorageError):
                self.store.save(self._archive(2))
        self.assertEqual(self.store.load("nightcord").revision, 1)
        self.assertEqual(self.store.residue("nightcord"), ())

    def test_residue_that_could_not_be_cleaned_is_reported_not_swallowed(self):
        self.store.save(self._archive(1))
        with patch.object(
            store_mod.os, "replace", side_effect=OSError("disk full")
        ), patch.object(store_mod.os, "unlink", side_effect=OSError("read-only")):
            with self.assertRaises(StorageError) as caught:
                self.store.save(self._archive(2))
        self.assertTrue(caught.exception.residue)
        self.assertTrue(self.store.residue("nightcord"))
        # 残留不是存档：读回来的仍然是上一份完整的。
        self.assertEqual(self.store.load("nightcord").revision, 1)

    def test_residue_is_reported_in_status_and_ignored_by_load(self):
        world = self.created()
        world.checkpoint()
        residue = self.root / "nightcord" / ("world.json.leftover" + FileWorldStore.TMP_SUFFIX)
        residue.write_text("{ truncated", encoding="utf-8")
        self.assertEqual(self.store.load("nightcord").revision, 2)
        status = world.status()
        self.assertIn(residue.name, [Path(item).name for item in status["residue"]])

    def test_loading_a_world_that_was_never_saved_is_a_named_failure(self):
        with self.assertRaises(ArchiveNotFound):
            self.store.load("nightcord")
        self.assertFalse(self.store.exists("nightcord"))

    def test_a_truncated_archive_is_corrupt_not_empty(self):
        self.store.save(self._archive(1))
        path = self.archive_path()
        blob = path.read_text(encoding="utf-8")
        path.write_text(blob[: len(blob) // 2], encoding="utf-8")
        with self.assertRaises(ArchiveCorrupt):
            self.store.load("nightcord")

    def test_a_non_object_archive_is_corrupt(self):
        self.store.save(self._archive(1))
        self.archive_path().write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(ArchiveError):
            self.store.load("nightcord")

    def test_listing_worlds_ignores_residue_and_stray_files(self):
        self.store.save(self._archive(1, world_id="nightcord"))
        self.store.save(self._archive(1, world_id="other"))
        (self.root / "not-a-world").mkdir()
        (self.root / "loose.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.store.list_worlds(), ("nightcord", "other"))

    def test_a_store_writes_nothing_until_it_is_asked_to(self):
        root = self.root.parent / "untouched"
        FileWorldStore(root)
        self.assertFalse(root.exists())
        self.assertEqual(FileWorldStore(root).list_worlds(), ())
        self.assertFalse(root.exists())

    def test_the_filesystem_store_is_a_world_store(self):
        self.assertIsInstance(self.store, WorldStore)
        for required in ("list_worlds", "exists", "load", "save", "residue", "acquire"):
            self.assertTrue(callable(getattr(self.store, required)), required)


# ── AC3 路径只在存档根之下 ──────────────────────────────────────────────
class ArchivePathSafetyTests(WorldTestCase):
    """world_id 是不可信文本。它绝不能直接变成一条会被写、被替换的路径。"""

    REJECTED = (
        "",
        ".",
        "..",
        "../evil",
        "../../etc/passwd",
        "/etc/passwd",
        "/absolute",
        "world/../..",
        "a/b",
        "a\\b",
        "world\x00id",
        "world id",
        " leading",
        "trailing ",
        "trailing.",
        "-leading-dash",
        "Nightcord",        # 大小写不敏感的文件系统上会跟 nightcord 撞成同一个目录
        "NIGHTCORD",
        "caf\u00e9",        # 非 ASCII：NFC / NFD 两种写法在磁盘上会归一成同一个目录
        "cafe\u0301",
        "x" * 65,
        "~root",
        "$HOME",
    )

    def test_dangerous_world_ids_are_refused_before_any_path_is_built(self):
        for world_id in self.REJECTED:
            with self.subTest(world_id=world_id):
                with self.assertRaises(WorldIdError):
                    self.store.archive_path(world_id)

    def test_dangerous_world_ids_are_refused_on_every_entry_point(self):
        for world_id in ("../evil", "/etc/passwd", "Nightcord", ".."):
            with self.subTest(world_id=world_id):
                with self.assertRaises(WorldIdError):
                    self.store.load(world_id)
                with self.assertRaises(WorldIdError):
                    self.store.exists(world_id)
                with self.assertRaises(WorldIdError):
                    self.store.residue(world_id)
                with self.assertRaises(WorldIdError):
                    self.store.acquire(world_id)
                with self.assertRaises(WorldIdError):
                    self.service.restore(world_id, adapters=_adapters())
                with self.assertRaises(WorldIdError):
                    self.service.create(
                        world_id, _cold_state(), adapters=_adapters()
                    )

    def test_a_traversing_world_id_writes_nothing_outside_the_root(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        victim = outside / "victim.json"
        victim.write_text("original", encoding="utf-8")
        with self.assertRaises(WorldIdError):
            self.store.save(
                WorldArchive.capture("../outside/victim", _cold_state(), revision=1)
            )
        self.assertEqual(victim.read_text(encoding="utf-8"), "original")
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["victim.json"])

    def test_a_symlinked_world_directory_is_refused(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "escape").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(StorageError):
            self.store.save(WorldArchive.capture("escape", _cold_state(), revision=1))
        self.assertEqual(list(outside.iterdir()), [])
        with self.assertRaises(StorageError):
            self.store.acquire("escape")

    def test_a_symlinked_archive_file_is_refused(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        secret = outside / "secret.json"
        secret.write_text('{"version": 1}', encoding="utf-8")
        (self.root / "nightcord").mkdir(parents=True)
        (self.root / "nightcord" / FileWorldStore.ARCHIVE_NAME).symlink_to(secret)
        with self.assertRaises(StorageError):
            self.store.load("nightcord")
        with self.assertRaises(StorageError):
            self.store.save(
                WorldArchive.capture("nightcord", _cold_state(), revision=1)
            )
        # 软链的目标一个字节都没被动过。
        self.assertEqual(secret.read_text(encoding="utf-8"), '{"version": 1}')

    def test_a_symlinked_archive_root_itself_is_still_confined(self):
        # 根本身是软链是允许的（部署常见），逃逸判定按解析后的真实根来算。
        real_root = self.root.parent / "real-root"
        real_root.mkdir(parents=True)
        linked = self.root.parent / "linked-root"
        linked.symlink_to(real_root, target_is_directory=True)
        store = FileWorldStore(linked)
        store.save(WorldArchive.capture("nightcord", _cold_state(), revision=1))
        self.assertTrue((real_root / "nightcord" / FileWorldStore.ARCHIVE_NAME).exists())
        self.assertEqual(store.load("nightcord").revision, 1)

    def test_an_archive_whose_envelope_id_disagrees_with_the_request_is_refused(self):
        self.store.save(WorldArchive.capture("other", _cold_state(), revision=1))
        (self.root / "nightcord").mkdir(parents=True, exist_ok=True)
        (self.root / "nightcord" / FileWorldStore.ARCHIVE_NAME).write_bytes(
            (self.root / "other" / FileWorldStore.ARCHIVE_NAME).read_bytes()
        )
        with self.assertRaises(ArchiveError) as caught:
            self.store.load("nightcord")
        self.assertIn("other", str(caught.exception))


# ── AC4 唯一所有权 ──────────────────────────────────────────────────────
_CHILD_HOLD = r"""
import json, os, sys
sys.path.insert(0, {root!r})
from pns.runtime.persistence.store import FileWorldStore

store = FileWorldStore({archive_root!r})
try:
    handle = store.acquire({world_id!r})
except BaseException as e:
    print(json.dumps({{"ok": False, "error": type(e).__name__ + ": " + str(e)}}), flush=True)
    raise SystemExit(3)
print(json.dumps({{"ok": True, "pid": os.getpid(), "owner": handle.owner.to_dict()}}), flush=True)
sys.stdin.readline()
if {release!r}:
    handle.release()
"""


def _child_hold(archive_root, world_id="nightcord", release=False):
    """起一个**真的另一个进程**，让它拿着这个世界的所有权等在那里。

    跨进程的所有权没法在一个 Python 进程里证明：同进程的第二把锁会被注册表
    挡住，证不到内核那一层。
    """
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _CHILD_HOLD.format(
                root=str(REPO_ROOT),
                archive_root=str(archive_root),
                world_id=world_id,
                release=release,
            ),
        ],
        cwd=str(REPO_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class OwnershipTests(WorldTestCase):
    """一个世界同一时刻只有一个拥有者，而且抢锁绝不夺走活着的那个。"""

    def test_one_process_cannot_open_the_same_world_twice(self):
        first = self.created()
        with self.assertRaises(WorldAlreadyOwned):
            self.service.restore("nightcord", adapters=_adapters())
        # 第二次尝试没有把第一个句柄的所有权弄坏。
        self.assertTrue(first.status()["owned"])
        first.checkpoint()
        self.assertEqual(first.status()["revision"], 2)

    def test_restoring_the_same_archive_twice_is_refused(self):
        world = self.created()
        world.close()
        first = self.service.restore("nightcord", adapters=_adapters())
        with self.assertRaises(WorldAlreadyOwned):
            self.service.restore("nightcord", adapters=_adapters())
        self.assertTrue(first.status()["owned"])

    def test_a_second_store_on_the_same_root_still_conflicts(self):
        # 同一份磁盘世界，两个 store 实例。只按 world_id 记一个 Python 集合
        # 是不够的 —— 这里要的是"同一份存档目录"这个身份。
        self.created()
        other = WorldLifecycleService(FileWorldStore(self.root))
        self.addCleanup(other.release_all)
        with self.assertRaises(WorldAlreadyOwned):
            other.restore("nightcord", adapters=_adapters())

    def test_a_symlinked_root_pointing_at_the_same_place_still_conflicts(self):
        # 身份是解析之后的那条锁路径，不是调用方写的那个字符串。两个 store
        # 一个走真实路径、一个走软链，指的是同一个世界。
        self.root.mkdir(parents=True, exist_ok=True)
        linked = self.root.parent / "linked"
        linked.symlink_to(self.root, target_is_directory=True)
        self.created()
        other = WorldLifecycleService(FileWorldStore(linked))
        self.addCleanup(other.release_all)
        with self.assertRaises(WorldAlreadyOwned):
            other.restore("nightcord", adapters=_adapters())

    def test_different_roots_with_the_same_id_are_different_worlds(self):
        self.created()
        other_root = self.root.parent / "other-root"
        other = WorldLifecycleService(FileWorldStore(other_root))
        self.addCleanup(other.release_all)
        second = other.create("nightcord", _cold_state(), adapters=_adapters())
        self.assertTrue(second.status()["owned"])

    def test_two_threads_racing_to_create_leave_exactly_one_owner(self):
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def attempt():
            barrier.wait()
            try:
                world = self.service.create(
                    "nightcord", _cold_state(), adapters=_adapters()
                )
            except BaseException as e:
                with lock:
                    results.append(("failed", e))
            else:
                with lock:
                    results.append(("owned", world))

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        owned = [item for kind, item in results if kind == "owned"]
        failed = [item for kind, item in results if kind == "failed"]
        self.assertEqual(len(owned), 1, results)
        self.assertEqual(len(failed), 1, results)
        self.assertIsInstance(failed[0], (WorldAlreadyOwned, LifecycleError))
        self.assertEqual(self.store.load("nightcord").revision, 1)

    def test_two_threads_racing_to_restore_leave_exactly_one_owner(self):
        self.created().close()
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def attempt():
            barrier.wait()
            try:
                world = self.service.restore("nightcord", adapters=_adapters())
            except BaseException as e:
                with lock:
                    results.append(("failed", e))
            else:
                with lock:
                    results.append(("owned", world))

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len([k for k, _ in results if k == "owned"]), 1, results)

    def test_a_live_owner_in_another_process_is_never_stolen_from(self):
        child = self.hold_in_another_process()
        line = child.stdout.readline()
        self.assertTrue(json.loads(line)["ok"], line)
        with self.assertRaises(WorldAlreadyOwned) as caught:
            self.store.acquire("nightcord")
        self.assertIn(str(json.loads(line)["pid"]), str(caught.exception))
        # 拒绝之后子进程仍然是拥有者：锁文件没被动过。
        child.stdin.write("\n")
        child.stdin.flush()
        child.wait(timeout=30)

    def test_a_crashed_owner_is_recovered_and_reported(self):
        child = self.hold_in_another_process()
        dead_pid = json.loads(child.stdout.readline())["pid"]
        child.kill()
        child.wait(timeout=30)
        handle = self.store.acquire("nightcord")
        self.addCleanup(handle.release)
        self.assertIsNotNone(handle.recovered_from)
        self.assertEqual(handle.recovered_from.pid, dead_pid)

    def test_a_cleanly_released_owner_leaves_nothing_to_recover(self):
        child = self.hold_in_another_process(release=True)
        self.assertTrue(json.loads(child.stdout.readline())["ok"])
        child.stdin.write("\n")
        child.stdin.flush()
        self.assertEqual(child.wait(timeout=30), 0)
        handle = self.store.acquire("nightcord")
        self.addCleanup(handle.release)
        self.assertIsNone(handle.recovered_from)

    def test_releasing_is_idempotent_and_frees_the_world(self):
        handle = self.store.acquire("nightcord")
        handle.release()
        handle.release()
        self.assertFalse(handle.held)
        again = self.store.acquire("nightcord")
        self.addCleanup(again.release)
        self.assertTrue(again.held)

    def test_a_released_handle_cannot_be_renewed(self):
        handle = self.store.acquire("nightcord")
        handle.release()
        with self.assertRaises(OwnershipError):
            handle.renew()

    def test_the_process_registry_is_empty_between_owners(self):
        before = owned_world_paths()
        handle = self.store.acquire("nightcord")
        self.assertEqual(len(owned_world_paths()), len(before) + 1)
        handle.release()
        self.assertEqual(owned_world_paths(), before)

    def test_an_owner_record_survives_a_json_round_trip(self):
        handle = self.store.acquire("nightcord")
        self.addCleanup(handle.release)
        record = ownership_mod.OwnerRecord.from_dict(
            json.loads(json.dumps(handle.owner.to_dict()))
        )
        self.assertEqual(record.pid, os.getpid())
        self.assertEqual(record.world_id, "nightcord")


# ── AC5 checkpoint 只在一条线性化边界上取快照 ───────────────────────────
class CheckpointBoundaryTests(WorldTestCase):
    """存档里绝不会出现半提交的世界，也绝不会出现被回滚掉的事务。"""

    def test_a_checkpoint_from_inside_a_running_transaction_is_refused(self):
        world = self.created()
        world.checkpoint()
        before = self.archive_json()["revision"]
        taken = []

        def sabotage(self_encoder, observations):
            # 事务正开着（事件已经写进去了，记忆还没）。这一刻取快照会存下
            # 一份半截世界 —— 必须响亮拒绝。
            try:
                world.checkpoint(reason="from-inside")
            except BaseException as e:
                taken.append(e)
                raise

        with patch.object(MemoryEncoder, "encode", sabotage):
            result = world.runtime.process_due(_due(world.runtime.scheduler))

        self.assertTrue(taken)
        self.assertIsInstance(taken[0], (CheckpointError, AutonomyError, LifecycleError))
        self.assertEqual(result.outcome.value, "failed_retryable")
        # 磁盘一动没动，而且里面没有那条被回滚掉的事件。
        self.assertEqual(self.archive_json()["revision"], before)
        self.assertEqual(len(world.state.events.by_type(EventType.MESSAGE_SENT)), 0)
        self.assertEqual(self._sent(self.archive_json()), [])

    def test_a_checkpoint_from_another_thread_waits_for_the_transaction(self):
        world = self.created()
        inside = threading.Event()
        release = threading.Event()
        real_encode = MemoryEncoder.encode

        def slow(self_encoder, observations):
            inside.set()
            release.wait(30)
            return real_encode(self_encoder, observations)

        done = threading.Event()
        errors = []

        def checkpoint():
            try:
                world.checkpoint(reason="racing")
            except BaseException as e:  # pragma: no cover - 只在失败时才有内容
                errors.append(e)
            finally:
                done.set()

        with patch.object(MemoryEncoder, "encode", slow):
            committer = threading.Thread(
                target=lambda: world.runtime.process_due(_due(world.runtime.scheduler))
            )
            committer.start()
            self.assertTrue(inside.wait(30))
            checkpointer = threading.Thread(target=checkpoint)
            checkpointer.start()
            # 事务还开着，checkpoint 必须还在等。
            self.assertFalse(done.wait(0.5))
            release.set()
            committer.join(30)
            checkpointer.join(30)

        self.assertEqual(errors, [])
        stored = self.archive_json()["state"]
        events = [
            event
            for event in stored["events"]["events"]
            if event["type"] == EventType.MESSAGE_SENT.value
        ]
        # 要么整条事务都在存档里，要么一点都不在。这里它已经落定了，所以
        # 事件、观察、记忆必须一起在。
        self.assertEqual(len(events), 1)
        self.assertTrue(stored["observations"]["observations"])
        self.assertTrue(stored["memory"]["store"]["records"])

    def test_a_rolled_back_transaction_never_reaches_the_archive(self):
        world = self.created()

        def boom(self_encoder, observations):
            raise RuntimeError("编码炸了")

        with patch.object(MemoryEncoder, "encode", boom):
            result = world.runtime.process_due(_due(world.runtime.scheduler))
        self.assertEqual(result.outcome.value, "failed_retryable")

        world.checkpoint(reason="after-rollback")
        stored = self.archive_json()["state"]
        # 推时钟那条 world.time_advanced 是提交过的，它当然在；被回滚掉的是
        # 那次台词提交。
        self.assertEqual(
            [
                event
                for event in stored["events"]["events"]
                if event["type"] == EventType.MESSAGE_SENT.value
            ],
            [],
        )
        self.assertEqual(stored["memory"]["store"]["records"], [])
        # 到期记录仍然待处理：恢复之后还能重来。
        self.assertTrue(stored["scheduler"]["outbox"]["records"])
        restored = self.store.load("nightcord").restore_state()
        self.assertFalse(restored.activation_outbox.is_acknowledged(result.due_id))

    def test_two_checkpoints_racing_still_produce_monotonic_revisions(self):
        world = self.created()
        barrier = threading.Barrier(2)
        seen = []
        lock = threading.Lock()

        def attempt():
            barrier.wait()
            status = world.checkpoint(reason="racing")
            with lock:
                seen.append(status["revision"])

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(seen), [2, 3])
        self.assertEqual(self.archive_json()["revision"], 3)
        self.assertEqual(world.status()["revision"], 3)

    def test_a_failed_checkpoint_does_not_advance_the_revision(self):
        world = self.created()
        world.runtime.process_due(_due(world.runtime.scheduler))
        with patch.object(
            store_mod.os, "replace", side_effect=OSError("disk full")
        ):
            with self.assertRaises(CheckpointError):
                world.checkpoint()
        self.assertEqual(world.status()["revision"], 1)
        self.assertEqual(self.archive_json()["revision"], 1)
        self.assertTrue(world.status()["dirty"])
        self.assertIn("disk full", world.status()["last_error"])
        # 下一次成功的 checkpoint 接着上一个成功的号往下走。
        self.assertEqual(world.checkpoint()["revision"], 2)
        self.assertIsNone(world.status()["last_error"])

    def test_a_checkpoint_captures_work_committed_before_it(self):
        world = self.created()
        world.runtime.process_due(_due(world.runtime.scheduler))
        world.checkpoint()
        restored = self.store.load("nightcord").restore_state()
        self.assertEqual(restored.events.to_dict(), world.state.events.to_dict())
        self.assertEqual(restored.memories.to_dict(), world.state.memories.to_dict())
        self.assertEqual(
            restored.world_state.clock, world.state.world_state.clock
        )

    def test_the_dirty_flag_tracks_authoritative_writes(self):
        world = self.created()
        self.assertFalse(world.status()["dirty"])
        world.runtime.process_due(_due(world.runtime.scheduler))
        self.assertTrue(world.status()["dirty"])
        world.checkpoint()
        self.assertFalse(world.status()["dirty"])

    def test_an_automatic_policy_coalesces_instead_of_writing_per_event(self):
        world = self.created(checkpoint_policy=CheckpointPolicy(every_boundaries=3))
        writes = []
        real_save = FileWorldStore.save

        def counting(store_self, archive):
            writes.append(archive.revision)
            return real_save(store_self, archive)

        with patch.object(FileWorldStore, "save", counting):
            for index in range(5):
                world.runtime.process_due(
                    _due(world.runtime.scheduler, activation_id=f"wake-{index}")
                )
                world.checkpoint_if_due()
        # 五次边界，最多两次写盘 —— 不是一次事件一个写手。
        self.assertEqual(len(writes), 1)
        self.assertEqual(world.status()["boundaries_since_checkpoint"], 2)

    def test_a_minimum_interval_also_coalesces(self):
        world = self.created(
            checkpoint_policy=CheckpointPolicy(
                every_boundaries=1, min_interval_seconds=3600
            )
        )
        world.runtime.process_due(_due(world.runtime.scheduler, activation_id="a"))
        # 创建时刚存过，一小时之内不再存 —— 边界照记，写盘被合并掉。
        self.assertIsNone(world.checkpoint_if_due())
        self.assertEqual(world.status()["revision"], 1)
        self.assertEqual(world.status()["boundaries_since_checkpoint"], 1)
        # 手动 checkpoint 不受这条约束：它是人明确要的。
        self.assertEqual(world.checkpoint()["revision"], 2)

    def test_a_policy_that_would_write_per_event_is_refused_at_construction(self):
        for bad in ({"every_boundaries": 0}, {"every_boundaries": -1},
                    {"every_boundaries": True}, {"min_interval_seconds": -1},
                    {"min_interval_seconds": "快点"}):
            with self.subTest(**bad):
                with self.assertRaises(LifecycleError):
                    CheckpointPolicy(**bad)

    def test_a_world_configured_not_to_save_on_close_says_so_in_its_status(self):
        world = self.created(checkpoint_policy=CheckpointPolicy(on_close=False))
        world.runtime.process_due(_due(world.runtime.scheduler))
        status = world.close()
        self.assertTrue(status["closed"])
        self.assertEqual(status["revision"], 1)
        # 没存就是没存：dirty 仍然如实说"状态跟磁盘上那一份不一样"。
        self.assertTrue(status["dirty"])
        restored = self.store.load("nightcord").restore_state()
        self.assertEqual(restored.events.by_type(EventType.MESSAGE_SENT), ())

    def test_an_automatic_checkpoint_never_runs_inside_a_transaction(self):
        # 自动 checkpoint 走的是同一条边界判断，没有"因为是自动的所以放行"
        # 这条捷径：事务内部调用一样响亮拒绝。
        world = self.created(checkpoint_policy=CheckpointPolicy(every_boundaries=1))
        errors = []

        def sabotage(self_encoder, observations):
            try:
                world.checkpoint_if_due(reason="auto")
            except BaseException as e:
                errors.append(e)
                raise
            return ()

        with patch.object(MemoryEncoder, "encode", sabotage):
            result = world.runtime.process_due(_due(world.runtime.scheduler))

        self.assertTrue(errors)
        self.assertIsInstance(
            errors[0], (CheckpointError, AutonomyError, LifecycleError)
        )
        self.assertEqual(result.outcome.value, "failed_retryable")
        self.assertEqual(self._sent(self.archive_json()), [])


# ── AC6 关闭顺序 ────────────────────────────────────────────────────────
class ShutdownOrderTests(WorldTestCase):
    """停准入 → 等事务落定 → 最后一次 checkpoint → 标记关闭 → 释放所有权。"""

    def test_a_clean_close_follows_the_fixed_order(self):
        world = self.created()
        order = []
        real_stop = AutonomousRuntime.stop
        real_save = FileWorldStore.save

        def stop(runtime_self, reason="stopped"):
            order.append("stop")
            return real_stop(runtime_self, reason)

        def save(store_self, archive):
            order.append("checkpoint")
            return real_save(store_self, archive)

        release = ownership_mod.OwnershipHandle.release

        def released(handle_self):
            order.append("release")
            return release(handle_self)

        with patch.object(AutonomousRuntime, "stop", stop), patch.object(
            FileWorldStore, "save", save
        ), patch.object(ownership_mod.OwnershipHandle, "release", released):
            status = world.close()

        self.assertEqual(order, ["stop", "checkpoint", "release"])
        self.assertTrue(status["closed"])
        self.assertTrue(status["clean"])
        self.assertFalse(status["owned"])
        self.assertFalse(status["running"])

    def test_close_waits_for_an_in_flight_transaction_to_settle(self):
        world = self.created()
        inside = threading.Event()
        release = threading.Event()
        real_encode = MemoryEncoder.encode

        def slow(self_encoder, observations):
            inside.set()
            release.wait(30)
            return real_encode(self_encoder, observations)

        results = []
        with patch.object(MemoryEncoder, "encode", slow):
            committer = threading.Thread(
                target=lambda: results.append(
                    world.runtime.process_due(_due(world.runtime.scheduler))
                )
            )
            committer.start()
            self.assertTrue(inside.wait(30))
            closer = threading.Thread(target=world.close)
            closer.start()
            closer.join(0.5)
            # 事务还没落定，close() 必须还堵在停机那一步。
            self.assertTrue(closer.is_alive())
            release.set()
            committer.join(30)
            closer.join(30)

        self.assertEqual(results[0].outcome.value, "acted")
        # 落定的那次提交进了最后一份存档：关闭没有把它丢掉。
        stored = self.archive_json()["state"]
        self.assertEqual(len(self._sent(self.archive_json())), 1)
        self.assertTrue(stored["agency"]["log"]["records"])

    def test_a_close_from_inside_a_transaction_does_not_claim_a_clean_close(self):
        # 事务内部的 stop() 只能登记、不能生效。这时候假装干净关闭，就会出现
        # "已经关了"之后 Agency 记录才落地。
        world = self.created()
        errors = []

        def sabotage(self_encoder, observations):
            try:
                world.close(reason="from-inside")
            except BaseException as e:
                errors.append(e)
            return ()

        with patch.object(MemoryEncoder, "encode", sabotage):
            world.runtime.process_due(_due(world.runtime.scheduler))

        self.assertTrue(errors)
        self.assertIsInstance(errors[0], LifecycleError)
        self.assertFalse(world.status()["closed"])
        self.assertTrue(world.status()["owned"])
        world.close()

    def test_a_failed_final_checkpoint_keeps_ownership_and_stays_open(self):
        world = self.created()
        world.runtime.process_due(_due(world.runtime.scheduler))
        with patch.object(
            store_mod.os, "replace", side_effect=OSError("disk full")
        ):
            with self.assertRaises(CheckpointError):
                world.close()
        status = world.status()
        self.assertFalse(status["closed"])
        self.assertTrue(status["owned"])
        self.assertFalse(status["clean"])
        self.assertEqual(status["revision"], 1)
        # 所有权真的还在：别人（哪怕另一个进程）现在也拿不到这个世界。
        with self.assertRaises(WorldAlreadyOwned):
            WorldLifecycleService(FileWorldStore(self.root)).restore(
                "nightcord", adapters=_adapters()
            )
        child = self.hold_in_another_process()
        self.assertFalse(json.loads(child.stdout.readline())["ok"])
        # 磁盘恢复正常之后，重试一次关闭就能干净收尾。
        status = world.close()
        self.assertTrue(status["clean"])
        self.assertEqual(status["revision"], 2)

    def test_abandoning_after_a_failed_checkpoint_is_explicit_and_honest(self):
        world = self.created()
        world.runtime.process_due(_due(world.runtime.scheduler))
        with patch.object(
            store_mod.os, "replace", side_effect=OSError("disk full")
        ):
            with self.assertRaises(CheckpointError):
                world.close()
            status = world.close(force=True)
        self.assertTrue(status["closed"])
        self.assertFalse(status["clean"])
        self.assertFalse(status["owned"])
        self.assertEqual(status["durable_revision"], 1)
        self.assertIn("disk full", status["last_error"])
        # 世界被放弃之后，最后一次成功 checkpoint 之后的工作确实丢了 ——
        # 存档里没有那条事件，而且这件事在状态里写着。
        restored = self.store.load("nightcord").restore_state()
        self.assertEqual(restored.events.by_type(EventType.MESSAGE_SENT), ())
        again = self.service.restore("nightcord", adapters=_adapters())
        self.addCleanup(again.close)
        self.assertEqual(again.state.events.by_type(EventType.MESSAGE_SENT), ())

    def test_closing_twice_is_idempotent_and_does_not_double_release(self):
        world = self.created()
        first = world.close()
        second = world.close()
        self.assertTrue(second["closed"])
        self.assertEqual(second["revision"], first["revision"])
        self.assertFalse(second["owned"])

    def test_a_closed_world_refuses_further_work(self):
        world = self.created()
        world.close()
        with self.assertRaises(LifecycleError):
            world.checkpoint()
        self.assertFalse(world.runtime.running)
        result = world.runtime.process_due(_due(world.runtime.scheduler))
        self.assertEqual(result.outcome.value, "stopped")

    def test_closing_frees_the_world_for_a_new_owner(self):
        world = self.created()
        world.runtime.process_due(_due(world.runtime.scheduler))
        world.close()
        again = self.service.restore("nightcord", adapters=_adapters())
        self.addCleanup(again.close)
        self.assertEqual(len(again.state.events.by_type(EventType.MESSAGE_SENT)), 1)
        # 创建写了第 1 版，关闭那次 checkpoint 写了第 2 版。
        self.assertEqual(again.status()["revision"], 2)


# ── AC7 恢复：数据在前，服务绑定在后 ────────────────────────────────────
class RestoreTests(WorldTestCase):
    """恢复必须先校验存档、再用调用方给的冷适配器显式绑服务。"""

    def test_restore_rebinds_every_service_onto_the_restored_state(self):
        world = self.created()
        world.runtime.process_due(_due(world.runtime.scheduler))
        world.close()

        again = self.service.restore("nightcord", adapters=_adapters())
        self.addCleanup(again.close)
        state = again.state
        self.assertIsInstance(state.scheduler, PersistentScheduler)
        self.assertIsInstance(state.autonomy, AutonomousRuntime)
        self.assertIsNotNone(state.agency_engine)
        self.assertIsNotNone(state.memory_encoder)
        # 绑的是**同一份**恢复出来的状态，不是另一份副本。
        self.assertIs(state.autonomy.state, state)
        self.assertIs(state.scheduler.state, state)
        self.assertIs(again.runtime.world, state.world_state)

    def test_a_restored_world_keeps_running(self):
        world = self.created()
        world.close()
        again = self.service.restore("nightcord", adapters=_adapters())
        self.addCleanup(again.close)
        result = again.runtime.process_due(_due(again.runtime.scheduler))
        self.assertEqual(result.outcome.value, "acted")
        again.checkpoint()
        restored = self.store.load("nightcord").restore_state()
        self.assertEqual(len(restored.events.by_type(EventType.MESSAGE_SENT)), 1)

    def test_a_restored_world_re_runs_a_due_that_was_never_acknowledged(self):
        # 崩溃恢复的边界就在这里：最后一次成功 checkpoint 之后的处理会重来。
        world = self.created()
        due = _due(world.runtime.scheduler)
        world.checkpoint()          # 到期已经落箱，但还没被处理
        world.runtime.process_due(due)
        self.assertTrue(world.state.activation_outbox.is_acknowledged(due.due_id))
        world.release()             # 模拟崩溃：checkpoint 之后的工作没落盘

        again = self.service.restore("nightcord", adapters=_adapters())
        self.addCleanup(again.close)
        self.assertFalse(again.state.activation_outbox.is_acknowledged(due.due_id))
        results = again.runtime.process_pending()
        self.assertEqual([item.outcome.value for item in results], ["acted"])

    def test_restoring_without_the_required_adapters_fails(self):
        self.created().close()
        for broken in (None, RuntimeAdapters, object()):
            with self.assertRaises((LifecycleError, TypeError)):
                self.service.restore("nightcord", adapters=broken)

    def test_an_adapter_that_cannot_audit_is_refused(self):
        with self.assertRaises(LifecycleError):
            RuntimeAdapters(auditor=object())

    def test_binding_failure_after_acquiring_ownership_releases_it(self):
        self.created().close()

        def exploding(state):
            raise RuntimeError("适配器起不来")

        with self.assertRaises(RuntimeError):
            self.service.restore(
                "nightcord",
                adapters=RuntimeAdapters(
                    auditor=ScriptedAuditor(), policy_factory=exploding
                ),
            )
        # 所有权必须还回去了 —— 否则一次绑定失败就把世界永久锁死。
        again = self.service.restore("nightcord", adapters=_adapters())
        self.addCleanup(again.close)
        self.assertTrue(again.status()["owned"])

    def test_restoring_a_corrupt_archive_fails_and_releases_ownership(self):
        self.created().close()
        path = self.archive_path()
        path.write_text(path.read_text(encoding="utf-8")[:40], encoding="utf-8")
        with self.assertRaises(ArchiveCorrupt):
            self.service.restore("nightcord", adapters=_adapters())
        # 没有"安静地恢复成一个空世界"这条路，也没有把锁留在地上。
        handle = self.store.acquire("nightcord")
        handle.release()

    def test_restoring_an_unsupported_version_fails(self):
        self.created().close()
        payload = self.archive_json()
        payload["version"] = WORLD_ARCHIVE_VERSION + 1
        self.archive_path().write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ArchiveError):
            self.service.restore("nightcord", adapters=_adapters())

    def test_restoring_a_mismatched_identity_fails(self):
        self.created().close()
        payload = self.archive_json()
        payload["world_id"] = "someone-else"
        self.archive_path().write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ArchiveError):
            self.service.restore("nightcord", adapters=_adapters())

    def test_restoring_a_clock_mismatch_fails(self):
        self.created().close()
        payload = self.archive_json()
        payload["clock"] = (CLOCK - timedelta(hours=3)).isoformat()
        self.archive_path().write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ArchiveError):
            self.service.restore("nightcord", adapters=_adapters())

    def test_restoring_a_tampered_section_fails_cross_validation(self):
        # 各段单独看都合法，合起来自相矛盾：排期排在了世界时钟之前。
        world = self.created()
        _due(world.runtime.scheduler, minutes=10)
        world.checkpoint()
        world.close()
        payload = self.archive_json()
        latest = payload["state"]["events"]["events"][-1]
        latest["occurred_at"] = (CLOCK + timedelta(days=1)).isoformat()
        self.archive_path().write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ArchiveError):
            self.service.restore("nightcord", adapters=_adapters())

    def test_restoring_a_world_that_was_never_created_fails(self):
        with self.assertRaises(ArchiveNotFound):
            self.service.restore("nightcord", adapters=_adapters())
        # 失败的恢复不留锁、不留目录残骸。
        self.assertEqual(self.store.list_worlds(), ())

    def test_creating_over_an_existing_world_is_refused(self):
        self.created().close()
        before = self.store.load("nightcord").revision
        with self.assertRaises(LifecycleError):
            self.service.create("nightcord", _cold_state(), adapters=_adapters())
        self.assertEqual(self.store.load("nightcord").revision, before)

    def test_creating_from_a_state_that_already_has_services_is_refused(self):
        state = _cold_state()
        PersistentScheduler(state)
        with self.assertRaises(LifecycleError):
            self.service.create("nightcord", state, adapters=_adapters())


# ── AC8 崩溃恢复的边界 ──────────────────────────────────────────────────
_CHILD_CRASH = r"""
import os, sys
sys.path.insert(0, {root!r})
from datetime import timedelta
from pns.models.activation import ActivationKind, ScheduledActivation
from pns.runtime.autonomy.audit import ScriptedAuditor
from pns.runtime.autonomy.generation import AuthoredLinePolicy, ScriptedLineGenerator
from pns.runtime.memory.recall import MemoryRecall
from pns.runtime.persistence import store as store_mod
from pns.runtime.persistence.lifecycle import RuntimeAdapters, WorldLifecycleService
from pns.runtime.persistence.store import FileWorldStore

service = WorldLifecycleService(FileWorldStore({archive_root!r}))
adapters = RuntimeAdapters(
    auditor=ScriptedAuditor(),
    policy_factory=lambda state: AuthoredLinePolicy(
        ScriptedLineGenerator({{"mizuki": "在的哦", "ena": "……嗯"}}),
        recall=MemoryRecall(state),
    ),
)
world = service.restore("nightcord", adapters=adapters)
scheduler = world.runtime.scheduler
scheduler.schedule(
    ScheduledActivation(
        activation_id="wake",
        kind=ActivationKind.CHARACTER_ACTIVATION,
        due_at=scheduler.clock + timedelta(minutes=10),
        character_id="mizuki",
    )
)
scheduler.advance_by(10)
results = world.runtime.process_pending()
assert [item.outcome.value for item in results] == ["acted"], results

real_replace = os.replace


def die(*args, **kwargs):
    os._exit(70)


def replace_then_die(src, dst):
    real_replace(src, dst)
    os._exit(71)


stage = {stage!r}
if stage == "before_tmp":
    store_mod.tempfile.mkstemp = die
elif stage == "after_flush":
    store_mod.os.fsync = die
elif stage == "after_replace":
    store_mod.os.replace = replace_then_die
world.checkpoint(reason="crash-test")
os._exit(9)
"""


class CrashRecoveryTests(WorldTestCase):
    """保证的**只有**这一条：恢复到最后一次成功的 checkpoint。

    之后的内存工作会丢。没有 WAL，也不假装有。
    """

    def _crash(self, stage):
        return subprocess.run(
            [
                sys.executable,
                "-c",
                _CHILD_CRASH.format(
                    root=str(REPO_ROOT), archive_root=str(self.root), stage=stage
                ),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )

    def _seed(self) -> int:
        """建一个世界并干净关闭。返回磁盘上那一版的号（创建 1 + 关闭那次 2）。"""
        self.created().close()
        baseline = self.store.load("nightcord").revision
        self.assertEqual(baseline, 2)
        return baseline

    def test_a_crash_before_the_temp_file_leaves_the_previous_archive(self):
        baseline = self._seed()
        result = self._crash("before_tmp")
        self.assertEqual(result.returncode, 70, result.stderr)
        archive = self.store.load("nightcord")
        self.assertEqual(archive.revision, baseline)
        self.assertEqual(
            archive.restore_state().events.by_type(EventType.MESSAGE_SENT), ()
        )
        self.assertEqual(self.store.residue("nightcord"), ())

    def test_a_crash_after_the_flush_leaves_the_previous_archive_and_residue(self):
        baseline = self._seed()
        result = self._crash("after_flush")
        self.assertEqual(result.returncode, 70, result.stderr)
        archive = self.store.load("nightcord")
        self.assertEqual(archive.revision, baseline)
        self.assertEqual(
            archive.restore_state().events.by_type(EventType.MESSAGE_SENT), ()
        )
        # 半截的临时文件留在那里 —— 它被**报告**，而不是被当成存档读回来。
        self.assertTrue(self.store.residue("nightcord"))
        self.assertIn("residue", self.service.status("nightcord"))
        self.assertTrue(self.service.status("nightcord")["residue"])

    def test_a_crash_after_the_replace_leaves_the_new_complete_archive(self):
        baseline = self._seed()
        result = self._crash("after_replace")
        self.assertEqual(result.returncode, 71, result.stderr)
        archive = self.store.load("nightcord")
        self.assertEqual(archive.revision, baseline + 1)
        restored = archive.restore_state()
        self.assertEqual(len(restored.events.by_type(EventType.MESSAGE_SENT)), 1)
        self.assertTrue(len(restored.memories) > 0)
        self.assertEqual(self.store.residue("nightcord"), ())

    def test_a_crashed_world_can_be_taken_over_and_keeps_working(self):
        baseline = self._seed()
        self.assertEqual(self._crash("after_flush").returncode, 70)
        world = self.service.restore("nightcord", adapters=_adapters())
        self.addCleanup(world.close)
        status = world.status()
        self.assertEqual(status["revision"], baseline)
        self.assertIsNotNone(status["recovered_from"])
        self.assertEqual(world.state.events.by_type(EventType.MESSAGE_SENT), ())
        # 崩溃前那次处理确实丢了，但世界还能继续跑。
        result = world.runtime.process_due(_due(world.runtime.scheduler))
        self.assertEqual(result.outcome.value, "acted")

    def test_work_committed_after_the_last_checkpoint_is_lost_and_re_run_once(self):
        """崩溃边界的正脸：checkpoint 之后提交的那次处理会丢，而且会重跑。

        重跑**不是**重复提交：恢复出来的世界里那条到期资格仍然没被确认，
        重新处理一次之后它有且只有一条事件。丢掉的是"上一次跑出来的那句话"，
        不是"这条到期被处理过两次"。
        """
        world = self.created()
        due = _due(world.runtime.scheduler)
        world.checkpoint(reason="before-the-work")   # 到期已落箱，还没处理
        result = world.runtime.process_due(due)
        self.assertEqual(result.outcome.value, "acted")
        self.assertEqual(len(world.state.events.by_type(EventType.MESSAGE_SENT)), 1)
        world.release()                              # 模拟崩溃：之后没再存过

        again = self.service.restore("nightcord", adapters=_adapters())
        self.addCleanup(again.close)
        self.assertEqual(again.state.events.by_type(EventType.MESSAGE_SENT), ())
        self.assertFalse(again.state.activation_outbox.is_acknowledged(due.due_id))
        results = again.runtime.process_pending()
        self.assertEqual([item.outcome.value for item in results], ["acted"])
        self.assertEqual(len(again.state.events.by_type(EventType.MESSAGE_SENT)), 1)
        # 再跑一次不会又提交一条：交接是一次性的。
        self.assertEqual(again.runtime.process_pending(), ())
        self.assertEqual(len(again.state.events.by_type(EventType.MESSAGE_SENT)), 1)

    def test_the_recovery_boundary_is_documented_as_last_checkpoint_only(self):
        # 契约写在代码里，而不是只写在提交信息里：不许出现 WAL / 零丢失的承诺。
        source = (PERSISTENCE_DIR / "lifecycle.py").read_text(encoding="utf-8")
        self.assertIn("最后一次成功的 checkpoint", source)
        self.assertIn("没有 WAL", source)


# ── AC9 生命周期服务面（WEB-1 之前的最小面） ────────────────────────────
class LifecycleServiceTests(WorldTestCase):
    """列出、创建、恢复、checkpoint、关闭、状态。UI 不在这一期。"""

    def test_listing_reports_what_is_on_disk_and_who_owns_it(self):
        world = self.created()
        self.service.create("other", _cold_state(session_id="s2"), adapters=_adapters())
        listed = {item["world_id"]: item for item in self.service.list_worlds()}
        self.assertEqual(sorted(listed), ["nightcord", "other"])
        self.assertTrue(listed["nightcord"]["owned"])
        self.assertEqual(listed["nightcord"]["revision"], 1)
        self.assertIsNotNone(listed["nightcord"]["last_saved_at"])
        world.close()
        listed = {item["world_id"]: item for item in self.service.list_worlds()}
        self.assertFalse(listed["nightcord"]["owned"])

    def test_listing_reports_a_corrupt_world_instead_of_exploding(self):
        self.created().close()
        self.archive_path().write_text("{ truncated", encoding="utf-8")
        listed = {item["world_id"]: item for item in self.service.list_worlds()}
        self.assertIsNone(listed["nightcord"]["revision"])
        self.assertTrue(listed["nightcord"]["error"])
        # 但真去恢复它必须响亮失败，不许安静地开一个空世界。
        with self.assertRaises(ArchiveError):
            self.service.restore("nightcord", adapters=_adapters())

    def test_status_covers_the_whole_contract(self):
        world = self.created()
        status = world.status()
        for field in (
            "world_id",
            "session_id",
            "revision",
            "dirty",
            "closed",
            "clean",
            "owned",
            "owner",
            "last_saved_at",
            "last_error",
            "recovered_from",
            "residue",
            "running",
            "clock",
            "archive_path",
            "boundaries_since_checkpoint",
        ):
            self.assertIn(field, status)
        self.assertEqual(status["world_id"], "nightcord")
        self.assertEqual(status["session_id"], "s1")
        self.assertTrue(status["running"])
        self.assertEqual(status["owner"]["pid"], os.getpid())

    def test_status_of_a_world_this_process_does_not_own(self):
        self.created().close()
        status = self.service.status("nightcord")
        self.assertFalse(status["owned"])
        self.assertEqual(status["revision"], 2)
        self.assertIsNone(status["running"])

    def test_status_dictionaries_are_fresh_and_unowned(self):
        world = self.created()
        first = world.status()
        first["revision"] = 999
        first["residue"].append("injected")
        first["owner"]["pid"] = -1
        second = world.status()
        self.assertEqual(second["revision"], 1)
        self.assertEqual(second["residue"], [])
        self.assertEqual(second["owner"]["pid"], os.getpid())
        self.assertIsNot(first, second)

    def test_the_service_checkpoints_and_closes_by_world_id(self):
        world = self.created()
        self.assertEqual(self.service.checkpoint("nightcord")["revision"], 2)
        self.assertTrue(self.service.close("nightcord")["closed"])
        self.assertTrue(world.status()["closed"])
        with self.assertRaises(LifecycleError):
            self.service.checkpoint("nightcord")

    def test_releasing_also_closes_admission(self):
        # 所有权还回去了，运行时却还在接写入 —— 那些写入既不会落盘，又可能
        # 跟接手这个世界的下一个进程并行发生。
        world = self.created()
        world.release()
        self.assertFalse(world.runtime.running)
        self.assertEqual(
            world.runtime.process_due(_due(world.runtime.scheduler)).outcome.value,
            "stopped",
        )

    def test_releasing_from_inside_a_transaction_is_refused_not_deadlocked(self):
        world = self.created()
        errors = []

        def sabotage(self_encoder, observations):
            try:
                world.release()
            except BaseException as e:
                errors.append(e)
            return ()

        with patch.object(MemoryEncoder, "encode", sabotage):
            world.runtime.process_due(_due(world.runtime.scheduler))
        self.assertTrue(errors)
        self.assertIsInstance(errors[0], LifecycleError)
        self.assertTrue(world.status()["owned"])

    def test_release_all_gives_every_world_back(self):
        self.created()
        self.service.create("other", _cold_state(session_id="s2"), adapters=_adapters())
        self.service.release_all()
        for world_id in ("nightcord", "other"):
            handle = self.store.acquire(world_id)
            handle.release()


# ── AC11 敌对自审补回来的反例 ───────────────────────────────────────────
class AdversarialRegressionTests(WorldTestCase):
    """这一节里的每一条都对应一个**真的攻出来过**的缺陷。

    它们不是"想到的边界情况"，是先用攻击脚本演示了后果、再补的反例：
      1. checkpoint 撞上一次不走协调器闸门的事务 → 存下一份丢了排期的世界；
      2. 锁文件被删之后两个进程同时拥有同一个世界，并且都在写；
      3. 文件系统说不的时候漏出原始 OSError，调用方接不住。
    """

    def test_a_checkpoint_racing_a_transaction_outside_the_gate_is_refused(self):
        # 攻击复现：scheduler.advance_by() 直接开事务，不经过协调器闸门。
        # 在"时钟推了、一次性激活已经从队列里摘掉、到期记录还没落进投递箱"
        # 这一刻取快照，会存下一份**那条激活凭空消失**的世界 —— 而且它能通过
        # 全部校验，看起来完好无损。
        world = self.created()
        scheduler = world.runtime.scheduler
        scheduler.schedule(
            ScheduledActivation(
                activation_id="wake",
                kind=ActivationKind.CHARACTER_ACTIVATION,
                due_at=scheduler.clock + timedelta(minutes=10),
                character_id="mizuki",
            )
        )
        world.checkpoint()

        inside = threading.Event()
        release = threading.Event()
        real_append = ActivationOutbox._append

        def slow_append(self_outbox, record):
            inside.set()
            release.wait(30)
            return real_append(self_outbox, record)

        with patch.object(ActivationOutbox, "_append", slow_append):
            ticker = threading.Thread(target=lambda: scheduler.advance_by(10))
            ticker.start()
            self.assertTrue(inside.wait(30))
            with self.assertRaises(CheckpointError) as caught:
                world.checkpoint(reason="racing-a-tick")
            release.set()
            ticker.join(30)

        self.assertIn("提交", str(caught.exception))
        stored = self.archive_json()["state"]
        # 存档停在推进之前：时钟没动，那条激活还在队列里。
        self.assertEqual(stored["world_state"]["clock"], CLOCK.isoformat())
        self.assertEqual(
            [item["activation_id"] for item in stored["scheduler"]["queue"]["activations"]],
            ["wake"],
        )
        # 而活状态里推进确实完成了 —— 被拒绝的是快照，不是那次事务。
        self.assertEqual(
            world.state.world_state.clock, CLOCK + timedelta(minutes=10)
        )
        self.assertEqual(len(world.state.activation_outbox.pending()), 1)

    def test_the_session_reports_a_transaction_in_flight(self):
        state = _cold_state()
        self.assertFalse(state.in_transaction)
        with state.atomic_commit():
            self.assertTrue(state.in_transaction)
            with state.atomic_commit():
                self.assertTrue(state.in_transaction)
            self.assertTrue(state.in_transaction)
        self.assertFalse(state.in_transaction)
        # 事务抛异常回滚之后也必须归零，否则之后所有 checkpoint 都会被挡掉。
        with self.assertRaises(RuntimeError):
            with state.atomic_commit():
                raise RuntimeError("炸了")
        self.assertFalse(state.in_transaction)

    def test_a_removed_lock_file_stops_the_old_owner_from_writing(self):
        # 攻击复现：锁挂在 inode 上。有人把锁文件删掉之后，下一个进程一拿就
        # 拿到，而原拥有者还以为自己是唯一的写手 —— 两个进程往同一份存档上写。
        world = self.created()
        world.checkpoint()
        before = self.archive_json()["revision"]
        (self.root / "nightcord" / FileWorldStore.LOCK_NAME).unlink()

        child = self.hold_in_another_process()
        self.assertTrue(json.loads(child.stdout.readline())["ok"])

        with self.assertRaises(OwnershipError):
            world.checkpoint(reason="after-the-lock-was-removed")
        self.assertEqual(self.archive_json()["revision"], before)
        # 关闭同样不许假装干净：所有权已经不成立了。
        with self.assertRaises(OwnershipError):
            world.close()
        self.assertFalse(world.status()["closed"])
        # 显式放弃仍然可以收尾，而且如实报告不干净。
        status = world.close(force=True)
        self.assertTrue(status["closed"])
        self.assertFalse(status["clean"])
        self.assertEqual(status["durable_revision"], before)

    def test_a_replaced_lock_file_is_detected_too(self):
        world = self.created()
        lock = self.root / "nightcord" / FileWorldStore.LOCK_NAME
        lock.unlink()
        lock.write_text("{}", encoding="utf-8")  # 换了一个新 inode
        with self.assertRaises(OwnershipError):
            world.checkpoint()
        world.close(force=True)

    def test_a_hostile_filesystem_speaks_the_storage_layers_language(self):
        # 攻击复现：世界目录的位置上放着一个普通文件。以前这里会漏出
        # FileExistsError —— 调用方要么接不住，要么被迫 except Exception。
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "nightcord").write_text("我不是目录", encoding="utf-8")
        with self.assertRaises(StorageError):
            self.store.save(
                WorldArchive.capture("nightcord", _cold_state(), revision=1)
            )
        with self.assertRaises(StorageError):
            self.store.acquire("nightcord")
        with self.assertRaises(StorageError):
            self.service.create("nightcord", _cold_state(), adapters=_adapters())
        with self.assertRaises(StorageError):
            self.store.load("nightcord")
        # 状态面照样能回答"这个世界怎么了"，不炸。
        self.assertTrue(self.service.status("nightcord")["error"])

    def test_a_failed_first_save_leaves_neither_a_world_nor_a_lock(self):
        with patch.object(
            store_mod.os, "replace", side_effect=OSError("disk full")
        ):
            with self.assertRaises(StorageError):
                self.service.create("nightcord", _cold_state(), adapters=_adapters())
        self.assertEqual(owned_world_paths(), ())
        self.assertEqual(self.store.list_worlds(), ())
        # 干干净净，可以重来。
        world = self.service.create("nightcord", _cold_state(), adapters=_adapters())
        self.assertEqual(world.status()["revision"], 1)

    def test_one_set_of_adapters_can_drive_two_different_worlds(self):
        adapters = _adapters()
        first = self.service.create("nightcord", _cold_state(), adapters=adapters)
        second = self.service.create(
            "other", _cold_state(session_id="s2"), adapters=adapters
        )
        self.assertIsNot(first.state, second.state)
        self.assertIsNot(first.runtime, second.runtime)
        # 策略是按世界现造的，不是共用一个绑着别人状态的实例。
        self.assertIsNot(first.state.agency_engine, second.state.agency_engine)
        for world in (first, second):
            self.assertEqual(
                world.runtime.process_due(_due(world.runtime.scheduler)).outcome.value,
                "acted",
            )


# ── AC10 研究会话与导入惰性 ─────────────────────────────────────────────
class ResearchSessionIsUntouchedTests(unittest.TestCase):
    """/ws/run 不拿世界锁、不写存档根、也不 import 这一层。"""

    def test_session_runtime_does_not_import_the_persistence_layer(self):
        source = (
            REPO_ROOT / "pns" / "runtime" / "session_runtime.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertFalse(
            {name for name in imported if "persistence" in name},
            imported,
        )

    def test_nothing_in_pns_imports_the_persistence_layer(self):
        """持久化是**明确要**才有的东西。

        任何 pns 模块顺手 import 它，都会让"这条路会不会去拿世界锁、会不会
        写存档根"变成一个要靠读代码才能回答的问题。
        """
        offenders = []
        for path in sorted((REPO_ROOT / "pns").rglob("*.py")):
            if "persistence" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                if any("persistence" in name for name in names):
                    offenders.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(offenders, [])

    def test_a_research_session_state_owns_no_world(self):
        state = SessionState("s", "gate", ["mizuki", "ena"])
        state.attach_world_state(_world())
        PersistentScheduler(state)
        self.assertEqual(owned_world_paths(), ())

    def test_importing_the_package_touches_no_disk_and_no_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            before = sorted(os.listdir(tmp))
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; import pns.runtime.persistence as p; "
                    "assert 'pns.runtime.reload' not in sys.modules, "
                    "'导入持久化层顺带拉起了重载边界'; "
                    "from pns.runtime.persistence.ownership import owned_world_paths; "
                    "assert owned_world_paths() == (), owned_world_paths(); "
                    "print('ok')",
                ],
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(sorted(os.listdir(tmp)), before)

    def test_the_modules_hold_no_live_world_at_import_time(self):
        for module in (archive_mod, store_mod, ownership_mod):
            live = [
                name
                for name, value in vars(module).items()
                if isinstance(value, (SessionState, WorldState, Path))
                and not name.startswith("_")
            ]
            self.assertEqual(live, [], module.__name__)


if __name__ == "__main__":
    unittest.main()
