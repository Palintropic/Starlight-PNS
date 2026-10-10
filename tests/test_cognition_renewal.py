# tests/test_cognition_renewal.py — 认知额度按世界日续（COG-1「Agamotto」）。
#
# 守的线（序号对应设计 v2.1 的对抗性用例）：
#   2. Stop 收回授权：耗尽、耗尽 + 故障之后照样记一条，之后跨边界不续、不调模型；
#      不续额的世界耗尽后 Stop 也记（有意改动）；
#   3. 每个边界都是落点：三种推进入口、max_steps 拆分、安静的分钟，各续一次；
#   4. 05:00 触发的到期属于前一天：旧日最后一次额度先花，离开边界才续；
#   5. 恢复后不 Start 零续额；补跑中 Start 只续严格晚于它的边界；故障期间照续、
#      故障仍在；
#   6. 存档篡改：重放与 (a)–(d) 各自拒绝哪一种；明确不保证的那一类被接受；
#   7. 旧存档前向安装；不续额世界序列化不多键；
#   8. 续额步回滚：异常与安静重走都只留下一次续额；
#   9. 调度器旁路守卫。
#
# 运行: python -m unittest discover -s tests -p test_cognition_renewal.py
import copy
import unittest
from datetime import timedelta

from test_clock_runtime import (
    SIM,
    WALL,
    _Calls,
    _causes,
    _records,
    _schedule,
    _world,
    sim,
    wall,
)
from pns.models.agency import AgencyOutcome
from pns.models.clock_anchor import ClockAnchor
from pns.models.cognition import (
    CognitionCause as C,
    CognitionTimeline,
    CognitionTimelineError,
    TransitionKind as K,
    next_boundary,
)
from pns.models.session import SessionState, SessionStateError, _validate_renewals
from pns.runtime.autonomy.audit import ScriptedAuditor
from pns.runtime.autonomy.coordinator import AutonomousRuntime, AutonomyError
from pns.runtime.autonomy.generation import AuthoredLinePolicy, ScriptedLineGenerator
from pns.runtime.memory.recall import MemoryRecall
from pns.runtime.scheduler import PersistentScheduler, SchedulerError

POLICY = "world-day-0500"


class _Due:
    """记录与到期记录的最小替身：规则只读 due_id 和 fired_at。"""

    def __init__(self, due_id, fired_at):
        self.due_id = due_id
        self.fired_at = fired_at


class _Outbox:
    def __init__(self, dues):
        self._dues = {due.due_id: due for due in dues}

    def get(self, due_id):
        return self._dues[due_id]

# SIM 是 09-27 19:00：sim(600) 是 09-28 05:00，sim(2040) 是 09-29 05:00。
B1, B2 = sim(600), sim(2040)
DAY0 = B1 - timedelta(days=1)  # SIM 所在世界日的起点：09-27 05:00


def _bind(state, calls, renewal=POLICY):
    scheduler = state.scheduler or PersistentScheduler(state)
    generator = ScriptedLineGenerator({"mizuki": calls, "ena": calls})
    policy = AuthoredLinePolicy(generator, recall=MemoryRecall(state))
    runtime = AutonomousRuntime(
        state, policy=policy, auditor=ScriptedAuditor(), allowance_renewal=renewal
    )
    return scheduler, runtime


def _new_world(calls, renewal=POLICY):
    state = SessionState(session_id="s1", scene="gate", characters=["mizuki", "ena"])
    state.attach_world_state(_world())
    state.initialize_runtime("开场")
    scheduler, runtime = _bind(state, calls, renewal)
    runtime.open_clock(ClockAnchor(SIM, WALL), wall=wall())
    state.publish()
    runtime.start()
    return state, scheduler, runtime


def _restore(state, calls, *, at, renewal=POLICY):
    restored = SessionState.from_dict(state.to_dict())
    scheduler, runtime = _bind(restored, calls, renewal)
    runtime.restore_clock(wall=at)
    restored.publish()
    runtime.start()
    return restored, scheduler, runtime


