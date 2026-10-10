# CAL-1A：测试定义只用于机制验收，不代表住民的正式安排。
from copy import deepcopy
from datetime import timedelta, timezone
from dataclasses import replace
import errno
import tempfile
import threading
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from unittest.mock import patch

from test_scheduler import _session, CLOCK, _activation
from test_world_lifecycle import _adapters, _dir_fsync
from pns.models.arrangement import (
    ArrangementDefinition,
    ArrangementBook,
    ArrangementError,
    ARRANGEMENT_PREFIX,
    verify_arrangements,
)
from pns.models.action import ActionProposal, ActionId
from pns.models.clock_anchor import ClockAnchor
from pns.models.activation import ScheduledActivation, ActivationKind
from pns.models.agency import AgencyOutcome, AgencyRecord, AgencyError, AgencyBudget
from pns.models.cognition import consumes_allowance
from pns.models.session import SessionState
from pns.runtime.scheduler import PersistentScheduler, SchedulerError
from pns.runtime.agency.policy import AbstainPolicy, PolicyDecision
from pns.runtime.agency.engine import AgencyEngineError, ProposalPlan
from pns.runtime.autonomy.audit import ScriptedAuditor
from pns.runtime.autonomy.coordinator import AutonomousRuntime, AutonomyError
from pns.runtime.autonomy.context import build_generation_context
from pns.runtime.persistence import (
    WorldArchive,
    ArchiveError,
    WorldLifecycleService,
    CheckpointError,
)
from pns.runtime.persistence.store import FileWorldStore, StorageError


def definition(aid="test-plan", *, minute=10, weekly=False, window=120):
    start = CLOCK + timedelta(minutes=minute)
    return {
        "id": aid,
        "cue": "计划整理测试画稿",
        "source": "inferred",
        "evidence": ["test-only"],
        "weekly" if weekly else "once": (
            {"weekday": start.weekday(), "at": start.strftime("%H:%M")}
            if weekly
            else {"date": start.strftime("%Y-%m-%d"), "at": start.strftime("%H:%M")}
        ),
        "window_minutes": window,
    }


class CapturePolicy:
    name = "test-capture"

    def __init__(self, error=False, action=False):
        self.contexts = []
        self.error = error
        self.action = action
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False

    def decide(self, context):
        self.contexts.append(context)
        if self.block:
            self.entered.set()
            if not self.release.wait(5):
                raise RuntimeError("test timeout")
        if self.error:
            raise ValueError("test policy error")
        if self.action:
            return PolicyDecision(
                proposals=(
                    ActionProposal(
                        "test-proposal:" + context.activation.due_id,
                        context.character_id,
                        ActionId.JOIN_CHANNEL,
                        "nightcord",
                    ),
                )
            )
        return PolicyDecision()


def rig(policy=None, budget=None, anchored=False):
    state = _session()
    scheduler = PersistentScheduler(state)
    runtime = AutonomousRuntime(
        state,
        auditor=ScriptedAuditor(),
        policy=policy or AbstainPolicy(),
        budget=budget,
    )
    runtime.start()
    if anchored:
        anchor_runtime(runtime)
    return state, scheduler, runtime


def anchor_runtime(runtime):
    runtime.open_clock(
        ClockAnchor(CLOCK, CLOCK.replace(tzinfo=timezone.utc)),
        wall=CLOCK.replace(tzinfo=timezone.utc),
    )
    runtime.start_cognition(None, wall=CLOCK.replace(tzinfo=timezone.utc))


def reach_goal(runtime, target, **kwargs):
    return runtime.advance_to_anchor(wall=target.replace(tzinfo=timezone.utc), **kwargs)


def adopt(runtime, **kwargs):
    return runtime.maintain_arrangement(
        "adopted", "mizuki", definition=definition(**kwargs)
    )


def advance(scheduler, runtime, minutes):
    # 公开时钟入口会处理 pending；留 pending 的测试直接调用调度器。
    return runtime.advance(minutes)


