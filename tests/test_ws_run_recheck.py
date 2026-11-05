# tests/test_ws_run_recheck.py — /ws/run 的身份要跟着连接走（FIX-WS-1）。
#
# 守卫只在握手时认一次身份。盯住的东西：
#   1. 握手以后等权限过期再发参数，不建会话。
#   2. 三个回合内检查点各自挡住它保护的动作：生成、判分、写结果。
#      检查必须在 yield 之后：发送背压期间过期也要挡住。
#   3. 判分期间失效，这一轮不写 drift、不进世界；已提交的轮次还在。
#   4. 复核的是 operate，不只是"认得出来"：降到 observer 也要停。
#   5. 复核本身：缓存、超时、名额满、查询抛错都按拒绝，拒绝不会恢复。
#   6. Accord 关着也能复核。
#
# 运行: python -m unittest tests.test_ws_run_recheck -v
import asyncio
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from starlette.websockets import WebSocket

from pns.interfaces import simulate
from pns.interfaces.authz import SCOPE_OPERATE
from pns.interfaces.recheck import IdentityGate, RecheckSlots
from pns.runtime.session_runtime import STOP_AUTHORIZATION_LOST
from tests.test_session_runtime import RuntimeTestBase, _run


class _Principal:
    def __init__(self, scopes):
        self.scopes = scopes

    def has(self, scope):
        return scope in self.scopes


class _Auth:
    def __init__(self, principal):
        self.principal = principal
        self.calls = 0

    def resolve(self, scope):
        self.calls += 1
        return self.principal


OPERATOR = _Principal({SCOPE_OPERATE, "read"})
OBSERVER = _Principal({"read"})


class RuntimeCheckpointTests(RuntimeTestBase):
    """每轮的检查顺序：生成前、判分前、写结果前。"""

    def _authorize(self, answers):
        answers = iter(answers)

        async def authorize():
            return next(answers)

        return authorize

    async def _drive(self, answers, max_turns=2):
        calls = {"generate": 0, "judge": 0}

        async def fake_call(*args, **kwargs):
            calls["generate"] += 1
            return "reply"

        async def fake_judge(*args, **kwargs):
            calls["judge"] += 1
            return {"drift_score": 1, "is_ooc": False, "evaluator_provider": "t", "evaluator_model": "t"}

        runtime = self._create(max_turns=max_turns)
        runtime._authorize = self._authorize(answers)
        with patch("pns.runtime.session_runtime.call_character_async", fake_call), \
             patch("pns.runtime.session_runtime.judge_async", fake_judge):
            messages = await _run(runtime)
        return runtime, messages, calls

    def _drift_lines(self):
        return [line for line in self.drift_file.read_text(encoding="utf-8").splitlines() if line]

    async def test_lost_before_generation_calls_nothing(self):
        runtime, messages, calls = await self._drive([False])
        self.assertEqual(calls, {"generate": 0, "judge": 0})
        self.assertEqual([m["type"] for m in messages], ["start", "generating", "stopped", "done"])
        self.assertEqual(messages[2]["reason"], STOP_AUTHORIZATION_LOST)
        self.assertEqual(self._drift_lines(), [])

    async def test_lost_before_judging_skips_the_judge(self):
        runtime, messages, calls = await self._drive([True, False])
        self.assertEqual(calls, {"generate": 1, "judge": 0})
        self.assertEqual(self._drift_lines(), [])
        self.assertEqual(runtime.state.turns, [])

    async def test_lost_during_judging_commits_nothing(self):
        fresh = self._create()
        before = len(fresh.state.events)
        fresh.close()
        runtime, messages, calls = await self._drive([True, True, False])
        self.assertEqual(calls, {"generate": 1, "judge": 1})
        self.assertEqual(self._drift_lines(), [])
        self.assertEqual(runtime.state.turns, [])
        self.assertEqual(len(runtime.state.events), before)
        self.assertNotIn("turn", [m["type"] for m in messages])

    async def test_turns_committed_before_the_loss_are_kept(self):
        runtime, messages, calls = await self._drive([True, True, True, False])
        self.assertEqual(calls, {"generate": 1, "judge": 1})
        self.assertEqual(len(runtime.state.turns), 1)
        self.assertEqual(len(self._drift_lines()), 1)
        self.assertTrue(Path(messages[-1]["history_file"]).exists())

    async def test_without_a_hook_nothing_changes(self):
        async def fake_call(*args, **kwargs):
            return "reply"

        async def fake_judge(*args, **kwargs):
            return {"drift_score": 1, "is_ooc": False, "evaluator_provider": "t", "evaluator_model": "t"}

        runtime = self._create(max_turns=1)
        with patch("pns.runtime.session_runtime.call_character_async", fake_call), \
             patch("pns.runtime.session_runtime.judge_async", fake_judge):
            messages = await _run(runtime)
        self.assertEqual([m["type"] for m in messages], ["start", "generating", "judging", "turn", "done"])