def _renewals(state):
    return [
        interval.opened_at_sim
        for interval in state.cognition.intervals
        if interval.opened_by is K.ALLOWANCE_RENEWED
    ]


def _kinds(state):
    return [interval.opened_by for interval in state.cognition.intervals]


def _reload(state):
    return SessionState.from_dict(state.to_dict())


# ── 时间线模型 ────────────────────────────────────────────────────────────
class PolicyTests(unittest.TestCase):
    def test_the_boundary_is_strictly_after_the_day_start(self):
        self.assertEqual(next_boundary(POLICY, sim(0)), B1)
        self.assertEqual(next_boundary(POLICY, sim(599)), B1)
        # Start 恰在 05:00：这一次就是新装满，下一次续在明天。
        self.assertEqual(next_boundary(POLICY, B1), B2)
        self.assertEqual(next_boundary(POLICY, sim(660)), B2)

    def test_an_unknown_policy_is_refused_everywhere(self):
        timeline = CognitionTimeline.open(log_length=0, sim=SIM, wall="w")
        with self.assertRaises(CognitionTimelineError):
            timeline.started(log_length=0, sim=SIM, wall="w", run_allowance=2, renewal="x")
        with self.assertRaises(AutonomyError):
            _new_world(_Calls(), renewal="world-day-0400")

    def test_an_unlimited_start_does_not_renew(self):
        timeline = CognitionTimeline.open(log_length=0, sim=SIM, wall="w")
        with self.assertRaises(CognitionTimelineError):
            timeline.started(
                log_length=0, sim=SIM, wall="w", run_allowance=None, renewal=POLICY
            )
        # 运行时那一侧：Start(None) 不带策略，不续。
        calls = _Calls()
        state, _, runtime = _new_world(calls)
        runtime.start_cognition(None, wall=wall())
        self.assertIsNone(state.cognition.current.renewal)
        self.assertIsNone(state.cognition.current.allowance_from_sim)


class SerializationTests(unittest.TestCase):
    LEGACY_KEYS = {
        "index",
        "from_log",
        "causes",
        "backlog",
        "run_allowance",
        "allowance_since_log",
        "opened_by",
        "opened_at_sim",
        "opened_at_wall",
    }

    def test_a_non_renewing_finite_start_gains_no_key(self):
        # 复审 F1：不续额的有限 Start 两个新字段都为空，序列化与加字段之前相同。
        state, _, runtime = _new_world(_Calls(), renewal=None)
        runtime.start_cognition(3, wall=wall())
        for interval in state.cognition.to_dict()["intervals"]:
            self.assertEqual(set(interval), self.LEGACY_KEYS)
        self.assertIsNone(state.cognition.current.allowance_from_sim)

    def test_a_renewing_start_records_the_policy_and_its_day(self):
        state, _, runtime = _new_world(_Calls())
        runtime.start_cognition(3, wall=wall())
        current = state.cognition.current.to_dict()
        self.assertEqual(current["renewal"], POLICY)
        # TODO 43：额度记的是它付账的那个世界日的起点（当天 05:00），不是按下的那一分钟。
        self.assertEqual(current["allowance_from_sim"], DAY0.isoformat())
        _reload(state)