class AContentAdoptionTests(unittest.TestCase):
    def test_invalid_definition_shapes(self):
        base = definition()
        defaulted = dict(base)
        defaulted.pop("window_minutes")
        self.assertEqual(ArrangementDefinition(defaulted).data["window_minutes"], 120)
        cases = [
            dict(base, weekly={"weekday": 0, "at": "00:00"}),
            {k: v for k, v in base.items() if k != "once"},
            dict(base, window_minutes=0),
            dict(base, window_minutes=True),
            dict(base, day_filter="rest_day"),
            dict(base, once={"date": "2026-02-30", "at": "24:00"}),
            dict(base, weekly={"weekday": 7, "at": "10:00"}),
        ]
        cases[-1].pop("once")
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ArrangementError):
                ArrangementDefinition(raw)

    def test_nonresident_and_past_once(self):
        state, scheduler, runtime = rig()
        before = state.to_dict()
        with self.assertRaises(AutonomyError):
            runtime.maintain_arrangement("adopted", "honami", definition=definition())
        with self.assertRaises(ArrangementError):
            adopt(runtime, minute=-1)
        self.assertEqual(before, state.to_dict())

    def test_permanent_identity_and_revision_versions(self):
        state, scheduler, runtime = rig()
        adopt(runtime)
        before = state.to_dict()
        for _ in range(2):
            with self.assertRaises(ArrangementError):
                adopt(runtime)
        self.assertEqual(before, state.to_dict())
        with self.assertRaises(ArrangementError):
            runtime.maintain_arrangement(
                "revised", "mizuki", definition=definition(), expected_version=1
            )
        changed = dict(definition(), cue="计划检查另一份测试画稿")
        with self.assertRaises(ArrangementError):
            runtime.maintain_arrangement(
                "revised", "mizuki", definition=changed, expected_version=0
            )
        runtime.maintain_arrangement(
            "revised", "mizuki", definition=changed, expected_version=1
        )
        with self.assertRaises(ArrangementError):
            runtime.maintain_arrangement(
                "revised",
                "mizuki",
                definition=dict(changed, cue="计划校对测试画稿"),
                expected_version=1,
            )
        runtime.maintain_arrangement(
            "revoked", "mizuki", arrangement_id="test-plan", expected_version=2
        )
        with self.assertRaises(ArrangementError):
            adopt(runtime)
        self.assertEqual(len(scheduler.pending()), 0)
        SessionState.from_dict(state.to_dict())

    def test_adoption_at_exact_clock_is_strict(self):
        state, scheduler, runtime = rig()
        adopt(runtime, minute=0, weekly=True)
        self.assertEqual(scheduler.next_due_at(), CLOCK + timedelta(days=7))
        with self.assertRaises(ArrangementError):
            adopt(runtime, aid="past-once", minute=0)
        runtime.advance(10)
        adopt(runtime, aid="new-after-clock", minute=20)
        self.assertEqual(len(state.arrangements.to_dict()["occurrences"]), 0)

    def test_initial_enqueue_failure_rolls_back_everything(self):
        state, scheduler, runtime = rig()
        before = state.to_dict()
        with patch.object(
            scheduler,
            "_schedule_arrangement",
            side_effect=SchedulerError("test failure"),
        ):
            with self.assertRaises(SchedulerError):
                adopt(runtime)
        self.assertEqual(before, state.to_dict())

    def test_checkpoint_before_publication_rolls_back(self):
        state, scheduler, runtime = rig()
        before = state.to_dict()

        def fail():
            self.assertFalse(state.in_transaction)
            raise StorageError("test before publication")

        with self.assertRaises(StorageError):
            runtime.maintain_arrangement(
                "adopted", "mizuki", definition=definition(), checkpoint=fail
            )
        self.assertEqual(before, state.to_dict())

    def test_adopted_snapshot_survives_content_changes(self):
        state, scheduler, runtime = rig()
        raw = definition()
        runtime.maintain_arrangement("adopted", "mizuki", definition=raw)
        raw["cue"] = "不应生效的编辑"
        restored = SessionState.from_dict(state.to_dict())
        self.assertEqual(
            restored.arrangements.to_dict()["current"]["test-plan"]["definition"][
                "cue"
            ],
            "计划整理测试画稿",
        )


class BRestoreTests(unittest.TestCase):
    def make_payload(self):
        state, scheduler, runtime = rig()
        adopt(runtime, weekly=True)
        runtime.advance(10)
        return state.to_dict()

    def assert_broken(self, payload):
        with self.assertRaises((ValueError, KeyError, TypeError)):
            SessionState.from_dict(payload)

    def test_new_shape_requires_book_target_and_carried(self):
        payload = self.make_payload()
        for key in ("arrangements", "catch_up_target"):
            broken = deepcopy(payload)
            del broken[key]
            self.assert_broken(broken)
        broken = deepcopy(payload)
        del broken["agency"]["log"]["records"][0]["arrangements_carried"]
        self.assert_broken(broken)

    def test_operation_current_occurrence_deletion_and_tampering(self):
        payload = self.make_payload()
        for key in ("operations", "current", "occurrences"):
            broken = deepcopy(payload)
            broken["arrangements"][key] = [] if key == "operations" else {}
            self.assert_broken(broken)
        for key, value in [
            ("version", 2),
            ("fingerprint", "bad"),
            ("revoked", True),
            ("character_id", "ena"),
        ]:
            broken = deepcopy(payload)
            broken["arrangements"]["current"]["test-plan"][key] = value
            self.assert_broken(broken)

    def test_missing_future_trigger_due_and_cue_corruption(self):
        payload = self.make_payload()
        broken = deepcopy(payload)
        broken["scheduler"]["queue"]["activations"] = []
        self.assert_broken(broken)
        broken = deepcopy(payload)
        broken["scheduler"]["outbox"]["records"] = []
        self.assert_broken(broken)
        broken = deepcopy(payload)
        broken["events"]["events"] = []
        self.assert_broken(broken)
        for key, value in [
            ("character_id", "ena"),
            ("payload", {"cue": "伪造"}),
            ("next_due_at", None),
        ]:
            broken = deepcopy(payload)
            broken["scheduler"]["outbox"]["records"][0][key] = value
            self.assert_broken(broken)
        broken = deepcopy(payload)
        broken["arrangements"]["current"]["test-plan"]["next_due_at"] = None
        self.assert_broken(broken)

    def test_ack_agency_bijection(self):
        payload = self.make_payload()
        broken = deepcopy(payload)
        broken["agency"]["log"]["records"] = []
        self.assert_broken(broken)
        broken = deepcopy(payload)
        broken["scheduler"]["outbox"]["records"][0]["acknowledged"] = False
        self.assert_broken(broken)
        broken = deepcopy(payload)
        broken["agency"]["log"]["records"].append(
            deepcopy(broken["agency"]["log"]["records"][0])
        )
        self.assert_broken(broken)

    def test_once_identity_cannot_requeue(self):
        state, scheduler, runtime = rig()
        adopt(runtime)
        activation = scheduler.pending()[0]
        runtime.advance(10)
        with self.assertRaises(SchedulerError):
            scheduler.schedule(
                replace(activation, due_at=scheduler.clock + timedelta(minutes=1))
            )
        with state.atomic_commit(), self.assertRaises(SchedulerError):
            scheduler._append_checked(
                replace(activation, due_at=scheduler.clock + timedelta(minutes=1))
            )
        payload = state.to_dict()
        payload["scheduler"]["queue"]["activations"].append(
            {
                "sequence": 1,
                **replace(
                    activation, due_at=scheduler.clock + timedelta(minutes=1)
                ).to_dict(),
            }
        )
        self.assert_broken(payload)

    def test_v2_v3_explicit_empty_migration(self):
        state, _, _ = rig()
        archive = WorldArchive.from_state_payload(
            "test-world", state.to_dict(), revision=1
        )
        for version in (2, 3):
            raw = archive.to_dict()
            raw["version"] = version
            if version == 2:
                raw.pop("segments", None)
            for key in ("arrangements", "catch_up_target"):
                raw["state"].pop(key)
            # Envelope checksum is recomputed by the migration fixture below.
            migrated = WorldArchive.from_dict(raw).restore_state()
            self.assertEqual(
                migrated.arrangements.to_dict(), ArrangementBook().to_dict()
            )

    def test_boolean_versions_are_not_integer_versions(self):
        state, _, runtime = rig()
        adopt(runtime)
        runtime.advance(10)
        payload = state.to_dict()
        for kind in ("operation", "current", "occurrence", "due"):
            broken = deepcopy(payload)
            if kind == "operation":
                broken["arrangements"]["operations"][0]["version"] = True
            elif kind == "current":
                broken["arrangements"]["current"]["test-plan"]["version"] = True
            elif kind == "occurrence":
                next(iter(broken["arrangements"]["occurrences"].values()))["identity"][
                    "version"
                ] = True
            else:
                broken["scheduler"]["outbox"]["records"][0]["payload"]["version"] = True
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                SessionState.from_dict(broken)

    def test_existing_resident_cannot_take_another_residents_arrangement(self):
        state, _, runtime = rig()
        adopt(runtime)
        before = state.to_dict()
        with self.assertRaises(ArrangementError):
            runtime.maintain_arrangement(
                "revised",
                "ena",
                definition=dict(definition(), cue="计划校对测试画稿"),
                expected_version=1,
            )
        self.assertEqual(before, state.to_dict())


