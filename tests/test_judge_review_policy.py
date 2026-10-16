# tests/test_judge_review_policy.py — JUDGE-1：低分的"需人工复核"不再一刀切作废。
#
# 实施单 kickoff/JUDGE_1_REVIEW_FLAG_POLICY.md。宽松策略 review-lenient-v1：标了复核的审计在
# 七维完整、drift < 4.0、unsupported_invention < 3.0 时仍可接受；其余照旧。策略随审计存档，
# 接受与否从存档字段可重推；旧记录（没写策略）就是 strict，被拒的重新加载仍被拒。
#
# 运行: python -m unittest tests.test_judge_review_policy -v
import copy
import unittest
from dataclasses import replace
from datetime import datetime

from pns.logic.router import DIMENSION_KEYS
from pns.models.authored import (
    REVIEW_LENIENT_V1,
    REVIEW_STRICT,
    AuthoredTextError,
    GenerationAudit,
)
from pns.models.event import EventType
from pns.models.session import SessionState
from pns.runtime.autonomy.audit import RouterAuditor
from pns.runtime.autonomy.coordinator import ActivationOutcome

from test_autonomous_runtime import _due, _rig  # noqa: E402

AT = datetime(2026, 8, 21, 23, 50)


def _dimensions(invention=1.0, other=1.0):
    return {
        key: {"score": invention if key == "unsupported_invention" else other, "reason": ""}
        for key in DIMENSION_KEYS
    }


def _audit(drift=3.0, *, invention=1.0, review=True, complete=True, policy=REVIEW_LENIENT_V1):
    return GenerationAudit(
        proposal_id="p1",
        character_id="mizuki",
        payload={"text": "在的哦"},
        drift_score=drift,
        threshold=5.0,
        audited_at=AT,
        needs_human_review=review,
        dimensions=_dimensions(invention) if complete else {},
        dimensions_complete=complete,
        review_policy=policy,
    )


class LenientRuleTests(unittest.TestCase):
    def test_drift_must_be_strictly_below_four(self):
        self.assertTrue(_audit(3.9).accepted)
        self.assertTrue(_audit(3.9).review_overridden)
        self.assertFalse(_audit(4.0).accepted)

    def test_invention_must_be_strictly_below_three(self):
        self.assertTrue(_audit(invention=2.9).accepted)
        self.assertFalse(_audit(invention=3.0).accepted)
        self.assertEqual(
            _audit(invention=3.0).refusal()["review_rule_failed"],
            "unsupported_invention_not_below_3",
        )

    def test_incomplete_dimensions_are_refused(self):
        audit = _audit(complete=False)
        self.assertFalse(audit.accepted)
        self.assertEqual(audit.refusal()["review_rule_failed"], "dimensions_incomplete")
        self.assertEqual(audit.refusal()["review_policy"], REVIEW_LENIENT_V1)

    def test_ooc_is_never_overridden(self):
        audit = _audit(6.0)
        self.assertFalse(audit.accepted)
        self.assertFalse(audit.review_overridden)

    def test_without_the_flag_nothing_is_overridden(self):
        audit = _audit(review=False)
        self.assertTrue(audit.accepted)
        self.assertFalse(audit.review_overridden)

    def test_strict_still_refuses_every_flag(self):
        audit = _audit(1.0, invention=0.0, policy=REVIEW_STRICT)
        self.assertFalse(audit.accepted)
        self.assertNotIn("review_policy", audit.refusal())

    def test_an_unknown_policy_is_refused(self):
        with self.assertRaises(AuthoredTextError):
            _audit(policy="whatever")


class ArchiveTests(unittest.TestCase):
    def test_an_old_record_loads_as_strict_and_stays_refused(self):
        old = _audit(1.0, policy=REVIEW_STRICT).to_dict()
        self.assertNotIn("review_policy", old)
        self.assertNotIn("review_overridden", old)
        loaded = GenerationAudit.from_dict(old)
        self.assertEqual(loaded.review_policy, REVIEW_STRICT)
        self.assertFalse(loaded.accepted)

    def test_strict_projection_is_byte_identical_to_the_old_shape(self):
        # Agency 事件的 provenance 里存着这份投影，恢复时逐项比对。
        audit = _audit(1.0, policy=REVIEW_STRICT)
        self.assertEqual(GenerationAudit.from_dict(audit.to_dict()).to_dict(), audit.to_dict())
        self.assertEqual(
            set(audit.to_dict()) - {"review_policy", "review_overridden"}, set(audit.to_dict())
        )

    def test_a_lenient_record_round_trips(self):
        audit = _audit(3.0)
        projection = audit.to_dict()
        self.assertEqual(projection["review_policy"], REVIEW_LENIENT_V1)
        self.assertTrue(projection["review_overridden"])
        loaded = GenerationAudit.from_dict(copy.deepcopy(projection))
        self.assertEqual(loaded.review_policy, REVIEW_LENIENT_V1)
        self.assertTrue(loaded.accepted)
        self.assertTrue(loaded.review_overridden)

    def test_the_stored_overridden_flag_cannot_change_the_verdict(self):
        projection = _audit(4.5).to_dict()
        projection["review_overridden"] = True
        self.assertFalse(GenerationAudit.from_dict(projection).accepted)


def _router(drift, invention):
    def judge(request):
        return {
            "drift_score": drift,
            "confidence": 0.8,
            "needs_human_review": True,
            "dimensions": _dimensions(invention),
            "dimensions_complete": True,
        }

    return RouterAuditor(judge, evaluator_model="fake")


class EndToEndTests(unittest.TestCase):
    def test_a_low_score_review_flag_becomes_an_event(self):
        state, scheduler, runtime = _rig(auditor=_router(3.0, 1.0))
        result = runtime.process_due(_due(scheduler))
        self.assertIs(result.outcome, ActivationOutcome.ACTED, result)
        events = state.events.by_type(EventType.MESSAGE_SENT)
        self.assertEqual(len(events), 1)
        audit = events[0].provenance["audit"]
        self.assertEqual(audit["review_policy"], REVIEW_LENIENT_V1)
        self.assertTrue(audit["review_overridden"])
        # 存档往返：Agency 记录按存档里的策略重建审计，与事件 provenance 逐项对上。
        restored = SessionState.from_dict(copy.deepcopy(state.to_dict()))
        self.assertEqual(len(restored.events.by_type(EventType.MESSAGE_SENT)), 1)

    def test_invention_is_still_refused(self):
        state, scheduler, runtime = _rig(auditor=_router(3.0, 3.0))
        result = runtime.process_due(_due(scheduler))
        self.assertIs(result.outcome, ActivationOutcome.REJECTED)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())

    def test_the_same_verdict_under_strict_is_not_an_event(self):
        state, scheduler, runtime = _rig(auditor=_router(3.0, 1.0))
        engine = state.agency_engine
        plan = engine.propose(_due(scheduler))
        audit = runtime._auditor.audit(runtime._audit_request(plan))
        self.assertTrue(audit.accepted)
        strict = replace(audit, review_policy=REVIEW_STRICT)
        record = engine.commit(plan.with_audit(strict))
        self.assertEqual(record.detail["reason"], "router_rejected")
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())


if __name__ == "__main__":
    unittest.main()