# ── Stop（用例 2）────────────────────────────────────────────────────────
class StopTests(unittest.TestCase):
    def _exhausted(self, renewal=POLICY):
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls, renewal)
        runtime.start_cognition(1, wall=wall())
        _schedule(scheduler, "a", sim(10))
        runtime.advance(11)
        self.assertIn(C.RUN_BUDGET_EXHAUSTED, state.cognition.current.causes)
        return calls, state, scheduler, runtime

    def test_stop_after_exhaustion_revokes_the_renewal(self):
        calls, state, scheduler, runtime = self._exhausted()
        runtime.stop_cognition(wall=wall(11))
        self.assertIs(state.cognition.current.opened_by, K.STOPPED)
        self.assertIsNone(state.cognition.current.renews_at)
        _schedule(scheduler, "b", sim(650))
        runtime.advance(700)
        self.assertEqual(_renewals(state), [])
        self.assertEqual(calls.n, 1)
        record = _records(state)["b"]
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_UNAVAILABLE)
        self.assertIn("operator_paused", _causes(record))
        _reload(state)

    def test_stop_after_exhaustion_is_recorded_without_renewal_too(self):
        # 有意改动（设计 §4）：不续额的世界耗尽后 Stop 也记一条，状态如实是"已停"。
        _, state, _, runtime = self._exhausted(renewal=None)
        before = len(state.cognition.intervals)
        runtime.stop_cognition(wall=wall(11))
        self.assertEqual(len(state.cognition.intervals), before + 1)
        self.assertIn(C.OPERATOR_PAUSED, state.cognition.current.causes)
        self.assertIn(C.RUN_BUDGET_EXHAUSTED, state.cognition.current.causes)
        _reload(state)

    def test_stop_with_exhaustion_and_fault_is_recorded(self):
        calls, state, scheduler, runtime = self._exhausted()
        runtime.begin_fault(wall=wall(11))
        runtime.stop_cognition(wall=wall(11))
        self.assertIs(state.cognition.current.opened_by, K.STOPPED)
        runtime.advance(700)
        self.assertEqual(_renewals(state), [])
        _reload(state)

    def test_stop_is_idempotent_once_paused(self):
        _, state, _, runtime = self._exhausted()
        runtime.stop_cognition(wall=wall(11))
        count = len(state.cognition.intervals)
        runtime.stop_cognition(wall=wall(12))
        self.assertEqual(len(state.cognition.intervals), count)

    def test_stop_right_after_a_renewal_closes_the_next_minute(self):
        calls, state, scheduler, runtime = self._exhausted()
        runtime.advance(590)  # 停在 05:01：已离开边界、已续额
        self.assertEqual(_renewals(state), [B1])
        runtime.stop_cognition(wall=wall(601))
        _schedule(scheduler, "b", sim(602))
        runtime.advance(5)
        self.assertEqual(calls.n, 1)
        self.assertIs(_records(state)["b"].outcome, AgencyOutcome.REJECTED_UNAVAILABLE)
        _reload(state)


# ── 每个边界都是落点（用例 3）───────────────────────────────────────────
class BoundaryLandingTests(unittest.TestCase):
    def _started(self):
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls)
        runtime.start_cognition(2, wall=wall())
        return calls, state, scheduler, runtime

    def _two_days(self, move):
        calls, state, scheduler, runtime = self._started()
        move(scheduler, runtime)
        self.assertEqual(state.world_state.clock, sim(2041))
        self.assertEqual(_renewals(state), [B1, B2])
        _reload(state)
        return state

    def test_advance_lands_on_both_boundaries(self):
        self._two_days(lambda scheduler, runtime: runtime.advance(2041))

    def test_advance_to_anchor_lands_on_both_boundaries(self):
        self._two_days(lambda scheduler, runtime: runtime.advance_to_anchor(wall(2041)))

    def test_advance_to_next_due_lands_on_both_boundaries(self):
        def move(scheduler, runtime):
            _schedule(scheduler, "far", sim(2041))
            runtime.advance_to_next_due()

        self._two_days(move)

    def test_an_empty_queue_next_due_does_not_move(self):
        _, state, _, runtime = self._started()
        self.assertIsNone(runtime.advance_to_next_due())
        self.assertEqual(state.world_state.clock, SIM)

    def test_arriving_at_a_boundary_does_not_renew_leaving_does(self):
        _, state, _, runtime = self._started()
        runtime.advance_to_anchor(wall(2041), max_steps=1)
        self.assertEqual(state.world_state.clock, B1)
        self.assertEqual(_renewals(state), [])
        # 停在边界上存一次档：时钟 = b、还没续额，合法。
        _reload(state)
        runtime.advance_to_anchor(wall(2041), max_steps=1)
        self.assertEqual(state.world_state.clock, B2)
        self.assertEqual(_renewals(state), [B1])
        runtime.advance_to_anchor(wall(2041), max_steps=1)
        self.assertEqual(_renewals(state), [B1, B2])
        _reload(state)

    def test_keep_going_pausing_at_a_boundary(self):
        _, state, _, runtime = self._started()
        answers = iter([True, False])
        runtime.advance_to_anchor(wall(2041), keep_going=lambda: next(answers))
        self.assertEqual(state.world_state.clock, B1)
        self.assertEqual(_renewals(state), [])
        runtime.advance_to_anchor(wall(2041))
        self.assertEqual(_renewals(state), [B1, B2])

    def test_quiet_minutes_still_renew_without_time_events(self):
        _, state, _, runtime = self._started()
        runtime.set_quiet_time_events(False, wall=wall())
        events = len(state.events)
        runtime.advance(2041)
        self.assertEqual(_renewals(state), [B1, B2])
        self.assertEqual(len(state.events), events, "续额是运维记录，不写时间事件")
        _reload(state)