class CWindowCatchupTests(unittest.TestCase):
    def test_elapsed_window_no_policy_and_missed(self):
        policy = CapturePolicy()
        state, scheduler, runtime = rig(policy)
        adopt(runtime, window=2)
        runtime.advance(60)
        record = state.agency.records()[0]
        self.assertEqual(record.outcome, AgencyOutcome.LAPSED)
        self.assertEqual(policy.contexts, [])
        self.assertEqual(state.arrangements.statistics["missed_occurrences"], 1)
        SessionState.from_dict(state.to_dict())

    def test_exact_expiry_still_visible(self):
        policy = CapturePolicy()
        state, scheduler, runtime = rig(policy)
        adopt(runtime, window=2)
        runtime.advance(12)
        self.assertEqual(len(policy.contexts), 1)
        self.assertEqual(state.arrangements.statistics["expired_occurrences"], 0)
        runtime.advance(1)
        self.assertEqual(state.arrangements.statistics["expired_occurrences"], 1)
        self.assertEqual(state.arrangements.statistics["missed_occurrences"], 0)

    def test_saved_open_pending_uses_total_target_first(self):
        state, scheduler, runtime = rig()
        adopt(runtime, window=2)
        scheduler.advance_by(10)
        restored = SessionState.from_dict(state.to_dict())
        policy = CapturePolicy()
        PersistentScheduler(restored)
        runtime2 = AutonomousRuntime(restored, auditor=ScriptedAuditor(), policy=policy)
        runtime2.start()
        runtime2.advance(30)
        self.assertEqual(policy.contexts, [])
        self.assertEqual(restored.agency.records()[0].outcome, AgencyOutcome.LAPSED)
        SessionState.from_dict(restored.to_dict())

    def test_window_restore_remains_visible(self):
        state, scheduler, runtime = rig()
        adopt(runtime)
        scheduler.advance_by(10)
        restored = SessionState.from_dict(state.to_dict())
        PersistentScheduler(restored)
        policy = CapturePolicy()
        runtime2 = AutonomousRuntime(restored, auditor=ScriptedAuditor(), policy=policy)
        runtime2.start()
        runtime2.advance(1)
        self.assertEqual(len(policy.contexts[0].arrangement_cues), 1)

    def test_segmented_and_unsegmented_total_goal_match(self):
        state, scheduler, runtime = rig(anchored=True)
        adopt(runtime, weekly=True, window=2)
        target = CLOCK + timedelta(days=22)
        reach_goal(runtime, target, max_steps=1)
        self.assertEqual(state.catch_up_target, target)
        restored = SessionState.from_dict(state.to_dict())
        PersistentScheduler(restored)
        runtime2 = AutonomousRuntime(
            restored, auditor=ScriptedAuditor(), policy=AbstainPolicy()
        )
        runtime2.start()
        reach_goal(runtime2, target)
        state2, _, runtime3 = rig(anchored=True)
        adopt(runtime3, weekly=True, window=2)
        reach_goal(runtime3, target)
        self.assertEqual(restored.arrangements.to_dict(), state2.arrangements.to_dict())
        self.assertEqual(restored.arrangements.statistics["missed_occurrences"], 4)
        self.assertIsNone(restored.catch_up_target)

    def test_target_expansion_and_shrinking(self):
        policy = CapturePolicy()
        state, _, runtime = rig(policy, anchored=True)
        adopt(runtime, window=60)
        reach_goal(runtime, CLOCK + timedelta(minutes=30), max_steps=0)
        with self.assertRaises(AutonomyError):
            reach_goal(runtime, CLOCK + timedelta(minutes=20))
        reach_goal(runtime, CLOCK + timedelta(hours=3))
        self.assertEqual(policy.contexts, [])
        self.assertEqual(state.arrangements.statistics["missed_occurrences"], 1)


