# tests/test_cache_warming.py — 提示词缓存续命（COST-2）
#
# 守的线（实施单 kickoff/COST_2_CACHE_WARMING_KICKOFF.md §3、§8–§10）：
#   I1 续命只重放被捕获的真实请求：与它相比只有 messages、max_tokens、thinking 三处不同，
#      采样参数（extra_body.temperature）原样带着；
#   I2 续命只碰 provider、账本和自己的内存状态；
#   I3 认知不可用（没 Start、Stop、故障……）时不续；
#   I4 续命的成败不进账本的连续失败；
#   I6 窗口只看真实调用，续命续不出续命；
#   F2 只有可用的 operation 才成为候选；seq 更新的才覆盖；状态挂在世界自己的 client 上；
#   F2.a 续命与真实请求在同一把 send lock 里串行，两种顺序都测；
#   F1 lag 预算：只在干净的一轮之后续，花的时间从等待里扣。
#
# 运行: python -m unittest discover -s tests -p test_cache_warming.py
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts"), str(REPO_ROOT / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from pns.interfaces.cache_warmer import WARM_PROMPT, CacheWarmer  # noqa: E402
from pns.interfaces.composition import AutonomySettings, CompositionError, WorldControlPlane  # noqa: E402
from pns.interfaces.usage_meter import MeteredClient, UsageMeter  # noqa: E402
from pns.runtime import usage_ledger as ul  # noqa: E402
from pns.runtime.autonomy.clock_worker import ClockConfig, ClockWorker  # noqa: E402
from pns.runtime.reload import BOUNDARY  # noqa: E402

from test_usage_meter import KEY, FakeClient, _response, _usage  # noqa: E402

NOW = datetime(2026, 10, 9, 14, tzinfo=timezone.utc)


class FakeMonotonic:
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class RecordingClient:
    """记下每一次发出去的请求；`hold` 给定时，发送会停在那里等它放行。"""

    def __init__(self):
        self.messages = self
        self.requests = []
        self.fail = None
        self.in_flight = 0
        self.max_in_flight = 0
        self.hold = None
        self.entered = threading.Event()
        self._lock = threading.Lock()

    def create(self, **kwargs):
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.entered.set()
        try:
            if self.hold is not None and kwargs.get("max_tokens") != 1:
                self.hold.wait(timeout=5)
            self.requests.append(kwargs)
            if self.fail is not None:
                raise self.fail
            return _response("嗯", usage=_usage(fresh=100, cached=2304, out=1))
        finally:
            with self._lock:
                self.in_flight -= 1


def real_request(system="【角色宪法】奏", temperature=0.85):
    return {
        "model": "mimo-v2.5-pro",
        "max_tokens": 1024,
        "system": system,
        "messages": [{"role": "user", "content": "【此刻】2026-10-09 01:00"}],
        "extra_body": {"temperature": temperature},
    }


class WarmerTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ledger = ul.UsageLedger(Path(tmp.name) / "usage", clock=lambda: NOW)
        self.now = FakeMonotonic()
        self.warmer = CacheWarmer(
            window_seconds=1200, interval_seconds=180, protocol="anthropic", monotonic=self.now
        )
        self.meter = UsageMeter(
            self.ledger, world_id="yoake-mae", protocol="anthropic", prices={}, warmer=self.warmer
        )
        self.raw = RecordingClient()
        self.client = MeteredClient(self.raw, self.meter)
        self.warmer.attach(self.client, self.meter)

    def real_call(self, character="kanade", path="generation", ok=True, request=None):
        with self.meter.operation(path, character, "mimo-v2.5-pro") as op:
            self.client.messages.create(**(request or real_request()))
            op.settle(ok)

    def rows(self):
        records, corrupt = self.ledger.read()
        self.assertEqual(corrupt, 0)
        return records