# ── 05:00 的账（用例 4）─────────────────────────────────────────────────
class BoundaryMinuteTests(unittest.TestCase):
    def test_dues_at_the_boundary_spend_the_old_day(self):
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls)
        runtime.start_cognition(1, wall=wall())
        _schedule(scheduler, "b1", B1)
        _schedule(scheduler, "b2", B1, "ena")
        runtime.advance(601, max_results=0)
        # 到期待处理，时钟停在边界，不续额。
        self.assertEqual(state.world_state.clock, B1)
        self.assertEqual(len(state.activation_outbox.pending()), 2)
        self.assertEqual(_renewals(state), [])
        _reload(state)
        runtime.advance(1)
        # 两条都按旧日结算：一条花掉旧日最后一次，另一条按耗尽不可用；然后才续。
        self.assertEqual(calls.n, 1)
        outcomes = sorted(record.outcome.value for record in state.agency.records())
        self.assertEqual(outcomes, ["acted", "rejected_unavailable"])
        unavailable = [
            record
            for record in state.agency.records()
            if record.outcome is AgencyOutcome.REJECTED_UNAVAILABLE
        ]
        self.assertEqual(_causes(unavailable[0]), {"run_budget_exhausted"})
        self.assertEqual(_renewals(state), [B1])
        self.assertEqual(state.world_state.clock, sim(601))
        # 新的一天：额度满了。
        _schedule(scheduler, "c", sim(605))
        runtime.advance(5)
        self.assertEqual(calls.n, 2)
        self.assertEqual(runtime.cognition_status(wall(605))["run_remaining"], 0)
        _reload(state)

    def test_leftover_allowance_is_not_carried(self):
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls)
        runtime.start_cognition(3, wall=wall())
        _schedule(scheduler, "a", sim(10))
        runtime.advance(601)
        self.assertEqual(runtime.cognition_status(wall(601))["run_remaining"], 3)

    def test_renewal_refuses_to_leave_with_pending_dues(self):
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls)
        runtime.start_cognition(1, wall=wall())
        _schedule(scheduler, "b1", B1)
        runtime.advance(600, max_results=0)
        timeline = state.cognition
        with self.assertRaises(AutonomyError):
            with runtime._gate:
                runtime._clock_step_locked(sim(601))
        self.assertIs(state.cognition, timeline)
        self.assertEqual(state.world_state.clock, B1)


