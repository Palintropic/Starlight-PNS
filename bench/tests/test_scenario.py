import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml

from bench.scenario import ScenarioError, load_scenario

ROOT = Path(__file__).resolve().parents[2]


def minimal_scenario():
    return {
        "id": "t-001",
        "benchmark": "BENCH-1A",
        "dimension": "knowledge_leakage",
        "methodology_version": "bench-1a-v1",
        "world": {"id": "w", "snapshot": {"revision": 1, "sha256": "a" * 64}},
        "model": {"name": "m", "temperature": 0.85, "max_tokens": 1024},
        "characters": {"observers": ["kanade", "mafuyu"], "probed": ["mizuki"]},
        "target_event": {
            "participants": ["kanade", "mafuyu"],
            "visible_to": ["kanade", "mafuyu"],
            "hidden_from": ["mizuki", "ena"],
        },
        "probe": {"question": "奏最近在忙什么？"},
        "runs": {"valid_per_character": 5},
        "conditions": [
            {"id": "pns", "builder": "pns"},
            {
                "id": "prompt_rag",
                "builder": "prompt_rag",
                "retriever": {
                    "type": "bm25",
                    "tokenization": "zh_char_ngram_1_2",
                    "k1": 1.5,
                    "b": 0.75,
                    "top_k": 5,
                    "fields": ["speaker_name", "text"],
                },
            },
        ],
    }


def load_dict(data):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "s.yaml"
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        return load_scenario(path)


class RealScenarioTests(unittest.TestCase):
    def test_kl001_loads(self):
        scenario = load_scenario(ROOT / "bench" / "scenarios" / "kl-001.yaml")
        self.assertEqual(scenario.id, "kl-001")
        self.assertEqual(scenario.observers, ["kanade", "mafuyu"])
        self.assertEqual(scenario.probed, ["mizuki", "ena"])
        self.assertEqual(scenario.snapshot_revision, 98)
        self.assertEqual(
            scenario.snapshot_sha256,
            "4e15bf1359e716b3497f66dca17a177a95922b173528b30932b4bc6adf29005d",
        )
        self.assertEqual(scenario.valid_runs_per_character, 20)
        # 问题原文已于 2026-11-10 从 artifact 逐字转录，不再是占位。
        self.assertEqual(scenario.probe_question, "奏刚才在做什么？她最近有没有说什么？")
        self.assertFalse(scenario.probe_question_is_placeholder)
        self.assertEqual(scenario.probe_question_status, "verbatim")
        self.assertIn("烧开", scenario.leak_markers)
        rag = scenario.condition("prompt_rag")
        self.assertEqual(rag.retriever.k1, 1.5)
        self.assertEqual(rag.retriever.b, 0.75)
        self.assertEqual(rag.retriever.top_k, 5)


class ValidationTests(unittest.TestCase):
    def test_minimal_valid(self):
        scenario = load_dict(minimal_scenario())
        self.assertEqual(scenario.probe_question, "奏最近在忙什么？")
        self.assertFalse(scenario.probe_question_is_placeholder)

    def test_probed_cannot_be_observer(self):
        data = minimal_scenario()
        data["characters"]["probed"] = ["mafuyu"]
        data["target_event"]["hidden_from"] = ["mafuyu"]
        with self.assertRaises(ScenarioError):
            load_dict(data)

    def test_visible_to_must_match_observers(self):
        data = minimal_scenario()
        data["target_event"]["visible_to"] = ["kanade"]
        with self.assertRaises(ScenarioError):
            load_dict(data)

    def test_bad_snapshot_hash(self):
        data = minimal_scenario()
        data["world"]["snapshot"]["sha256"] = "xyz"
        with self.assertRaises(ScenarioError):
            load_dict(data)

    def test_question_placeholder_must_be_declared(self):
        data = minimal_scenario()
        data["probe"] = {"question": None}
        with self.assertRaises(ScenarioError):
            load_dict(data)
        data["probe"] = {"question": None, "question_status": "todo_from_artifact"}
        scenario = load_dict(data)
        self.assertTrue(scenario.probe_question_is_placeholder)

    def test_rag_requires_bm25_spec(self):
        data = minimal_scenario()
        data["conditions"][1] = {"id": "prompt_rag", "builder": "prompt_rag"}
        with self.assertRaises(ScenarioError):
            load_dict(data)

    def test_unknown_builder_rejected(self):
        data = minimal_scenario()
        data["conditions"][0] = {"id": "x", "builder": "telepathy"}
        with self.assertRaises(ScenarioError):
            load_dict(data)


if __name__ == "__main__":
    unittest.main()
