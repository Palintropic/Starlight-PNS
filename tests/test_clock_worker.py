# tests/test_clock_worker.py — 世界时钟 worker（WORLD-1 设计 §2、§7、§12.7）。
#
# 守的线：
#   1. 时间跟着现实走：世界打开就有 worker，Start/Stop 不启停它；
#   2. 一个世界一个 worker，句柄登记之后才起，起不来就整个退回去；
#   3. 关闭先停 worker：卡在模型调用上时，普通关闭拒绝（不存、不还所有权），
#      force 关闭不等，晚到的结果落不了地；
#   4. 失败不忙等：连续失败进入 faulted、指数退避，故障写进认知时间线，恢复后
#      补跑经过故障时段的到期带 fault；
#   5. 现实时钟落后于存档时钟：不倒退、不前进，追上之后接着走；
#   6. 状态里没有 provider 那侧的原文；静止的世界不会每一轮都写一版存档；
#   7. 生产环境不许快进；恢复时按配置的倍率重新锚定，时间不跳。
#
# 运行: python -m unittest discover -s tests -p test_clock_worker.py
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from grants_support import grant_everything  # noqa: E402
from pns.interfaces.composition import (  # noqa: E402
    CLOCK_RATE_ENV,
    AutonomySettings,
    CompositionError,
)
from pns.models.activation import ActivationKind, ScheduledActivation
from pns.models.agency import AgencyOutcome
from pns.models.cognition import CognitionCause as C
from pns.models.event import EventType
from pns.models.session import SessionState
from pns.models.world_state import WorldState
from pns.runtime.autonomy.audit import ScriptedAuditor
from pns.runtime.autonomy.clock_worker import (
    CLOCK_FAULTED,
    CLOCK_HEALTHY,
    OPAQUE_ERROR,
    ClockConfig,
    ClockWorker,
    ClockWorkerError,
)
from pns.runtime.autonomy.coordinator import AutonomyError
from pns.runtime.autonomy.generation import AuthoredLinePolicy, ScriptedLineGenerator
from pns.runtime.memory.recall import MemoryRecall
from pns.runtime.persistence.lifecycle import (
    CheckpointPolicy,
    LifecycleError,
    RuntimeAdapters,
    WorldLifecycleService,
)
from pns.runtime.persistence.store import FileWorldStore
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph

CLOCK = datetime(2026, 9, 27, 19, 0)
WALL = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
FAST = ClockConfig(interval_seconds=0.005, stop_timeout_seconds=0.3)