# ── 恢复、补跑、故障（用例 5）──────────────────────────────────────────
class RestoreAndCatchUpTests(unittest.TestCase):
    def test_restore_without_start_never_renews(self):
        calls = _Calls()
        state, _, runtime = _new_world(calls)
        runtime.start_cognition(2, wall=wall())
        runtime.advance(10)
        restored, _, runtime = _restore(state, calls, at=wall(10))
        runtime.advance_to_anchor(wall(2041))
        self.assertEqual(_renewals(restored), [])
        self.assertIsNone(restored.cognition.current.renewal)
        _reload(restored)

    def test_a_catch_up_start_renews_only_after_its_own_minute(self):
        calls = _Calls()
        state, _, runtime = _new_world(calls)
        runtime.advance(10)  # 19:10 停机
        restored, scheduler, runtime = _restore(state, calls, at=wall(660))
        # 补跑途中、时钟还在前一天 19:10，按下 Start：生效分钟是次日 06:00。
        # TODO 43 起，这份额度付的是时钟所在的那一天（补跑要处理的到期属于它），
        # 补跑越过 05:00 时照常续；Start 之前的到期仍由 backlog 挡成不可用。
        runtime.start_cognition(2, wall=wall(660))
        self.assertEqual(restored.world_state.clock, sim(10))
        self.assertEqual(restored.cognition.current.allowance_from_sim, DAY0)
        runtime.advance_to_anchor(wall(2041))
        self.assertEqual(_renewals(restored), [B1, B2])
        _reload(restored)

    def test_a_start_exactly_on_the_boundary_renews_tomorrow(self):
        calls = _Calls()
        state, _, runtime = _new_world(calls)
        runtime.advance(600)
        # 时钟停在 05:00（到了、还没离开）：这一分钟的到期属于前一天，Start 付的是
        # 前一天剩下的；离开 05:00 照常续满（TODO 43，ena 复审 P2 的反例 1）。
        runtime.start_cognition(2, wall=wall(600))
        runtime.advance(1441)
        self.assertEqual(_renewals(state), [B1, B2])

    def test_renewal_during_a_fault_keeps_the_fault(self):
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls)
        runtime.start_cognition(1, wall=wall())
        runtime.begin_fault(wall=wall())
        _schedule(scheduler, "x", sim(700))
        runtime.advance(2041)
        self.assertEqual(_renewals(state), [B1, B2])
        self.assertIn(C.FAULT, state.cognition.current.causes)
        self.assertEqual(calls.n, 0)
        self.assertIs(_records(state)["x"].outcome, AgencyOutcome.REJECTED_UNAVAILABLE)
        status = runtime.cognition_status(wall(2041))
        self.assertFalse(status["available"])
        self.assertIsNotNone(status["renews_at"])
        runtime.clear_fault(wall=wall(2041))
        self.assertTrue(state.cognition.current.available)
        _reload(state)


# ── 存档篡改（用例 6）───────────────────────────────────────────────────
def _drop(payload, index):
    """删掉一个区间，后面沿用它额度起点的区间改回沿用前一个（自洽地删）。"""
    intervals = payload["cognition"]["intervals"]
    del intervals[index]
    previous = intervals[index - 1]
    for interval in intervals[index:]:
        if interval["opened_by"] in ("started", "restored", "allowance_renewed"):
            break
        interval["allowance_since_log"] = previous["allowance_since_log"]
        if "allowance_from_sim" in previous:
            interval["allowance_from_sim"] = previous["allowance_from_sim"]
    for position, interval in enumerate(intervals):
        interval["index"] = position


def _forge(payload):
    """把每条 Agency 记录写的区间序号改成篡改后时间线定位出的那个。

    单独测某一条规则时用：否则"记录写明自己所在区间"那道已有检查会先拦下。
    """
    try:
        timeline = CognitionTimeline.from_dict(payload["cognition"])
    except CognitionTimelineError:
        return  # 重放就不成立：交给加载器去拒绝
    for position, record in enumerate(payload["agency"]["log"]["records"]):
        interval = timeline.locate(position)
        if interval is None:
            continue
        key = "interval" if record["outcome"] == "rejected_unavailable" else "cognition_interval"
        record["detail"][key] = interval.index


