# tests/test_mvp_world_api.py — MVP-1 端到端：操作台按下去，世界真的动起来。
#
# 这个文件走的是**完整的产品路径**：HTTP 请求 → 组装边界 → P12 生命周期 →
# 驱动 → P11 协调器 → Agency/生成/判分 → 事件/曝光/观察/记忆 → checkpoint。
# 唯一被替换掉的是 provider 客户端（不联网），别的一个环节都没有被绕过。
#
# 盯住的东西按"错了会怎样"排：
#   1. 自动模型调用是 opt-in。建世界、恢复世界、重启进程都不会自己开始花钱。
#   2. Start / Stop 幂等且诚实；`stopping` 不许被说成 `stopped`。
#   3. P12 的 `running` 与驱动的 `state` 是两件事，两边都要能看见。
#   4. 关闭与进程收尾会先停驱动，再走 P12 的终局关闭，所有权照常归还。
#   5. 恢复保住排期/历史/记忆，不重复播种，而且不自己接着跑。
#   6. 任何一条响应、驱动状态、存档里都不许出现那把 key。
#   7. 既有的 WEB-1 控制面与 /ws/run 一点没变。
#
# 运行: python -m unittest tests.test_mvp_world_api -v
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from fastapi.testclient import TestClient  # noqa: E402

from pns.interfaces.app import create_app  # noqa: E402
from pns.interfaces.composition import (  # noqa: E402
    AutonomySettings,
    WorldControlPlane,
)
from pns.models.event import EventType  # noqa: E402
from pns.runtime.autonomy.driver import DriverConfig  # noqa: E402
from pns.runtime.persistence import CheckpointPolicy  # noqa: E402
from pns.runtime.autonomy.seeding import (  # noqa: E402
    ActivationCadence,
    seed_activation_id,
)
from pns.runtime.reload import BOUNDARY  # noqa: E402

from tests.test_autonomy_driver import (  # noqa: E402
    BlockingProvider,
    autonomy_threads,
    wait_for,
)
from tests.test_mvp_generation import CANARY, CHARACTERS, SCENE  # noqa: E402

TEST_DRIVER = DriverConfig(
    tick_minutes=5, interval_seconds=0.01, stop_timeout_seconds=1.0
)


