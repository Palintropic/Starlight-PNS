# tests/test_clock_runtime.py — 锚点时间与认知时间线在运行时上的接线（WORLD-1 设计 §2、§5、§13–§14）。
#
# 守的线：
#   1. 新世界从"还没 Start"开始：时间照走，到期不调模型、以 not_started 收尾；
#   2. 恢复：停机期间触发的到期记 process_stopped；补跑途中 Start，旧到期仍不可用；
#   3. 现实时钟落后于存档时钟：记 wall_clock_behind，时钟不倒退、不前进；
#   4. 认知不可用的到期不进策略、不调模型；Stop 之后待处理的也一样；
#   5. 单次额度在同一事务里耗尽，时钟继续走；
#   6. 故障与解除：补跑经过故障时段的到期带 fault；
#   7. 每一步之后存档都能原样加载（判定能从存档本身重新推出来）。
#
# 运行: python -m unittest discover -s tests -p test_clock_runtime.py
import unittest
from datetime import datetime, timedelta, timezone

from grants_support import grant_everything
from pns.models.activation import ActivationKind, ScheduledActivation
from pns.models.agency import AgencyOutcome
from pns.models.clock_anchor import ClockAnchor
from pns.models.cognition import CognitionCause as C
from pns.models.session import SessionState
from pns.models.world_state import WorldState
from pns.runtime.autonomy.audit import ScriptedAuditor
from pns.runtime.autonomy.coordinator import AutonomousRuntime, AutonomyError
from pns.runtime.autonomy.generation import AuthoredLinePolicy, ScriptedLineGenerator
from pns.runtime.memory.recall import MemoryRecall
from pns.runtime.scheduler import PersistentScheduler
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph

SIM = datetime(2026, 9, 27, 19, 0)
WALL = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)


def wall(minutes=0):
    return WALL + timedelta(minutes=minutes)


def sim(minutes=0):
    return SIM + timedelta(minutes=minutes)


class _Calls:
    def __init__(self):
        self.n = 0

    def __call__(self, context):
        self.n += 1
        return "在的哦"


def _world(clock=SIM):
    world = WorldState(
        clock=clock,
        locations=build_default_location_graph(),
        channels=build_default_channel_registry(),
    )
    grant_everything(world)
    world.place_character("mizuki", "mizuki_home_room")
    world.place_character("ena", "ena_home_studio")
    for character_id in ("mizuki", "ena"):
        world.join_channel(character_id, "nightcord")
    return world


def _bind(state, calls):
    scheduler = state.scheduler or PersistentScheduler(state)
    generator = ScriptedLineGenerator({"mizuki": calls, "ena": calls})
    policy = AuthoredLinePolicy(generator, recall=MemoryRecall(state))
    runtime = AutonomousRuntime(state, policy=policy, auditor=ScriptedAuditor())
    return scheduler, runtime


def _new_world(calls):
    state = SessionState(session_id="s1", scene="gate", characters=["mizuki", "ena"])
    state.attach_world_state(_world())
    state.initialize_runtime("开场")
    scheduler, runtime = _bind(state, calls)
    runtime.open_clock(ClockAnchor(SIM, WALL), wall=wall())
    state.publish()
    runtime.start()
    return state, scheduler, runtime


def _restore(state, calls, *, at):
    restored = SessionState.from_dict(state.to_dict())
    scheduler, runtime = _bind(restored, calls)
    runtime.restore_clock(wall=at)
    restored.publish()
    runtime.start()
    return restored, scheduler, runtime


def _schedule(scheduler, activation_id, at, character_id="mizuki"):
    scheduler.schedule(
        ScheduledActivation(
            activation_id=activation_id,
            kind=ActivationKind.CHARACTER_ACTIVATION,
            due_at=at,
            character_id=character_id,
        )
    )


def _records(state):
    return {record.due_id.split("@")[0]: record for record in state.agency.records()}


def _causes(record):
    return set(record.detail["causes"])


def _reloads(test, state):
    SessionState.from_dict(state.to_dict())