class TamperTests(unittest.TestCase):
    def setUp(self):
        # 0 opened, 1 started(19:00, N=3), 2 renewed(05:00)；
        # 记录 a 在 19:10 触发（位置 0），b 在 05:20 触发（位置 1）；时钟 05:30。
        # N=3：挪刀之后两条落进同一份额度也不会用完，拦下它的只能是续额规则。
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls)
        runtime.start_cognition(3, wall=wall())
        _schedule(scheduler, "a", sim(10))
        runtime.advance(601)
        _schedule(scheduler, "b", sim(620))
        runtime.advance(29)
        self.assertEqual(_kinds(state), [K.OPENED, K.STARTED, K.ALLOWANCE_RENEWED])
        self.assertEqual(state.cognition.intervals[2].from_log, 1)
        self.payload = state.to_dict()
        SessionState.from_dict(self.payload)

    def _rejects(self, change, message=None):
        payload = copy.deepcopy(self.payload)
        change(payload)
        _forge(payload)
        with self.assertRaises(SessionStateError) as caught:
            SessionState.from_dict(payload)
        if message is not None:
            self.assertIn(message, str(caught.exception))

    def _intervals(self, payload):
        return payload["cognition"]["intervals"]

    # 重放直接拒绝的单字段篡改
    def test_replay_rejects_a_moved_renewal_minute(self):
        def change(p):
            self._intervals(p)[2]["opened_at_sim"] = sim(601).isoformat()
            self._intervals(p)[2]["allowance_from_sim"] = sim(601).isoformat()

        self._rejects(change, "认知时间线不合法")

    def test_replay_rejects_a_bigger_renewed_allowance(self):
        self._rejects(lambda p: self._intervals(p)[2].update(run_allowance=5), "认知时间线不合法")

    def test_replay_rejects_a_renewal_after_a_stop(self):
        def change(p):
            self._intervals(p)[1]["causes"] = ["operator_paused"]
            self._intervals(p)[1]["opened_by"] = "stopped"

        self._rejects(change, "认知时间线不合法")

    def test_replay_rejects_the_same_boundary_twice(self):
        def change(p):
            twice = copy.deepcopy(self._intervals(p)[2])
            twice["index"] = 3
            self._intervals(p).append(twice)

        self._rejects(change, "认知时间线不合法")

    def test_replay_rejects_an_unknown_policy_on_start(self):
        def change(p):
            for interval in self._intervals(p)[1:]:
                interval["renewal"] = "world-day-0400"

        self._rejects(change, "认知时间线不合法")

    def test_replay_rejects_a_renewal_in_a_non_renewing_generation(self):
        def change(p):
            for interval in self._intervals(p)[1:]:
                interval.pop("renewal")
                interval.pop("allowance_from_sim")

        self._rejects(change, "认知时间线不合法")

    def test_replay_rejects_a_missing_middle_renewal(self):
        # 跨两天的存档里删掉第一次续额：第二次的 sim 就不是下一个边界了。
        calls = _Calls()
        state, _, runtime = _new_world(calls)
        runtime.start_cognition(2, wall=wall())
        runtime.advance(2041)
        payload = state.to_dict()
        _drop(payload, 2)
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(payload)

    # (a)–(d)
    def test_a_dropped_renewal_with_a_record_after_it_is_rule_a(self):
        self._rejects(lambda p: _drop(p, 2), "越过了认知区间")

    def test_a_cut_moved_early_is_rule_b(self):
        def change(p):
            self._intervals(p)[2]["from_log"] = 0
            self._intervals(p)[2]["allowance_since_log"] = 0

        self._rejects(change, "续额之前的那一天")

    def test_a_cut_moved_late_is_rule_a(self):
        def change(p):
            self._intervals(p)[2]["from_log"] = 2
            self._intervals(p)[2]["allowance_since_log"] = 2

        self._rejects(change, "越过了认知区间")

    def test_a_clock_transition_past_a_missing_renewal_is_rule_c(self):
        calls = _Calls()
        state, _, runtime = _new_world(calls)
        runtime.start_cognition(2, wall=wall())
        runtime.advance(630)
        runtime.stop_cognition(wall=wall(630))
        payload = state.to_dict()
        _drop(payload, 2)
        _forge(payload)
        with self.assertRaises(SessionStateError) as caught:
            SessionState.from_dict(payload)
        self.assertIn("按时钟转换，越过了续额边界", str(caught.exception))

    def test_a_future_renewal_is_rule_d(self):
        def change(p):
            future = copy.deepcopy(self._intervals(p)[2])
            future.update(
                index=3,
                from_log=2,
                allowance_since_log=2,
                opened_at_sim=B2.isoformat(),
                allowance_from_sim=B2.isoformat(),
            )
            self._intervals(p).append(future)

        self._rejects(change, "续额晚于世界时钟")

    def test_a_missing_tail_renewal_is_rule_d(self):
        calls = _Calls()
        state, _, runtime = _new_world(calls)
        runtime.start_cognition(2, wall=wall())
        runtime.advance(630)
        payload = state.to_dict()
        _drop(payload, 2)
        _forge(payload)
        with self.assertRaises(SessionStateError) as caught:
            SessionState.from_dict(payload)
        self.assertIn("时间线上却没有那次续额", str(caught.exception))

    def test_an_inherited_day_start_still_obeys_rule_b(self):
        # 续额之后故障开始又解除：最后一个区间沿用续额的起点，(b) 照样要查，
        # 不能只看 opened_by 是不是续额。走运行时造不出"只有 (b) 能拦"的存档
        # （故障解除的 backlog 会先把早于它的记录判成不可用），所以直接喂规则。
        timeline = (
            CognitionTimeline.open(log_length=0, sim=SIM, wall="w")
            .started(log_length=0, sim=SIM, wall="w", run_allowance=3, renewal=POLICY)
            .allowance_renewed(log_length=0, sim=B1, wall="w")
            .fault_began(log_length=0, sim=B1, wall="w")
            .fault_cleared(log_length=0, sim=B1, wall="w")
        )
        self.assertIs(timeline.current.opened_by, K.FAULT_CLEARED)
        self.assertEqual(timeline.current.allowance_from_sim, B1)
        early = _Due("a", sim(10))
        with self.assertRaises(SessionStateError) as caught:
            _validate_renewals(timeline, [early], _Outbox([early]), sim(630))
        self.assertIn("续额之前的那一天", str(caught.exception))
        late = _Due("b", sim(620))
        _validate_renewals(timeline, [late], _Outbox([late]), sim(630))

    # 明确不保证的那一类（设计 §6）：断言它们**被接受**，免得以后有人以为覆盖了。
    def test_an_unreferenced_renewal_between_anchor_transitions_can_be_dropped(self):
        calls = _Calls()
        state, _, runtime = _new_world(calls)
        runtime.start_cognition(2, wall=wall())
        runtime.advance(601)
        runtime.start_cognition(2, wall=wall(601))
        payload = state.to_dict()
        self.assertEqual(
            [interval["opened_by"] for interval in payload["cognition"]["intervals"]],
            ["opened", "started", "allowance_renewed", "started"],
        )
        _drop(payload, 2)
        SessionState.from_dict(payload)


