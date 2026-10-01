# tests/test_model1_compare.py — MODEL-1 comparison script (scripts/model1_compare.py).
#
# Guards:
#   1. the budget cap stops a run even though router.judge swallows every
#      exception the client raises;
#   2. the judge situation replay reproduces the coordinator's facts at the
#      line's moment, not the archive's final state;
#   3. recent lines are the actor's own observations up to the line, never
#      the line itself and never someone else's observations;
#   4. outputs are refused inside --world-root.
#
# Run: python -m unittest discover -s tests -p test_model1_compare.py
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "model1_compare", ROOT / "scripts" / "model1_compare.py"
)
m1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m1)


class _Usage:
    def __init__(self, **fields):
        self._fields = fields

    def model_dump(self):
        return dict(self._fields)


class _FakeClient:
    def __init__(self, text, usage):
        self.calls = 0
        self._text = text
        self._usage = usage
        self.messages = self

    def create(self, **request):
        self.calls += 1
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._text)],
            usage=_Usage(**self._usage),
            stop_reason="end_turn",
        )


def _event(seq, type_, actor=None, at="2026-09-29T02:00:00", **kw):
    return {
        "sequence": seq, "type": type_, "actor_id": actor, "occurred_at": at,
        "event_id": kw.pop("event_id", f"e{seq}"), "location_id": kw.pop("location_id", None),
        "channel_id": kw.pop("channel_id", None), "payload": kw.pop("payload", {}),
    }


def _obs(observer, actor, text, at, source):
    return {
        "observer_id": observer, "observed_at": at, "reason": "channel_member",
        "source_event_id": source,
        "perceived": {"actor_id": actor, "char_name": actor.upper(), "text": text,
                      "type": "message.sent"},
    }


def _audit_record(actor, at, text, score, event_id=None, seq=0):
    return {
        "character_id": actor, "decided_at": at, "due_id": f"due:{actor}@{at}",
        "sequence": seq, "event_id": event_id,
        "outcome": "acted" if score < 5 else "rejected_illegal",
        "detail": {"audit": {"drift_score": score, "accepted": score < 5,
                             "payload": {"text": text}, "evaluator_model": "old"}},
    }


def _write_world(root, events, observations, records):
    world_dir = Path(root) / "w"
    (world_dir / "history").mkdir(parents=True)
    (world_dir / "history" / "events-000001.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events[:2]), encoding="utf-8"
    )
    (world_dir / "world.json").write_text(json.dumps({"state": {
        "characters": ["mizuki", "ena"],
        "events": {"events": events[2:]},
        "observations": {"observations": observations},
        "agency": {"log": {"records": records}},
    }}), encoding="utf-8")
    return world_dir


class MeterTest(unittest.TestCase):
    def test_estimate_uses_cache_price_for_cached_input(self):
        usage = {"input_tokens": 1_000_000, "cache_read_input_tokens": 400_000,
                 "output_tokens": 100_000}
        self.assertAlmostEqual(m1.estimate_yuan(usage), 0.6 * 3 + 0.4 * 0.025 + 0.1 * 6)

    def test_budget_stops_even_when_router_swallows_the_raise(self):
        from pns.logic import router as router_mod

        fake = _FakeClient('{"drift_score": 1}', {"input_tokens": 1_000_000,
                                                  "output_tokens": 0})
        meter = m1.Meter(fake, budget_yuan=2.5)
        for _ in range(3):  # 3 yuan per call: the first call already crosses 2.5
            result = router_mod.judge(meter, "mizuki", "hi", 0)
        self.assertEqual(fake.calls, 1)
        self.assertTrue(meter.exhausted)
        # The swallowed raise surfaces as a judge failure, which the script
        # must not record as a score; the flag is what tells it apart.
        self.assertEqual(result["drift_type"], "error")


class JudgeItemsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        events = [
            _event(0, "dialogue.spoken", "mizuki", "2026-09-29T02:10:00",
                   location_id="mizuki_room"),
            _event(1, "presence.left_channel", "mizuki", "2026-09-29T04:00:00",
                   channel_id="nightcord"),
            _event(2, "character.activity_changed", "mizuki", "2026-09-29T04:00:00",
                   payload={"activity": "resting"}),
            _event(3, "message.sent", "ena", "2026-09-29T05:00:00", event_id="ena-line"),
            _event(4, "message.sent", "mizuki", "2026-09-29T05:10:00", event_id="miz-line"),
            _event(5, "character.location_changed", "mizuki", "2026-09-29T06:00:00",
                   location_id="streets"),
        ]
        observations = [
            _obs("mizuki", "ena", "ena says", "2026-09-29T05:00:00", "ena-line"),
            _obs("mizuki", "mizuki", "the judged line", "2026-09-29T05:10:00", "miz-line"),
            _obs("ena", "ena", "ena only", "2026-09-29T05:00:00", "ena-line"),
            _obs("mizuki", "ena", "later", "2026-09-29T07:00:00", "later"),
        ]
        records = [
            _audit_record("mizuki", "2026-09-29T05:10:00", "the judged line", 4.0,
                          event_id="miz-line"),
            _audit_record("mizuki", "2026-09-29T06:30:00", "rejected", 6.0),
            _audit_record("ena", "2026-09-29T05:00:00", "too low", 1.0, event_id="ena-line"),
        ]
        self.world_dir = _write_world(self.tmp.name, events, observations, records)

    def tearDown(self):
        self.tmp.cleanup()

    def test_situation_is_the_one_at_the_line_not_the_final_state(self):
        items = m1.build_judge_items(self.world_dir, 3.0)
        self.assertEqual([i["text"] for i in items], ["the judged line", "rejected"])
        first, second = items
        self.assertIn("自己的 location_id：mizuki_room", first["situation_facts"])
        self.assertIn("自己的当前活动：resting", first["situation_facts"])
        self.assertIn("自己加入的 channel_id：none", first["situation_facts"])
        # A rejected line has no event; it still sees the move at 06:00.
        self.assertIn("自己的 location_id：streets", second["situation_facts"])

    def test_channel_membership_before_first_leave_is_inferred(self):
        events = m1.read_raw_world(self.world_dir)[1]
        replay = m1.SituationReplay(events, ["mizuki", "ena"])
        _, _, channels = replay.at(1)
        self.assertEqual(channels["mizuki"], {"nightcord"})
        self.assertEqual(channels["ena"], set())

    def test_recent_lines_are_own_observations_before_the_line(self):
        first = m1.build_judge_items(self.world_dir, 3.0)[0]
        self.assertEqual(first["recent_lines"], ["ENA：ena says"])


class OutputGuardTest(unittest.TestCase):
    def test_output_inside_world_root_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(SystemExit):
                m1.guard_output([Path(root) / "w" / "out.jsonl"], root)
            m1.guard_output([None, Path(root).parent / "elsewhere.jsonl"], root)


class SummaryTest(unittest.TestCase):
    def test_failed_judgements_are_not_scored(self):
        rows = [
            {"kind": "judge", "key": "a", "model": "x", "rep": 0, "failed": False,
             "drift_score": 6.0, "original": {"drift_score": 4.0}, "usage": None},
            {"kind": "judge", "key": "b", "model": "x", "rep": 0, "failed": True,
             "drift_score": None, "original": {"drift_score": 6.0}, "usage": None},
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in rows))
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            m1.cmd_summary(SimpleNamespace(path=fh.name))
        Path(fh.name).unlink()
        self.assertIn("1 failed", out.getvalue())
        self.assertIn("archived vs x#0: 1/1 verdict flips", out.getvalue())


if __name__ == "__main__":
    unittest.main()
