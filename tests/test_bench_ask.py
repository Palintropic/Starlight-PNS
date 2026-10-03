"""BENCH-1: a private event must not enter another resident's model request."""
import tempfile
import unittest
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from pns.models.event import Event, EventScope, EventType
from pns.runtime.content_registry import build_content_registry
from pns.runtime.event_commit import commit_session_event
from pns.runtime.persistence import FileWorldStore
from pns.runtime.persistence.archive import WorldArchive
from scripts.bench_ask import (
    ask, inspect_ask, prepare_ask, prepare_prompt_only, prompt_only_ask,
    prepare_shared_memory, shared_memory_ask,
)
from test_world_lifecycle import _cold_state


SECRET = "水，烧开了吗？"
QUESTION = "奏刚才在做什么？她最近有没有说什么？"


class CaptureClient:
    def __init__(self, response="我不清楚她刚才说了什么。"):
        self.requests = []
        self.response = response
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(text=self.response)],
                               stop_reason="end_turn")


class BenchAskTests(unittest.TestCase):
    def test_shared_memory_retrieval_is_fixed_and_only_top_k_enters_request(self):
        corpus_path = (Path(__file__).resolve().parents[1] / "benchmarks" /
                       "continuity" / "KL-001-shared-memory-28.json")
        original = corpus_path.read_bytes()
        registry = build_content_registry()
        registry = replace(registry, models=replace(
            registry.models, generator_model="mimo-v2.5-pro"
        ))
        _, mizuki_prompt, mizuki = prepare_shared_memory(
            corpus_path, "mizuki", QUESTION, registry,
        )
        _, ena_prompt, ena = prepare_shared_memory(
            corpus_path, "ena", QUESTION, registry,
        )
        self.assertEqual(mizuki_prompt, ena_prompt)
        self.assertEqual(mizuki["retrieved_memory_ids"], ena["retrieved_memory_ids"])
        self.assertEqual(mizuki["target_memory_rank"], 5)
        self.assertTrue(mizuki["target_memory_retrieved"])
        self.assertEqual(len(mizuki["retrieved_memory_ids"]), 5)
        self.assertIn(SECRET, mizuki_prompt)
        self.assertNotIn("……这道题，算了三遍答案都不一样。", mizuki_prompt)
        self.assertNotIn("target_memory_id", mizuki_prompt)
        client = CaptureClient("奏刚才问水烧开了没有。")
        result = shared_memory_ask(
            corpus_path, "mizuki", QUESTION, registry, client,
        )
        self.assertEqual(result["answer"], "奏刚才问水烧开了没有。")
        self.assertEqual(client.requests[0]["messages"][0]["content"], mizuki_prompt)
        self.assertFalse(result["world_written"])
        self.assertEqual(corpus_path.read_bytes(), original)

    def test_prompt_only_uses_locked_shared_history_and_keeps_raw_failure(self):
        registry = build_content_registry()
        fixture = {
            "case_id": "KL-001", "world_id": "yoake-mae",
            "archive_revision": 98, "archive_sha256": "fixed-snapshot",
            "world_clock": "2026-10-01T21:47:00",
            "model": registry.models.generator_model,
            "temperature": 0.85, "max_tokens": 1024,
            "probe_characters": ["mizuki", "ena"], "question": QUESTION,
            "events": [{
                "event_id": "private", "type": "dialogue.spoken",
                "occurred_at": "2026-10-01T21:29:00", "scope": "location",
                "actor_id": "kanade", "location_id": "kanade_home_room",
                "location_label": "宵崎家・奏的房间", "text": SECRET,
            }],
        }
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "fixture.json"
            path.write_text(json.dumps(fixture, ensure_ascii=False))
            scene, situation, provenance = prepare_prompt_only(
                path, "mizuki", QUESTION, registry,
            )
            self.assertEqual(scene["location"], "未提供")
            self.assertIn(SECRET, situation)
            self.assertEqual(provenance["history_source_event_ids"], ["private"])
            self.assertEqual(provenance["system_prompt_sha256"], hashlib.sha256(
                registry.character_system("mizuki", scene).encode()
            ).hexdigest())

            client = CaptureClient("奏刚才问水烧开了没有。")
            result = prompt_only_ask(path, "mizuki", QUESTION, registry, client)
            self.assertEqual(result["answer"], "奏刚才问水烧开了没有。")
            self.assertIn(SECRET, str(client.requests[0]["messages"]))
            self.assertFalse(result["world_written"])
            self.assertFalse(result["provider_failure"])
            self.assertFalse(result["parser_failure"])

            malformed = prompt_only_ask(
                path, "ena", QUESTION, registry,
                CaptureClient("（停顿）我不知道。"),
            )
            self.assertFalse(malformed["generation_valid"])
            self.assertTrue(malformed["parser_failure"])
            self.assertEqual(malformed["raw_output"], "（停顿）我不知道。")
            self.assertEqual(json.loads(path.read_text()), fixture)

    def test_private_event_stays_out_of_mizuki_prompt_and_archive_unchanged(self):
        state = _cold_state()
        world = state.world_state
        world.leave_channel("mizuki", "nightcord")
        world.leave_channel("ena", "nightcord")
        event = Event(
            event_id="hidden", type=EventType.DIALOGUE_SPOKEN,
            occurred_at=world.clock, scope=EventScope.LOCATION,
            actor_id="ena", participants=("ena",),
            location_id="ena_home_studio", payload={"text": SECRET},
        )
        commit_session_event(state, event)
        with tempfile.TemporaryDirectory() as root:
            store = FileWorldStore(root)
            store.save(WorldArchive.capture("bench", state, revision=1))
            path = Path(root) / "bench" / "world.json"
            original = path.read_bytes()
            registry = build_content_registry()
            client = CaptureClient()
            result = ask(store, "bench", "mizuki", QUESTION, registry, client)

            request = client.requests[0]
            model_input = request["system"] + str(request["messages"])
            self.assertNotIn(SECRET, model_input)
            self.assertIn(QUESTION, model_input)
            self.assertEqual(result["observation_source_event_ids"], [])
            self.assertEqual(result["recalled_memory_ids"], [])
            self.assertEqual(result["answer"], "我不清楚她刚才说了什么。")
            self.assertTrue(result["generation_valid"])
            self.assertEqual(path.read_bytes(), original)

            malformed = ask(
                store, "bench", "mizuki", QUESTION, registry,
                CaptureClient("（停顿）我不知道。"),
            )
            self.assertFalse(malformed["generation_valid"])
            self.assertEqual(malformed["raw_output"], "（停顿）我不知道。")
            self.assertIsNone(malformed["answer"])
            self.assertEqual(path.read_bytes(), original)

            inspected = inspect_ask(store, "bench", "mizuki", QUESTION, registry)
            self.assertEqual(inspected["mode"], "inspect_only")
            self.assertEqual(inspected["answer"], None)
            self.assertEqual(
                inspected["system_prompt_sha256"],
                hashlib.sha256(request["system"].encode()).hexdigest(),
            )
            self.assertEqual(
                inspected["situation_prompt_sha256"],
                hashlib.sha256(request["messages"][0]["content"].encode()).hexdigest(),
            )
            self.assertEqual(path.read_bytes(), original)

            ena_context, _ = prepare_ask(store, "bench", "ena", registry)
            self.assertIn(SECRET, str(ena_context.observed_lines))


if __name__ == "__main__":
    unittest.main()