# ── 旧存档前向安装（用例 7）─────────────────────────────────────────────
class ForwardInstallTests(unittest.TestCase):
    def test_an_old_archive_renews_from_the_next_start(self):
        calls = _Calls()
        state, _, runtime = _new_world(calls, renewal=None)
        runtime.start_cognition(2, wall=wall())
        runtime.advance(10)
        restored, _, runtime = _restore(state, calls, at=wall(10))
        runtime.advance(700)
        self.assertEqual(_renewals(restored), [])
        runtime.start_cognition(2, wall=wall(710))
        self.assertEqual(restored.cognition.current.renewal, POLICY)
        runtime.advance(1440)
        self.assertEqual(_renewals(restored), [B2])
        _reload(restored)

    def test_a_non_renewing_world_never_renews(self):
        calls = _Calls()
        state, _, runtime = _new_world(calls, renewal=None)
        runtime.start_cognition(2, wall=wall())
        runtime.advance(2041)
        self.assertEqual(_renewals(state), [])
        self.assertIsNone(runtime.cognition_status(wall(2041))["renews_at"])


# ── 续额步回滚（用例 8）─────────────────────────────────────────────────
class RollbackTests(unittest.TestCase):
    def _at_boundary(self):
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls)
        runtime.start_cognition(2, wall=wall())
        runtime.advance(600)
        self.assertEqual(state.world_state.clock, B1)
        return state, scheduler, runtime

    def test_a_failing_renewal_step_rolls_everything_back(self):
        state, _, runtime = self._at_boundary()
        before = state.to_dict()
        real = runtime._apply_rhythm_locked
        failures = iter([True])

        def flaky():
            if next(failures, False):
                raise RuntimeError("boom")
            return real()

        runtime._apply_rhythm_locked = flaky
        with self.assertRaises(RuntimeError):
            runtime.advance(1)
        self.assertEqual(state.to_dict(), before)
        runtime.advance(1)
        self.assertEqual(_renewals(state), [B1])
        _reload(state)

    def test_a_not_quiet_rewalk_leaves_one_renewal(self):
        state, _, runtime = self._at_boundary()
        runtime.set_quiet_time_events(False, wall=wall(600))
        real = runtime._apply_rhythm_locked
        noisy = iter([True])

        def once_noisy():
            if next(noisy, False):
                # 按安静的一步走，作息却交出了事件：整步回滚，按记录模式重走。
                return ({"event_id": "fake"},)
            return real()

        runtime._apply_rhythm_locked = once_noisy
        runtime.advance(1)
        self.assertEqual(_renewals(state), [B1])
        self.assertEqual(state.world_state.clock, sim(601))
        _reload(state)