class ReplayTests(WarmerTestCase):
    def test_warm_replays_the_captured_request_with_only_three_changes(self):
        self.real_call()
        self.now.advance(180)
        self.assertTrue(self.warmer.warm_once(timeout=5))
        real, warm = self.raw.requests
        self.assertEqual(warm.pop("timeout"), 5)  # SDK 参数，不发给 provider
        for key in ("messages", "max_tokens", "thinking"):
            real.pop(key, None)
        changed = {k: warm.pop(k) for k in ("messages", "max_tokens", "thinking")}
        self.assertEqual(warm, real, "除三处以外逐字节一致（含 extra_body.temperature）")
        self.assertEqual(changed["max_tokens"], 1)
        self.assertEqual(changed["thinking"], {"type": "disabled"})
        self.assertEqual(changed["messages"], [{"role": "user", "content": WARM_PROMPT}])

    def test_replay_does_not_share_mutable_state_with_the_caller(self):
        request = real_request()
        self.real_call(request=request)
        request["system"] = "被调用方改掉了"
        request["extra_body"]["temperature"] = 0.1
        self.now.advance(180)
        self.warmer.warm_once(timeout=5)
        warm = self.raw.requests[-1]
        self.assertEqual(warm["system"], "【角色宪法】奏")
        self.assertEqual(warm["extra_body"], {"temperature": 0.85})

    def test_openai_keeps_the_system_message_only(self):
        warmer = CacheWarmer(window_seconds=1200, interval_seconds=180, protocol="openai")
        prefix = warmer.capture(
            "generation", "kanade", "m",
            {"model": "m", "temperature": 0.85, "max_tokens": 1024,
             "messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]},
        )
        warm = warmer.warm_request(prefix)
        self.assertEqual(
            warm["messages"], [{"role": "system", "content": "S"}, {"role": "user", "content": WARM_PROMPT}]
        )
        self.assertNotIn("thinking", warm)
        self.assertEqual((warm["temperature"], warm["max_tokens"]), (0.85, 1))


class CandidateTests(WarmerTestCase):
    def test_only_usable_operations_become_candidates(self):
        self.real_call(ok=False)
        self.now.advance(180)
        self.assertFalse(self.warmer.warm_once(timeout=5), "不可用的 operation 不续")
        self.assertEqual(len(self.raw.requests), 1)

    def test_failed_transport_is_not_a_candidate(self):
        self.raw.fail = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.real_call()
        self.raw.fail = None
        self.now.advance(180)
        self.assertFalse(self.warmer.warm_once(timeout=5))

    def test_a_later_seq_wins_even_if_it_completes_first(self):
        a = self.warmer.capture("generation", "kanade", "m", real_request(system="A"))
        b = self.warmer.capture("generation", "kanade", "m", real_request(system="B"))
        self.assertTrue(self.warmer.promote(b))
        self.assertFalse(self.warmer.promote(a), "旧请求晚完成，不许覆盖新的")
        self.now.advance(180)
        self.assertEqual(self.warmer.due().request["system"], "B")

    def test_keys_are_separate_per_path_and_character(self):
        self.real_call(character="kanade", request=real_request(system="K"))
        self.real_call(character="mafuyu", request=real_request(system="M"))
        self.real_call(character="kanade", path="judge", request=real_request(system="J"))
        self.now.advance(180)
        sent = []
        while self.warmer.warm_once(timeout=5):
            sent.append(self.raw.requests[-1]["system"])
        self.assertEqual(sorted(sent), ["J", "K", "M"])

    def test_warm_traffic_is_never_captured_as_a_real_call(self):
        self.real_call()
        for _ in range(3):
            self.now.advance(180)
            self.warmer.warm_once(timeout=5)
        prefix = self.warmer.due() or self.warmer._slots[("generation", "kanade", "mimo-v2.5-pro", "anthropic")].prefix
        self.assertEqual(prefix.seq, 1)
        self.assertEqual(prefix.request["max_tokens"], 1024)

    def test_no_warmer_means_no_capture(self):
        meter = UsageMeter(self.ledger, world_id="w", protocol="anthropic", prices={})
        client = MeteredClient(self.raw, meter)
        with meter.operation("generation", "kanade", "m") as op:
            client.messages.create(**real_request())
            op.settle(True)
        self.assertIsNone(op.prefix)


class TimingTests(WarmerTestCase):
    def test_interval_is_respected(self):
        self.real_call()
        self.now.advance(179)
        self.assertIsNone(self.warmer.due())
        self.now.advance(1)
        self.assertIsNotNone(self.warmer.due())

    def test_window_counts_from_the_last_real_call_not_the_last_warm(self):
        self.real_call()
        warms = 0
        for _ in range(20):
            self.now.advance(180)
            warms += self.warmer.warm_once(timeout=5)
        # 1200 秒的窗口、每 180 秒一次：第 6 次在 1080 秒，第 7 次在 1260 秒已经出窗。
        self.assertEqual(warms, 6, "续命成功不延长窗口")

    def test_a_new_real_call_reopens_the_window(self):
        self.real_call()
        self.now.advance(1300)
        self.assertIsNone(self.warmer.due())
        self.real_call()
        self.now.advance(180)
        self.assertIsNotNone(self.warmer.due())

    def test_the_stalest_key_is_warmed_first(self):
        self.real_call(character="kanade")
        self.now.advance(60)
        self.real_call(character="mafuyu")
        self.now.advance(200)
        self.assertEqual(self.warmer.due().key[1], "kanade")


class FailureTests(WarmerTestCase):
    def test_warm_failure_never_raises_and_is_recorded_as_warm(self):
        self.real_call()
        self.raw.fail = RuntimeError(KEY)
        self.now.advance(180)
        self.assertTrue(self.warmer.warm_once(timeout=5))
        gen, warm = self.rows()
        self.assertEqual((warm.path, warm.operation, warm.transport), ("warm", "failed", "error"))
        self.assertNotIn(KEY, (Path(self.ledger.root) / "2026-10-09.jsonl").read_text())

    def test_warm_does_not_paint_or_clear_the_failure_streak(self):
        self.real_call()
        self.raw.fail = RuntimeError("down")
        self.now.advance(180)
        self.warmer.warm_once(timeout=5)
        self.assertEqual(ul.failure_streak(self.rows())["consecutive"], 0, "续命失败不染红")
        with self.assertRaises(RuntimeError):
            self.real_call(character="mafuyu")
        self.raw.fail = None
        self.now.advance(180)
        self.warmer.warm_once(timeout=5)
        self.assertEqual(ul.failure_streak(self.rows())["consecutive"], 1, "续命成功不洗白")

    def test_three_failures_pause_until_the_same_key_really_succeeds(self):
        self.real_call(character="kanade")
        self.raw.fail = RuntimeError("down")
        for _ in range(3):
            self.now.advance(180)
            self.assertTrue(self.warmer.warm_once(timeout=5))
        self.now.advance(180)
        self.assertIsNone(self.warmer.due(), "暂停")
        self.raw.fail = None
        self.real_call(character="mafuyu")  # 别的 key 的真实调用不解除
        self.now.advance(180)
        self.assertEqual(self.warmer.due().key[1], "mafuyu")
        self.warmer.warm_once(timeout=5)
        self.now.advance(180)
        self.assertNotEqual((self.warmer.due() or SimpleNamespace(key=(0, "x"))).key[1], "kanade")
        self.real_call(character="kanade")
        self.now.advance(180)
        keys = set()
        while (prefix := self.warmer.due()) is not None:
            keys.add(prefix.key[1])
            self.warmer.warm_once(timeout=5)
        self.assertIn("kanade", keys, "同一 key 的真实、可用调用解除暂停")

    def test_summary_lists_warm_separately(self):
        self.real_call()
        self.now.advance(180)
        self.warmer.warm_once(timeout=5)
        summary = ul.summarize(self.ledger, days=1, anchor=None, now=NOW)
        self.assertEqual(summary["by_path"]["warm"]["calls"], 1)
        self.assertEqual(summary["by_path"]["generation"]["calls"], 1)


class SendLockTests(WarmerTestCase):
    def _race(self, real_first):
        self.real_call()
        self.now.advance(180)
        self.raw.hold = threading.Event()
        self.raw.entered.clear()
        real = threading.Thread(target=self.real_call, kwargs={"character": "mafuyu"})
        warm = threading.Thread(target=self.warmer.warm_once, kwargs={"timeout": 5})
        first, second = (real, warm) if real_first else (warm, real)
        first.start()
        self.raw.entered.wait(timeout=5)
        second.start()
        second.join(timeout=0.2)
        self.raw.hold.set()
        first.join(timeout=5)
        second.join(timeout=5)
        self.assertEqual(self.raw.max_in_flight, 1, "同一个世界的发送不交错")

    def test_real_then_warm(self):
        self._race(real_first=True)

    def test_warm_then_real(self):
        self._race(real_first=False)


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.registry = BOUNDARY.active()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        env = patch.dict(os.environ, {self.registry.models.key_name: KEY})
        env.start()
        self.addCleanup(env.stop)
        self.client = FakeClient()
        calls = []
        original = self.client.messages.create

        def create(**kwargs):
            calls.append(kwargs)
            return original(**kwargs)

        self.client.messages.create = create
        self.calls = calls
        self.ledger = ul.UsageLedger(self.tmp / "usage", clock=lambda: NOW)

    def plane(self, **autonomy):
        settings = AutonomySettings(
            clock=ClockConfig(), cadence=AutonomySettings.from_env().cadence, **autonomy
        )
        return WorldControlPlane(
            root=self.tmp / "worlds",
            client_factory=lambda *a, **k: self.client,
            usage_ledger=self.ledger,
            autonomy=settings,
        )

    def test_each_world_gets_its_own_warmer(self):
        plane = self.plane()
        a = plane.build_adapters(self.registry, world_id="a")
        b = plane.build_adapters(self.registry, world_id="b")
        self.assertIsNotNone(a.warmer)
        self.assertIsNot(a.warmer, b.warmer)
        self.assertEqual((a.warmer.window_seconds, a.warmer.interval_seconds), (1200.0, 180.0))

    def test_window_zero_turns_it_off(self):
        adapters = self.plane(warm_window_seconds=0).build_adapters(self.registry, world_id="a")
        self.assertIsNone(adapters.warmer)

    def test_real_generation_is_captured_with_its_temperature(self):
        adapters = self.plane().build_adapters(self.registry, world_id="a")
        state = self.plane().new_session_state(
            world_id="w", scene_id=self.registry.default_scene,
            character_ids=["kanade"], registry=self.registry,
        )
        call = adapters.policy_factory(state)._generator._call
        call("kanade", self.registry.scene(None), [{"role": "user", "content": "在吗"}])
        (slot,) = adapters.warmer._slots.values()
        self.assertEqual(slot.prefix.key[:2], ("generation", "kanade"))
        self.assertEqual(slot.prefix.request["extra_body"], {"temperature": 0.85})
        self.assertEqual(slot.prefix.request["system"], self.calls[0]["system"])

    def test_bad_settings_are_refused(self):
        for kwargs in ({"warm_window_seconds": -1}, {"warm_interval_seconds": 5}, {"warm_window_seconds": "20m"}):
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}):
                with self.assertRaises(CompositionError):
                    self.plane(**kwargs)


