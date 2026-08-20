# tests/test_session_runtime.py — SessionRuntime 编排逻辑的单测。
#
# 不依赖 FastAPI/WebSocket：直接构造 SessionRuntime，monkeypatch 掉
# 会打真实 API 的 call_character_async / judge_async，断言 run() 产出的
# 消息序列、drift JSONL 落盘、history md 落盘和 SessionState 记录。
#
# 运行: python -m unittest tests.test_session_runtime -v
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pns.runtime.session_runtime import SessionRuntime, SessionSetupError
from pns.world.characters import registry as character_registry


async def _run(runtime):
    return [msg async for msg in runtime.run()]


class SessionSetupTests(unittest.TestCase):
    """SessionRuntime.create() 校验路径 — 对应原来 simulate.py 里三条早退分支。"""

    def test_rejects_fewer_than_two_characters(self):
        with self.assertRaises(SessionSetupError):
            SessionRuntime.create({"characters": ["mizuki"]})

    def test_rejects_unknown_character(self):
        with self.assertRaises(SessionSetupError):
            SessionRuntime.create({"characters": ["mizuki", "not_a_real_character"]})

    @patch("pns.runtime.session_runtime.router_mod._get_api_key", return_value="")
    def test_rejects_missing_api_key(self, _mock_key):
        with self.assertRaises(SessionSetupError):
            SessionRuntime.create({"characters": ["mizuki", "ena"]})


class SessionRuntimeRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.history_dir = Path(self._tmp.name) / "history"
        self.drift_file = Path(self._tmp.name) / "drift_scores.jsonl"
        self.drift_file.parent.mkdir(parents=True, exist_ok=True)
        self.drift_file.touch()

        self._key_patch = patch("pns.runtime.session_runtime.router_mod._get_api_key", return_value="test-key")
        self._client_patch = patch("pns.runtime.session_runtime.router_mod.create_client", return_value=object())
        self._key_patch.start()
        self._client_patch.start()

    def tearDown(self):
        self._key_patch.stop()
        self._client_patch.stop()
        self._tmp.cleanup()

    def _create(self, **params):
        base = {"characters": ["mizuki", "ena"], "max_turns": 2, "api_delay": 0}
        base.update(params)
        return SessionRuntime.create(base, history_dir=self.history_dir, drift_scores_file=self.drift_file)

    async def test_happy_path_message_sequence_and_persistence(self):
        async def fake_call(client, character, history, scene, model, max_tokens, temperature, correction):
            return f"reply-from-{character}"

        async def fake_judge(client, character, message, turn, scene, **kwargs):
            return {"drift_score": 1, "is_ooc": False, "evaluator_provider": "test", "evaluator_model": "test-judge"}

        runtime = self._create()
        with patch("pns.runtime.session_runtime.call_character_async", fake_call), \
             patch("pns.runtime.session_runtime.judge_async", fake_judge):
            messages = await _run(runtime)

        types = [m["type"] for m in messages]
        self.assertEqual(types, ["start", "generating", "judging", "turn", "generating", "judging", "turn", "done"])

        turn_messages = [m for m in messages if m["type"] == "turn"]
        self.assertEqual(turn_messages[0]["character"], "mizuki")
        self.assertEqual(turn_messages[0]["reply"], "reply-from-mizuki")
        self.assertEqual(turn_messages[1]["character"], "ena")

        done = messages[-1]
        self.assertEqual(done["stats"]["total_turns"], 2)
        self.assertIsNotNone(done["history_file"])
        self.assertTrue(Path(done["history_file"]).exists())

        drift_lines = self.drift_file.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(drift_lines), 2)
        record = json.loads(drift_lines[0])
        self.assertEqual(record["character"], "mizuki")
        self.assertEqual(record["evaluator_model"], "test-judge")

        self.assertEqual(len(runtime.state.turns), 2)
        self.assertEqual(runtime.state.turns[0].character, "mizuki")

    async def test_generation_error_still_emits_done(self):
        # 保留原 simulate.py 的既有行为：轮内异常 break 出循环，但仍然会
        # 走到结尾发一条 "done"（哪怕本轮没有产出任何 turn）。
        async def failing_call(*args, **kwargs):
            raise ValueError("boom")

        runtime = self._create()
        with patch("pns.runtime.session_runtime.call_character_async", failing_call):
            messages = await _run(runtime)

        types = [m["type"] for m in messages]
        self.assertEqual(types, ["start", "generating", "error", "done"])
        self.assertEqual(messages[-2]["message"], "boom")
        self.assertEqual(messages[-1]["stats"]["total_turns"], 0)
        # 没有任何轮次落地，history 不应该被写出
        self.assertIsNone(messages[-1]["history_file"])
        self.assertEqual(self.drift_file.read_text(encoding="utf-8"), "")

    async def test_character_not_ready_error_uses_detail(self):
        async def not_ready(*args, **kwargs):
            raise character_registry.CharacterNotReadyError("mizuki", "缺少 prompt")

        runtime = self._create()
        with patch("pns.runtime.session_runtime.call_character_async", not_ready):
            messages = await _run(runtime)

        error_msg = next(m for m in messages if m["type"] == "error")
        self.assertEqual(error_msg["character"], "mizuki")
        self.assertIn("缺少 prompt", error_msg["message"])


if __name__ == "__main__":
    unittest.main()