class DClosureConcurrencyTests(unittest.TestCase):
    def test_revoke_and_revise_pending_lapsed_ack(self):
        for action in ("revoked", "revised"):
            with self.subTest(action=action):
                state, scheduler, runtime = rig()
                adopt(runtime)
                scheduler.advance_by(10)
                kwargs = (
                    {"arrangement_id": "test-plan"}
                    if action == "revoked"
                    else {"definition": definition(minute=20)}
                )
                runtime.maintain_arrangement(
                    action, "mizuki", expected_version=1, **kwargs
                )
                record = state.agency.records()[0]
                self.assertEqual(record.outcome, AgencyOutcome.LAPSED)
                self.assertFalse(consumes_allowance(record))
                self.assertTrue(state.activation_outbox.is_acknowledged(record.due_id))
                self.assertIsNone(runtime.agency._last_charged("mizuki"))
                SessionState.from_dict(state.to_dict())

    def test_record_and_ack_failure_rollback_pending_closure(self):
        for method in ("_append", "_acknowledge"):
            with self.subTest(method=method):
                state, scheduler, runtime = rig()
                adopt(runtime)
                scheduler.advance_by(10)
                before = state.to_dict()
                obj = state.agency if method == "_append" else state.activation_outbox
                with patch.object(
                    obj, method, side_effect=RuntimeError("test closure failure")
                ):
                    with self.assertRaises(RuntimeError):
                        runtime.maintain_arrangement(
                            "revoked",
                            "mizuki",
                            arrangement_id="test-plan",
                            expected_version=1,
                        )
                self.assertEqual(before, state.to_dict())
                runtime.maintain_arrangement(
                    "revoked", "mizuki", arrangement_id="test-plan", expected_version=1
                )
                self.assertEqual(len(state.agency), 1)

    def test_lapsed_cannot_be_forged_or_billed_by_code_only(self):
        state, scheduler, runtime = rig()
        adopt(runtime)
        scheduler.advance_by(10)
        runtime.maintain_arrangement(
            "revoked", "mizuki", arrangement_id="test-plan", expected_version=1
        )
        record = state.agency.records()[0]
        self.assertTrue(consumes_allowance(AgencyOutcome.LAPSED))
        for changes in (
            {"due_id": "ordinary@2026-08-22T00:00:00"},
            {"policy": "forged"},
            {"detail": dict(record.detail, audit={})},
            {"detail": {"reason": "arrangement_revoked"}},
        ):
            with self.subTest(changes=changes), self.assertRaises(AgencyError):
                replace(record, **changes)

    def test_unsigned_lapsed_plan_is_rejected(self):
        state, scheduler, runtime = rig()
        adopt(runtime)
        scheduler.advance_by(10)
        due = scheduler.pending_due()[0]
        with self.assertRaises(AgencyEngineError):
            runtime.agency.commit(
                ProposalPlan(
                    due,
                    "mizuki",
                    "",
                    scheduler.clock,
                    AgencyOutcome.LAPSED,
                    detail={"reason": "arrangement_expired"},
                )
            )

    def test_already_closed_due_is_not_closed_again(self):
        state, _, runtime = rig()
        adopt(runtime)
        runtime.advance(10)
        runtime.maintain_arrangement(
            "revoked", "mizuki", arrangement_id="test-plan", expected_version=1
        )
        self.assertEqual(len(state.agency), 1)
        self.assertEqual(state.agency.records()[0].outcome, AgencyOutcome.ABSTAINED)
        self.assertEqual(
            state.arrangements.statistics,
            {
                "expired_occurrences": 0,
                "missed_occurrences": 0,
                "revoked_occurrences": 1,
            },
        )
        SessionState.from_dict(state.to_dict())

    def test_slow_policy_revoke_revise_first_rejects_whole_result(self):
        for action in ("revoked", "revised"):
            with self.subTest(action=action):
                policy = CapturePolicy(action=True)
                policy.block = True
                state, scheduler, runtime = rig(policy)
                adopt(runtime)
                adopt(runtime, aid="other-plan")
                scheduler.advance_by(10)
                due = scheduler.pending_due()[0]
                results = []
                errors = []

                def work():
                    try:
                        results.append(runtime.process_due(due))
                    except BaseException as error:
                        errors.append(error)

                thread = threading.Thread(target=work)
                thread.start()
                self.assertTrue(policy.entered.wait(3))
                try:
                    kwargs = (
                        {"arrangement_id": "test-plan"}
                        if action == "revoked"
                        else {"definition": definition(minute=20)}
                    )
                    runtime.maintain_arrangement(
                        action, "mizuki", expected_version=1, **kwargs
                    )
                    self.assertFalse(state.agency.has(due.due_id))
                finally:
                    policy.release.set()
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                record = state.agency.get(due.due_id)
                self.assertEqual(record.outcome, AgencyOutcome.REJECTED_STALE)
                self.assertEqual(len(record.arrangements_carried), 2)
                self.assertTrue(consumes_allowance(record))
                self.assertNotIn("nightcord", state.world_state.channels_for("mizuki"))
                runtime.process_pending()
                SessionState.from_dict(state.to_dict())

    def test_same_version_expiry_rejects_slow_proposal(self):
        policy = CapturePolicy(action=True)
        policy.block = True
        state, scheduler, runtime = rig(policy, anchored=True)
        adopt(runtime, window=2)
        scheduler.advance_by(10)
        due = scheduler.pending_due()[0]
        results = []
        thread = threading.Thread(
            target=lambda: results.append(runtime.process_due(due))
        )
        thread.start()
        self.assertTrue(policy.entered.wait(3))
        try:
            reach_goal(runtime, CLOCK + timedelta(minutes=30), max_steps=0)
        finally:
            policy.release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1)
        record = state.agency.get(due.due_id)
        self.assertEqual(record.outcome, AgencyOutcome.REJECTED_STALE)
        self.assertTrue(consumes_allowance(record))
        self.assertNotIn("nightcord", state.world_state.channels_for("mizuki"))
        SessionState.from_dict(state.to_dict())


