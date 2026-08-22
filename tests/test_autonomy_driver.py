# tests/test_autonomy_driver.py — MVP-1 有界自主驱动的并发不变量。
#
# 这个文件盯的全是"只在特定交错下才出现"的东西，所以它一律用**确定性屏障**，
# 不赌调度：等条件成立就往下走，等不到就在有界时间内失败。
#
# 盯住的东西按"错了会怎样"排：
#   1. 一个世界至多一个 worker。两个并发 start 造出两个 worker，就等于同一份
#      世界状态上有两条时间线在推 —— 而且每条都在花 API 额度。
#   2. Stop 说的是实话。报了 stopped 就必须真的没有后续 tick；等不到就报
#      stopping，绝不把"还在跑"说成"停了"。
#   3. Stop 是可重启的暂停，绝不动 P11 的终局停机。拿终局 stop() 去暂停，
#      等于一次"停一下"把世界永久关死。
#   4. 失败的 tick 被记下来，而且不变成忙等。
#   5. 驱动状态里不出现 provider 那侧的任何东西 —— 连异常类型名都不行。
#   6. 关闭是终局的：驱动先停、然后 P12 的 close 接管，所有权照常归还。
#   7. 驱动是进程内操作状态，一个字节都不进存档；import 它不起线程。
#   8. 花费有两道边界，而且它们是两件不同的事：单次 Start 的额度按 Start
#      重置（用完了自己停下，再按一次就是新的一轮），世界一生的动作上限跨
#      重启和恢复都成立（偷不走），而且到顶时是**响亮停机**，不是让引擎从此
#      把每一条激活都静静判掉。
#
# 运行: python -m unittest tests.test_autonomy_driver -v
import json
import os
import subprocess
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

from pns.interfaces.composition import (  # noqa: E402
    AutonomySettings,
    WorldControlPlane,
)
from pns.models.agency import AgencyOutcome  # noqa: E402
from pns.models.event import EventType  # noqa: E402
from pns.runtime.autonomy.coordinator import AutonomousRuntime, AutonomyError  # noqa: E402
from pns.runtime.autonomy.driver import (  # noqa: E402
    EXIT_RUN_BUDGET,
    EXIT_WORLD_CAP,
    OPAQUE_ERROR,
    DriverBusy,
    DriverConfig,
    DriverError,
    WorldDriver,
)
from pns.runtime.autonomy.seeding import ActivationCadence  # noqa: E402
from pns.runtime.persistence import CheckpointPolicy, FileWorldStore  # noqa: E402
from pns.runtime.reload import BOUNDARY  # noqa: E402
from pns.runtime.scheduler import SchedulerError  # noqa: E402

from tests.test_mvp_generation import (  # noqa: E402
    CANARY,
    CHARACTERS,
    SCENE,
    FakeProvider,
)

# 测试节拍：真实秒数压到最小，模拟分钟保持产品值。
TEST_DRIVER = DriverConfig(
    tick_minutes=5, interval_seconds=0.01, stop_timeout_seconds=1.0
)


def wait_for(predicate, timeout=5.0, what="条件"):
    """等一个条件成立。轮询而不是 sleep：等到就立刻往下走，等不到就失败。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    raise AssertionError(f"等不到{what}（{timeout} 秒）")


def _slow_spawn(record=None):
    """把 start() 的临界区撑开一段，好让两个并发 start 真的重叠。

    延迟放在"检查完、还没起跑"那一刻 —— 那正是互斥要保护的窗口。
    """
    original = WorldDriver._spawn_locked

    def slow(self, stop_event):
        if record is not None:
            record.append(stop_event)
        time.sleep(0.05)
        return original(self, stop_event)

    return slow


def autonomy_threads():
    return [t for t in threading.enumerate() if t.name.startswith("pns-autonomy-")]


class BlockingProvider(FakeProvider):
    """一个可以被卡在"模型调用中"的 provider 替身。

    它让"停机时正好有一次生成在飞"这件事变成可控的，而不是靠赌时序。
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.blocking = False

    def _create(self, **kwargs):
        # 生成和判分都会被卡住 —— 这正是"停机时正好有一次模型调用在飞"的两种
        # 形态，而它们对停机的意义完全一样。
        if self.blocking:
            self.entered.set()
            # 有界地等：测试挂住比测试失败难查得多。
            self.release.wait(10)
        return super()._create(**kwargs)