class NewWorldTests(unittest.TestCase):
    def test_time_runs_before_start_but_nobody_decides(self):
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls)
        _schedule(scheduler, "wake", sim(10))
        report = runtime.advance_to_anchor(wall(30))
        self.assertEqual(state.world_state.clock, sim(30))
        self.assertEqual(report["minutes"], 30)
        record = _records(state)["wake"]
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_UNAVAILABLE)
        self.assertEqual(_causes(record), {"not_started"})
        self.assertEqual(calls.n, 0)
        _reloads(self, state)

    def test_after_start_a_due_is_decided_at_its_own_minute(self):
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls)
        runtime.start_cognition(5, wall=wall())
        _schedule(scheduler, "wake", sim(10))
        runtime.advance_to_anchor(wall(30))
        record = _records(state)["wake"]
        self.assertIs(record.outcome, AgencyOutcome.ACTED)
        self.assertEqual(record.decided_at, sim(10))
        self.assertEqual(calls.n, 1)
        self.assertEqual(runtime.cognition_status(wall(30))["run_remaining"], 4)
        _reloads(self, state)

    def test_the_start_minute_itself_still_belongs_to_before(self):
        # Start 在 19:00:30 按下 → 生效于 19:01；19:00 触发的仍是 not_started。
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls)
        _schedule(scheduler, "same-minute", sim(1))
        runtime.advance_to_anchor(wall(1))
        runtime.start_cognition(5, wall=wall(1) + timedelta(seconds=30))
        _schedule(scheduler, "next-minute", sim(2))
        runtime.advance_to_anchor(wall(2))
        records = _records(state)
        self.assertEqual(_causes(records["same-minute"]), {"not_started"})
        self.assertIs(records["next-minute"].outcome, AgencyOutcome.ACTED)
        _reloads(self, state)

    def test_the_anchor_bounds_the_clock(self):
        calls = _Calls()
        state, _scheduler, runtime = _new_world(calls)
        runtime.advance_to_anchor(wall(10) + timedelta(seconds=59))
        self.assertEqual(state.world_state.clock, sim(10))
        runtime.advance_to_anchor(wall(5))  # 现实时间"回到"更早：不倒退
        self.assertEqual(state.world_state.clock, sim(10))

    def test_max_steps_splits_a_long_catch_up(self):
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls)
        for index in range(3):
            _schedule(scheduler, f"w{index}", sim(10 * (index + 1)))
        runtime.advance_to_anchor(wall(60), max_steps=2)
        self.assertEqual(state.world_state.clock, sim(20))
        runtime.advance_to_anchor(wall(60))
        self.assertEqual(state.world_state.clock, sim(60))

    def test_a_session_without_an_anchor_cannot_follow_wall_time(self):
        state = SessionState(session_id="s1", scene="gate", characters=["mizuki", "ena"])
        state.attach_world_state(_world())
        state.initialize_runtime("开场")
        _scheduler, runtime = _bind(state, _Calls())
        runtime.start()
        for call in (
            lambda: runtime.advance_to_anchor(wall()),
            lambda: runtime.restore_clock(wall=wall()),
            lambda: runtime.start_cognition(1, wall=wall()),
        ):
            with self.subTest(call=call):
                with self.assertRaises(AutonomyError):
                    call()


class UnavailableDuesSkipTheModelTests(unittest.TestCase):
    def test_a_pending_due_after_stop_closes_without_a_model_call(self):
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls)
        runtime.start_cognition(5, wall=wall())
        _schedule(scheduler, "wake", sim(10))
        runtime.advance_to_anchor(wall(10))  # 触发并处理
        _schedule(scheduler, "later", sim(20))
        # 只推时钟、不处理：用 max_results=0 让它留在投递箱里。
        runtime.advance(10, max_results=0)
        runtime.stop_cognition(wall=wall(20))
        before = calls.n
        (result,) = runtime.process_pending()
        record = _records(state)["later"]
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_UNAVAILABLE)
        self.assertEqual(_causes(record), {"operator_paused"})
        self.assertEqual(calls.n, before, "不可用的到期不许调模型")
        _reloads(self, state)

    def test_stop_before_start_writes_nothing(self):
        state, _scheduler, runtime = _new_world(_Calls())
        intervals = len(state.cognition.intervals)
        runtime.stop_cognition(wall=wall())
        self.assertEqual(len(state.cognition.intervals), intervals)


class AllowanceTests(unittest.TestCase):
    def test_the_allowance_runs_out_and_the_clock_keeps_going(self):
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls)
        runtime.start_cognition(1, wall=wall())
        _schedule(scheduler, "a", sim(5), "mizuki")
        _schedule(scheduler, "b", sim(5), "ena")
        _schedule(scheduler, "c", sim(15), "mizuki")
        runtime.advance_to_anchor(wall(30))
        records = _records(state)
        outcomes = sorted(record.outcome.value for record in records.values())
        self.assertEqual(
            outcomes, ["acted", "rejected_unavailable", "rejected_unavailable"]
        )
        self.assertEqual(calls.n, 1)
        self.assertEqual(state.world_state.clock, sim(30))
        self.assertEqual(_causes(records["c"]), {"run_budget_exhausted"})
        self.assertEqual(runtime.cognition_status(wall(30))["run_remaining"], 0)
        _reloads(self, state)


