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

from pns.models.event import EventScope, EventType
from pns.models.world_state import WorldState
from pns.runtime.event_commit import EventCommitError
from pns.runtime.session_runtime import SessionRuntime, SessionSetupError
from pns.world.characters import registry as character_registry
from pns.world.context import render_session_location
from pns.world.scenes import SCENES


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

    def test_rejects_invalid_parameter_types_before_api_setup(self):
        for params in (
            [],
            {"max_turns": "many"},
            {"characters": ["mizuki", "mizuki"]},
            {"characters": "mizuki,ena"},
        ):
            with self.subTest(params=params), self.assertRaises(SessionSetupError):
                SessionRuntime.create(params)

    @patch("pns.runtime.session_runtime.router_mod._get_api_key", return_value="test-key")
    @patch("pns.runtime.session_runtime.router_mod.create_client", return_value=object())
    def test_scene_without_a_world_mapping_is_a_setup_error(self, _mock_client, _mock_key):
        unmapped = {**SCENES, "user_authored": dict(SCENES["gate"], id="user_authored")}
        with patch("pns.runtime.session_runtime.world_mod.SCENES", unmapped):
            with self.assertRaises(SessionSetupError) as ctx:
                SessionRuntime.create(
                    {"characters": ["mizuki", "ena"], "scene": "user_authored"}
                )
        self.assertIn("user_authored", str(ctx.exception))

    @patch("pns.runtime.session_runtime.router_mod._get_api_key", return_value="test-key")
    @patch("pns.runtime.session_runtime.router_mod.create_client", return_value=object())
    def test_unknown_scene_id_still_falls_back_to_the_default_scene(
        self, _mock_client, _mock_key
    ):
        runtime = SessionRuntime.create(
            {"characters": ["mizuki", "ena"], "scene": "definitely_not_a_scene"}
        )
        self.assertEqual(runtime.scene["id"], "gate")
        self.assertEqual(
            runtime.world.characters_at("kamiyama_high_gate"), ["ena", "mizuki"]
        )

    @patch("pns.runtime.session_runtime.router_mod._get_api_key", return_value="test-key")
    @patch("pns.runtime.session_runtime.router_mod.create_client", return_value=object())
    def test_session_ids_are_unique(self, _mock_client, _mock_key):
        params = {"characters": ["mizuki", "ena"]}
        first = SessionRuntime.create(params)
        second = SessionRuntime.create(params)
        self.assertNotEqual(first.session_id, second.session_id)


class RuntimeTestBase(unittest.IsolatedAsyncioTestCase):
    """共享的 SessionRuntime 夹具：临时归档目录 + 打掉真实 API 的两个补丁。"""

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