class IdentityGateTests(unittest.IsolatedAsyncioTestCase):
    def _gate(self, auth, clock=None, slots=None, **kwargs):
        kwargs.setdefault("timeout_seconds", 1.0)
        if clock is not None:
            kwargs["clock"] = clock
        return IdentityGate(auth, {}, SCOPE_OPERATE, slots or RecheckSlots(), **kwargs)

    async def test_the_answer_is_cached_from_when_the_query_started(self):
        now = [100.0]
        auth = _Auth(OPERATOR)
        gate = self._gate(auth, clock=lambda: now[0])
        self.assertTrue(await gate())
        now[0] = 104.9
        self.assertTrue(await gate())
        self.assertEqual(auth.calls, 1)
        now[0] = 105.0
        self.assertTrue(await gate())
        self.assertEqual(auth.calls, 2)

    async def test_a_revoked_session_is_seen_after_the_cache_expires(self):
        now = [0.0]
        auth = _Auth(OPERATOR)
        gate = self._gate(auth, clock=lambda: now[0])
        self.assertTrue(await gate())
        auth.principal = None
        self.assertTrue(await gate())  # 缓存窗口内
        now[0] = 5.0
        self.assertFalse(await gate())
        self.assertTrue(gate.denied)

    async def test_being_recognised_is_not_enough(self):
        gate = self._gate(_Auth(OBSERVER))
        self.assertFalse(await gate())

    async def test_denial_is_permanent(self):
        auth = _Auth(None)
        gate = self._gate(auth)
        self.assertFalse(await gate())
        auth.principal = OPERATOR
        self.assertFalse(await gate())
        self.assertEqual(auth.calls, 1)

    async def test_a_lookup_that_raises_is_a_denial(self):
        class Broken:
            def resolve(self, scope):
                raise RuntimeError("db is locked")

        self.assertFalse(await self._gate(Broken())())

    async def test_a_stuck_lookup_times_out_as_a_denial_and_keeps_its_slot(self):
        release = threading.Event()

        class Stuck:
            def resolve(self, scope):
                release.wait(5)
                return OPERATOR

        slots = RecheckSlots(1)
        gate = self._gate(Stuck(), slots=slots, timeout_seconds=0.05)
        try:
            self.assertFalse(await gate())
            self.assertIs(slots.run(lambda: True), RecheckSlots.BUSY)
        finally:
            release.set()

    async def test_no_free_slot_times_out_as_a_denial(self):
        slots = RecheckSlots(0)
        self.assertFalse(await self._gate(_Auth(OPERATOR), slots=slots, timeout_seconds=0.05)())


class _FakeRuntime:
    def __init__(self, gate):
        self.gate = gate
        self.closed = False
        self.finished = False

    async def run(self):
        yield {"type": "start"}
        self.gate.denied = True
        yield {"type": "stopped", "reason": STOP_AUTHORIZATION_LOST}
        yield {"type": "done", "stats": {"secret": 1}}
        self.finished = True

    def close(self):
        self.closed = True


def _socket(auth):
    """带 app 的 WebSocket。app.state 只有 admin_auth，没有 Accord 的任何东西。"""
    queue = [
        {"type": "websocket.connect"},
        {"type": "websocket.receive", "text": json.dumps({})},
    ]
    sent = []

    async def receive():
        return queue.pop(0) if queue else {"type": "websocket.disconnect", "code": 1006}

    async def send(message):
        sent.append(message)

    app = SimpleNamespace(state=SimpleNamespace(admin_auth=auth))
    scope = {"type": "websocket", "path": "/ws/run", "headers": [], "query_string": b"", "app": app}
    return WebSocket(scope, receive, send), sent


def _sent_json(sent):
    return [json.loads(m["text"]) for m in sent if m["type"] == "websocket.send"]


def _close_codes(sent):
    return [m.get("code") for m in sent if m["type"] == "websocket.close"]


class HandlerTests(unittest.TestCase):
    def test_identity_lost_before_the_params_arrive_creates_no_session(self):
        ws, sent = _socket(_Auth(None))
        with patch.object(simulate.SessionRuntime, "create") as create:
            asyncio.run(simulate.run_simulation(ws))
        create.assert_not_called()
        self.assertEqual([m["type"] for m in _sent_json(sent)], ["error"])
        self.assertEqual(_close_codes(sent), [1008])

    def test_downgraded_before_the_params_arrive_creates_no_session(self):
        ws, sent = _socket(_Auth(OBSERVER))
        with patch.object(simulate.SessionRuntime, "create") as create:
            asyncio.run(simulate.run_simulation(ws))
        create.assert_not_called()

    def test_identity_lost_mid_run_sends_stopped_and_closes_but_lets_run_finish(self):
        ws, sent = _socket(_Auth(OPERATOR))
        holder = {}

        def create(params, authorize=None):
            holder["runtime"] = _FakeRuntime(authorize)
            return holder["runtime"]

        with patch.object(simulate.SessionRuntime, "create", side_effect=create):
            asyncio.run(simulate.run_simulation(ws))
        self.assertEqual([m["type"] for m in _sent_json(sent)], ["start", "stopped"])
        self.assertEqual(_close_codes(sent), [1008])
        self.assertTrue(holder["runtime"].finished)
        self.assertTrue(holder["runtime"].closed)

    def test_the_gate_reaches_the_runtime_and_a_healthy_run_is_untouched(self):
        ws, sent = _socket(_Auth(OPERATOR))

        class Healthy:
            async def run(self):
                yield {"type": "done"}

            def close(self):
                pass

        seen = {}

        def create(params, authorize=None):
            seen["authorize"] = authorize
            return Healthy()

        with patch.object(simulate.SessionRuntime, "create", side_effect=create):
            asyncio.run(simulate.run_simulation(ws))
        self.assertIsInstance(seen["authorize"], IdentityGate)
        self.assertEqual([m["type"] for m in _sent_json(sent)], ["done"])
        self.assertEqual(_close_codes(sent), [])


if __name__ == "__main__":
    unittest.main()