class WorldApiTestCase(unittest.TestCase):
    # 默认用产品策略（每个边界问一次，最快一分钟落一次盘）。要盯自动
    # checkpoint 本身的用例把最短间隔调掉 —— 不然它得等一分钟。
    checkpoint_policy = None
    world_action_cap = 100_000

    def setUp(self):
        self.registry = BOUNDARY.active()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "worlds"
        self._env = patch.dict(os.environ, {self.registry.models.key_name: CANARY})
        self._env.start()
        self.provider = BlockingProvider()
        self.plane = WorldControlPlane(
            root=self.root,
            client_factory=lambda *a, **k: self.provider,
            checkpoint_policy=self.checkpoint_policy,
            autonomy=AutonomySettings(
                driver=TEST_DRIVER,
                cadence=ActivationCadence(),
                shutdown_timeout_seconds=1.0,
                world_action_cap=self.world_action_cap,
            ),
        )
        self.app = create_app(self.plane)
        # 不走 with：lifespan 的收尾关闭是单独一组用例的被测对象。
        self.client = TestClient(self.app)
        self.before = set(autonomy_threads())

    def tearDown(self):
        try:
            self.provider.release.set()
            self.plane.drivers.stop_all("test teardown", 5.0)
            self.plane.service.release_all()
            wait_for(
                lambda: not (set(autonomy_threads()) - self.before),
                timeout=5.0,
                what="worker 线程退出",
            )
        finally:
            self._env.stop()
            self._tmp.cleanup()

    # ── 便捷 ────────────────────────────────────────────────────────────
    def create(self, world_id="nightcord"):
        response = self.client.post(
            "/api/persistent-worlds",
            json={
                "world_id": world_id,
                "scene": SCENE,
                "characters": list(CHARACTERS),
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def start(self, world_id="nightcord"):
        return self.client.post(
            f"/api/persistent-worlds/{world_id}/autonomy/start"
        )

    def stop(self, world_id="nightcord"):
        return self.client.post(f"/api/persistent-worlds/{world_id}/autonomy/stop")

    def status(self, world_id="nightcord"):
        response = self.client.get(f"/api/persistent-worlds/{world_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def world(self, world_id="nightcord"):
        return self.plane.service.opened(world_id)

    def spoken(self, world_id="nightcord"):
        world = self.world(world_id)
        return len(world.state.events.by_type(EventType.MESSAGE_SENT))

    def detail(self, response):
        return response.json()["detail"]

    def assert_no_canary(self, *blobs):
        for blob in blobs:
            self.assertNotIn(CANARY, blob)


# ── AC1/AC10 一次有界的完整回路 ─────────────────────────────────────────
class SmokeSessionTests(WorldApiTestCase):
    def test_a_new_world_is_open_but_nobody_is_pushing_it(self):
        created = self.create()
        self.assertTrue(created["running"], "P12 的运行时是开着的")
        self.assertIsNone(created["autonomy"], "但还没有人在推它")
        # 排期已经播好了，只是没人推时间。
        world = self.world()
        self.assertEqual(len(world.state.activations.pending()), len(CHARACTERS))
        time.sleep(0.1)
        self.assertEqual(self.spoken(), 0, "没人按 Start，就一句话都不该产生")
        self.assertEqual(self.provider.generations, [])

    def test_start_makes_the_characters_actually_speak_then_stop_closes_cleanly(self):
        self.create()
        started = self.start()
        self.assertEqual(started.status_code, 200, started.text)
        driver = started.json()["autonomy"]
        self.assertEqual(driver["state"], "running")
        self.assertEqual(driver["cadence"]["tick_minutes"], TEST_DRIVER.tick_minutes)

        wait_for(lambda: self.spoken() >= 2, what="两个角色都说上话")

        stopped = self.stop()
        self.assertEqual(stopped.status_code, 200, stopped.text)
        self.assertEqual(stopped.json()["autonomy"]["state"], "stopped")
        # 停了之后世界仍然开着、仍然属于本进程 —— 这是暂停，不是关闭。
        self.assertTrue(stopped.json()["owned"])
        self.assertTrue(stopped.json()["running"])

        closed = self.client.post("/api/persistent-worlds/nightcord/close")
        self.assertEqual(closed.status_code, 200, closed.text)
        self.assertTrue(closed.json()["clean"])
        self.assertFalse(closed.json()["owned"])

    def test_the_committed_dialogue_survives_into_the_archive(self):
        self.create()
        self.start()
        wait_for(lambda: self.spoken() >= 1, what="至少说上一句")
        self.stop()
        self.client.post("/api/persistent-worlds/nightcord/close")

        archive = json.loads(
            (self.root / "nightcord" / "world.json").read_text(encoding="utf-8")
        )
        blob = json.dumps(archive, ensure_ascii=False)
        self.assertIn("message.sent", blob)
        self.assertIn(self.provider._line, blob)



class AutoCheckpointTests(WorldApiTestCase):
    """自动 checkpoint 由 P12 的策略说了算，驱动只负责在边界上问一句。

    这里把最短间隔调掉，好在一次用例里看见它；产品默认那份（每个边界问一次、
    最快一分钟一次）由 test_persistent_world_api 盯着。
    """

    checkpoint_policy = CheckpointPolicy(every_boundaries=1, min_interval_seconds=0.0)

    def test_the_driver_checkpoints_on_completed_boundaries(self):
        self.create()
        before = self.status()["revision"]
        self.start()
        wait_for(
            lambda: self.status()["revision"] > before, what="自动 checkpoint 落盘"
        )
        status = self.stop().json()
        self.assertEqual(status["last_checkpoint_reason"], "autonomy_tick")
        self.assertIsNotNone(status["autonomy"]["last_tick"]["checkpoint_revision"])

    def test_a_world_that_is_never_started_never_checkpoints_by_itself(self):
        self.create()
        before = self.status()["revision"]
        time.sleep(0.1)
        self.assertEqual(self.status()["revision"], before)


# ── AC7 Start / Stop 的幂等与诚实 ───────────────────────────────────────
class StartStopContractTests(WorldApiTestCase):
    def test_starting_twice_is_idempotent(self):
        self.create()
        first = self.start()
        second = self.start()
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["autonomy"]["state"], "running")
        self.assertEqual(len(set(autonomy_threads()) - self.before), 1)

    def test_stopping_twice_is_idempotent(self):
        self.create()
        self.start()
        self.assertEqual(self.stop().json()["autonomy"]["state"], "stopped")
        again = self.stop()
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["autonomy"]["state"], "stopped")

    def test_stopping_a_world_that_never_started_is_not_an_error(self):
        self.create()
        response = self.stop()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["autonomy"])

    def test_a_timed_out_stop_reports_stopping_over_http(self):
        self.create()
        self.provider.blocking = True
        self.start()
        wait_for(self.provider.entered.is_set, what="一次生成卡住")
        driver = self.plane.drivers.get("nightcord")
        # 直接用一个很短的上限问一次：HTTP 那条路用的是同一个方法。
        status = driver.stop("operator", timeout=0.05)
        self.assertEqual(status["state"], "stopping")
        body = self.status()
        self.assertEqual(body["autonomy"]["state"], "stopping")
        self.assertFalse(body["autonomy"]["stopped"])
        # 而且 P12 那边**没有**因此说自己停了。
        self.assertTrue(body["running"])
        self.provider.release.set()

    def test_autonomy_on_a_world_that_is_not_open_is_a_conflict(self):
        self.create()
        self.client.post("/api/persistent-worlds/nightcord/close")
        for call in (self.start, self.stop):
            response = call()
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(self.detail(response)["category"], "world_not_open")

    def test_autonomy_on_an_invalid_world_id_is_a_400(self):
        response = self.client.post("/api/persistent-worlds/NOT%20VALID/autonomy/start")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.detail(response)["category"], "invalid_world_id")

    def test_close_while_the_driver_is_running_still_hands_the_world_back(self):
        self.create()
        self.start()
        wait_for(lambda: self.spoken() >= 1, what="至少说上一句")
        closed = self.client.post("/api/persistent-worlds/nightcord/close")
        self.assertEqual(closed.status_code, 200, closed.text)
        self.assertTrue(closed.json()["closed"])
        self.assertTrue(closed.json()["clean"])
        self.assertIsNone(closed.json()["autonomy"])
        wait_for(
            lambda: not (set(autonomy_threads()) - self.before), what="worker 退出"
        )

    def test_a_concurrent_start_and_close_do_not_leave_a_worker_behind(self):
        """两个操作同时发生，两种顺序都不许留下一个还在写的 worker。"""
        self.create()
        self.start()
        wait_for(lambda: self.spoken() >= 1, what="至少说上一句")
        results = {}
        ready = threading.Barrier(2)

        def do(name, call):
            ready.wait(5)
            results[name] = call()

        threads = [
            threading.Thread(target=do, args=("start", self.start)),
            threading.Thread(
                target=do,
                args=(
                    "close",
                    lambda: self.client.post("/api/persistent-worlds/nightcord/close"),
                ),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(set(results), {"start", "close"})
        # close 要么成功、要么被拒；无论哪种，都不许留下一个活着的 worker。
        wait_for(
            lambda: not (set(autonomy_threads()) - self.before), what="worker 退出"
        )
        if results["close"].status_code == 200:
            self.assertFalse(self.status()["owned"])


# ── 花费边界在操作台上看得见 ────────────────────────────────────────────
class SpendBoundaryTests(WorldApiTestCase):
    def test_the_status_reports_both_boundaries_separately(self):
        self.create()
        driver = self.start().json()["autonomy"]
        # 一道按 Start 重置……
        self.assertEqual(
            driver["run_budget"]["limit"], TEST_DRIVER.max_activations_per_run
        )
        # ……一道跟着这个世界一辈子。
        self.assertEqual(driver["world_actions"]["cap"], self.world_action_cap)
        # 两个用量都不断言具体数字：worker 已经在跑了，读到几都对。要断言的
        # 是它们**只往上走**，而且是两笔各自独立的账。
        self.assertGreaterEqual(driver["run_budget"]["used"], 0)
        self.assertGreaterEqual(driver["world_actions"]["committed"], 0)
        wait_for(lambda: self.spoken() >= 1, what="至少说上一句")
        self.stop()
        after = self.status()["autonomy"]
        self.assertGreater(after["run_budget"]["used"], 0)
        self.assertGreater(after["world_actions"]["committed"], 0)


class CappedWorldTests(WorldApiTestCase):
    world_action_cap = 1

    def test_starting_a_world_at_its_lifetime_cap_is_a_conflict_that_explains_itself(
        self,
    ):
        self.create()
        self.start()
        wait_for(
            lambda: self.status()["autonomy"]["exit_reason"] == "world_action_cap",
            what="到达世界动作上限",
        )
        refused = self.start()
        self.assertEqual(refused.status_code, 409, refused.text)
        detail = self.detail(refused)
        self.assertEqual(detail["category"], "autonomy_refused")
        self.assertIn("一生的动作上限", detail["message"])
        # 而且世界仍然是开着的、干净的 —— 到顶不是一次失败，是一条边界。
        self.assertTrue(self.status()["owned"])


# ── AC6 恢复：不重复播种，也不自己接着跑 ────────────────────────────────
class RestoreTests(WorldApiTestCase):
    def test_restore_keeps_the_schedule_and_leaves_autonomy_off(self):
        self.create()
        self.start()
        wait_for(lambda: self.spoken() >= 1, what="至少说上一句")
        self.stop()
        world = self.world()
        before = {
            a.activation_id: a.due_at.isoformat()
            for a in world.state.activations.pending()
        }
        memories = len(world.state.memories)
        self.client.post("/api/persistent-worlds/nightcord/close")

        restored = self.client.post("/api/persistent-worlds/nightcord/restore")
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertIsNone(restored.json()["autonomy"], "恢复不许自己接着烧额度")
        back = self.world()
        after = {
            a.activation_id: a.due_at.isoformat()
            for a in back.state.activations.pending()
        }
        self.assertEqual(after, before)
        self.assertEqual(len(after), len(CHARACTERS), "排期条数不许因为恢复而变多")
        self.assertEqual(len(back.state.memories), memories)
        for cid in CHARACTERS:
            self.assertIn(seed_activation_id(cid), after)

        # 而且恢复之后还能再开起来。
        again = self.start()
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["autonomy"]["state"], "running")

    def test_a_fresh_process_does_not_resume_autonomy(self):
        """服务器重启：世界恢复得回来，但没有人在推它。"""
        self.create()
        self.start()
        wait_for(lambda: self.spoken() >= 1, what="至少说上一句")
        self.plane.shutdown("server shutdown")

        second = WorldControlPlane(
            root=self.root,
            client_factory=lambda *a, **k: self.provider,
            autonomy=AutonomySettings(
                driver=TEST_DRIVER,
                cadence=ActivationCadence(),
                shutdown_timeout_seconds=1.0,
            ),
        )
        try:
            status = second.restore("nightcord")
            self.assertIsNone(status["autonomy"])
            self.assertEqual(len(status["world_id"]) > 0, True)
            before = second.service.opened("nightcord").state.world_state.clock
            time.sleep(0.1)
            after = second.service.opened("nightcord").state.world_state.clock
            self.assertEqual(before, after, "没人按 Start，时间不许自己往前走")
        finally:
            second.drivers.stop_all("test", 5.0)
            second.service.release_all()


# ── AC8 一条响应、一份状态、一份存档里都不许有那把 key ──────────────────
class LeakTests(WorldApiTestCase):
    def test_no_response_carries_the_key_even_after_a_full_run(self):
        self.create()
        self.start()
        wait_for(lambda: self.spoken() >= 1, what="至少说上一句")
        self.stop()
        for path in (
            "/api/persistent-worlds",
            "/api/persistent-worlds/nightcord",
        ):
            response = self.client.get(path)
            self.assert_no_canary(response.text)
            self.assertNotIn(self.registry.models.key_name, response.text)
        self.assert_no_canary(
            (self.root / "nightcord" / "world.json").read_text(encoding="utf-8")
        )

    def test_a_provider_failure_leaves_no_key_in_the_driver_status(self):
        hostile = type(CANARY, (RuntimeError,), {"__module__": "anthropic"})(
            f"rejected {CANARY}"
        )
        self.provider._generate_error = hostile
        self.create()
        self.start()
        wait_for(lambda: self.provider.generations, what="至少试过一次生成")
        self.stop()
        body = self.client.get("/api/persistent-worlds/nightcord").text
        self.assert_no_canary(body)

    def test_the_full_prompt_never_appears_in_a_response(self):
        self.create()
        self.start()
        wait_for(lambda: self.provider.generations, what="至少试过一次生成")
        self.stop()
        system = self.provider.generations[0]["system"]
        body = self.client.get("/api/persistent-worlds/nightcord").text
        # 提示词是服务器侧的东西：它整段都不该出现在状态接口里。
        self.assertNotIn(system[:60], body)


# ── AC5 进程收尾 ────────────────────────────────────────────────────────
class ShutdownTests(WorldApiTestCase):
    def test_the_lifespan_shutdown_stops_the_driver_and_closes_cleanly(self):
        with TestClient(self.app) as client:
            client.post(
                "/api/persistent-worlds",
                json={
                    "world_id": "nightcord",
                    "scene": SCENE,
                    "characters": list(CHARACTERS),
                },
            )
            client.post("/api/persistent-worlds/nightcord/autonomy/start")
            wait_for(lambda: self.spoken() >= 1, what="至少说上一句")
        # 退出 with 之后 lifespan 的收尾已经跑完。
        wait_for(
            lambda: not (set(autonomy_threads()) - self.before), what="worker 退出"
        )
        self.assertIsNone(self.plane.service.opened("nightcord"))
        archive = json.loads(
            (self.root / "nightcord" / "world.json").read_text(encoding="utf-8")
        )
        self.assertIn("message.sent", json.dumps(archive, ensure_ascii=False))


# ── AC9 既有控制面没被动过 ──────────────────────────────────────────────
class CompatibilityTests(WorldApiTestCase):
    def test_the_web1_lifecycle_controls_still_behave(self):
        self.create()
        checkpoint = self.client.post("/api/persistent-worlds/nightcord/checkpoint")
        self.assertEqual(checkpoint.status_code, 200, checkpoint.text)
        self.assertEqual(checkpoint.json()["revision"], 2)
        listing = self.client.get("/api/persistent-worlds")
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(
            [w["world_id"] for w in listing.json()["worlds"]], ["nightcord"]
        )
        missing = self.client.get("/api/persistent-worlds/nope")
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_the_autonomy_projection_is_detached_data(self):
        self.create()
        self.start()
        first = self.status()["autonomy"]
        first["state"] = "tampered"
        self.assertEqual(self.status()["autonomy"]["state"], "running")
        self.stop()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