class SessionRuntimeRunTests(RuntimeTestBase):
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
        self.assertEqual(runtime.state.status, "completed")
        self.assertEqual(runtime.state.current_character_index, 0)
        self.assertEqual(len(runtime.state.histories["mizuki"]), 3)
        self.assertEqual(runtime.state.final_stats(), done["stats"])
        self.assertEqual(runtime.state.to_dict()["stats"], done["stats"])
        for duplicate in (
            "_histories",
            "_stats",
            "_current_idx",
            "_pending_corrections",
            "_turn_log",
            "_has_run",
        ):
            self.assertFalse(hasattr(runtime, duplicate), duplicate)

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
        self.assertEqual(runtime.state.status, "completed")

    async def test_character_not_ready_error_uses_detail(self):
        async def not_ready(*args, **kwargs):
            raise character_registry.CharacterNotReadyError("mizuki", "缺少 prompt")

        runtime = self._create()
        with patch("pns.runtime.session_runtime.call_character_async", not_ready):
            messages = await _run(runtime)

        error_msg = next(m for m in messages if m["type"] == "error")
        self.assertEqual(error_msg["character"], "mizuki")
        self.assertIn("缺少 prompt", error_msg["message"])

    async def test_judge_error_emits_error_and_done(self):
        async def fake_call(*args, **kwargs):
            return "reply"

        async def failing_judge(*args, **kwargs):
            raise RuntimeError("judge boom")

        runtime = self._create()
        with patch("pns.runtime.session_runtime.call_character_async", fake_call), \
             patch("pns.runtime.session_runtime.judge_async", failing_judge):
            messages = await _run(runtime)

        self.assertEqual([m["type"] for m in messages], ["start", "generating", "judging", "error", "done"])
        self.assertEqual(messages[-2]["message"], "judge boom")
        self.assertEqual(messages[-1]["stats"]["total_turns"], 0)

    async def test_drift_failure_does_not_publish_turn(self):
        async def fake_call(*args, **kwargs):
            return "reply"

        async def fake_judge(*args, **kwargs):
            return {"drift_score": 1, "is_ooc": False}

        runtime = self._create()
        runtime.drift_scores_file = Path(self._tmp.name)
        with patch("pns.runtime.session_runtime.call_character_async", fake_call), \
             patch("pns.runtime.session_runtime.judge_async", fake_judge):
            messages = await _run(runtime)

        self.assertEqual([m["type"] for m in messages], ["start", "generating", "judging", "error", "done"])
        self.assertEqual(runtime.state.turns, [])
        self.assertEqual(messages[-1]["stats"]["total_turns"], 0)

    async def test_runtime_is_single_use(self):
        async def fake_call(*args, **kwargs):
            return "reply"

        async def fake_judge(*args, **kwargs):
            return {"drift_score": 1, "is_ooc": False}

        runtime = self._create(max_turns=1)
        with patch("pns.runtime.session_runtime.call_character_async", fake_call), \
             patch("pns.runtime.session_runtime.judge_async", fake_judge):
            await _run(runtime)
        with self.assertRaises(RuntimeError):
            await _run(runtime)

    async def test_state_owns_pending_corrections(self):
        corrections_seen = []

        async def fake_call(client, character, history, scene, model, max_tokens, temperature, correction):
            corrections_seen.append(correction)
            return "reply"

        async def fake_judge(client, character, message, turn, scene, **kwargs):
            if turn == 1:
                return {"drift_score": 6, "is_ooc": True, "correction": "stay in character"}
            return {"drift_score": 1, "is_ooc": False}

        runtime = self._create(max_turns=3)
        with patch("pns.runtime.session_runtime.call_character_async", fake_call), \
             patch("pns.runtime.session_runtime.judge_async", fake_judge):
            await _run(runtime)

        self.assertEqual(corrections_seen, [None, None, "stay in character"])
        self.assertIsNone(runtime.state.pending_corrections["mizuki"])
        self.assertEqual(runtime.state.final_stats()["ooc_count"], 1)
        self.assertEqual(runtime.state.final_stats()["corrections"], 1)

    async def test_session_state_owns_the_single_authoritative_world_state(self):
        runtime = self._create()

        self.assertIsInstance(runtime.state.world_state, WorldState)
        self.assertIs(runtime.world, runtime.state.world_state)
        # 只允许绑定一次：不存在第二份可变世界状态。
        with self.assertRaises(RuntimeError):
            runtime.state.attach_world_state(runtime.world)

        runtime.world.advance_time(15)
        self.assertEqual(
            runtime.state.to_dict()["world_state"]["time"], runtime.world.time
        )

    async def test_start_message_projects_time_and_place_from_world_state(self):
        runtime = self._create(scene="nightcord")
        runtime.world.advance_time(30)

        async def fake_call(*args, **kwargs):
            return "reply"

        async def fake_judge(*args, **kwargs):
            return {"drift_score": 1, "is_ooc": False}

        with patch("pns.runtime.session_runtime.call_character_async", fake_call), \
             patch("pns.runtime.session_runtime.judge_async", fake_judge):
            messages = await _run(runtime)

        start = messages[0]
        # 遗留 scene 块形状不变……
        self.assertEqual(
            set(start["scene"]), {"id", "label", "trigger", "time", "location"}
        )
        self.assertEqual(start["scene"]["id"], "nightcord")
        self.assertEqual(start["scene"]["trigger"], SCENES["nightcord"]["trigger"])
        # ……但 time/location 来自当前世界状态，不是 scene 里的静态文本。
        self.assertEqual(start["scene"]["time"], "深夜 02:30")
        self.assertNotEqual(start["scene"]["time"], SCENES["nightcord"]["time"])
        self.assertEqual(
            start["scene"]["location"], render_session_location(runtime.world)
        )
        self.assertEqual(start["world"], runtime.world.to_dict())

    async def test_generation_receives_the_live_world_state_not_the_scene_dict(self):
        seen = []

        async def fake_call(client, character, history, context, model, max_tokens, temperature, correction):
            seen.append(context)
            return "reply"

        async def fake_judge(client, character, message, turn, scene, **kwargs):
            # Router 仍然拿遗留 scene（它评估的是对白/世界观确定性，本阶段不动）
            self.assertEqual(scene["id"], "gate")
            return {"drift_score": 1, "is_ooc": False}

        runtime = self._create(max_turns=2)
        with patch("pns.runtime.session_runtime.call_character_async", fake_call), \
             patch("pns.runtime.session_runtime.judge_async", fake_judge):
            await _run(runtime)

        self.assertEqual(len(seen), 2)
        for context in seen:
            self.assertIs(context, runtime.world)

    async def test_markdown_history_projects_place_from_world_state(self):
        async def fake_call(*args, **kwargs):
            return "reply"

        async def fake_judge(*args, **kwargs):
            return {"drift_score": 1, "is_ooc": False}

        runtime = self._create(scene="nightcord", max_turns=1)
        with patch("pns.runtime.session_runtime.call_character_async", fake_call), \
             patch("pns.runtime.session_runtime.judge_async", fake_judge):
            messages = await _run(runtime)

        history = Path(messages[-1]["history_file"]).read_text(encoding="utf-8")
        self.assertIn(f"| 地点 | {render_session_location(runtime.world)} |", history)
        self.assertIn(f"| 时间 | 深夜 02:00 |", history)
        # 标题/开场白仍是遗留 scene 的叙事投影
        self.assertIn(SCENES["nightcord"]["label"], history)

    async def test_closing_run_marks_authoritative_state_cancelled(self):
        runtime = self._create()
        stream = runtime.run()

        start = await anext(stream)
        self.assertEqual(start["type"], "start")
        self.assertEqual(runtime.state.status, "active")

        await stream.aclose()
        self.assertEqual(runtime.state.status, "cancelled")


