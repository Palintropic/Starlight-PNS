# tests/test_persistent_world_api.py — WEB-1 持久世界控制面的不变量。
#
# 盯住的东西按"错了会怎样"排：
#   1. 生命周期的权威只有一处。接口层不复制所有权、修订号、dirty、running，
#      也不在 P12 之外另写一套 ID / 路径规则。
#   2. 确切的失败必须**穿过**这层边界：404 是 404，冲突是冲突，
#      checkpoint 失败是 500 而不是一句"操作失败"，更不是 200 + error 字符串。
#   3. 创建绝不覆盖既有存档；恢复绝不回落到一个空世界。
#   4. 同一个 world_id 开不了两次 —— 同进程两个请求不行，两个进程也不行。
#      并发的两种事件顺序都要试，不是挑一种顺手的。
#   5. 拿不到适配器的那次创建，不许留下一个被锁住却没人跑的世界。
#   6. 关不干净就不许说自己关干净了，进程收尾也不许把一次不安全的释放
#      粉饰成一次干净的 checkpoint。
#   7. 配置重载动不了一个开着的持久世界的权威状态。
#   8. import 这个应用不建目录、不拿锁、不起运行时。
#   9. 既有的 /ws/run、审核、World Editor、配置重载路由一点没变。
#
# 运行: python -m unittest tests.test_persistent_world_api -v
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
# pns.interfaces.config 从 scripts/oobe.py 取 provider 表，跟 scripts/server.py
# 的启动上下文一致；测试在这里补上同一条路径，而不是给生产代码加一条捷径。
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from fastapi.testclient import TestClient  # noqa: E402

from pns.interfaces.app import create_app  # noqa: E402
from pns.interfaces.composition import (  # noqa: E402
    WORLD_ROOT_ENV,
    AdaptersUnavailable,
    ContentUnavailable,
    WorldControlPlane,
    default_world_root,
)
from pns.interfaces.persistent_worlds import WorldStatusModel, _safe  # noqa: E402
from pns.runtime.autonomy.coordinator import AutonomyError  # noqa: E402
from pns.runtime.persistence.lifecycle import LifecycleError  # noqa: E402
from pns.runtime.persistence.naming import WorldIdError  # noqa: E402
from pns.runtime.persistence.ownership import (  # noqa: E402
    WorldAlreadyOwned,
    owned_world_paths,
)
from pns.runtime.persistence.store import (  # noqa: E402
    ArchiveNotDurable,
    ArchiveNotFound,
    FileWorldStore,
    StorageError,
)
from pns.runtime.reload import BOUNDARY  # noqa: E402

SCENE = "nightcord"
CHARACTERS = ["mizuki", "ena"]
# 一把只在测试里存在、形状独一无二的"凭据"。任何一条响应里出现它，都说明
# 服务器侧的 API Key 从某条路径漏到了浏览器。
CANARY = "CANARY-SECRET-6f3a9c2e-DO-NOT-LEAK"


class _FakeModelClient:
    """一个绝不该被真的调用的模型客户端。

    WEB-1 的默认 Agency 策略是 AbstainPolicy，所以判分器在这些测试里一次都
    不会被调用。这个替身只是让"服务器侧凭据齐了"这条**产品**路径能在没有
    网络的地方跑完；任何一次真的调用都说明测试自己走错了路，所以它响亮失败。
    """

    def __getattr__(self, name):  # pragma: no cover - 被调用即测试有问题
        raise AssertionError(f"测试不该真的调用模型客户端（.{name}）")


class WorldApiTestCase(unittest.TestCase):
    """每个用例一个独立的存档根、一个独立的组装边界。"""

    def setUp(self):
        self.registry = BOUNDARY.active()
        self.assertIn(SCENE, self.registry.scenes, "内容包里没有测试用场景")
        for cid in CHARACTERS:
            self.assertTrue(self.registry.has_character(cid), cid)

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "worlds"
        # 判分器需要一份服务器侧凭据才造得出来。CI 上没有 .env，所以这里显式
        # 给一个，用的是注册表自己报出来的变量名，不写死。
        self._env = patch.dict(
            os.environ, {self.registry.models.key_name: "test-key-not-a-real-one"}
        )
        self._env.start()
        self.plane = WorldControlPlane(
            root=self.root, client_factory=lambda *a, **k: _FakeModelClient()
        )
        self.app = create_app(self.plane)
        # 不走 with：lifespan 的收尾关闭是单独一组用例的被测对象。
        self.client = TestClient(self.app)

    def tearDown(self):
        try:
            self.plane.service.release_all()
        finally:
            self._env.stop()
            self._tmp.cleanup()

    # ── 便捷 ────────────────────────────────────────────────────────────
    def create(self, world_id="nightcord", scene=SCENE, characters=None):
        return self.client.post(
            "/api/persistent-worlds",
            json={
                "world_id": world_id,
                "scene": scene,
                "characters": list(CHARACTERS if characters is None else characters),
            },
        )

    def open_world(self, world_id="nightcord"):
        response = self.create(world_id)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def detail(self, response):
        body = response.json()
        self.assertIn("detail", body, body)
        self.assertIsInstance(body["detail"], dict, body)
        return body["detail"]

    def category(self, response):
        return self.detail(response)["category"]

    def assert_no_canary(self, response):
        """整条响应里都不许出现那把 canary —— 正文、响应头、状态行都算。"""
        self.assertNotIn(CANARY, response.text)
        self.assertNotIn(CANARY, response.reason_phrase or "")
        for name, value in response.headers.items():
            self.assertNotIn(CANARY, name)
            self.assertNotIn(CANARY, value)

    def archive(self, world_id="nightcord"):
        return json.loads((self.root / world_id / "world.json").read_text("utf-8"))

    def lock_record(self, world_id="nightcord"):
        return json.loads((self.root / world_id / "OWNER.lock").read_text("utf-8"))