class EOfferedEvidenceTests(unittest.TestCase):
    def test_two_occurrences_ordinary_due_abstain_then_again(self):
        policy = CapturePolicy()
        state, scheduler, runtime = rig(policy)
        adopt(runtime)
        adopt(runtime, aid="second-plan")
        runtime.advance(10)
        scheduler.schedule(
            _activation("ordinary-later", clock=scheduler.clock, minutes_ahead=1)
        )
        runtime.advance(1)
        occurrences = state.arrangements.to_dict()["occurrences"].values()
        self.assertEqual([o["cognition_offered"] for o in occurrences], [3, 3])
        self.assertTrue(all(o["status"] == "open" for o in occurrences))
        self.assertEqual(len(policy.contexts[-1].arrangement_cues), 2)
        SessionState.from_dict(state.to_dict())

    def test_policy_error_keeps_actual_input(self):
        state, _, runtime = rig(CapturePolicy(error=True))
        adopt(runtime)
        # 非暂时策略错误直接形成结案。
        runtime.advance(10)
        record = state.agency.records()[0]
        self.assertEqual(record.outcome, AgencyOutcome.REJECTED_POLICY_ERROR)
        self.assertEqual(len(record.arrangements_carried), 1)
        self.assertEqual(
            next(iter(state.arrangements.to_dict()["occurrences"].values()))[
                "cognition_offered"
            ],
            1,
        )

    def test_commit_rollback_leaves_no_offered_and_retry_once(self):
        state, scheduler, runtime = rig()
        adopt(runtime)
        scheduler.advance_by(10)
        due = scheduler.pending_due()[0]
        plan = runtime.agency.propose(due)
        before = state.to_dict()
        with patch.object(
            state.activation_outbox,
            "_acknowledge",
            side_effect=RuntimeError("test ack"),
        ):
            with self.assertRaises(RuntimeError):
                runtime.agency.commit(plan)
        self.assertEqual(before, state.to_dict())
        runtime.agency.commit(runtime.agency.propose(due))
        with self.assertRaises(AgencyEngineError):
            runtime.agency.commit(plan)
        self.assertEqual(
            next(iter(state.arrangements.to_dict()["occurrences"].values()))[
                "cognition_offered"
            ],
            1,
        )

    def test_offered_count_evidence_and_record_tampering_rejected(self):
        state, _, runtime = rig()
        adopt(runtime)
        runtime.advance(10)
        payload = state.to_dict()
        for mutation in ("count", "offered", "carried", "cue_fingerprint", "identity"):
            broken = deepcopy(payload)
            occurrence = next(iter(broken["arrangements"]["occurrences"].values()))
            record = broken["agency"]["log"]["records"][0]
            if mutation == "count":
                occurrence["cognition_offered"] = 5
            elif mutation == "offered":
                occurrence["offered"] = {}
            elif mutation == "carried":
                record["arrangements_carried"] = []
            elif mutation == "cue_fingerprint":
                record["arrangements_carried"][0]["cue_fingerprint"] = "bad"
            else:
                record["arrangements_carried"][0]["identity"]["version"] = 2
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                SessionState.from_dict(broken)

    def test_quiet_then_ordinary_activation_carries_open_cue(self):
        policy = CapturePolicy()
        state, scheduler, runtime = rig(policy, AgencyBudget(quiet_cooldown_minutes=30))
        scheduler.schedule(_activation("first-cognition", minutes_ahead=1))
        runtime.advance(1)
        adopt(runtime)
        runtime.advance(9)
        self.assertEqual(state.agency.records()[-1].outcome, AgencyOutcome.QUIET)
        self.assertEqual(state.agency.records()[-1].arrangements_carried, ())
        scheduler.schedule(
            _activation("later-cognition", clock=scheduler.clock, minutes_ahead=30)
        )
        runtime.advance(30)
        self.assertEqual(len(policy.contexts[-1].arrangement_cues), 1)

    def test_asleep_and_unstarted_never_offer(self):
        from pns.models.world_state import Availability
        from pns.models.cognition import CognitionTimeline

        for asleep in (True, False):
            policy = CapturePolicy()
            state, _, runtime = rig(policy, AgencyBudget(quiet_cooldown_minutes=30))
            if asleep:
                state.world_state.set_availability("mizuki", Availability.ASLEEP)
            else:
                with state.atomic_commit():
                    state.set_cognition(
                        CognitionTimeline.open(
                            log_length=len(state.agency), sim=CLOCK, wall="test"
                        )
                    )
            adopt(runtime, window=2)
            runtime.advance(10)
            self.assertEqual(policy.contexts, [])
            self.assertFalse(state.agency.records()[0].arrangements_carried)
            runtime.advance(3)
            self.assertEqual(state.arrangements.statistics["missed_occurrences"], 1)


