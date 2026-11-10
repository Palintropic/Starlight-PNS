import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench import fixtures
from bench.conditions import pns as pns_condition
from bench.conditions import prompt_only, prompt_rag


class PNSProjectionTests(unittest.TestCase):
    """BENCH-1A 的机制本体：同一份语料，PNS 条件里不在场者看不见目标
    事件，A 条件里看得一清二楚——差别只能来自曝光投影本身。"""

    @classmethod
    def setUpClass(cls):
        cls.state = pns_condition.build_session_state(
            session_id="bench-test",
            characters=["kanade", "mafuyu", "mizuki", "ena"],
            corpus=list(fixtures.CORPUS),
            placements=dict(fixtures.PLACEMENTS),
            clock=fixtures.FIXTURE_CLOCK,
            trigger=fixtures.FIXTURE_TRIGGER,
        )

    def test_non_observer_history_has_no_target_event(self):
        for character in ("mizuki", "ena"):
            history = self.state.history_for(character)
            joined = "\n".join(m["content"] for m in history)
            self.assertNotIn(fixtures.TARGET_MARKER, joined)

    def test_observer_history_has_target_event(self):
        history = self.state.history_for("mafuyu")
        joined = "\n".join(m["content"] for m in history)
        self.assertIn(fixtures.TARGET_MARKER, joined)

    def test_probe_does_not_mutate_world(self):
        before = pns_condition.world_fingerprint(self.state)
        pns_condition.build_history(self.state, "mizuki", fixtures.FIXTURE_QUESTION)
        after = pns_condition.world_fingerprint(self.state)
        self.assertEqual(before, after)

    def test_prompt_only_history_leaks_by_construction(self):
        history, _ = prompt_only.build_history(
            list(fixtures.CORPUS), "mizuki", fixtures.FIXTURE_QUESTION
        )
        joined = "\n".join(m["content"] for m in history)
        self.assertIn(fixtures.TARGET_MARKER, joined)

    def test_rag_retrieves_target_with_fixture_question(self):
        retriever = prompt_rag.BM25Retriever(k1=1.5, b=0.75, top_k=5)
        _, info = prompt_rag.build_history(
            list(fixtures.CORPUS), "mizuki", fixtures.FIXTURE_QUESTION, retriever
        )
        self.assertTrue(any(h["is_target"] for h in info["retrieved"]))


if __name__ == "__main__":
    unittest.main()