# ── AC1 最小操作回路 ─────────────────────────────────────────────────────
class OperatorLoopTests(WorldApiTestCase):
    def test_the_whole_loop_runs_end_to_end(self):
        self.assertEqual(
            self.client.get("/api/persistent-worlds").json(), {"worlds": []}
        )

        created = self.open_world()
        self.assertEqual(created["revision"], 1)
        self.assertTrue(created["owned"])
        self.assertTrue(created["running"])
        self.assertFalse(created["closed"])
        self.assertFalse(created["dirty"])
        self.assertTrue(created["durable"])

        listed = self.client.get("/api/persistent-worlds").json()["worlds"]
        self.assertEqual([w["world_id"] for w in listed], ["nightcord"])

        checkpointed = self.client.post("/api/persistent-worlds/nightcord/checkpoint")
        self.assertEqual(checkpointed.status_code, 200, checkpointed.text)
        self.assertEqual(checkpointed.json()["revision"], 2)
        self.assertEqual(self.archive()["revision"], 2)

        closed = self.client.post("/api/persistent-worlds/nightcord/close")
        self.assertEqual(closed.status_code, 200, closed.text)
        self.assertTrue(closed.json()["closed"])
        self.assertTrue(closed.json()["clean"])
        self.assertFalse(closed.json()["owned"])
        self.assertEqual(owned_world_paths(), ())

        restored = self.client.post("/api/persistent-worlds/nightcord/restore")
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["revision"], 3)
        self.assertEqual(restored.json()["session_id"], created["session_id"])
        self.assertTrue(restored.json()["owned"])
        # 从存档恢复出来的句柄没有携带可验证的目录同步证据，所以耐久性是
        # "不知道"，不是 True。
        self.assertIsNone(restored.json()["durable"])

    def test_a_world_that_was_never_created_is_a_404_not_an_empty_status(self):
        response = self.client.get("/api/persistent-worlds/nope")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.category(response), "archive_not_found")

    def test_status_of_an_open_world_reports_the_live_handle(self):
        self.open_world()
        status = self.client.get("/api/persistent-worlds/nightcord").json()
        self.assertTrue(status["owned"])
        self.assertEqual(status["owner"]["pid"], os.getpid())
        self.assertEqual(
            status["policy"],
            {
                "every_boundaries": None,
                "min_interval_seconds": 0.0,
                "on_close": True,
            },
        )

    def test_the_response_carries_no_credentials(self):
        self.open_world()
        for path in ("/api/persistent-worlds", "/api/persistent-worlds/nightcord"):
            body = self.client.get(path).text
            self.assertNotIn("test-key-not-a-real-one", body)
            self.assertNotIn("MIMO_API_KEY", body)
            self.assertNotIn("_FakeModelClient", body)