# ── 调度器旁路（用例 9）─────────────────────────────────────────────────
class SchedulerBypassTests(unittest.TestCase):
    def test_a_renewing_authority_owns_the_clock(self):
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls)
        # 还没 Start：没有续额授权，研究式的直接推进不受影响。
        scheduler.advance_by(1)
        runtime.start_cognition(2, wall=wall(1))
        for push in (lambda: scheduler.advance_by(1000), lambda: scheduler.advance_to(B2)):
            with self.assertRaises(SchedulerError):
                push()
        self.assertEqual(state.world_state.clock, sim(1))

    def test_a_non_renewing_start_keeps_the_old_behaviour(self):
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls, renewal=None)
        runtime.start_cognition(2, wall=wall())
        scheduler.advance_by(5)
        self.assertEqual(state.world_state.clock, sim(5))


# ── 状态（§7）────────────────────────────────────────────────────────────
class StatusTests(unittest.TestCase):
    def test_status_reports_the_renewal_only_while_it_will_happen(self):
        calls = _Calls()
        state, _, runtime = _new_world(calls)
        status = runtime.cognition_status(wall())
        self.assertIsNone(status["renews_at"])
        runtime.start_cognition(2, wall=wall())
        status = runtime.cognition_status(wall())
        self.assertEqual(status["renewal"], POLICY)
        self.assertEqual(status["renews_at"], B1.isoformat())
        self.assertEqual(status["day_start"], DAY0.isoformat())
        runtime.advance(601)
        status = runtime.cognition_status(wall(601))
        self.assertEqual(status["renews_at"], B2.isoformat())
        self.assertEqual(status["day_start"], B1.isoformat())
        runtime.stop_cognition(wall=wall(601))
        status = runtime.cognition_status(wall(601))
        self.assertIsNone(status["renews_at"])
        self.assertEqual(status["renewal"], POLICY)


if __name__ == "__main__":
    unittest.main()
