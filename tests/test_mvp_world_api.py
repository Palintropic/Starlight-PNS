# tests/test_mvp_world_api.py — MVP-1 端到端：操作台按下去，世界真的动起来。
#
# 这个文件走的是**完整的产品路径**：HTTP 请求 → 组装边界 → P12 生命周期 →
# 时钟 worker → P11 协调器 → Agency/生成/判分 → 事件/曝光/观察/记忆 → checkpoint。
# 唯一被替换掉的是 provider 客户端（不联网），别的一个环节都没有被绕过。
# 时间用开发倍率快进（生产是 1:1，见 test_deployment_process）。
#
# 盯住的东西按"错了会怎样"排：
#   1. 自动模型调用是 opt-in。建世界、恢复世界、重启进程都不会自己开始花钱——
#      但时间与作息从世界打开起就在走（WORLD-1）。
#   2. Start / Stop 幂等且诚实；Stop 时正在飞的那次调用回来也落不了地。
#   3. P12 的 `running` 与认知的 `state` 是两件事，两边都要能看见。
#   4. 关闭与进程收尾先停时钟 worker，再走 P12 的终局关闭，所有权照常归还。
#   5. 恢复保住排期/历史/记忆，不重复播种，而且不自己接着花钱。
#   6. 任何一条响应、状态、存档里都不许出现那把 key。
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
from pns.runtime.autonomy.clock_worker import ClockConfig  # noqa: E402
from pns.runtime.persistence import CheckpointPolicy  # noqa: E402
from pns.runtime.autonomy.seeding import (  # noqa: E402
    ActivationCadence,
    seed_activation_id,
)
from pns.runtime.reload import BOUNDARY  # noqa: E402

from tests.autonomy_support import (  # noqa: E402
    BlockingProvider,
    clock_threads,
    wait_for,
)
from tests.test_mvp_generation import CANARY, CHARACTERS, SCENE  # noqa: E402