def wait_for(predicate, timeout=5.0, what="条件"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    raise AssertionError(f"等不到{what}（{timeout} 秒）")


def clock_threads():
    return [t for t in threading.enumerate() if t.name.startswith("pns-clock-")]


class FakeWall:
    """可控的现实时间。worker 每一轮问它一次"现在几点"。"""

    def __init__(self, start=WALL):
        self._now = start
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self._now

    def advance(self, **delta):
        with self._lock:
            self._now += timedelta(**delta)

    def set(self, moment):
        with self._lock:
            self._now = moment


def _cold_state():
    world = WorldState(
        clock=CLOCK,
        locations=build_default_location_graph(),
        channels=build_default_channel_registry(),
    )
    grant_everything(world)
    world.place_character("mizuki", "mizuki_home_room")
    world.place_character("ena", "ena_home_studio")
    world.join_channel("mizuki", "nightcord")
    world.join_channel("ena", "nightcord")
    state = SessionState(session_id="s1", scene="gate", characters=["mizuki", "ena"])
    state.attach_world_state(world)
    state.initialize_runtime("开场")
    return state


def _adapters(line="在的哦"):
    return RuntimeAdapters(
        auditor=ScriptedAuditor(),
        policy_factory=lambda state: AuthoredLinePolicy(
            ScriptedLineGenerator({"mizuki": line, "ena": line}),
            recall=MemoryRecall(state),
        ),
    )


class WorkerTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "worlds"
        self.service = WorldLifecycleService(FileWorldStore(self.root))
        self.before = set(clock_threads())
        self.addCleanup(self._no_worker_left)
        self.addCleanup(self.service.release_all)
        self.wall = FakeWall()

    def _no_worker_left(self):
        wait_for(
            lambda: not (set(clock_threads()) - self.before), what="时钟 worker 退出"
        )

    def open(self, config=FAST, *, line="在的哦", policy=None):
        return self.service.create(
            "nightcord",
            _cold_state(),
            adapters=_adapters(line),
            clock=config,
            wall_clock=self.wall,
            checkpoint_policy=policy,
        )

    def restore(self, config=FAST, wall=None):
        return self.service.restore(
            "nightcord",
            adapters=_adapters(),
            clock=config,
            wall_clock=wall if wall is not None else self.wall,
        )

    @staticmethod
    def schedule(world, activation_id, minutes, character_id="mizuki"):
        world.runtime.scheduler.schedule(
            ScheduledActivation(
                activation_id=activation_id,
                kind=ActivationKind.CHARACTER_ACTIVATION,
                due_at=CLOCK + timedelta(minutes=minutes),
                character_id=character_id,
            )
        )

    @staticmethod
    def clock(world):
        return world.state.world_state.clock

    @staticmethod
    def record(world, activation_id):
        for record in world.state.agency.records():
            if record.due_id.startswith(f"{activation_id}@"):
                return record
        return None


class TimeFollowsTheWallTests(WorkerTestCase):
    def test_the_world_clock_follows_the_wall_without_start(self):
        world = self.open()
        self.assertEqual(world.clock_worker.status()["cognition_causes"], ["not_started"])
        self.wall.advance(minutes=30, seconds=40)
        wait_for(lambda: self.clock(world) == CLOCK + timedelta(minutes=30), what="走到 19:30")
        time.sleep(0.05)
        self.assertEqual(self.clock(world), CLOCK + timedelta(minutes=30), "不超过锚点")

    def test_start_and_stop_do_not_touch_the_worker(self):
        world = self.open()
        worker = world.clock_worker
        worker.start_cognition()
        worker.stop_cognition()
        self.assertTrue(worker.alive)
        self.assertEqual(len(set(clock_threads()) - self.before), 1)
        with self.assertRaises(ClockWorkerError):
            worker.start()

    def test_a_static_world_does_not_write_a_revision_every_iteration(self):
        world = self.open(policy=CheckpointPolicy(every_boundaries=1, min_interval_seconds=0))
        revision = world.revision
        time.sleep(0.1)  # 几十轮，现实时间没动
        self.assertEqual(world.revision, revision)
        self.wall.advance(minutes=1)
        wait_for(lambda: world.revision > revision, what="过了一个边界才落盘")


class RunBudgetStatusTests(WorkerTestCase):
    def test_the_budget_says_not_started_finite_and_unlimited_apart(self):
        world = self.open()
        worker = world.clock_worker
        # 还没 Start：显示 Start 会给的配置值。
        budget = worker.status()["run_budget"]
        self.assertEqual(
            (budget["limit"], budget["used"], budget["remaining"]),
            (FAST.max_activations_per_run, 0, FAST.max_activations_per_run),
        )
        worker.start_cognition()
        self.assertEqual(worker.status()["run_budget"]["limit"], FAST.max_activations_per_run)
        # 不限额的 Start 没有 N：不拿配置值冒充成有限额度（COG-1 实现审 F1）。
        world.runtime.start_cognition(None)
        budget = worker.status()["run_budget"]
        self.assertEqual((budget["limit"], budget["used"], budget["remaining"]), (None, None, None))
        worker.stop_cognition()
        self.assertIsNone(worker.status()["run_budget"]["limit"])


class OwnershipAndStartupTests(WorkerTestCase):
    def test_close_stops_the_worker_first_and_closes_clean(self):
        world = self.open()
        status = self.service.close("nightcord")
        self.assertTrue(status["clean"])
        self.assertFalse(world.clock_worker.alive)
        self.assertIsNone(self.service.opened("nightcord"))

    def test_a_worker_that_cannot_start_leaves_nothing_behind(self):
        with patch.object(ClockWorker, "start", side_effect=ClockWorkerError("boom")):
            with self.assertRaises(ClockWorkerError):
                self.open()
        self.assertIsNone(self.service.opened("nightcord"))
        # 所有权已经还回去：首存档是一份合法的开局存档，可以恢复。
        world = self.restore()
        self.assertTrue(world.clock_worker.alive)


class StuckModelCallTests(WorkerTestCase):
    def _stuck(self):
        entered, release = threading.Event(), threading.Event()

        def line(context):
            entered.set()
            release.wait(10)
            return "在的哦"

        self.addCleanup(release.set)
        world = self.open(line=line)
        world.clock_worker.start_cognition()
        self.schedule(world, "wake", 5)
        self.wall.advance(minutes=5)
        self.assertTrue(entered.wait(5), "模型调用没有开始")
        return world, release

    def test_a_plain_close_refuses_while_a_call_is_in_flight(self):
        world, release = self._stuck()
        with self.assertRaises(LifecycleError):
            self.service.close("nightcord")
        self.assertIsNotNone(self.service.opened("nightcord"), "拒绝的关闭不还所有权")
        release.set()
        wait_for(lambda: not world.clock_worker.alive, what="worker 退出")
        self.assertTrue(self.service.close("nightcord")["clean"])

    def test_force_close_does_not_wait_and_the_late_result_does_not_land(self):
        world, release = self._stuck()
        status = self.service.close("nightcord", force=True)
        self.assertTrue(status["closed"])
        revision = status["revision"]
        release.set()
        wait_for(lambda: not world.clock_worker.alive, what="worker 退出")
        self.assertEqual(world.state.events.by_type(EventType.MESSAGE_SENT), ())
        self.assertIsNone(self.record(world, "wake"), "终局停机之后不许落地")
        restored = self.restore()
        self.assertEqual(restored.revision, revision)


class FaultTests(WorkerTestCase):
    def _flaky(self, world, error=None):
        failing = {"on": True, "attempts": 0}
        real = world.runtime.advance_to_anchor

        def advance(*args, **kwargs):
            if failing["on"]:
                failing["attempts"] += 1
                raise error if error is not None else AutonomyError("时钟步写不进去")
            return real(*args, **kwargs)

        world.runtime.advance_to_anchor = advance
        return failing

    def test_repeated_failures_fault_back_off_and_recover(self):
        config = ClockConfig(
            interval_seconds=0.005,
            fault_threshold=2,
            max_backoff_seconds=0.1,
            stop_timeout_seconds=0.3,
        )
        world = self.open(config)
        world.clock_worker.start_cognition()
        self.schedule(world, "during", 5)
        self.schedule(world, "crossed", 40)  # 恢复后补跑时经过，仍在故障切点之前
        self.schedule(world, "after", 55)
        failing = self._flaky(world)
        self.wall.advance(minutes=10)
        worker = world.clock_worker
        wait_for(lambda: worker.status()["clock_state"] == CLOCK_FAULTED, what="进入 faulted")
        wait_for(lambda: C.FAULT in world.state.cognition.current.causes, what="故障写进时间线")
        self.assertEqual(self.clock(world), CLOCK, "故障期间时钟不动")

        # 退避：0.3 秒里，无退避要试 ~60 次；有退避（上限 0.1 秒）只有几次。
        start = failing["attempts"]
        time.sleep(0.3)
        self.assertLess(failing["attempts"] - start, 10, "faulted 之后还在忙等")
        self.assertIn("时钟步写不进去", worker.status()["last_clock_error"])

        failing["on"] = False
        self.wall.advance(minutes=40)
        wait_for(lambda: worker.status()["clock_state"] == CLOCK_HEALTHY, what="恢复 healthy")
        wait_for(lambda: self.clock(world) == CLOCK + timedelta(minutes=50), what="补跑完")
        for name in ("during", "crossed"):
            record = self.record(world, name)
            self.assertIs(record.outcome, AgencyOutcome.REJECTED_UNAVAILABLE, name)
            self.assertIn("fault", record.detail["causes"], name)
        # 解除切点（19:51）之后触发的，照常做决定。
        self.wall.advance(minutes=10)
        wait_for(lambda: self.record(world, "after") is not None, what="19:55 那条")
        self.assertIs(self.record(world, "after").outcome, AgencyOutcome.ACTED)
        self.assertNotIn(C.FAULT, world.state.cognition.current.causes)
        SessionState.from_dict(world.state.to_dict())

    def test_a_foreign_error_stays_opaque(self):
        world = self.open()
        canary = "sk-canary-0000"
        hostile = type(canary, (RuntimeError,), {"__module__": "anthropic"})(canary)
        self._flaky(world, hostile)
        self.wall.advance(minutes=1)
        wait_for(lambda: world.clock_worker.status()["failures"] > 0, what="失败一次")
        status = world.clock_worker.status()
        self.assertEqual(status["last_error"], OPAQUE_ERROR)
        self.assertNotIn(canary, repr(status))


class WallClockTests(WorkerTestCase):
    def test_a_wall_clock_behind_the_archive_holds_the_clock_until_it_catches_up(self):
        world = self.open()
        self.wall.advance(minutes=10)
        wait_for(lambda: self.clock(world) == CLOCK + timedelta(minutes=10), what="走到 19:10")
        self.service.close("nightcord")

        behind = FakeWall(WALL + timedelta(minutes=2))  # UTC 被往回拨了 8 分钟
        world = self.restore(wall=behind)
        self.assertIn(C.WALL_CLOCK_BEHIND, world.state.cognition.current.causes)
        time.sleep(0.05)
        self.assertEqual(self.clock(world), CLOCK + timedelta(minutes=10), "不倒退")
        behind.set(WALL + timedelta(minutes=15))
        wait_for(lambda: self.clock(world) == CLOCK + timedelta(minutes=15), what="追上后接着走")
        self.assertNotIn(C.WALL_CLOCK_BEHIND, world.state.cognition.current.causes)
        SessionState.from_dict(world.state.to_dict())

    def test_restore_rebases_to_the_configured_rate_without_a_jump(self):
        fast = ClockConfig(interval_seconds=0.005, rate=60.0, stop_timeout_seconds=0.3)
        world = self.open(fast)
        self.service.close("nightcord")
        later = FakeWall(WALL + timedelta(seconds=30))  # 按 60 倍：离线 30 分钟
        world = self.restore(FAST, wall=later)
        self.assertEqual(world.state.anchor.rate, 1.0)
        self.assertEqual(world.state.anchor.sim_at(later()), CLOCK + timedelta(minutes=30))
        wait_for(lambda: self.clock(world) == CLOCK + timedelta(minutes=30), what="补跑离线的 30 分钟")


class ProductionTests(unittest.TestCase):
    def test_production_refuses_a_fast_clock(self):
        with self.assertRaises(ClockWorkerError):
            ClockConfig(rate=2, require_real_time=True)
        ClockConfig(rate=1, require_real_time=True)
        with patch.dict(os.environ, {CLOCK_RATE_ENV: "60"}):
            with self.assertRaises(CompositionError):
                AutonomySettings.from_env(production=True)
            self.assertEqual(AutonomySettings.from_env().clock.rate, 60.0)

    def test_importing_the_worker_starts_nothing(self):
        code = (
            "import threading, pns.runtime.autonomy.clock_worker as m;"
            "print(len(threading.enumerate()))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "1")


if __name__ == "__main__":
    unittest.main()