class _StubRuntime:
    def __init__(self, admissible=True):
        self.admissible = admissible
        self.asked = 0

    def warm_admissible(self):
        self.asked += 1
        return self.admissible


class _StubWarmer:
    def __init__(self, monotonic, cost):
        self.monotonic = monotonic
        self.cost = cost
        self.calls = []

    def warm_once(self, timeout):
        self.calls.append(timeout)
        self.monotonic.advance(self.cost)
        return True


def _stub_worker(admissible=True, cost=1.5, interval=5.0):
    now = FakeMonotonic()
    warmer = _StubWarmer(now, cost)
    world = SimpleNamespace(
        world_id="w", runtime=_StubRuntime(admissible), checkpoint_if_due=lambda reason: None,
        warmer=warmer, closed=False,
    )
    worker = ClockWorker(world, ClockConfig(interval_seconds=interval), monotonic=now)
    return worker, warmer, world


class ClockWorkerLagTests(unittest.TestCase):
    def test_warm_time_is_taken_out_of_the_wait(self):
        worker, warmer, _ = _stub_worker(cost=1.5)
        worker._round_clean = True
        self.assertAlmostEqual(worker._warm(5.0), 3.5, msg="一轮总时长不因续命变长")
        self.assertEqual(warmer.calls, [5.0], "超时不超过这一轮本来要等的时间")

    def test_a_slow_warm_never_makes_the_wait_negative(self):
        worker, _, _ = _stub_worker(cost=9.0)
        worker._round_clean = True
        self.assertEqual(worker._warm(5.0), 0.0)

    def test_no_warm_after_an_unclean_round(self):
        worker, warmer, world = _stub_worker()
        worker._round_clean = False
        self.assertEqual(worker._warm(5.0), 5.0)
        self.assertEqual((warmer.calls, world.runtime.asked), ([], 0))

    def test_no_warm_when_cognition_is_unavailable(self):
        worker, warmer, _ = _stub_worker(admissible=False)
        worker._round_clean = True
        self.assertEqual(worker._warm(5.0), 5.0)
        self.assertEqual(warmer.calls, [])

    def test_no_warm_once_stop_was_asked(self):
        worker, warmer, _ = _stub_worker()
        worker._round_clean = True
        worker._stop_event.set()
        self.assertEqual(worker._warm(5.0), 5.0)
        self.assertEqual(warmer.calls, [])

    def test_only_a_clean_round_is_clean(self):
        cases = [
            (({"minutes": 1, "results": []}, 0), True),   # 正常走了一分钟
            (({"minutes": 0, "results": []}, 0), True),   # 这一轮没到下一分钟
            (({"minutes": 3, "results": []}, 2), False),  # 补跑中
            (({"minutes": 0, "results": []}, 4), False),  # 落后
        ]
        for step, clean in cases:
            with self.subTest(step=step):
                worker, _, _ = _stub_worker()
                worker._step = lambda step=step: step
                worker._record_success = lambda *a, **k: None
                worker._iterate()
                self.assertIs(worker._round_clean, clean)

    def test_a_failed_round_is_not_clean(self):
        worker, _, _ = _stub_worker()

        def boom():
            raise RuntimeError("x")

        worker._step = boom
        worker._record_failure = lambda e: 5.0
        worker._round_clean = True
        worker._iterate()
        self.assertFalse(worker._round_clean)

    def test_worker_without_warmer_is_unchanged(self):
        now = FakeMonotonic()
        world = SimpleNamespace(
            world_id="w", runtime=_StubRuntime(), checkpoint_if_due=lambda reason: None, closed=False,
        )
        worker = ClockWorker(world, ClockConfig(interval_seconds=5.0), monotonic=now)
        worker._round_clean = True
        self.assertEqual(worker._warm(5.0), 5.0)
        self.assertEqual(world.runtime.asked, 0)