class FRecurrenceRollbackTests(unittest.TestCase):
    def test_following_enqueue_failure_rolls_back_whole_tick(self):
        state, scheduler, runtime = rig()
        adopt(runtime, weekly=True)
        before = state.to_dict()
        with patch.object(
            scheduler,
            "_schedule_arrangement",
            side_effect=SchedulerError("test following enqueue"),
        ):
            with self.assertRaises(SchedulerError):
                scheduler.advance_by(10)
        self.assertEqual(before, state.to_dict())

    def test_weekly_has_no_calendar_range_dependency_and_restores_next(self):
        state, scheduler, runtime = rig()
        adopt(runtime, weekly=True)
        runtime.advance(10)
        restored = SessionState.from_dict(state.to_dict())
        scheduler2 = PersistentScheduler(restored)
        self.assertEqual(
            scheduler2.next_due_at(), CLOCK + timedelta(minutes=10, days=7)
        )
        definition2 = ArrangementDefinition(
            {
                "id": "distant",
                "cue": "计划核对测试",
                "source": "inferred",
                "weekly": {"weekday": 6, "at": "12:00"},
            }
        )
        self.assertGreater(
            definition2.next_after(CLOCK.replace(year=2099)), CLOCK.replace(year=2099)
        )

    def test_revision_replaces_old_future_once(self):
        state, scheduler, runtime = rig()
        adopt(runtime, weekly=True)
        old = scheduler.pending()[0]
        runtime.maintain_arrangement(
            "revised",
            "mizuki",
            definition=definition(minute=20, weekly=True),
            expected_version=1,
        )
        self.assertEqual(len(scheduler.pending()), 1)
        self.assertNotEqual(scheduler.pending()[0].activation_id, old.activation_id)
        self.assertEqual(scheduler.pending()[0].payload["version"], 2)
        SessionState.from_dict(state.to_dict())

    def test_public_prefix_cannot_be_impersonated(self):
        state, scheduler, _ = rig()
        with self.assertRaises(SchedulerError):
            scheduler.schedule(_activation(ARRANGEMENT_PREFIX + "forged"))

    def test_direct_scheduler_leap_records_each_week_and_rollback_last(self):
        original, _, runtime = rig()
        adopt(runtime, weekly=True)
        state = SessionState.from_dict(original.to_dict())
        scheduler = PersistentScheduler(state)
        before = state.to_dict()
        real = scheduler._schedule_arrangement
        calls = []

        def fail_last(activation):
            calls.append(activation)
            if len(calls) == 3:
                raise SchedulerError("last test enqueue")
            return real(activation)

        with patch.object(
            scheduler, "_schedule_arrangement", side_effect=fail_last
        ), self.assertRaises(SchedulerError):
            scheduler.advance_by(22 * 1440)
        self.assertEqual(before, state.to_dict())
        tick = scheduler.advance_by(22 * 1440)
        self.assertEqual(len(tick.due), 4)
        self.assertTrue(all(d.missed_occurrences == 0 for d in tick.due))
        SessionState.from_dict(state.to_dict())


class GProjectionConsequenceTests(unittest.TestCase):
    def test_relative_words_rejected(self):
        from pns.models.arrangement import RELATIVE_WORDS

        for word in RELATIVE_WORDS:
            with self.subTest(word=word), self.assertRaises(ArrangementError):
                ArrangementDefinition(
                    dict(definition(), cue="计划" + word + "整理测试画稿")
                )

    def test_absolute_date_is_stable_across_midnight(self):
        policy = CapturePolicy()
        state, scheduler, runtime = rig(policy)
        adopt(runtime, minute=1, window=180)
        runtime.advance(1)
        first = policy.contexts[-1].arrangement_cues
        scheduler.schedule(
            _activation("ordinary-midnight", clock=scheduler.clock, minutes_ahead=45)
        )
        runtime.advance(45)
        self.assertEqual(first, policy.contexts[-1].arrangement_cues)
        self.assertIn("2026年08月21日 23:51", first[0]["cue"])

    def test_projection_abstain_expiry_never_create_observation_memory_or_action(self):
        policy = CapturePolicy()
        state, _, runtime = rig(policy)
        before = (len(state.observations), len(state.memories))
        adopt(runtime)
        runtime.advance(10)
        runtime.advance(121)
        self.assertEqual((len(state.observations), len(state.memories)), before)
        self.assertTrue(
            all(event.type.value == "world.time_advanced" for event in state.events)
        )
        self.assertTrue(
            all(record.event_id is None for record in state.agency.records())
        )

    def test_legal_speech_then_revoke_preserves_event_observation_memory(self):
        from test_autonomous_runtime import _rig

        state, scheduler, runtime = _rig(
            lines={"mizuki": "计划整理测试画稿", "ena": "测试回应"}
        )
        adopt(runtime)
        runtime.advance(10)
        record = state.agency.records()[0]
        self.assertEqual(record.outcome, AgencyOutcome.ACTED)
        before = (
            state.events.to_dict(),
            state.observations.to_dict(),
            state.memories.to_dict(),
        )
        runtime.maintain_arrangement(
            "revoked", "mizuki", arrangement_id="test-plan", expected_version=1
        )
        self.assertEqual(
            before,
            (
                state.events.to_dict(),
                state.observations.to_dict(),
                state.memories.to_dict(),
            ),
        )
        self.assertEqual(state.events.get(record.event_id).type.value, "message.sent")
        SessionState.from_dict(state.to_dict())

    def test_legal_speech_then_revision_preserves_event_observation_memory(self):
        from test_autonomous_runtime import _rig

        state, _, runtime = _rig(
            lines={"mizuki": "计划整理测试画稿", "ena": "测试回应"}
        )
        adopt(runtime)
        runtime.advance(10)
        before = (
            state.events.to_dict(),
            state.observations.to_dict(),
            state.memories.to_dict(),
        )
        runtime.maintain_arrangement(
            "revised", "mizuki", definition=definition(minute=20), expected_version=1
        )
        self.assertEqual(
            before,
            (
                state.events.to_dict(),
                state.observations.to_dict(),
                state.memories.to_dict(),
            ),
        )
        self.assertEqual(
            state.arrangements.to_dict()["current"]["test-plan"]["version"], 2
        )
        SessionState.from_dict(state.to_dict())