class DriverTestCase(unittest.TestCase):
    # 默认用产品策略。要盯 checkpoint 本身的用例把最短间隔调掉。
    checkpoint_policy = None

    # 覆盖它就能换一份驱动节拍/额度。
    driver_config = TEST_DRIVER
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
                driver=self.driver_config,
                cadence=ActivationCadence(),
                shutdown_timeout_seconds=1.0,
                world_action_cap=self.world_action_cap,
            ),
        )
        self.before = set(autonomy_threads())

    def tearDown(self):
        try:
            self.provider.release.set()
            self.plane.drivers.stop_all("test teardown", 5.0)
            self.plane.service.release_all()
            # 一个都不许活过用例：漏掉的 worker 会在别的用例里接着推世界。
            wait_for(
                lambda: not (set(autonomy_threads()) - self.before),
                timeout=5.0,
                what="worker 线程退出",
            )
        finally:
            self._env.stop()
            self._tmp.cleanup()

    def open_world(self, world_id="nightcord"):
        self.plane.create(
            world_id=world_id, scene_id=SCENE, character_ids=list(CHARACTERS)
        )
        return self.plane.service.opened(world_id)

    def driver(self, world):
        return self.plane.drivers.for_world(world)


# ── AC7 一个世界至多一个 worker ─────────────────────────────────────────
class SingleWorkerTests(DriverTestCase):
    def test_starting_twice_is_idempotent(self):
        world = self.open_world()
        driver = self.driver(world)
        first = driver.start()
        second = driver.start()
        self.assertEqual(first["state"], "running")
        self.assertEqual(second["state"], "running")
        self.assertEqual(len(set(autonomy_threads()) - self.before), 1)

    def test_a_slow_start_still_makes_only_one_worker(self):
        """把临界区撑开，两个并发 start 仍然只造一个 worker。

        撑开是刻意的：不撑开的话这个用例只是在赌 GIL 的切换点，赌赢了也证明
        不了什么。撑开之后，**锁**是唯一能让它通过的东西 —— 下一个用例把锁
        拿掉，同一段代码就会造出两个。
        """
        world = self.open_world()
        driver = self.driver(world)
        with patch.object(WorldDriver, "_spawn_locked", _slow_spawn()):
            self._race_start(driver)
        self.assertEqual(len(set(autonomy_threads()) - self.before), 1)

    def test_removing_the_lock_turns_the_previous_test_red(self):
        """反证：把互斥拿掉，同样的交错就会造出两个 worker。

        它不是在测产品代码，它是在测**上一个用例真的在测东西**。
        """
        world = self.open_world()
        driver = self.driver(world)
        events = []

        class _NoLock:
            def __enter__(self):
                return None

            def __exit__(self, *exc):
                return False

        driver._lock = _NoLock()
        try:
            with patch.object(WorldDriver, "_spawn_locked", _slow_spawn(events)):
                self._race_start(driver)
            self.assertEqual(
                len(set(autonomy_threads()) - self.before),
                2,
                "拿掉锁之后居然没造出第二个 worker —— 那上一个用例证明不了互斥",
            )
        finally:
            # 收拾干净：两个 worker 各有自己的停止信号，都要放倒，否则它们会
            # 一直推着这个世界跑进后面的用例。
            driver._lock = threading.Lock()
            for event in events:
                event.set()
            driver._stop_event.set()
            wait_for(
                lambda: not (set(autonomy_threads()) - self.before),
                timeout=5.0,
                what="两个 worker 都退出",
            )

    def _race_start(self, driver):
        ready = threading.Barrier(2)
        errors = []

        def go():
            try:
                ready.wait(5)
                driver.start()
            except Exception as e:  # noqa: BLE001 - 记下来在主线程断言
                errors.append(e)

        threads = [threading.Thread(target=go) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual([type(e).__name__ for e in errors], [], errors)


# ── AC4 Stop 说实话，而且可重启 ─────────────────────────────────────────
class StopTruthfulnessTests(DriverTestCase):
    def test_stopping_twice_is_idempotent(self):
        world = self.open_world()
        driver = self.driver(world)
        driver.start()
        self.assertEqual(driver.stop("first")["state"], "stopped")
        second = driver.stop("second")
        self.assertEqual(second["state"], "stopped")
        # 第一个理由才是真正的原因，后来的都是它的后果。
        self.assertEqual(second["stop_reason"], "first")

    def test_stopping_a_driver_that_never_started_is_stopped(self):
        world = self.open_world()
        self.assertEqual(self.driver(world).stop()["state"], "stopped")

    def test_a_timed_out_stop_reports_stopping_not_stopped(self):
        world = self.open_world()
        driver = self.driver(world)
        self.provider.blocking = True
        driver.start()
        wait_for(self.provider.entered.is_set, what="一次生成卡在模型调用里")

        status = driver.stop("operator", timeout=0.05)
        self.assertEqual(status["state"], "stopping")
        self.assertFalse(status["stopped"])
        self.assertTrue(status["stopping"])
        # 还在跑的 worker 不许被说成不存在。
        self.assertEqual(len(set(autonomy_threads()) - self.before), 1)

        # 放行之后，同一个 stop 再问一次才是真的停了。
        self.provider.release.set()
        wait_for(
            lambda: driver.status()["state"] == "stopped", what="worker 真的停下来"
        )
        self.assertEqual(driver.stop("operator")["state"], "stopped")

    def test_start_is_refused_while_a_stop_has_not_settled(self):
        world = self.open_world()
        driver = self.driver(world)
        self.provider.blocking = True
        driver.start()
        wait_for(self.provider.entered.is_set, what="一次生成卡在模型调用里")
        self.assertEqual(driver.stop(timeout=0.05)["state"], "stopping")
        with self.assertRaises(DriverBusy):
            driver.start()
        self.provider.release.set()

    def test_stop_is_a_restartable_pause_not_a_terminal_stop(self):
        world = self.open_world()
        driver = self.driver(world)
        driver.start()
        wait_for(lambda: driver.status()["ticks"] > 0, what="至少跑过一轮")
        driver.stop("operator")

        # P11 的运行时一点没被动过：它仍然接受写入，也没有停机理由。
        self.assertTrue(world.runtime.running)
        self.assertFalse(world.runtime.stop_requested)
        self.assertIsNone(world.runtime.stop_reason)

        ticks = driver.status()["ticks"]
        self.assertEqual(driver.start()["state"], "running")
        wait_for(lambda: driver.status()["ticks"] > ticks, what="重启之后接着跑")
        self.assertEqual(len(set(autonomy_threads()) - self.before), 1)

    def test_the_driver_never_calls_the_terminal_runtime_stop(self):
        world = self.open_world()
        driver = self.driver(world)
        calls = []
        real_stop = AutonomousRuntime.stop

        def record(runtime_self, reason="stopped"):
            calls.append(reason)
            return real_stop(runtime_self, reason)

        with patch.object(AutonomousRuntime, "stop", record):
            driver.start()
            wait_for(lambda: driver.status()["ticks"] > 0, what="至少跑过一轮")
            driver.stop("operator")
            self.assertEqual(calls, [], "驱动暂停不许调 P11 的终局停机")
            # 关闭才是终局的那一次。
            self.plane.close("nightcord")
        self.assertEqual(len(calls), 1, calls)

    def test_after_stop_reports_stopped_no_later_tick_can_land(self):
        world = self.open_world()
        driver = self.driver(world)
        driver.start()
        wait_for(lambda: driver.status()["ticks"] > 0, what="至少跑过一轮")
        status = driver.stop("operator")
        self.assertEqual(status["state"], "stopped")
        clock = world.state.world_state.clock
        events = len(world.state.events)
        ticks = status["ticks"]
        # 报了 stopped 就是真的没有后续了：等一段远大于节拍的时间，什么都不该动。
        time.sleep(0.2)
        self.assertEqual(driver.status()["ticks"], ticks)
        self.assertEqual(world.state.world_state.clock, clock)
        self.assertEqual(len(world.state.events), events)


# ── AC7 失败的 tick ─────────────────────────────────────────────────────
class FailedTickTests(DriverTestCase):
    def test_a_failing_tick_is_recorded_and_does_not_busy_loop(self):
        world = self.open_world()
        driver = self.driver(world)
        with patch.object(
            AutonomousRuntime, "advance", side_effect=SchedulerError("推不动")
        ):
            driver.start()
            wait_for(lambda: driver.status()["failures"] > 0, what="第一次失败被记下")
            time.sleep(0.2)
            status = driver.status()
            driver.stop("operator")
        self.assertIn("推不动", status["last_error"])
        self.assertGreater(status["consecutive_failures"], 0)
        # 每轮之间恒有一次有界等待，所以 0.2 秒里跑不出几百轮。
        self.assertLess(status["ticks"], 100, status)
        self.assertTrue(status["last_tick"]["failed"])

    def test_an_exception_before_the_time_advance_leaves_the_clock_alone(self):
        world = self.open_world()
        driver = self.driver(world)
        clock = world.state.world_state.clock
        with patch.object(
            AutonomousRuntime, "advance", side_effect=SchedulerError("推不动")
        ):
            driver.start()
            wait_for(lambda: driver.status()["failures"] > 0, what="失败被记下")
            driver.stop("operator")
        self.assertEqual(world.state.world_state.clock, clock)
        self.assertEqual(len(world.state.events.by_type(EventType.MESSAGE_SENT)), 0)

    def test_an_exception_after_the_boundary_does_not_undo_the_committed_tick(self):
        """checkpoint 那一步炸了，已经提交的那一轮仍然算数。"""
        world = self.open_world()
        driver = self.driver(world)
        with patch.object(
            type(world), "checkpoint_if_due", side_effect=RuntimeError("存不下去")
        ):
            driver.start()
            wait_for(lambda: driver.status()["failures"] > 0, what="checkpoint 失败")
            driver.stop("operator")
        # 时间确实往前走了，事件也确实提交了。
        self.assertGreater(len(world.state.events), 0)
        self.assertGreater(driver.status()["ticks"], 0)

    def test_an_unexpected_error_type_leaks_nothing_into_the_driver_status(self):
        """provider 侧的异常连类型名都可能是一把 key，所以它整个不许过边界。"""
        hostile = type(CANARY, (RuntimeError,), {"__module__": "anthropic"})(
            f"boom {CANARY}"
        )
        hostile.api_key = CANARY
        world = self.open_world()
        driver = self.driver(world)
        printed = []
        with patch("builtins.print", lambda *a, **k: printed.append(a)):
            with patch.object(AutonomousRuntime, "advance", side_effect=hostile):
                driver.start()
                wait_for(lambda: driver.status()["failures"] > 0, what="失败被记下")
                status = driver.status()
                driver.stop("operator")
        self.assertEqual(status["last_error"], OPAQUE_ERROR)
        self.assertNotIn(CANARY, json.dumps(status, ensure_ascii=False))
        self.assertNotIn(
            CANARY, json.dumps(self.plane.status("nightcord"), ensure_ascii=False)
        )
        # 日志也是"外面看得见的地方"：把原文打出去只是换个地方泄漏。
        self.assertTrue(printed, "出过一次没见过的错，却一点痕迹都没留")
        self.assertNotIn(CANARY, json.dumps(printed, ensure_ascii=False, default=str))

    def test_a_repository_error_is_reported_in_full(self):
        """仓库自己的异常要原文交出去 —— 操作者靠它判断该重试还是去看那块盘。"""
        world = self.open_world()
        driver = self.driver(world)
        with patch.object(
            AutonomousRuntime, "advance", side_effect=AutonomyError("运行时已经停止")
        ):
            driver.start()
            wait_for(lambda: driver.status()["failures"] > 0, what="失败被记下")
            status = driver.status()
            driver.stop("operator")
        self.assertIn("AutonomyError", status["last_error"])
        self.assertIn("运行时已经停止", status["last_error"])


# ── AC5 关闭是终局 ───────────────────────────────────────────────────────
class CloseTests(DriverTestCase):
    def test_closing_a_world_stops_the_worker_and_hands_back_ownership(self):
        world = self.open_world()
        driver = self.driver(world)
        driver.start()
        wait_for(lambda: driver.status()["ticks"] > 0, what="至少跑过一轮")

        status = self.plane.close("nightcord")
        self.assertTrue(status["closed"])
        self.assertTrue(status["clean"])
        self.assertFalse(status["owned"])
        wait_for(
            lambda: not (set(autonomy_threads()) - self.before),
            what="worker 随着关闭退出",
        )
        # 关掉之后驱动账本里也不再留着它 —— 那份句柄已经死了。
        self.assertIsNone(self.plane.drivers.get("nightcord"))
        self.assertIsNone(self.plane.status("nightcord")["autonomy"])

    def test_closing_while_a_model_call_is_blocked_is_bounded_and_final(self):
        """关闭时正好卡着一次模型调用 —— 这是最要命的那个交错。

        要同时成立三件事：关闭**有界地**返回（不许被一次慢调用挂住）、所有权
        归还、而且那次晚到的调用回来之后**提交不了**。挡住它的不是那次等待，
        是 P11 的终局 stop()。
        """
        world = self.open_world()
        driver = self.driver(world)
        self.provider.blocking = True
        driver.start()
        wait_for(self.provider.entered.is_set, what="一次生成卡在模型调用里")

        events_before = len(world.state.events)
        deadline = time.monotonic()
        status = self.plane.close("nightcord")
        # 有界：驱动的 stop 上限是 1 秒，close 不该比它慢太多。
        self.assertLess(time.monotonic() - deadline, 10.0)
        self.assertTrue(status["closed"])
        self.assertFalse(status["owned"])

        # 放行那次晚到的模型调用，它回来之后一个字节都提交不了。
        self.provider.release.set()
        wait_for(
            lambda: not (set(autonomy_threads()) - self.before), what="worker 退出"
        )
        self.assertEqual(len(world.state.events), events_before)
        self.assertFalse(world.runtime.running)

    def test_a_worker_stops_itself_when_the_runtime_is_terminally_stopped(self):
        world = self.open_world()
        driver = self.driver(world)
        driver.start()
        wait_for(lambda: driver.status()["ticks"] > 0, what="至少跑过一轮")
        world.runtime.stop("terminal")
        wait_for(
            lambda: driver.status()["state"] == "stopped", what="worker 自己收摊"
        )
        self.assertEqual(driver.status()["exit_reason"], "runtime_stopped")

    def test_a_driver_cannot_be_started_on_a_closed_world(self):
        world = self.open_world()
        self.plane.close("nightcord")
        with self.assertRaises(DriverError):
            WorldDriver(world, config=TEST_DRIVER).start()

    def test_shutdown_stops_every_driver_before_closing(self):
        world = self.open_world()
        self.driver(world).start()
        wait_for(lambda: self.driver(world).status()["ticks"] > 0, what="至少跑过一轮")
        reports = self.plane.shutdown("server shutdown")
        self.assertEqual([r["world_id"] for r in reports], ["nightcord"])
        self.assertTrue(reports[0]["closed"])
        self.assertTrue(reports[0]["clean"])
        wait_for(
            lambda: not (set(autonomy_threads()) - self.before),
            what="worker 随着停机退出",
        )


class BlockedCheckpointTests(DriverTestCase):
    """关闭时正好卡在一次落盘上。

    这是锁序最容易出事的地方：worker 是「世界锁 → 闸门 → 会话边界」，close
    也是「世界锁 → 闸门」。顺序一致才不会死锁 —— 而"不会死锁"只能靠真的把
    一次落盘卡住来证明。
    """

    checkpoint_policy = CheckpointPolicy(every_boundaries=1, min_interval_seconds=0.0)

    def test_closing_while_a_checkpoint_is_blocked_still_finishes(self):
        world = self.open_world()
        driver = self.driver(world)
        blocked = threading.Event()
        release = threading.Event()
        real_save = FileWorldStore.save

        def slow_save(store_self, archive):
            blocked.set()
            release.wait(10)
            return real_save(store_self, archive)

        with patch.object(FileWorldStore, "save", slow_save):
            driver.start()
            wait_for(blocked.is_set, what="一次落盘卡住")

            done = threading.Event()
            result = {}

            def close():
                try:
                    result["status"] = self.plane.close("nightcord")
                except Exception as e:  # noqa: BLE001
                    result["error"] = e
                finally:
                    done.set()

            closer = threading.Thread(target=close)
            closer.start()
            # 卡着的时候，关闭当然还没完成 —— 但它也不许把自己锁死。
            self.assertFalse(done.wait(0.2))
            release.set()
            self.assertTrue(done.wait(15), "关闭被卡住的落盘挂死了")
            closer.join(5)

        self.assertNotIn("error", result, result.get("error"))
        self.assertTrue(result["status"]["closed"])
        self.assertFalse(result["status"]["owned"])
        wait_for(
            lambda: not (set(autonomy_threads()) - self.before), what="worker 退出"
        )


# ── 花费边界 ────────────────────────────────────────────────────────────
class RunBudgetTests(DriverTestCase):
    """单次 Start 的额度：明确、可重置，而且刷不出来。"""

    driver_config = DriverConfig(
        tick_minutes=5,
        interval_seconds=0.01,
        stop_timeout_seconds=1.0,
        max_activations_per_run=2,
    )

    def spoken(self, world):
        return len(world.state.events.by_type(EventType.MESSAGE_SENT))

    def test_a_run_stops_itself_when_its_budget_is_gone(self):
        world = self.open_world()
        driver = self.driver(world)
        driver.start()
        wait_for(
            lambda: driver.status()["exit_reason"] == EXIT_RUN_BUDGET,
            what="这一轮的额度用完、worker 自己收摊",
        )
        status = driver.status()
        self.assertEqual(status["state"], "stopped")
        self.assertGreaterEqual(status["run_budget"]["used"], 2)
        self.assertEqual(status["run_budget"]["remaining"], 0)
        # 用完了不是失败：没有错误，只是这一轮跑到头了。
        self.assertIsNone(status["last_error"])
        self.assertEqual(status["failures"], 0)

        # 而且它真的不再推了：时间和事件都停在那儿。
        clock = world.state.world_state.clock
        spoken = self.spoken(world)
        time.sleep(0.2)
        self.assertEqual(world.state.world_state.clock, clock)
        self.assertEqual(self.spoken(world), spoken)

    def test_a_new_start_is_a_new_round_and_the_world_speaks_again(self):
        world = self.open_world()
        driver = self.driver(world)
        driver.start()
        wait_for(
            lambda: driver.status()["exit_reason"] == EXIT_RUN_BUDGET,
            what="第一轮额度用完",
        )
        spoken = self.spoken(world)
        self.assertGreater(spoken, 0)

        # 显式的新一轮：额度重新装满，世界接着说。
        again = driver.start()
        self.assertEqual(again["state"], "running")
        self.assertEqual(again["run_budget"]["used"], 0)
        self.assertIsNone(again["exit_reason"])
        wait_for(lambda: self.spoken(world) > spoken, what="新一轮里又说上话")

    def test_pressing_start_again_while_running_does_not_refill_the_round(self):
        """幂等的 start 不许当成新一轮 —— 那等于按住 Start 就能无限花下去。"""
        world = self.open_world()
        driver = self.driver(world)
        driver.start()
        wait_for(lambda: driver.status()["run_budget"]["used"] > 0, what="花掉一条")
        used = driver.status()["run_budget"]["used"]
        self.assertGreaterEqual(driver.start()["run_budget"]["used"], used)
        self.assertGreaterEqual(driver.status()["run_budget"]["used"], used)

    def test_checkpoint_failures_do_not_refund_model_work(self):
        """落盘是另一条边界：失败不能抹掉已经发生的生成与判分。"""
        world = self.open_world()
        driver = self.driver(world)
        with patch.object(
            world,
            "checkpoint_if_due",
            side_effect=RuntimeError("simulated disk failure"),
        ):
            driver.start()
            wait_for(
                lambda: driver.status()["exit_reason"] == EXIT_RUN_BUDGET,
                what="落盘持续失败时本轮额度仍然耗尽",
            )
        status = driver.status()
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(status["run_budget"]["used"], 2)
        self.assertGreater(status["failures"], 0)

    def test_neither_a_restore_nor_a_restart_refills_the_same_round(self):
        """跑完一轮之后关掉再恢复：没有人在推，也没有一句新的台词。

        新的一轮必须由操作者显式按下 —— 这才是"可重置"和"偷刷"的区别。
        """
        world = self.open_world()
        driver = self.driver(world)
        driver.start()
        wait_for(
            lambda: driver.status()["exit_reason"] == EXIT_RUN_BUDGET,
            what="这一轮额度用完",
        )
        spoken = self.spoken(world)
        committed = world.state.agency.committed_actions()
        self.plane.close("nightcord")

        self.plane.restore("nightcord")
        back = self.plane.service.opened("nightcord")
        # 恢复本身不起驱动，所以时间不动、也不会多出一句话。
        self.assertIsNone(self.plane.status("nightcord")["autonomy"])
        time.sleep(0.2)
        self.assertEqual(self.spoken(back), spoken)
        # 世界一生的用量跟着存档回来了，一次恢复换不来新的额度。
        self.assertEqual(back.state.agency.committed_actions(), committed)

        # 显式 Start 之后才是新的一轮。
        self.plane.start_autonomy("nightcord")
        wait_for(lambda: self.spoken(back) > spoken, what="新一轮里又说上话")


class WorldActionCapTests(DriverTestCase):
    """世界一生的动作上限：到顶时响亮停机，不是从此静静判掉每一条激活。"""

    driver_config = DriverConfig(
        tick_minutes=5, interval_seconds=0.01, stop_timeout_seconds=1.0
    )
    world_action_cap = 2

    def test_reaching_the_cap_stops_the_driver_loudly(self):
        world = self.open_world()
        driver = self.driver(world)
        driver.start()
        wait_for(
            lambda: driver.status()["exit_reason"] == EXIT_WORLD_CAP,
            what="到达世界动作上限、worker 自己收摊",
        )
        status = driver.status()
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(status["world_actions"]["cap"], 2)
        self.assertEqual(status["world_actions"]["committed"], 2)
        self.assertEqual(status["world_actions"]["remaining"], 0)
        # 关键：它是在引擎开始静默拒绝**之前**停的。
        self.assertEqual(
            world.state.agency.for_outcome(AgencyOutcome.REJECTED_BUDGET), ()
        )

    def test_start_is_refused_while_the_cap_is_reached(self):
        world = self.open_world()
        driver = self.driver(world)
        driver.start()
        wait_for(
            lambda: driver.status()["exit_reason"] == EXIT_WORLD_CAP, what="到达上限"
        )
        with self.assertRaises(DriverError) as caught:
            driver.start()
        # 报错要说清楚怎么解开：这不是"再按一次就好"的那一档。
        self.assertIn("一生的动作上限", str(caught.exception))
        self.assertIn("重新打开", str(caught.exception))

    def test_a_restore_does_not_hand_back_a_fresh_lifetime_allowance(self):
        world = self.open_world()
        self.driver(world).start()
        wait_for(
            lambda: self.driver(world).status()["exit_reason"] == EXIT_WORLD_CAP,
            what="到达上限",
        )
        self.plane.close("nightcord")
        self.plane.restore("nightcord")
        back = self.plane.service.opened("nightcord")
        self.assertEqual(back.state.agency.committed_actions(), 2)
        with self.assertRaises(DriverError):
            self.plane.start_autonomy("nightcord")

    def test_raising_the_cap_and_reopening_lets_the_world_speak_again(self):
        """上限不是死刑：调高它、重新打开这个世界，角色就能接着说。"""
        world = self.open_world()
        self.driver(world).start()
        wait_for(
            lambda: self.driver(world).status()["exit_reason"] == EXIT_WORLD_CAP,
            what="到达上限",
        )
        spoken = len(world.state.events.by_type(EventType.MESSAGE_SENT))
        self.plane.close("nightcord")

        roomy = WorldControlPlane(
            root=self.root,
            client_factory=lambda *a, **k: self.provider,
            autonomy=AutonomySettings(
                driver=self.driver_config,
                cadence=ActivationCadence(),
                shutdown_timeout_seconds=1.0,
                world_action_cap=50,
            ),
        )
        try:
            roomy.restore("nightcord")
            back = roomy.service.opened("nightcord")
            roomy.start_autonomy("nightcord")
            wait_for(
                lambda: len(back.state.events.by_type(EventType.MESSAGE_SENT)) > spoken,
                what="调高上限之后又说上话",
            )
        finally:
            roomy.drivers.stop_all("test", 5.0)
            roomy.service.release_all()


class CollidingWorldActionCapTests(DriverTestCase):
    """同一 tick 的整批到期也不能越过世界剩余额度。"""

    # 开局两名角色分别在 +5/+10 分钟到期；一次推进 10 分钟会让两条同时待办。
    driver_config = DriverConfig(
        tick_minutes=10, interval_seconds=0.01, stop_timeout_seconds=1.0
    )
    world_action_cap = 1

    def test_only_the_remaining_allowance_reaches_agency(self):
        world = self.open_world()
        driver = self.driver(world)
        driver.start()
        wait_for(
            lambda: driver.status()["exit_reason"] == EXIT_WORLD_CAP,
            what="一条动作填满世界上限",
        )
        self.assertEqual(world.state.agency.committed_actions(), 1)
        self.assertEqual(
            world.state.agency.for_outcome(AgencyOutcome.REJECTED_BUDGET), ()
        )
        # 没拿到额度的另一条仍在投递箱里，而不是被静默拒绝并确认掉。
        self.assertEqual(len(world.state.activation_outbox.pending()), 1)


# ── AC8 驱动状态不进存档，import 不起线程 ───────────────────────────────
class DriverIsProcessLocalTests(DriverTestCase):
    def test_the_archive_carries_nothing_about_the_driver(self):
        world = self.open_world()
        driver = self.driver(world)
        driver.start()
        wait_for(lambda: driver.status()["ticks"] > 2, what="跑过几轮")
        driver.stop("operator")
        world.checkpoint("test")

        archive = (self.root / "nightcord" / "world.json").read_text(encoding="utf-8")
        for forbidden in (
            "pns-autonomy",
            "consecutive_failures",
            "last_tick",
            "interval_seconds",
            "tick_minutes",
        ):
            self.assertNotIn(forbidden, archive, f"存档里出现了驱动状态：{forbidden}")

    def test_a_restored_world_does_not_resume_autonomy(self):
        """进程重启不许自己接着烧 API 额度。"""
        world = self.open_world()
        self.driver(world).start()
        wait_for(lambda: self.driver(world).status()["ticks"] > 0, what="至少跑过一轮")
        self.plane.close("nightcord")

        status = self.plane.restore("nightcord")
        self.assertIsNone(status["autonomy"], "恢复之后不该有人在推它")
        self.assertTrue(status["running"], "P12 的运行时是开着的 —— 只是没人推")
        self.assertEqual(autonomy_threads(), list(self.before))

    def test_importing_the_driver_module_starts_nothing(self):
        code = (
            "import sys, threading;"
            f"sys.path.insert(0, {str(REPO_ROOT)!r});"
            "import pns.runtime.autonomy.driver as d;"
            "print(threading.active_count(), len(threading.enumerate()))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        counts = result.stdout.split()
        self.assertEqual(counts[0], "1", f"import 起了线程：{result.stdout}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