from test_clock_worker import WorkerTestCase, wait_for  # noqa: E402


class AdmissionTests(WorkerTestCase):
    """warm_admissible() 跟真实生成用同一个认知可用性判据（真的 runtime，不是桩）。"""

    def test_follows_the_cognition_timeline(self):
        world = self.open()
        runtime, worker = world.runtime, world.clock_worker
        self.assertFalse(runtime.warm_admissible(), "没 Start")
        worker.start_cognition()
        self.wall.advance(minutes=2)
        wait_for(runtime.warm_admissible, what="Start 之后的下一个整分钟可用")
        runtime.begin_fault(wall=self.wall())
        self.assertFalse(runtime.warm_admissible(), "故障")
        runtime.clear_fault(wall=self.wall())
        # 故障期间的 backlog 盖住解除那一刻；时钟走过去之后才恢复，跟真实生成一样。
        self.wall.advance(minutes=2)
        wait_for(runtime.warm_admissible, what="故障解除、时钟走过 backlog")
        worker.stop_cognition()
        self.assertFalse(runtime.warm_admissible(), "Stop 之后")

    def test_a_stopped_runtime_is_never_admissible(self):
        world = self.open()
        world.clock_worker.start_cognition()
        self.wall.advance(minutes=2)
        wait_for(world.runtime.warm_admissible, what="可用")
        world.close("test")
        self.assertFalse(world.runtime.warm_admissible(), "世界关了")

    def test_a_session_without_a_cognition_timeline_never_warms(self):
        world = self.open()
        state = world.runtime._state
        with patch.object(type(state), "cognition", new=property(lambda self: None)):
            self.assertFalse(world.runtime.warm_admissible())


if __name__ == "__main__":
    unittest.main()