class APersistenceFailureTests(unittest.TestCase):
    def create(self):
        from pathlib import Path
        from test_world_lifecycle import _cold_state

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = FileWorldStore(Path(directory.name) / "worlds")
        service = WorldLifecycleService(store)
        self.addCleanup(service.release_all)
        world = service.create(
            "arrangement-test", _cold_state(clock=CLOCK), adapters=_adapters()
        )
        return store, world

    def test_real_prepublication_failure_restores_book_and_revision(self):
        store, world = self.create()
        before = world.state.to_dict()
        revision = world.status()["revision"]
        with patch.object(
            store, "save", side_effect=StorageError("test write failure")
        ):
            with self.assertRaises(CheckpointError):
                world.maintain_arrangement("adopted", "mizuki", definition=definition())
        self.assertEqual(before, world.state.to_dict())
        self.assertEqual(revision, world.status()["revision"])

    def test_published_not_durable_retains_memory_disk_and_revision(self):
        store, world = self.create()
        revision = world.status()["revision"]
        with patch(
            "os.fsync",
            side_effect=_dir_fsync(OSError(errno.EIO, "test directory sync")),
        ):
            with self.assertRaises(CheckpointError):
                world.maintain_arrangement("adopted", "mizuki", definition=definition())
        self.assertEqual(world.status()["revision"], revision + 1)
        self.assertFalse(world.status()["durable"])
        self.assertEqual(
            store.load("arrangement-test").restore_state().arrangements.to_dict(),
            world.state.arrangements.to_dict(),
        )


class AdditionalContractTests(unittest.TestCase):
    def test_old_archive_cannot_smuggle_arrangement_records(self):
        state, _, runtime = rig()
        adopt(runtime)
        runtime.advance(10)
        raw = WorldArchive.from_state_payload(
            "test-world", state.to_dict(), revision=1
        ).to_dict()
        raw["version"] = 3
        raw["state"].pop("arrangements")
        raw["state"].pop("catch_up_target")
        with self.assertRaises(ArchiveError):
            WorldArchive.from_dict(raw).restore_state()

    def test_v4_missing_book_is_not_migrated(self):
        state, _, _ = rig()
        raw = WorldArchive.from_state_payload(
            "test-world", state.to_dict(), revision=1
        ).to_dict()
        raw["state"].pop("arrangements")
        with self.assertRaises(ArchiveError):
            WorldArchive.from_dict(raw).restore_state()

    def test_queue_time_date_and_tombstone_corruption(self):
        state, scheduler, runtime = rig()
        adopt(runtime, weekly=True)
        runtime.advance(10)
        payload = state.to_dict()
        for kind in ("queue", "date", "trigger"):
            broken = deepcopy(payload)
            if kind == "queue":
                broken["scheduler"]["queue"]["activations"][0]["due_at"] = (
                    scheduler.clock + timedelta(days=8)
                ).isoformat()
            elif kind == "date":
                broken["scheduler"]["outbox"]["records"][0]["payload"][
                    "date_binding"
                ] = "2026-08-23"
            else:
                broken["events"]["events"][0]["provenance"]["due_activations"] = []
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                SessionState.from_dict(broken)
        runtime.maintain_arrangement(
            "revoked", "mizuki", arrangement_id="test-plan", expected_version=1
        )
        broken = state.to_dict()
        broken["arrangements"]["current"]["test-plan"]["revoked"] = False
        with self.assertRaises(ValueError):
            SessionState.from_dict(broken)

    def test_pending_budget_exhaustion_no_offer_and_postcall_exhaustion_keeps_input(
        self,
    ):
        from pns.models.cognition import CognitionTimeline

        policy = CapturePolicy()
        state, scheduler, runtime = rig(policy)
        with state.atomic_commit():
            state.set_cognition(
                CognitionTimeline.open(log_length=0, sim=CLOCK, wall="test")
            )
        runtime.start_cognition(run_allowance=1)
        adopt(runtime)
        scheduler.schedule(_activation("same-time-normal", minutes_ahead=10))
        scheduler.advance_by(10)
        dues = scheduler.pending_due()
        plans = [runtime.agency.propose(due) for due in dues]
        first = runtime.agency.commit(plans[0])
        last = runtime.agency.commit(plans[1])
        self.assertEqual(first.outcome, AgencyOutcome.ABSTAINED)
        self.assertEqual(last.outcome, AgencyOutcome.REJECTED_UNAVAILABLE)
        self.assertEqual(len(last.arrangements_carried), 1)
        scheduler.schedule(
            _activation("closed-budget", clock=scheduler.clock, minutes_ahead=1)
        )
        runtime.advance(1)
        self.assertFalse(state.agency.records()[-1].arrangements_carried)
        self.assertEqual(len(policy.contexts), 2)
        SessionState.from_dict(state.to_dict())

    def test_offer_after_revocation_cannot_claim_an_older_operation_sequence(self):
        state, scheduler, runtime = rig()
        adopt(runtime)
        runtime.advance(10)
        runtime.maintain_arrangement(
            "revoked", "mizuki", arrangement_id="test-plan", expected_version=1
        )
        runtime.advance(1)
        broken = state.to_dict()
        record = broken["agency"]["log"]["records"][0]
        record["arrangements_carried"][0]["offered_at"] = scheduler.clock.isoformat()
        record["arrangements_carried"][0][
            "eligibility_at"
        ] = scheduler.clock.isoformat()
        with self.assertRaises(ValueError):
            SessionState.from_dict(broken)

    def test_production_target_checkpoint_precedes_pending_policy(self):
        from pathlib import Path
        from test_world_lifecycle import _cold_state

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = FileWorldStore(Path(directory.name) / "worlds")
        service = WorldLifecycleService(store)
        self.addCleanup(service.release_all)
        policy = CapturePolicy()
        from pns.runtime.persistence import RuntimeAdapters

        world = service.create(
            "goal-test",
            _cold_state(clock=CLOCK),
            adapters=RuntimeAdapters(
                auditor=ScriptedAuditor(), policy_factory=lambda state: policy
            ),
        )
        anchor_runtime(world.runtime)
        world.maintain_arrangement("adopted", "mizuki", definition=definition(window=2))
        world.state.scheduler.advance_by(10)
        target = CLOCK + timedelta(hours=2)
        reach_goal(world.runtime, target, max_steps=0)
        disk = store.load("goal-test").restore_state()
        self.assertEqual(disk.catch_up_target, target)
        self.assertEqual(disk.arrangements.statistics["missed_occurrences"], 1)
        self.assertEqual(policy.contexts, [])
        reach_goal(world.runtime, target)
        self.assertEqual(world.state.agency.records()[0].outcome, AgencyOutcome.LAPSED)

    def test_failed_target_checkpoint_does_not_process_pending(self):
        state, scheduler, runtime = rig(CapturePolicy(), anchored=True)
        adopt(runtime)
        scheduler.advance_by(10)
        runtime._target_checkpoint = lambda: (_ for _ in ()).throw(
            StorageError("test target save")
        )
        with self.assertRaises(StorageError):
            reach_goal(runtime, CLOCK + timedelta(minutes=20))
        self.assertEqual(len(state.agency), 0)
        self.assertEqual(len(scheduler.pending_due()), 1)

    def test_cue_is_immutable_policy_input(self):
        policy = CapturePolicy()
        state, _, runtime = rig(policy)
        adopt(runtime)
        runtime.advance(10)
        with self.assertRaises(TypeError):
            policy.contexts[0].arrangement_cues[0]["cue"] = "伪造"

    def test_arrangement_projection_reaches_generation_without_due_bookkeeping(self):
        policy = CapturePolicy()
        _, _, runtime = rig(policy)
        adopt(runtime)
        runtime.advance(10)
        from pns.models.action import LegalAction, ActionId

        context = build_generation_context(
            policy.contexts[0], LegalAction(ActionId.SPEAK_HERE)
        )
        self.assertIsNone(context.activation.cue)
        self.assertEqual(len(context.arrangement_cues), 1)
        self.assertNotIn(ARRANGEMENT_PREFIX, str(context.to_dict()))