class SessionRuntimeEventTests(RuntimeTestBase):
    """被接受的发言进世界历史；被拒绝或失败的候选输出永远不进。"""

    @staticmethod
    def _accepting_judge():
        async def fake_judge(*args, **kwargs):
            return {"drift_score": 1, "is_ooc": False}

        return fake_judge

    @staticmethod
    def _reply():
        async def fake_call(client, character, *args, **kwargs):
            return f"reply-from-{character}"

        return fake_call

    async def _run_accepted(self, **params):
        runtime = self._create(**params)
        with patch("pns.runtime.session_runtime.call_character_async", self._reply()), \
             patch("pns.runtime.session_runtime.judge_async", self._accepting_judge()):
            messages = await _run(runtime)
        return runtime, messages

    async def test_accepted_dialogue_becomes_exactly_one_committed_event(self):
        runtime, messages = await self._run_accepted(max_turns=2)

        events = runtime.state.events.events()
        self.assertEqual(len(events), 2)
        self.assertEqual(len(runtime.state.turns), 2)
        self.assertEqual([e.actor_id for e in events], ["mizuki", "ena"])
        self.assertEqual(events[0].payload["text"], "reply-from-mizuki")
        self.assertEqual(events[0].type, EventType.DIALOGUE_SPOKEN)
        self.assertEqual(events[0].scope, EventScope.LOCATION)
        self.assertEqual(events[0].location_id, "kamiyama_high_gate")
        # 同一模拟时刻的多条事件按提交顺序排列
        self.assertEqual(events[0].occurred_at, events[1].occurred_at)
        self.assertEqual(events[1].causation_id, events[0].event_id)

    async def test_the_turn_message_projects_the_committed_event(self):
        runtime, messages = await self._run_accepted(max_turns=2)

        turn_messages = [m for m in messages if m["type"] == "turn"]
        self.assertEqual(
            [m["event_id"] for m in turn_messages],
            [event.event_id for event in runtime.state.events.events()],
        )
        # 遗留字段一个没少
        for message, turn in zip(turn_messages, runtime.state.turns):
            for key, value in turn.to_wire_dict().items():
                self.assertEqual(message[key], value, key)

    async def test_online_sessions_commit_channel_scoped_events(self):
        runtime, _ = await self._run_accepted(scene="nightcord", max_turns=1)

        event = runtime.state.events.latest()
        self.assertEqual(event.scope, EventScope.CHANNEL)
        self.assertEqual(event.channel_id, "nightcord")
        self.assertEqual(event.location_id, "mizuki_home_room")

    async def test_failed_generation_commits_no_event(self):
        async def failing_call(*args, **kwargs):
            raise ValueError("boom")

        runtime = self._create()
        with patch("pns.runtime.session_runtime.call_character_async", failing_call):
            await _run(runtime)
        self.assertEqual(len(runtime.state.events), 0)

    async def test_failed_judgement_commits_no_event(self):
        async def failing_judge(*args, **kwargs):
            raise RuntimeError("judge boom")

        runtime = self._create()
        with patch("pns.runtime.session_runtime.call_character_async", self._reply()), \
             patch("pns.runtime.session_runtime.judge_async", failing_judge):
            await _run(runtime)
        self.assertEqual(len(runtime.state.events), 0)

    async def test_failed_audit_write_commits_no_event(self):
        runtime = self._create()
        runtime.drift_scores_file = Path(self._tmp.name)  # 目录，写盘必失败
        with patch("pns.runtime.session_runtime.call_character_async", self._reply()), \
             patch("pns.runtime.session_runtime.judge_async", self._accepting_judge()):
            await _run(runtime)
        self.assertEqual(len(runtime.state.events), 0)
        self.assertEqual(runtime.state.turns, [])

    async def test_an_ooc_line_still_happened_in_the_world(self):
        # Router 是完整性评估器，不是事件权威：判高分只会排队 correction，
        # 不代表这句话没被说出口。真正的拒绝走的是上面那三条失败路径。
        async def ooc_judge(*args, **kwargs):
            return {"drift_score": 8, "is_ooc": True, "correction": "回到角色"}

        runtime = self._create(max_turns=1)
        with patch("pns.runtime.session_runtime.call_character_async", self._reply()), \
             patch("pns.runtime.session_runtime.judge_async", ooc_judge):
            await _run(runtime)

        self.assertEqual(len(runtime.state.events), 1)
        self.assertTrue(runtime.state.events.latest().provenance["is_ooc"])

    async def test_a_failed_commit_publishes_no_turn_and_leaves_no_half_state(self):
        async def fake_judge(*args, **kwargs):
            return {"drift_score": 1, "is_ooc": False}

        runtime = self._create(max_turns=2)
        with patch("pns.runtime.session_runtime.call_character_async", self._reply()), \
             patch("pns.runtime.session_runtime.judge_async", fake_judge), \
             patch(
                 "pns.runtime.session_runtime.commit_dialogue",
                 side_effect=EventCommitError("commit boom"),
             ):
            messages = await _run(runtime)

        self.assertEqual(
            [m["type"] for m in messages],
            ["start", "generating", "judging", "error", "done"],
        )
        self.assertIn("commit boom", messages[-2]["message"])
        self.assertEqual(runtime.state.turns, [])
        self.assertEqual(len(runtime.state.events), 0)
        self.assertEqual(messages[-1]["stats"]["total_turns"], 0)
        # 审计历史里仍然留有这次尝试 —— 失败的候选属于审计，不属于世界历史
        self.assertEqual(
            len(self.drift_file.read_text(encoding="utf-8").strip().splitlines()), 1
        )

    async def test_an_unexpected_commit_failure_is_closed_by_the_wire_protocol(self):
        runtime = self._create(max_turns=1)
        with patch("pns.runtime.session_runtime.call_character_async", self._reply()), \
             patch("pns.runtime.session_runtime.judge_async", self._accepting_judge()), \
             patch(
                 "pns.runtime.session_runtime.commit_dialogue",
                 side_effect=RuntimeError("unexpected commit boom"),
             ):
            messages = await _run(runtime)

        self.assertEqual(
            [message["type"] for message in messages],
            ["start", "generating", "judging", "error", "done"],
        )
        self.assertIn("unexpected commit boom", messages[-2]["message"])
        self.assertEqual(runtime.state.turns, [])
        self.assertEqual(len(runtime.state.events), 0)

    async def test_world_history_is_not_copied_into_character_histories(self):
        runtime, _ = await self._run_accepted(max_turns=2)

        for history in runtime.state.histories.values():
            for item in history:
                self.assertNotIn("event_id", item)
                self.assertNotIn("scope", item)
        self.assertEqual(len(runtime.state.events), 2)

    async def test_two_sessions_do_not_share_event_history(self):
        first, _ = await self._run_accepted(max_turns=1)
        second = self._create(max_turns=1)

        self.assertIsNot(first.state.events, second.state.events)
        self.assertEqual(len(first.state.events), 1)
        self.assertEqual(len(second.state.events), 0)

    async def test_session_serialization_exposes_the_committed_history(self):
        runtime, _ = await self._run_accepted(max_turns=1)

        payload = runtime.state.to_dict()
        self.assertEqual(len(payload["events"]["events"]), 1)
        self.assertEqual(payload["events"]["events"][0]["sequence"], 0)
        self.assertEqual(
            payload["events"]["events"][0]["actor_id"], runtime.state.turns[0].character
        )

if __name__ == "__main__":
    unittest.main()