# ── AC2 创建不覆盖、恢复不兜底 ───────────────────────────────────────────
class CreateAndRestoreRefusalTests(WorldApiTestCase):
    def test_create_never_overwrites_an_existing_archive(self):
        first = self.open_world()
        self.client.post("/api/persistent-worlds/nightcord/close")

        again = self.create()
        self.assertEqual(again.status_code, 409, again.text)
        self.assertEqual(self.category(again), "archive_already_exists")
        # 存档里仍然是第一个世界，一个字都没被换掉。
        self.assertEqual(self.archive()["session_id"], first["session_id"])

    def test_create_on_an_open_world_is_a_conflict_not_a_second_owner(self):
        self.open_world()
        again = self.create()
        self.assertEqual(again.status_code, 409, again.text)
        self.assertEqual(self.category(again), "world_already_open")
        self.assertEqual(len(owned_world_paths()), 1)

    def test_restore_without_an_archive_does_not_invent_an_empty_world(self):
        response = self.client.post("/api/persistent-worlds/ghost/restore")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.category(response), "archive_not_found")
        self.assertFalse((self.root / "ghost" / "world.json").exists())
        self.assertEqual(owned_world_paths(), ())

    def test_restore_twice_is_a_conflict(self):
        self.open_world()
        self.client.post("/api/persistent-worlds/nightcord/close")
        self.assertEqual(
            self.client.post("/api/persistent-worlds/nightcord/restore").status_code,
            200,
        )
        again = self.client.post("/api/persistent-worlds/nightcord/restore")
        self.assertEqual(again.status_code, 409, again.text)
        self.assertEqual(self.category(again), "world_already_open")

    def test_an_unknown_scene_is_refused_instead_of_silently_becoming_the_default(self):
        response = self.create(scene="no-such-scene")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.category(response), "invalid_content")
        # 拒绝发生在拿所有权之前：磁盘上什么都没留下。
        self.assertFalse(self.root.exists())

    def test_unknown_and_duplicate_characters_are_refused(self):
        unknown = self.create(characters=["mizuki", "not-a-character"])
        self.assertEqual(unknown.status_code, 400, unknown.text)
        self.assertEqual(self.category(unknown), "invalid_content")

        duplicated = self.create(characters=["mizuki", "mizuki"])
        self.assertEqual(duplicated.status_code, 400, duplicated.text)
        self.assertEqual(self.category(duplicated), "invalid_content")
        self.assertFalse(self.root.exists())

    def test_the_browser_cannot_choose_the_session_identity_or_the_archive_path(self):
        created = self.open_world()
        # session_id 由服务器生成，请求体里没有这个字段可传。
        self.assertTrue(created["session_id"].startswith("nightcord_"))
        # 存档路径永远落在服务器配置的存档根之下。
        self.assertEqual(
            Path(created["archive_path"]).parent.parent.resolve(), self.root.resolve()
        )
        # 多余字段不会被吞下去当成配置。
        response = self.client.post(
            "/api/persistent-worlds",
            json={
                "world_id": "extra",
                "scene": SCENE,
                "characters": CHARACTERS,
                "archive_root": "/tmp/anywhere",
                "session_id": "attacker-chosen",
                "api_key": "leak-me",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertNotEqual(body["session_id"], "attacker-chosen")
        self.assertTrue(body["archive_path"].startswith(str(self.root)))


# ── AC3 ID 与路径安全 ────────────────────────────────────────────────────
class WorldIdSafetyTests(WorldApiTestCase):
    BAD_IDS = (
        "..",
        "../etc",
        "..%2Fetc",
        "%2e%2e%2fetc",
        "/etc/passwd",
        "Nightcord",  # 大小写不敏感的文件系统上会跟 nightcord 撞成同一个目录
        "café",
        "night cord",
        "nightcord.",
        ".hidden",
        "-rf",
        "n" * 65,
    )

    def test_dangerous_ids_are_refused_on_create(self):
        for bad in self.BAD_IDS:
            with self.subTest(world_id=bad):
                response = self.create(world_id=bad)
                self.assertIn(response.status_code, (400, 422), response.text)
                if response.status_code == 400:
                    self.assertEqual(self.category(response), "invalid_world_id")
        self.assertFalse(self.root.exists())

    def test_dangerous_ids_are_refused_on_every_action(self):
        """每条动作路由都要拒，而且拒完磁盘上一点痕迹都没有。

        `..` 和 `..%2F..` 这类会在到达服务器之前就被 URL 规范化掉（客户端、
        代理、ASGI 服务器都会做），于是它们落在一条根本不存在的路由上、拿到
        405。那也是一次安全的拒绝，但它证明的是**别人**挡住了，所以真正的
        判据在下一个用例：直接问组装边界。
        """
        for bad in ("..", "..%2Fetc", "Nightcord", "café", "nightcord."):
            for action in ("restore", "checkpoint", "close"):
                with self.subTest(world_id=bad, action=action):
                    response = self.client.post(
                        f"/api/persistent-worlds/{bad}/{action}"
                    )
                    self.assertIn(
                        response.status_code, (400, 404, 405, 422), response.text
                    )
                    if response.status_code == 400:
                        self.assertEqual(self.category(response), "invalid_world_id")
        self.assertEqual(owned_world_paths(), ())
        self.assertFalse(self.root.exists())

    def test_the_composition_boundary_itself_refuses_dangerous_ids(self):
        """不依赖任何 URL 规范化：直接拿脏 ID 去问组装边界。

        这一层不许自己写一套 ID 或路径规则 —— 它调的就是 P12 的
        `validate_world_id`，所以下面每一个都在造出任何路径**之前**就断掉。
        """
        for bad in self.BAD_IDS + (
            "../../etc/passwd",
            "nightcord/../../x",
            "a\\b",
            "a\x00b",
        ):
            with self.subTest(world_id=bad):
                with self.assertRaises(WorldIdError):
                    self.plane.create(
                        world_id=bad, scene_id=SCENE, character_ids=CHARACTERS
                    )
                with self.assertRaises(WorldIdError):
                    self.plane.restore(bad)
                with self.assertRaises(WorldIdError):
                    self.plane.checkpoint(bad)
                with self.assertRaises(WorldIdError):
                    self.plane.close(bad)
                with self.assertRaises(WorldIdError):
                    self.plane.status(bad)
        self.assertFalse(self.root.exists())
        self.assertEqual(owned_world_paths(), ())

    def test_ids_that_normalize_alike_stay_two_different_refusals(self):
        self.open_world("nightcord")
        # 归一化会把这两个变成同一个世界；P12 的做法是拒绝，不是悄悄合并。
        for twin in ("Nightcord", "NIGHTCORD"):
            response = self.create(world_id=twin)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(self.category(response), "invalid_world_id")


# ── AC4 坏存档 ───────────────────────────────────────────────────────────
class BrokenArchiveTests(WorldApiTestCase):
    def _write_archive(self, payload, world_id="nightcord"):
        path = self.root / world_id / "world.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            payload if isinstance(payload, str) else json.dumps(payload),
            encoding="utf-8",
        )

    def test_a_truncated_archive_reads_as_broken_and_refuses_to_restore(self):
        self.open_world()
        self.client.post("/api/persistent-worlds/nightcord/close")
        blob = (self.root / "nightcord" / "world.json").read_text("utf-8")
        self._write_archive(blob[: len(blob) // 2])

        status = self.client.get("/api/persistent-worlds/nightcord")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertIsNotNone(status.json()["error"])
        self.assertIsNone(status.json()["revision"])

        restore = self.client.post("/api/persistent-worlds/nightcord/restore")
        self.assertEqual(restore.status_code, 422, restore.text)
        self.assertEqual(self.category(restore), "archive_corrupt")
        self.assertEqual(owned_world_paths(), ())

    def test_an_unsupported_version_is_refused_not_best_effort(self):
        self.open_world()
        self.client.post("/api/persistent-worlds/nightcord/close")
        payload = self.archive()
        payload["version"] = 999
        self._write_archive(payload)

        restore = self.client.post("/api/persistent-worlds/nightcord/restore")
        self.assertEqual(restore.status_code, 422, restore.text)
        self.assertEqual(self.category(restore), "archive_unusable")

    def test_an_archive_with_the_wrong_identity_is_refused(self):
        self.open_world()
        self.client.post("/api/persistent-worlds/nightcord/close")
        payload = self.archive()
        payload["world_id"] = "someone-else"
        self._write_archive(payload)

        restore = self.client.post("/api/persistent-worlds/nightcord/restore")
        self.assertEqual(restore.status_code, 422, restore.text)
        self.assertEqual(owned_world_paths(), ())

    def test_a_broken_archive_still_shows_up_in_the_list(self):
        self.open_world()
        self.client.post("/api/persistent-worlds/nightcord/close")
        self._write_archive("{ not json")
        worlds = self.client.get("/api/persistent-worlds").json()["worlds"]
        self.assertEqual([w["world_id"] for w in worlds], ["nightcord"])
        self.assertIsNotNone(worlds[0]["error"])

    def test_a_hostile_archive_cannot_flood_the_error_body(self):
        """存档里的字段会原样出现在校验报错里，所以它能把一条响应撑到多大，
        取决于**存档**，不取决于代码。这里把身份撑到 20 万字符再看。"""
        self.open_world()
        self.client.post("/api/persistent-worlds/nightcord/close")
        payload = self.archive()
        payload["session_id"] = "x" * 200_000
        self._write_archive(payload)

        restore = self.client.post("/api/persistent-worlds/nightcord/restore")
        self.assertEqual(restore.status_code, 422, restore.text)
        message = self.detail(restore)["message"]
        self.assertGreater(len("x" * 200_000), len(message))
        self.assertLessEqual(len(message), 400)
        self.assertLess(len(restore.content), 4096)

    def test_error_messages_are_single_lined_and_bounded(self):
        long_line = "行一\n\t行二   行三 " + "y" * 5000
        safe = _safe(long_line)
        self.assertLessEqual(len(safe), 400)
        self.assertNotIn("\n", safe)
        self.assertNotIn("\t", safe)
        self.assertTrue(safe.startswith("行一 行二 行三 "))


# ── AC5 并发：两种事件顺序都要试 ─────────────────────────────────────────
def _race(first, second):
    """让两个可调用对象尽量在同一瞬间进入被测代码，返回 (结果, 异常) 两对。

    用栅栏而不是 sleep：sleep 只是把竞态推迟，栅栏才是"两边都到齐了再放行"。
    """
    outcomes = [None, None]
    barrier = threading.Barrier(2)

    def run(index, fn):
        barrier.wait()
        try:
            outcomes[index] = ("ok", fn())
        except Exception as e:  # 竞态的输家就该拿到异常
            outcomes[index] = ("error", e)

    threads = [
        threading.Thread(target=run, args=(0, first)),
        threading.Thread(target=run, args=(1, second)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    for thread in threads:
        assert not thread.is_alive(), "并发用例超时：有人挂在锁上没回来"
    return outcomes


class ConcurrencyTests(WorldApiTestCase):
    """所有权是**互斥**，不是一次检查。两边都要试，不挑顺手的那一种。"""

    def _create(self, world_id="nightcord"):
        return lambda: self.plane.create(
            world_id=world_id, scene_id=SCENE, character_ids=CHARACTERS
        )

    def _restore(self, world_id="nightcord"):
        return lambda: self.plane.restore(world_id)

    def _winners(self, outcomes):
        return [value for kind, value in outcomes if kind == "ok"]

    def assert_no_two_owners(self, outcomes):
        self.assertLessEqual(len(self._winners(outcomes)), 1, outcomes)
        # 输家拿到的必须是"这个世界已经有人了"或"存档已经在了"这类冲突，
        # 不能是一个说不清的内部错误。
        for kind, value in outcomes:
            if kind == "error":
                self.assertIsInstance(
                    value, (WorldAlreadyOwned, LifecycleError, ArchiveNotFound), value
                )

    def test_two_concurrent_creates_produce_exactly_one_world(self):
        outcomes = _race(self._create(), self._create())
        winners = self._winners(outcomes)
        self.assertEqual(len(winners), 1, outcomes)
        self.assert_no_two_owners(outcomes)
        self.assertEqual(winners[0]["revision"], 1)
        self.assertEqual(self.archive()["revision"], 1)
        self.assertEqual(len(owned_world_paths()), 1)

    def test_two_concurrent_creates_over_http_produce_exactly_one_world(self):
        results = _race(lambda: self.create(), lambda: self.create())
        codes = sorted(value.status_code for kind, value in results if kind == "ok")
        self.assertEqual(codes, [201, 409], [r[1].text for r in results])
        self.assertEqual(self.archive()["revision"], 1)

    def test_create_racing_restore_never_yields_two_owners_in_either_order(self):
        for first, second in (
            (self._create(), self._restore()),
            (self._restore(), self._create()),
        ):
            with self.subTest(order=(first, second)):
                outcomes = _race(first, second)
                self.assert_no_two_owners(outcomes)
                self.plane.service.release_all()
                # 下一轮从干净的磁盘开始。
                for child in sorted(self.root.rglob("world.json")):
                    child.unlink()

    def test_two_concurrent_restores_yield_exactly_one_owner_in_either_order(self):
        self.open_world()
        self.plane.close("nightcord")
        for _ in range(2):
            outcomes = _race(self._restore(), self._restore())
            self.assertEqual(len(self._winners(outcomes)), 1, outcomes)
            self.assert_no_two_owners(outcomes)
            self.assertEqual(len(owned_world_paths()), 1)
            self.plane.close("nightcord")

    def test_two_concurrent_checkpoints_both_land_and_neither_reuses_a_revision(self):
        self.open_world()
        outcomes = _race(
            lambda: self.plane.checkpoint("nightcord"),
            lambda: self.plane.checkpoint("nightcord"),
        )
        revisions = sorted(
            value["revision"] for kind, value in outcomes if kind == "ok"
        )
        self.assertEqual(revisions, [2, 3], outcomes)
        self.assertEqual(self.archive()["revision"], 3)

    def test_checkpoint_racing_close_never_reports_a_clean_close_over_a_lost_write(
        self,
    ):
        for _ in range(2):
            self.open_world()
            outcomes = _race(
                lambda: self.plane.checkpoint("nightcord"),
                lambda: self.plane.close("nightcord"),
            )
            statuses = {}
            for (kind, value), label in zip(outcomes, ("checkpoint", "close")):
                statuses[label] = (kind, value)
            kind, value = statuses["close"]
            if kind == "ok":
                # 关干净了 → 磁盘上就是它报出来的那一版，不多不少。
                self.assertTrue(value["clean"], value)
                self.assertEqual(self.archive()["revision"], value["revision"])
            else:
                self.assertIsInstance(value, LifecycleError, value)
            kind, value = statuses["checkpoint"]
            if kind == "ok":
                self.assertLessEqual(value["revision"], self.archive()["revision"])
            else:
                self.assertIsInstance(value, LifecycleError, value)
            self.plane.service.release_all()
            (self.root / "nightcord" / "world.json").unlink()

    def test_a_stale_browser_action_gets_a_conflict_not_a_silent_success(self):
        self.open_world()
        # 另一个请求把它关了；浏览器还以为它开着。
        self.client.post("/api/persistent-worlds/nightcord/close")
        for action in ("checkpoint", "close"):
            with self.subTest(action=action):
                response = self.client.post(
                    f"/api/persistent-worlds/nightcord/{action}"
                )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self.category(response), "world_not_open")

    def test_closing_twice_is_a_conflict_not_a_second_clean_close(self):
        self.open_world()
        first = self.client.post("/api/persistent-worlds/nightcord/close")
        self.assertEqual(first.status_code, 200, first.text)
        second = self.client.post("/api/persistent-worlds/nightcord/close")
        self.assertEqual(second.status_code, 409, second.text)


# ── AC6 另一个进程持有这个世界 ───────────────────────────────────────────
_HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from pns.runtime.persistence.store import FileWorldStore
store = FileWorldStore(sys.argv[2])
handle = store.acquire(sys.argv[3])
print("held", flush=True)
sys.stdin.readline()
"""


class ForeignOwnerTests(WorldApiTestCase):
    def _hold(self, world_id="nightcord"):
        """在另一个**真实进程**里拿住这个世界的锁。

        进程内注册表挡不住别的进程；判据必须是内核给的那把文件锁，所以这里
        非用子进程不可。
        """
        holder = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(REPO_ROOT), str(self.root), world_id],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._reap, holder)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        return holder

    @staticmethod
    def _reap(holder):
        if holder.poll() is None:
            holder.kill()
        holder.wait(30)
        for stream in (holder.stdin, holder.stdout):
            if stream is not None:
                stream.close()

    def test_a_world_owned_by_another_process_cannot_be_restored_here(self):
        self.open_world()
        self.plane.close("nightcord")

        self._hold()
        response = self.client.post("/api/persistent-worlds/nightcord/restore")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.category(response), "world_already_open")
        # 抢不到就是抢不到：本进程手上一把锁都没有。
        self.assertEqual(owned_world_paths(), ())
        create = self.create()
        self.assertEqual(create.status_code, 409, create.text)

    def test_after_the_foreign_owner_crashes_the_takeover_is_reported(self):
        self.open_world()
        self.plane.close("nightcord")
        holder = self._hold()
        holder.kill()
        holder.wait(30)

        response = self.client.post("/api/persistent-worlds/nightcord/restore")
        self.assertEqual(response.status_code, 200, response.text)
        # 接管的是一个崩掉的拥有者，这件事要如实报出来。
        self.assertIsNotNone(response.json()["recovered_from"])


# ── AC7 适配器缺席与绑定失败 ─────────────────────────────────────────────
class AdapterTests(WorldApiTestCase):
    def test_a_missing_api_key_refuses_before_taking_any_ownership(self):
        with patch.dict(os.environ, {self.registry.models.key_name: ""}):
            response = self.create()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.category(response), "adapters_unavailable")
        # 要害在这里：造不出适配器的那次创建，绝不许留下一个锁着却没人跑的世界。
        self.assertFalse(self.root.exists())
        self.assertEqual(owned_world_paths(), ())

    def test_a_client_that_cannot_be_built_refuses_before_taking_any_ownership(self):
        def explode(*args, **kwargs):
            raise RuntimeError("provider 配置坏了")

        plane = WorldControlPlane(root=self.root, client_factory=explode)
        client = TestClient(create_app(plane))
        response = client.post(
            "/api/persistent-worlds",
            json={"world_id": "nightcord", "scene": SCENE, "characters": CHARACTERS},
        )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.category(response), "adapters_unavailable")
        self.assertFalse(self.root.exists())
        self.assertEqual(owned_world_paths(), ())

    def test_a_client_factory_failure_never_reflects_the_api_key_back(self):
        """客户端工厂**收到过** API Key，所以它抛出来的话可能原样带着那把 key。

        `provider rejected sk-…` 是真实会发生的形状。这条边界只允许异常的
        **类型名**出去 —— 类型名装不下一把 key，而原文一旦进了 503 的正文，
        就等于把服务器侧凭据发给了浏览器。
        """

        def explode(api_key, **kwargs):
            raise RuntimeError(f"provider rejected {api_key}")

        with patch.dict(os.environ, {self.registry.models.key_name: CANARY}):
            plane = WorldControlPlane(root=self.root, client_factory=explode)
            response = TestClient(create_app(plane)).post(
                "/api/persistent-worlds",
                json={
                    "world_id": "nightcord",
                    "scene": SCENE,
                    "characters": CHARACTERS,
                },
            )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.category(response), "adapters_unavailable")
        self.assert_no_canary(response)
        self.assertEqual(
            self.detail(response)["message"],
            "判分模型客户端建不起来；请检查服务器侧的 provider 与凭据配置",
        )
        # 而且这一档在拿所有权之前就断掉了。
        self.assertFalse(self.root.exists())
        self.assertEqual(owned_world_paths(), ())

    def test_an_exception_type_name_is_untrusted_data_too(self):
        """连**类型名**都不能过边界。

        Python 不校验类名，所以工厂可以现造一个名字就是那把 key 的异常类：

            raise type(api_key, (RuntimeError,), {})("rejected")

        于是 `type(e).__name__` 就是凭据本身。这就是为什么对外那句话必须是
        完全固定的 —— 只要还有任何一处从异常派生的数据能出去，这条边界就还
        是漏的。
        """

        def explode(api_key, **kwargs):
            raise type(api_key, (RuntimeError,), {})("rejected")

        with patch.dict(os.environ, {self.registry.models.key_name: CANARY}):
            plane = WorldControlPlane(root=self.root, client_factory=explode)
            response = TestClient(create_app(plane)).post(
                "/api/persistent-worlds",
                json={
                    "world_id": "nightcord",
                    "scene": SCENE,
                    "characters": CHARACTERS,
                },
            )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.category(response), "adapters_unavailable")
        self.assert_no_canary(response)
        self.assertFalse(self.root.exists())
        self.assertEqual(owned_world_paths(), ())

    def test_the_visible_message_is_fixed_whatever_the_factory_raises(self):
        """无论工厂怎么炸，对外那句话逐字相同 —— 没有任何一处随异常变化。"""
        shapes = {
            "消息带 key": lambda k: RuntimeError(f"rejected {k}"),
            "类型名是 key": lambda k: type(k, (RuntimeError,), {})("rejected"),
            "参数里带 key": lambda k: ValueError(k, "rejected"),
            "自定义 __str__": lambda k: type(
                "Weird", (RuntimeError,), {"__str__": lambda self: k}
            )(),
        }
        messages = set()
        for label, make in shapes.items():
            with self.subTest(shape=label):

                def explode(api_key, _make=make, **kwargs):
                    raise _make(api_key)

                with patch.dict(os.environ, {self.registry.models.key_name: CANARY}):
                    plane = WorldControlPlane(root=self.root, client_factory=explode)
                    response = TestClient(create_app(plane)).post(
                        "/api/persistent-worlds",
                        json={
                            "world_id": "nightcord",
                            "scene": SCENE,
                            "characters": CHARACTERS,
                        },
                    )
                self.assertEqual(response.status_code, 503, response.text)
                self.assert_no_canary(response)
                messages.add(self.detail(response)["message"])

        self.assertEqual(len(messages), 1, messages)

    def test_the_original_client_failure_stays_available_for_server_side_diagnosis(
        self,
    ):
        """对外不说，不等于把它扔掉：原始异常留在 __cause__ 里。"""

        def explode(api_key, **kwargs):
            raise RuntimeError(f"provider rejected {api_key}")

        plane = WorldControlPlane(root=self.root, client_factory=explode)
        with patch.dict(os.environ, {self.registry.models.key_name: CANARY}):
            with self.assertRaises(AdaptersUnavailable) as caught:
                plane.build_adapters(BOUNDARY.active())

        self.assertNotIn(CANARY, str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertIn(CANARY, str(caught.exception.__cause__))

    def test_no_failure_path_on_this_prefix_reflects_the_api_key(self):
        """相邻失败路径的一次清扫。

        Finding 1 修的是客户端工厂那一处，但"凭据会不会从某条错误路径漏出去"
        不该靠每次读代码来回答。这里把这条前缀上能触发的失败挨个走一遍，断言
        canary 一次都不出现 —— 以后谁在任何一档里插进 `{e}`，这里就会红。
        """

        def explode(api_key, **kwargs):
            raise RuntimeError(f"provider rejected {api_key}")

        with patch.dict(os.environ, {self.registry.models.key_name: CANARY}):
            # 先用一个正常的工厂建出一个真实世界，再逐个撞失败路径。
            client = self.client
            self.open_world()
            probes = [
                ("重复创建", lambda: self.create()),
                ("非法 ID", lambda: self.create(world_id="../etc")),
                ("未知场景", lambda: self.create(world_id="other", scene="nope")),
                (
                    "未知角色",
                    lambda: self.create(world_id="other", characters=["nope"]),
                ),
                (
                    "恢复不存在的世界",
                    lambda: client.post("/api/persistent-worlds/ghost/restore"),
                ),
                ("查不存在的世界", lambda: client.get("/api/persistent-worlds/ghost")),
                ("列表", lambda: client.get("/api/persistent-worlds")),
                ("状态", lambda: client.get("/api/persistent-worlds/nightcord")),
            ]
            for label, probe in probes:
                with self.subTest(path=label):
                    self.assert_no_canary(probe())

            # 适配器造不出来的两种：消息带 key，以及**类型名**就是 key。
            def explode_by_type(api_key, **kwargs):
                raise type(api_key, (RuntimeError,), {})("rejected")

            for factory in (explode, explode_by_type):
                broken = TestClient(
                    create_app(
                        WorldControlPlane(root=self.root, client_factory=factory)
                    )
                )
                self.assert_no_canary(
                    broken.post(
                        "/api/persistent-worlds",
                        json={
                            "world_id": "other",
                            "scene": SCENE,
                            "characters": CHARACTERS,
                        },
                    )
                )

    def test_a_binding_failure_after_ownership_releases_the_world_again(self):
        with patch(
            "pns.runtime.persistence.lifecycle.RuntimeAdapters.bind",
            side_effect=AutonomyError("判分器绑不上去"),
        ):
            response = self.create()
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(self.category(response), "adapter_binding_failed")
        # 一次失败的创建不该让世界永久锁死。
        self.assertEqual(owned_world_paths(), ())
        self.assertFalse((self.root / "nightcord" / "world.json").exists())
        # 而且它没把这个 ID 用掉：修好之后照样能建。
        self.assertEqual(self.create().status_code, 201)

    def test_the_default_policy_is_abstain_and_the_auditor_is_the_real_one(self):
        """WEB-1 不塞占位策略：没有生成层的世界诚实地什么都不做。"""
        adapters = self.plane.build_adapters(self.registry)
        self.assertIsNone(adapters.policy_factory)
        self.assertEqual(adapters.auditor.name, "router")
        world = self.plane.service.opened("nightcord")
        self.assertIsNone(world)
        self.open_world()
        engine = self.plane.service.opened("nightcord").state.agency_engine
        self.assertEqual(engine.policy.name, "abstain")


# ── AC8 存储失败 ─────────────────────────────────────────────────────────
class StorageFailureTests(WorldApiTestCase):
    def test_a_failed_checkpoint_is_a_500_and_leaves_the_previous_archive_intact(self):
        self.open_world()
        with patch.object(FileWorldStore, "save", side_effect=StorageError("磁盘满了")):
            response = self.client.post("/api/persistent-worlds/nightcord/checkpoint")
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(self.category(response), "checkpoint_failed")
        # 磁盘上仍然是上一版，而且世界仍然开着、仍然属于本进程。
        self.assertEqual(self.archive()["revision"], 1)
        status = self.client.get("/api/persistent-worlds/nightcord").json()
        self.assertEqual(status["revision"], 1)
        self.assertTrue(status["owned"])
        self.assertFalse(status["closed"])
        self.assertIsNotNone(status["last_error"])

    def test_a_failed_close_does_not_claim_a_clean_close_or_hand_back_the_world(self):
        self.open_world()
        with patch.object(FileWorldStore, "save", side_effect=StorageError("磁盘满了")):
            response = self.client.post("/api/persistent-worlds/nightcord/close")
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(self.category(response), "checkpoint_failed")
        status = self.client.get("/api/persistent-worlds/nightcord").json()
        self.assertTrue(status["owned"], "存不下去的世界不许把所有权还回去")
        self.assertFalse(status["closed"])
        self.assertEqual(len(owned_world_paths()), 1)
        # 修好之后再关一次，这次才算干净。
        good = self.client.post("/api/persistent-worlds/nightcord/close")
        self.assertEqual(good.status_code, 200, good.text)
        self.assertTrue(good.json()["clean"])

    def test_an_unprovable_durability_is_not_reported_as_a_successful_checkpoint(self):
        self.open_world()
        with patch.object(
            FileWorldStore,
            "save",
            side_effect=ArchiveNotDurable("目录同步失败", revision=2, path="x"),
        ):
            response = self.client.post("/api/persistent-worlds/nightcord/checkpoint")
        self.assertEqual(response.status_code, 500, response.text)
        # P12 会把这一档包进 CheckpointError —— 它必须抛，否则这次 checkpoint
        # 会被当成干净的。但"写下去了、只是保证不到"和"根本没写下去"是两种不同
        # 的后续动作，所以边界必须保住这个区分。
        self.assertEqual(self.category(response), "archive_not_durable")
        status = self.client.get("/api/persistent-worlds/nightcord").json()
        self.assertFalse(status["durable"])
        self.assertFalse(status["directory_synced"])
        # 修订号照常往前走：那一版**已经**在磁盘上了，下一次不许重用这个号。
        self.assertEqual(status["revision"], 2)


# ── AC9 进程收尾 ─────────────────────────────────────────────────────────
class ShutdownTests(WorldApiTestCase):
    def test_a_clean_world_is_checkpointed_and_released_on_shutdown(self):
        with TestClient(self.app) as client:
            created = client.post(
                "/api/persistent-worlds",
                json={
                    "world_id": "nightcord",
                    "scene": SCENE,
                    "characters": CHARACTERS,
                },
            ).json()
            self.assertEqual(created["revision"], 1)
        self.assertIsNone(self.plane.service.opened("nightcord"))
        self.assertEqual(owned_world_paths(), ())
        self.assertEqual(self.archive()["revision"], 2)
        self.assertEqual(self.lock_record()["state"], "released")

    def test_unsaved_work_is_checkpointed_on_shutdown(self):
        with TestClient(self.app) as client:
            client.post(
                "/api/persistent-worlds",
                json={
                    "world_id": "nightcord",
                    "scene": SCENE,
                    "characters": CHARACTERS,
                },
            )
            world = self.plane.service.opened("nightcord")
            world.runtime.advance(90)
            advanced = world.state.world_state.clock.isoformat()
            self.assertTrue(world.status()["dirty"])
        self.assertEqual(self.archive()["clock"], advanced)

    def test_a_world_that_cannot_be_saved_is_not_relabelled_as_a_clean_exit(self):
        """收尾失败时**不**调用 release()。

        release() 会把锁记录写成 "released"，也就是向下一个拥有者宣布"上一个
        是干净走的"。最后一次 checkpoint 都没成的世界不配这句话 —— 下一个
        拥有者必须能从 recovered_from 看出它接的是一个没收好的世界。
        """
        saver = patch.object(
            FileWorldStore, "save", side_effect=StorageError("磁盘满了")
        )
        with TestClient(self.app) as client:
            client.post(
                "/api/persistent-worlds",
                json={
                    "world_id": "nightcord",
                    "scene": SCENE,
                    "characters": CHARACTERS,
                },
            )
            saver.start()
        # 收尾发生在退出 with 的那一刻，此时 save 还是坏的。
        try:
            self.assertIsNotNone(
                self.plane.service.opened("nightcord"),
                "关不掉的世界不许从账上消失",
            )
            self.assertEqual(len(owned_world_paths()), 1)
            self.assertEqual(self.lock_record()["state"], "held")
        finally:
            saver.stop()

    def test_the_shutdown_report_tells_the_truth_about_each_world(self):
        self.open_world("clean-world")
        self.open_world("stuck-world")
        with patch.object(
            FileWorldStore,
            "save",
            autospec=True,
            side_effect=_only_fails_for("stuck-world"),
        ):
            reports = {item["world_id"]: item for item in self.plane.shutdown()}
        self.assertTrue(reports["clean-world"]["clean"])
        self.assertTrue(reports["clean-world"]["closed"])
        self.assertFalse(reports["stuck-world"]["clean"])
        self.assertFalse(reports["stuck-world"]["closed"])
        # P12 把存储失败包成 CheckpointError；报告要带着原文，不许抹平成"失败"。
        self.assertIn("CheckpointError", reports["stuck-world"]["error"])
        self.assertIn("磁盘满了", reports["stuck-world"]["error"])

    def test_shutdown_is_idempotent_and_survives_an_empty_process(self):
        self.assertEqual(self.plane.shutdown(), [])
        self.open_world()
        self.assertEqual(len(self.plane.shutdown()), 1)
        self.assertEqual(self.plane.shutdown(), [])


def _only_fails_for(world_id):
    """一个只对某一个世界失败的 save，用来证明收尾报告是逐个世界说实话的。"""
    real = FileWorldStore.save

    def save(self, archive):
        if archive.world_id == world_id:
            raise StorageError("磁盘满了")
        return real(self, archive)

    return save


# ── AC10 配置重载 × 持久世界 ─────────────────────────────────────────────
class ConfigReloadBoundaryTests(WorldApiTestCase):
    """P7 × P12 的接缝，WEB-1 在这里定死。

    重载不被持久世界拒绝，也影响不到已经开着的持久世界：它换掉的是全局那份
    内容快照（喂**将来**的冷构造），动不到任何一份已经存在的权威状态。
    """

    def test_a_reload_does_not_touch_an_open_world(self):
        created = self.open_world()
        world = self.plane.service.opened("nightcord")
        world.runtime.advance(45)
        before_clock = world.state.world_state.clock.isoformat()
        before_events = len(world.state.events)

        result = BOUNDARY.reload("WEB-1 测试")
        self.assertEqual(result.status, "ok", result.error)

        after = self.client.get("/api/persistent-worlds/nightcord").json()
        self.assertTrue(after["owned"])
        self.assertTrue(after["running"])
        self.assertIsNone(after["stop_reason"])
        self.assertEqual(after["clock"], before_clock)
        self.assertEqual(after["session_id"], created["session_id"])
        self.assertEqual(len(world.state.events), before_events)

    def test_a_persistent_world_is_not_registered_as_a_research_session(self):
        """登记到 SessionSupervisor 上的话，一次配置重载就能掐掉一个世界。"""
        created = self.open_world()
        live = BOUNDARY.supervisor.live_session_ids()
        self.assertNotIn(created["session_id"], live)
        self.assertNotIn("nightcord", live)
        result = BOUNDARY.reload("WEB-1 测试")
        self.assertNotIn(created["session_id"], result.stopped_sessions)

    def test_a_reload_is_not_refused_while_a_world_is_open(self):
        self.open_world()
        self.assertEqual(self.client.get("/api/config/reload").status_code, 200)
        response = self.client.post("/api/config/reload")
        self.assertEqual(response.status_code, 200, response.text)


# ── AC11 既有接口没被动过 ────────────────────────────────────────────────
class ExistingSurfaceTests(WorldApiTestCase):
    def test_the_existing_routes_still_answer_and_take_no_world_lock(self):
        self.open_world()
        held = owned_world_paths()
        for path in (
            "/api/review/turns",
            "/api/review/decisions",
            "/api/config",
            "/api/config/providers",
            "/api/config/reload",
            "/api/world/scenes",
            "/api/world/facts",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(owned_world_paths(), held)

    def test_ws_run_neither_opens_nor_joins_a_persistent_world(self):
        """研究会话是另一条路：不拿世界锁、不写存档根、不加入这个生命周期。"""
        self.open_world()
        before = set(owned_world_paths())
        with self.client.websocket_connect("/ws/run") as ws:
            ws.send_json({"characters": ["mizuki"], "max_turns": 1})
            message = ws.receive_json()
        # 参数不合法就被拒；关键不是它跑不跑得起来，而是它一路上什么都没碰。
        self.assertEqual(message["type"], "error")
        self.assertEqual(set(owned_world_paths()), before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["nightcord"])

    def test_the_new_prefix_did_not_move_any_existing_route(self):
        """既有 URL 一条都不许因为新前缀而改变形状。"""
        paths = set(self.app.openapi()["paths"])
        for expected in (
            "/api/review/turns",
            "/api/review/decisions",
            "/api/review/decision",
            "/api/config",
            "/api/config/providers",
            "/api/config/reload",
            "/api/world/scenes",
            "/api/world/scenes/source",
            "/api/world/facts",
            "/api/world/facts/source",
        ):
            self.assertIn(expected, paths)
        for expected in (
            "/api/persistent-worlds",
            "/api/persistent-worlds/{world_id}",
            "/api/persistent-worlds/{world_id}/restore",
            "/api/persistent-worlds/{world_id}/checkpoint",
            "/api/persistent-worlds/{world_id}/close",
        ):
            self.assertIn(expected, paths)
        # 新前缀只多了这六条，一条不多。
        self.assertEqual(
            len([p for p in paths if p.startswith("/api/persistent-worlds")]), 5
        )


# ── AC12 返回的是脱钩的数据 ──────────────────────────────────────────────
class DetachedPayloadTests(WorldApiTestCase):
    def test_mutating_a_response_cannot_reach_the_running_world(self):
        self.open_world()
        body = self.client.get("/api/persistent-worlds/nightcord").json()
        body["revision"] = 999
        body["owned"] = False
        body["residue"].append("/etc/passwd")
        body["policy"]["on_close"] = False

        again = self.client.get("/api/persistent-worlds/nightcord").json()
        self.assertEqual(again["revision"], 1)
        self.assertTrue(again["owned"])
        self.assertEqual(again["residue"], [])
        self.assertTrue(again["policy"]["on_close"])
        self.assertTrue(self.plane.checkpoint_policy.on_close)

    def test_the_status_dict_handed_out_by_the_plane_is_a_fresh_structure(self):
        self.open_world()
        first = self.plane.status("nightcord")
        first["revision"] = 999
        first["residue"].append("x")
        second = self.plane.status("nightcord")
        self.assertEqual(second["revision"], 1)
        self.assertEqual(second["residue"], [])
        self.assertIsNot(first, second)


class StatusVocabularyTests(WorldApiTestCase):
    """接口层照抄 P12 的状态词汇，一个字段都不改名、不丢。

    悄悄丢一个字段是最容易发生的漂移：pydantic 默认忽略多余字段，所以 P12
    以后加一个诊断字段，这边只会安静地不显示它。让那件事在**测试里**响，
    而不是在生产里变成一个没人看得见的空白。
    """

    def test_the_response_model_matches_the_p12_status_vocabulary(self):
        self.open_world()
        live = set(self.plane.service.opened("nightcord").status())
        self.assertEqual(set(WorldStatusModel.model_fields), live)

        self.plane.close("nightcord")
        cold = set(self.plane.status("nightcord"))
        self.assertEqual(set(WorldStatusModel.model_fields), cold)

    def test_the_json_body_carries_the_same_keys_as_the_lifecycle_status(self):
        self.open_world()
        live = self.plane.service.opened("nightcord").status()
        body = self.client.get("/api/persistent-worlds/nightcord").json()
        self.assertEqual(set(body), set(live))


class CreateDurabilityTests(WorldApiTestCase):
    def test_a_create_whose_durability_cannot_be_proven_is_not_reported_as_success(
        self,
    ):
        """第一份存档写下去了、但目录同步证实不了。

        这一档不是"没建成"：存档**在磁盘上**。所以对外必须说清楚它不干净，
        而且不许把这个世界留在一个锁着却没人跑的状态里。
        """
        real = FileWorldStore.save

        def save(store, archive):
            real(store, archive)
            raise ArchiveNotDurable("目录同步失败", revision=archive.revision, path="x")

        with patch.object(FileWorldStore, "save", autospec=True, side_effect=save):
            response = self.create()
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(self.category(response), "archive_not_durable")
        # 所有权还回去了：一次失败的创建不该让世界永久锁死。
        self.assertEqual(owned_world_paths(), ())
        self.assertIsNone(self.plane.service.opened("nightcord"))
        # 而存档确实在那儿，所以它现在是一个可以被恢复的世界，不是不存在。
        self.assertEqual(self.archive()["revision"], 1)
        self.assertEqual(
            self.client.post("/api/persistent-worlds/nightcord/restore").status_code,
            200,
        )


# ── AC13 组装边界本身 ────────────────────────────────────────────────────
class CompositionBoundaryTests(unittest.TestCase):
    def test_building_the_plane_touches_no_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "worlds"
            plane = WorldControlPlane(root=root)
            self.assertFalse(root.exists())
            self.assertEqual(plane.list_worlds(), ())
            self.assertFalse(root.exists())
            self.assertEqual(owned_world_paths(), ())

    def test_creating_the_app_touches_no_disk_and_starts_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "worlds"
            app = create_app(WorldControlPlane(root=root))
            self.assertFalse(root.exists())
            self.assertEqual(owned_world_paths(), ())
            self.assertIsNotNone(app.state.world_control_plane)

    def test_two_apps_in_one_process_do_not_share_an_ownership_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "worlds"
            with patch.dict(os.environ, {BOUNDARY.active().models.key_name: "k"}):
                first = WorldControlPlane(
                    root=root, client_factory=lambda *a, **k: _FakeModelClient()
                )
                second = WorldControlPlane(
                    root=root, client_factory=lambda *a, **k: _FakeModelClient()
                )
                first.create(
                    world_id="shared", scene_id=SCENE, character_ids=CHARACTERS
                )
                try:
                    # 身份挂在锁文件的规范路径上，不是挂在某个 store 实例上：
                    # 两个 store 指着同一个存档根，第二个照样拿不到。
                    with self.assertRaises(WorldAlreadyOwned):
                        second.restore("shared")
                finally:
                    first.service.release_all()

    def test_the_archive_root_comes_from_server_configuration(self):
        with patch.dict(os.environ, {WORLD_ROOT_ENV: "/tmp/pns-web1-root"}):
            self.assertEqual(default_world_root(), Path("/tmp/pns-web1-root"))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(WORLD_ROOT_ENV, None)
            self.assertEqual(default_world_root().name, "worlds")

    def test_importing_the_app_creates_nothing_and_owns_nothing(self):
        """import 这个应用不建目录、不拿锁、不起运行时。

        用子进程，因为本进程早就把这些模块 import 过了 —— 在已经 import 过的
        进程里断言"import 没有副作用"，断言的是空气。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "worlds"
            script = (
                "import sys, os\n"
                f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
                f"sys.path.insert(0, {str(REPO_ROOT / 'scripts')!r})\n"
                "import pns.interfaces as interfaces\n"
                "from pns.runtime.persistence.ownership import owned_world_paths\n"
                f"assert not os.path.exists({str(root)!r}), '导入建出了存档根'\n"
                "assert owned_world_paths() == (), owned_world_paths()\n"
                "app = interfaces.create_app()\n"
                f"assert not os.path.exists({str(root)!r}), 'create_app 建出了存档根'\n"
                "assert owned_world_paths() == (), owned_world_paths()\n"
                "print('ok')\n"
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=tmp,
                env={**os.environ, WORLD_ROOT_ENV: str(root)},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "ok")
            self.assertEqual(sorted(os.listdir(tmp)), [])


# ── AC14 冷内容工厂 ──────────────────────────────────────────────────────
class InitialStateTests(WorldApiTestCase):
    def test_the_initial_state_comes_from_the_validated_content_registry(self):
        state = self.plane.new_session_state(
            world_id="nightcord",
            scene_id=SCENE,
            character_ids=CHARACTERS,
            registry=self.registry,
        )
        self.assertEqual(state.scene, SCENE)
        self.assertEqual(state.characters, CHARACTERS)
        self.assertIsNotNone(state.world_state)
        # 服务绑定是生命周期的一步，这里交出来的必须是一份**冷**状态。
        self.assertIsNone(state.scheduler)
        self.assertIsNone(state.agency_engine)
        self.assertIsNone(state.memory_encoder)
        self.assertIsNone(state.autonomy)

    def test_two_worlds_never_share_a_mutable_initial_object(self):
        first = self.plane.new_session_state(
            world_id="a",
            scene_id=SCENE,
            character_ids=CHARACTERS,
            registry=self.registry,
        )
        second = self.plane.new_session_state(
            world_id="b",
            scene_id=SCENE,
            character_ids=CHARACTERS,
            registry=self.registry,
        )
        self.assertNotEqual(first.session_id, second.session_id)
        self.assertIsNot(first.world_state, second.world_state)
        self.assertIsNot(first.world_state.locations, second.world_state.locations)
        self.assertIsNot(first.characters, second.characters)

    def test_an_unknown_scene_never_silently_becomes_the_default_scene(self):
        with self.assertRaises(ContentUnavailable):
            self.plane.new_session_state(
                world_id="x",
                scene_id="definitely-not-a-scene",
                character_ids=CHARACTERS,
                registry=self.registry,
            )

    def test_adapters_are_refused_without_server_side_credentials(self):
        with patch.dict(os.environ, {self.registry.models.key_name: ""}):
            with self.assertRaises(AdaptersUnavailable):
                self.plane.build_adapters(self.registry)


if __name__ == "__main__":
    unittest.main()