class AMaintenanceInterfaceTests(unittest.TestCase):
    def plane(self):
        from pathlib import Path
        from types import SimpleNamespace
        from test_world_lifecycle import _cold_state
        from pns.interfaces.arrangement_maintenance import run_arrangement_decision

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        service = WorldLifecycleService(FileWorldStore(Path(directory.name) / "worlds"))
        self.addCleanup(service.release_all)
        world = service.create(
            "cold-test", _cold_state(clock=CLOCK), adapters=_adapters(), start=False
        )
        world.close()
        characters = {
            "mizuki": SimpleNamespace(
                arrangements=(ArrangementDefinition(definition()),)
            ),
            "ena": SimpleNamespace(arrangements=()),
        }
        registry = SimpleNamespace(character=lambda cid: characters[cid])
        return (
            SimpleNamespace(
                service=service,
                registry=lambda: registry,
                build_adapters=lambda *args, **kwargs: _adapters(),
            ),
            characters,
        )

    def test_pending_definition_adopt_revision_and_deletion_are_explicit(self):
        from pns.interfaces.arrangement_maintenance import (
            pending_arrangements,
            run_arrangement_decision,
        )

        plane, characters = self.plane()
        pending = pending_arrangements(plane, "cold-test")
        self.assertEqual(pending[0]["action"], "adopted")
        report = run_arrangement_decision(
            plane, "cold-test", "adopted", "mizuki", "test-plan"
        )
        self.assertTrue(report["durable"])
        self.assertEqual(pending_arrangements(plane, "cold-test"), [])
        characters["mizuki"].arrangements = (
            ArrangementDefinition(dict(definition(), cue="计划校对测试画稿")),
        )
        self.assertEqual(
            pending_arrangements(plane, "cold-test")[0]["action"], "revised"
        )
        report = run_arrangement_decision(
            plane, "cold-test", "revised", "mizuki", "test-plan", 1
        )
        self.assertTrue(report["durable"])
        characters["mizuki"].arrangements = ()
        self.assertEqual(pending_arrangements(plane, "cold-test"), [])
        entry = (
            plane.service.store.load("cold-test")
            .restore_state()
            .arrangements.to_dict()["current"]["test-plan"]
        )
        self.assertFalse(entry["revoked"])
        report = run_arrangement_decision(
            plane, "cold-test", "revoked", "mizuki", "test-plan", 2
        )
        self.assertTrue(report["durable"])

    def test_cold_checkpoint_error_never_reports_durable_success(self):
        from pns.interfaces.arrangement_maintenance import run_arrangement_decision

        plane, _ = self.plane()
        with patch.object(
            plane.service.store, "save", side_effect=StorageError("test cold save")
        ):
            report = run_arrangement_decision(
                plane, "cold-test", "adopted", "mizuki", "test-plan"
            )
        self.assertFalse(report["durable"])
        self.assertTrue(report["errors"])
        self.assertEqual(
            plane.service.store.load("cold-test").restore_state().arrangements.sequence,
            0,
        )