class RestoreTests(unittest.TestCase):
    def _offline(self, calls):
        state, scheduler, runtime = _new_world(calls)
        runtime.start_cognition(5, wall=wall())
        for index, minutes in enumerate((20, 70, 130)):
            _schedule(scheduler, f"w{index}", sim(minutes))
        runtime.advance_to_anchor(wall(10))  # 19:10 存档，然后进程没了
        return state

    def test_dues_during_the_outage_end_as_process_stopped(self):
        calls = _Calls()
        archived = self._offline(calls)
        # 两小时后（21:10）恢复，还没 Start。
        state, _scheduler, runtime = _restore(archived, calls, at=wall(130))
        runtime.advance_to_anchor(wall(135))
        records = _records(state)
        self.assertEqual(_causes(records["w0"]), {"not_started", "process_stopped"})
        self.assertEqual(_causes(records["w1"]), {"not_started", "process_stopped"})
        self.assertEqual(_causes(records["w2"]), {"not_started", "process_stopped"})
        self.assertEqual(calls.n, 0)
        _reloads(self, state)

    def test_start_during_catch_up_does_not_revive_the_outage(self):
        calls = _Calls()
        archived = self._offline(calls)
        state, scheduler, runtime = _restore(archived, calls, at=wall(130))
        runtime.start_cognition(5, wall=wall(130))  # 21:10 Start，时钟还在 19:10
        _schedule(scheduler, "fresh", sim(140))
        runtime.advance_to_anchor(wall(145))
        records = _records(state)
        for name in ("w0", "w1", "w2"):
            self.assertEqual(_causes(records[name]), {"not_started", "process_stopped"})
        self.assertIs(records["fresh"].outcome, AgencyOutcome.ACTED)
        self.assertEqual(calls.n, 1)
        _reloads(self, state)

    def test_a_wall_clock_behind_the_archive_holds_the_clock(self):
        calls = _Calls()
        archived = self._offline(calls)
        # UTC 被往回拨：换算出的 19:05 早于存档时钟 19:10。
        state, _scheduler, runtime = _restore(archived, calls, at=wall(5))
        self.assertIn(C.WALL_CLOCK_BEHIND, state.cognition.current.causes)
        runtime.advance_to_anchor(wall(8))
        self.assertEqual(state.world_state.clock, sim(10))
        runtime.mark_wall_clock_caught_up(wall=wall(10))
        self.assertNotIn(C.WALL_CLOCK_BEHIND, state.cognition.current.causes)
        runtime.start_cognition(5, wall=wall(10))
        runtime.advance_to_anchor(wall(30))
        record = _records(state)["w0"]  # 19:20：追上并 Start 之后触发
        self.assertIs(record.outcome, AgencyOutcome.ACTED)
        _reloads(self, state)


    def test_a_pending_due_survives_a_wall_clock_rollback_as_process_stopped(self):
        # 存档时钟 19:10，那一刻触发的到期还没处理；恢复时 UTC 被拨回到 19:05。
        # cutoff 不能按回拨后的分钟算——那条到期是在停机前触发的。
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls)
        runtime.start_cognition(5, wall=wall())
        _schedule(scheduler, "pending", sim(10))
        runtime.advance(10, max_results=0)
        self.assertEqual(len(state.activation_outbox.pending()), 1)
        restored, _scheduler, runtime = _restore(state, calls, at=wall(5))
        runtime.process_pending()
        record = _records(restored)["pending"]
        self.assertIn("process_stopped", _causes(record))
        self.assertEqual(calls.n, 0)
        _reloads(self, restored)


class FaultTests(unittest.TestCase):
    def test_dues_crossed_during_a_fault_carry_it(self):
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls)
        runtime.start_cognition(5, wall=wall())
        _schedule(scheduler, "during", sim(20))
        _schedule(scheduler, "after", sim(70))
        runtime.advance_to_anchor(wall(10))
        runtime.begin_fault(wall=wall(10))  # 时钟 19:10 起故障
        runtime.clear_fault(wall=wall(60))  # 20:00 解除 → cutoff 20:01
        runtime.advance_to_anchor(wall(75))
        records = _records(state)
        self.assertEqual(_causes(records["during"]), {"fault"})
        self.assertIs(records["after"].outcome, AgencyOutcome.ACTED)
        self.assertEqual(calls.n, 1)
        _reloads(self, state)


if __name__ == "__main__":
    unittest.main()