# 开发倍率：一秒真实时间 ≈ 50 分钟模拟时间。节拍压到最小。
TEST_CLOCK = ClockConfig(rate=3000.0, interval_seconds=0.01, stop_timeout_seconds=2.0)


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
                clock=TEST_CLOCK,
                cadence=ActivationCadence(),
                shutdown_timeout_seconds=1.0,
                world_action_cap=self.world_action_cap,
                # 这组用例钉的是固定节拍与播种；回话机会另有用例。
                reply_delay_minutes=None,
            ),
        )
        self.app = create_app(self.plane)
        # 不走 with：lifespan 的收尾关闭是单独一组用例的被测对象。
        self.client = TestClient(self.app)
        self.before = set(clock_threads())

    def tearDown(self):
        try:
            self.provider.release.set()
            self.plane.service.release_all()
            wait_for(
                lambda: not (set(clock_threads()) - self.before),
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

    def clock(self, world_id="nightcord"):
        return self.world(world_id).state.world_state.clock

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
    def test_a_new_world_runs_its_time_but_nobody_decides(self):
        created = self.create()
        self.assertTrue(created["running"], "P12 的运行时是开着的")
        autonomy = created["autonomy"]
        self.assertEqual(autonomy["state"], "stopped", "认知还没 Start")
        self.assertEqual(autonomy["cognition_causes"], ["not_started"])
        self.assertTrue(autonomy["worker_alive"], "时间从世界打开起就在走")
        start = self.clock()
        wait_for(lambda: self.clock() > start, what="时间往前走")
        wait_for(lambda: len(self.world().state.agency) >= 1, what="一条到期以 not_started 收尾")
        self.assertEqual(self.spoken(), 0, "没人按 Start，就一句话都不该产生")
        self.assertEqual(self.provider.generations, [])

    def test_start_makes_the_characters_actually_speak_then_stop_closes_cleanly(self):
        self.create()
        started = self.start()
        self.assertEqual(started.status_code, 200, started.text)
        autonomy = started.json()["autonomy"]
        self.assertEqual(autonomy["state"], "running")
        self.assertEqual(autonomy["cadence"]["rate"], TEST_CLOCK.rate)

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



class DailyRhythmTests(WorldApiTestCase):
    """CONTENT-1：作者写下的作息表真的走完了产品路径。

    这里刻意不构造任何测试专用的作息表 —— 用的就是角色包里那两份。世界从
    遗留 nightcord fixture 起步（深夜 02:00，两个人都在 Nightcord 上），
    时间推过 04:00 之后，两个人各自回到自己作息表上的那一段。
    """

    def _activity(self, character_id, world_id="nightcord"):
        world = self.world(world_id)
        return world.state.world_state.activity_of(character_id).kind.value

    def test_the_pack_rhythm_moves_an_open_world_through_its_day(self):
        created = self.create()
        self.assertEqual(created["clock"][11:16], "02:00")
        # fixture 给的初始活动：频道在场是明说的，所以它有资格写 online_chatting。
        self.assertEqual(created["autonomy"]["cognition_causes"], ["not_started"])

        # 不按 Start：时间与作息不归 Start 管。04:00 之后瑞希去睡了，绘名接着
        # 画。两条都来自角色包，不是这里写的。
        wait_for(
            lambda: self._activity("mizuki") == "resting"
            and self._activity("ena") == "drawing",
            what="作息表把世界推过 04:00 那道边界",
        )
        self.assertEqual(self.provider.generations, [])

        world = self.world()
        events = [
            event
            for event in world.state.events.events()
            if event.type is EventType.CHARACTER_ACTIVITY_CHANGED
        ]
        self.assertTrue(events, "作息变更必须留在世界历史里")
        for event in events:
            self.assertEqual(event.provenance["kind"], "daily_rhythm")
        # 关掉再恢复：作息表是内容，世界状态是存档 —— 两边都要接得上。
        self.client.post("/api/persistent-worlds/nightcord/close")
        restored = self.client.post("/api/persistent-worlds/nightcord/restore")
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(self._activity("mizuki"), "resting")

    def test_an_operator_change_is_not_overwritten_inside_the_segment(self):
        self.create()
        # 02:00 属于"25 時 Nightcord"那一段，它一直管到 04:00。
        response = self.client.post(
            "/api/persistent-worlds/nightcord/activity",
            json={"character_id": "mizuki", "activity": "editing_video"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        at = response.json()["since"]
        self.assertLess(at[11:16], "04:00", "操作要落在同一段里")

        wait_for(lambda: self.clock().strftime("%H:%M") >= "03:00", what="走到 03:00")
        self.assertEqual(self._activity("mizuki"), "editing_video")
        # 下一段开始，作息表重新接手。
        wait_for(lambda: self._activity("mizuki") == "resting", what="04:00 之后回到作息")


class AutoCheckpointTests(WorldApiTestCase):
    """自动 checkpoint 由 P12 的策略说了算，时钟 worker 只负责在边界上问一句。

    这里把最短间隔调掉，好在一次用例里看见它；产品默认那份（每个边界问一次、
    最快一分钟一次）由 test_persistent_world_api 盯着。
    """

    checkpoint_policy = CheckpointPolicy(every_boundaries=1, min_interval_seconds=0.0)

    def test_the_clock_checkpoints_on_completed_boundaries_without_start(self):
        # 时间在走，世界在变（时钟、作息、以 not_started 收尾的到期），所以
        # 不按 Start 也会按边界落盘——只是一次模型调用都没有。
        self.create()
        before = self.status()["revision"]
        wait_for(
            lambda: self.status()["revision"] > before, what="自动 checkpoint 落盘"
        )
        self.assertEqual(self.status()["last_checkpoint_reason"], "clock_step")
        self.assertEqual(self.provider.generations, [])


# ── AC7 Start / Stop 的幂等与诚实 ───────────────────────────────────────
class StartStopContractTests(WorldApiTestCase):
    def test_starting_twice_is_idempotent(self):
        self.create()
        first = self.start()
        second = self.start()
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["autonomy"]["state"], "running")
        self.assertEqual(len(set(clock_threads()) - self.before), 1)

    def test_a_legacy_world_start_does_not_renew(self):
        # 续额策略只来自代码里的正式世界定义（COG-1 §5）：普通世界一次 Start 一份。
        self.create()
        budget = self.start().json()["autonomy"]["run_budget"]
        self.assertIsNone(budget["renewal"])
        self.assertIsNone(budget["renews_at"])

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
        self.assertEqual(response.json()["autonomy"]["cognition_causes"], ["not_started"])

    def test_a_call_in_flight_at_stop_does_not_land(self):
        # Stop 不等任何线程：它是一次认知时间线转换。正在飞的那次调用回来之后，
        # 提交时按新区间判为不可用——模型费用花了，世界不受影响。
        self.create()
        self.provider.blocking = True
        self.start()
        wait_for(self.provider.entered.is_set, what="一次模型调用卡住")
        spoken = self.spoken()
        records = len(self.world().state.agency)
        stopped = self.stop()
        self.assertEqual(stopped.status_code, 200, stopped.text)
        self.assertEqual(stopped.json()["autonomy"]["state"], "stopped")
        self.assertTrue(stopped.json()["running"], "P12 那边没有停")
        self.provider.release.set()
        wait_for(
            lambda: any(
                record.outcome.value == "rejected_unavailable"
                and "operator_paused" in record.detail["causes"]
                for record in self.world().state.agency.records()[records:]
            ),
            what="飞着的那条以 operator_paused 收尾",
        )
        self.assertEqual(self.spoken(), spoken, "Stop 之后不许再有台词落地")

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

    def test_close_while_cognition_is_running_still_hands_the_world_back(self):
        self.create()
        self.start()
        wait_for(lambda: self.spoken() >= 1, what="至少说上一句")
        closed = self.client.post("/api/persistent-worlds/nightcord/close")
        self.assertEqual(closed.status_code, 200, closed.text)
        self.assertTrue(closed.json()["closed"])
        self.assertTrue(closed.json()["clean"])
        self.assertIsNone(closed.json()["autonomy"])
        wait_for(
            lambda: not (set(clock_threads()) - self.before), what="worker 退出"
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
            lambda: not (set(clock_threads()) - self.before), what="worker 退出"
        )
        if results["close"].status_code == 200:
            self.assertFalse(self.status()["owned"])


# ── 花费边界在操作台上看得见 ────────────────────────────────────────────
class SpendBoundaryTests(WorldApiTestCase):
    def test_the_status_reports_both_boundaries_separately(self):
        self.create()
        autonomy = self.start().json()["autonomy"]
        # 一道按 Start 重置……
        self.assertEqual(
            autonomy["run_budget"]["limit"], TEST_CLOCK.max_activations_per_run
        )
        # ……一道跟着这个世界一辈子。
        self.assertEqual(autonomy["world_actions"]["cap"], self.world_action_cap)
        # 两个用量都不断言具体数字：worker 已经在跑了，读到几都对。要断言的
        # 是它们**只往上走**，而且是两笔各自独立的账。
        self.assertGreaterEqual(autonomy["run_budget"]["used"], 0)
        self.assertGreaterEqual(autonomy["world_actions"]["committed"], 0)
        wait_for(lambda: self.spoken() >= 1, what="至少说上一句")
        self.stop()
        after = self.status()["autonomy"]
        self.assertGreater(after["run_budget"]["used"], 0)
        self.assertGreater(after["world_actions"]["committed"], 0)


class CappedWorldTests(WorldApiTestCase):
    world_action_cap = 1

    def test_starting_a_world_at_its_lifetime_cap_explains_itself(self):
        self.create()
        self.start()
        wait_for(
            lambda: self.status()["autonomy"]["exit_reason"] == "world_action_cap",
            what="到达世界动作上限",
        )
        spoken = self.spoken()
        # Start 清不掉世界上限：它照样返回，但如实报告认知仍不可用、为什么。
        again = self.start()
        self.assertEqual(again.status_code, 200, again.text)
        autonomy = again.json()["autonomy"]
        self.assertEqual(autonomy["state"], "stopped")
        self.assertIn("world_action_cap", autonomy["cognition_causes"])
        # 世界仍然开着、时间仍然在走——到顶不是一次失败，是一条边界。
        self.assertTrue(self.status()["owned"])
        start = self.clock()
        wait_for(lambda: self.clock() > start, what="时间继续走")
        self.assertEqual(self.spoken(), spoken)


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
        autonomy = restored.json()["autonomy"]
        self.assertEqual(autonomy["state"], "stopped", "恢复不许自己接着烧额度")
        self.assertIn("not_started", autonomy["cognition_causes"])
        back = self.world()
        # 时间在恢复之后立刻开始补跑，排期的到期时刻会随之往后排；要守的是
        # "每个角色恰好一条"，不重复播种。
        after = {a.activation_id for a in back.state.activations.pending()}
        self.assertEqual(after, set(before))
        self.assertEqual(len(after), len(CHARACTERS), "排期条数不许因为恢复而变多")
        self.assertEqual(len(back.state.memories), memories)
        for cid in CHARACTERS:
            self.assertIn(seed_activation_id(cid), after)

        # 而且恢复之后还能再开起来。
        again = self.start()
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["autonomy"]["state"], "running")

    def test_a_fresh_process_does_not_resume_cognition(self):
        """服务器重启：世界恢复得回来、时间接着走，但没有人替角色做决定。"""
        self.create()
        self.start()
        wait_for(lambda: self.spoken() >= 1, what="至少说上一句")
        self.plane.shutdown("server shutdown")

        second = WorldControlPlane(
            root=self.root,
            client_factory=lambda *a, **k: self.provider,
            autonomy=AutonomySettings(
                clock=TEST_CLOCK,
                cadence=ActivationCadence(),
                shutdown_timeout_seconds=1.0,
            ),
        )
        try:
            status = second.restore("nightcord")
            self.assertIn("not_started", status["autonomy"]["cognition_causes"])
            calls = len(self.provider.generations)
            world = second.service.opened("nightcord")
            before = world.state.world_state.clock
            wait_for(lambda: world.state.world_state.clock > before, what="时间接着走")
            time.sleep(0.1)
            self.assertEqual(len(self.provider.generations), calls, "没人按 Start，不许调模型")
        finally:
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

    def test_a_provider_failure_leaves_no_key_in_the_autonomy_status(self):
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
    def test_the_lifespan_shutdown_stops_the_clock_and_closes_cleanly(self):
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
            lambda: not (set(clock_threads()) - self.before), what="worker 退出"
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
