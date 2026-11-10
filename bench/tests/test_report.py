import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench.report import aggregate, render_markdown


def record(condition, character, *, valid=True, failure=None, auto=None,
           human=None, judge=None, usage=None, cost=None, info=None,
           latency=100.0):
    return {
        "condition": condition,
        "character": character,
        "valid": valid,
        "failure": failure,
        "auto": auto,
        "human_label": human,
        "judge": judge,
        "usage": usage,
        "cost_estimate": cost,
        "latency_ms": latency if valid else 0.0,
        "condition_info": info or {},
    }


USAGE = {"fresh_input": 100, "cache_write": 0, "cache_read": 0, "output": 10}


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            # pns/mizuki: 两条有效、无泄漏、示不确定；一条截断失败
            record("pns", "mizuki", auto={"leak": False, "correct_uncertainty": True, "unsupported_claim": False}, usage=USAGE, cost=0.01),
            record("pns", "mizuki", auto={"leak": False, "correct_uncertainty": True, "unsupported_claim": False}, usage=USAGE, cost=0.01),
            record("pns", "mizuki", valid=False, failure="GenerationTruncated"),
            # prompt_only/mizuki: 两条泄漏，Router 一条检出（7 分）一条没检出（3 分）
            record("prompt_only", "mizuki", auto={"leak": True, "correct_uncertainty": False, "unsupported_claim": False}, judge={"drift_score": 7}, usage=USAGE, cost=0.02),
            record("prompt_only", "mizuki", auto={"leak": True, "correct_uncertainty": False, "unsupported_claim": False}, judge={"drift_score": 3}, usage=USAGE, cost=0.02),
            # prompt_rag/ena: 一条泄漏 + 检索命中目标（rank 5）；成本未知
            record("prompt_rag", "ena", auto={"leak": True, "correct_uncertainty": False, "unsupported_claim": False}, usage=USAGE, cost=None,
                   info={"retrieved": [{"index": 2, "rank": 5, "score": 1.0, "speaker": "kanade", "is_target": True}]}),
        ]
        self.agg = aggregate(self.records, ooc_threshold=5)

    def test_leakage_rates(self):
        pns = self.agg["by_cell"][("pns", "mizuki")]
        self.assertEqual(pns["leaks"], 0)
        self.assertEqual(pns["leakage_rate"], 0.0)
        only = self.agg["by_cell"][("prompt_only", "mizuki")]
        self.assertEqual(only["leakage_rate"], 1.0)
        self.assertEqual(self.agg["by_condition"]["prompt_only"]["leaks"], 2)

    def test_failures_counted_and_excluded_from_valid(self):
        pns = self.agg["by_cell"][("pns", "mizuki")]
        self.assertEqual(pns["attempts"], 3)
        self.assertEqual(pns["valid"], 2)
        self.assertEqual(pns["failures"], {"GenerationTruncated": 1})

    def test_router_detection_rate(self):
        only = self.agg["by_cell"][("prompt_only", "mizuki")]
        self.assertEqual(only["router_detection_rate"], 0.5)
        # 没判分的条件必须是 None（n/a），不许按 0 报
        pns = self.agg["by_cell"][("pns", "mizuki")]
        self.assertIsNone(pns["router_detection_rate"])

    def test_retrieval_stats(self):
        rag = self.agg["by_cell"][("prompt_rag", "ena")]
        self.assertEqual(rag["retrieval_target_hit_rate"], 1.0)
        self.assertEqual(rag["retrieval_target_ranks"], [5])

    def test_cost_unknown_propagates(self):
        rag = self.agg["by_condition"]["prompt_rag"]
        self.assertIsNone(rag["cost_total"])
        pns = self.agg["by_condition"]["pns"]
        self.assertAlmostEqual(pns["cost_total"], 0.02)

    def test_human_label_overrides_auto(self):
        records = [
            record("pns", "ena", auto={"leak": True, "correct_uncertainty": False, "unsupported_claim": False}, human="no_leak"),
            record("pns", "ena", auto={"leak": False, "correct_uncertainty": False, "unsupported_claim": False}, human="attribution_error"),
        ]
        cell = aggregate(records)["by_cell"][("pns", "ena")]
        self.assertEqual(cell["leaks"], 0)
        self.assertEqual(cell["attribution_errors"], 1)
        self.assertEqual(cell["leak_basis_human"], 2)

    def test_render_smoke(self):
        manifest = {
            "run_id": "test-run",
            "benchmark": "BENCH-1A",
            "scenario": {"id": "kl-001", "file": "bench/scenarios/kl-001.yaml", "sha256": "x" * 64, "methodology_version": "bench-1a-v1"},
            "world": {"id": "yoake-mae", "snapshot_revision": 98, "snapshot_sha256": "y" * 64, "corpus_source": "synthetic_fixture", "corpus_events": 8, "corpus_sha256": "z" * 64, "world_unchanged": True, "events_before": 8, "events_after": 8},
            "model": {"name": "mock", "temperature": 0.85, "max_tokens": 1024},
            "generation": {"mock": True},
            "git": {"commit": "abc", "branch": "bench/bench1-harness", "dirty": False},
        }
        text = render_markdown(manifest, self.agg, self.records)
        self.assertIn("MOCK RUN", text)
        self.assertIn("GenerationTruncated × 1", text)
        self.assertIn("0/2 (0%)", text)


if __name__ == "__main__":
    unittest.main()
