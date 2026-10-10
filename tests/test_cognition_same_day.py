# tests/test_cognition_same_day.py — 同一世界日里再 Start 不重新装满（TODO 43）。
#
# 起因：2026-10-10 生产发版重启，当天已用 408 次，restore → Start 之后从 0 重算，
# 等于每重启一次白送一份 N。COG-1 v2.1 原本有意让"Stop 后再 Start 重新装满"，
# 跟"每个世界日最多 N 次"相冲突，按后者改。
#
# 守的线：
#   1. 同一世界日 Stop → Start、restore → Start，都只装 N − 今天已用；
#   2. N 不比已用的多就拒绝（AllowanceSpentToday），时间线不动；N 改大照新 N 算；
#   3. 离开 05:00 之后是新的一天，从 0 算；
#   4. 续额续到 N，不是续到当天剩下的那一截；
#   5. 状态里的 limit 是 N、used 是今天一共用的；
#   6. 没有 day_allowance 的旧存档照常加载（包括旧规则下同日重装满的 Start）；
#   7. 篡改：少记已用（改大 run_allowance）、day_allowance 小于 run_allowance 被拒；
#   8. HTTP 把它翻成 409 allowance_spent_today。
#
# 运行: python -m unittest discover -s tests -p test_cognition_same_day.py
import copy
import sys
import unittest
from pathlib import Path

from test_clock_runtime import _Calls, _records, _schedule, sim, wall
from test_cognition_renewal import B1, DAY0, POLICY, _new_world, _reload, _restore

from pns.models.agency import AgencyOutcome
from dataclasses import replace

from pns.models.cognition import (
    CognitionCause as C,
    CognitionTimeline,
    TransitionKind as K,
    allowance_used_on_day,
)
from pns.models.session import SessionState, SessionStateError
from pns.runtime.autonomy.coordinator import AllowanceSpentToday

N = 3

# pns.interfaces.config 从 scripts/oobe.py 取 provider 表（同 test_persistent_world_api）。
REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def _spend_two():
    """开局 Start(N=3)，19:10、19:11 各用一次，时钟停在 19:12。"""
    calls = _Calls()
    state, scheduler, runtime = _new_world(calls)
    runtime.start_cognition(N, wall=wall())
    _schedule(scheduler, "a", sim(10))
    _schedule(scheduler, "b", sim(11))
    runtime.advance(12)
    assert calls.n == 2
    return calls, state, scheduler, runtime


def _started(state):
    return [i for i in state.cognition.intervals if i.opened_by is K.STARTED]


class SameDayStartTests(unittest.TestCase):
    def test_stop_then_start_keeps_what_the_day_used(self):
        calls, state, scheduler, runtime = _spend_two()
        runtime.stop_cognition(wall=wall(12))
        runtime.start_cognition(N, wall=wall(13))
        current = state.cognition.current
        self.assertEqual((current.run_allowance, current.day_allowance), (1, N))
        _schedule(scheduler, "c", sim(20))
        _schedule(scheduler, "d", sim(21))
        runtime.advance(10)
        self.assertEqual(calls.n, 3)
        self.assertIs(_records(state)["d"].outcome, AgencyOutcome.REJECTED_UNAVAILABLE)
        self.assertIn(C.RUN_BUDGET_EXHAUSTED, state.cognition.current.causes)
        _reload(state)

    def test_restore_then_start_keeps_what_the_day_used(self):
        calls, state, _, _ = _spend_two()
        restored, scheduler, runtime = _restore(state, calls, at=wall(30))
        runtime.start_cognition(N, wall=wall(31))
        current = restored.cognition.current
        self.assertEqual((current.run_allowance, current.day_allowance), (1, N))
        _reload(restored)

    def test_a_spent_day_refuses_another_start(self):
        _, state, _, runtime = _spend_two()
        runtime.stop_cognition(wall=wall(12))
        before = state.cognition.to_dict()
        for allowance in (1, 2):
            with self.assertRaises(AllowanceSpentToday) as caught:
                runtime.start_cognition(allowance, wall=wall(13))
            self.assertEqual(caught.exception.used, 2)
        self.assertEqual(state.cognition.to_dict(), before)
        self.assertIn(C.OPERATOR_PAUSED, state.cognition.current.causes)

    def test_an_exhausted_day_refuses_the_same_n_after_restore(self):
        # 生产上的那个口子本身：用完 → 重启 → 原样 Start。
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls)
        runtime.start_cognition(1, wall=wall())
        _schedule(scheduler, "a", sim(10))
        runtime.advance(11)
        self.assertIn(C.RUN_BUDGET_EXHAUSTED, state.cognition.current.causes)
        restored, _, runtime = _restore(state, calls, at=wall(20))
        with self.assertRaises(AllowanceSpentToday):
            runtime.start_cognition(1, wall=wall(21))

    def test_a_larger_n_on_the_same_day_counts_from_the_new_n(self):
        _, state, _, runtime = _spend_two()
        runtime.stop_cognition(wall=wall(12))
        runtime.start_cognition(5, wall=wall(13))
        current = state.cognition.current
        self.assertEqual((current.run_allowance, current.day_allowance), (3, 5))

    def test_a_later_start_counts_every_earlier_start_of_the_day(self):
        calls, state, scheduler, runtime = _spend_two()
        runtime.stop_cognition(wall=wall(12))
        runtime.start_cognition(5, wall=wall(13))  # 剩 3
        _schedule(scheduler, "c", sim(20))
        runtime.advance(10)
        runtime.stop_cognition(wall=wall(22))
        runtime.start_cognition(5, wall=wall(23))  # 今天已用 3，剩 2
        self.assertEqual(state.cognition.current.run_allowance, 2)
        _reload(state)

    def test_a_new_world_day_starts_from_zero(self):
        _, state, _, runtime = _spend_two()
        runtime.stop_cognition(wall=wall(12))
        runtime.advance(600)  # 停着越过 05:00：不续额
        self.assertGreater(runtime.clock, B1)
        runtime.start_cognition(N, wall=wall(613))
        current = state.cognition.current
        self.assertEqual((current.run_allowance, current.day_allowance), (N, N))
        _reload(state)

    def test_the_boundary_renews_to_n_not_to_the_remainder(self):
        _, state, _, runtime = _spend_two()
        runtime.stop_cognition(wall=wall(12))
        runtime.start_cognition(N, wall=wall(13))
        self.assertEqual(state.cognition.current.run_allowance, 1)
        runtime.advance(600)
        renewed = [i for i in state.cognition.intervals if i.opened_by is K.ALLOWANCE_RENEWED]
        self.assertEqual(len(renewed), 1)
        self.assertEqual(renewed[0].run_allowance, N)
        _reload(state)

    def test_status_reports_the_day_limit_and_the_day_usage(self):
        _, state, _, runtime = _spend_two()
        runtime.stop_cognition(wall=wall(12))
        runtime.start_cognition(N, wall=wall(13))
        status = runtime.cognition_status(wall(13))
        self.assertEqual(
            (status["day_allowance"], status["run_allowance"], status["run_remaining"]),
            (N, 1, 1),
        )


class OldDayDuesTests(unittest.TestCase):
    """ena 复审 P2 的两个反例：直接再按 Start（不 Stop）时，旧日待处理的到期不能花新日的额度。"""

    def _old_day_spent_one(self):
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls)
        runtime.start_cognition(2, wall=wall())
        _schedule(scheduler, "a", sim(10))
        runtime.advance(11)
        self.assertEqual(calls.n, 1)
        return calls, state, scheduler, runtime

    def test_a_restart_parked_on_the_boundary_pays_from_the_old_day(self):
        calls, state, scheduler, runtime = self._old_day_spent_one()
        _schedule(scheduler, "b1", B1)
        _schedule(scheduler, "b2", B1, "ena")
        runtime.advance(590, max_results=0)  # 停在 05:00，两条整点到期待处理
        self.assertEqual(state.world_state.clock, B1)
        runtime.start_cognition(2, wall=wall(600))  # 不 Stop，直接再按
        self.assertEqual(state.cognition.current.run_allowance, 1)
        runtime.advance(1)
        self.assertEqual(calls.n, 2, "旧日合计不能超过 N=2")
        renewed = [i for i in state.cognition.intervals if i.opened_by is K.ALLOWANCE_RENEWED]
        self.assertEqual([i.opened_at_sim for i in renewed], [B1])
        self.assertEqual(renewed[0].run_allowance, 2)
        _reload(state)

    def test_a_restart_during_catch_up_pays_from_the_clock_day(self):
        calls, state, scheduler, runtime = self._old_day_spent_one()
        _schedule(scheduler, "late1", sim(590))  # 04:50
        _schedule(scheduler, "late2", B1, "ena")  # 05:00，仍是旧日
        # 时钟还在前一天 19:11，锚点已经到次日 06:00：直接再按 Start。
        runtime.start_cognition(2, wall=wall(660))
        self.assertEqual(state.world_state.clock, sim(11))
        self.assertEqual(state.cognition.current.run_allowance, 1)
        runtime.advance_to_anchor(wall(661))
        self.assertEqual(calls.n, 2, "旧日合计不能超过 N=2")
        _reload(state)


def _make_old(payload, *, refill_same_day):
    """把存档改成 day_allowance 出现之前的样子。

    refill_same_day：同日第二次 Start 按旧规则装满 N（这正是生产存档里已有的）。
    """
    intervals = payload["cognition"]["intervals"]
    starts = 0
    day_start = None
    for interval in intervals:
        if interval["opened_by"] == "started":
            starts += 1
            # 旧规则下 Start 的额度起点是按下的那一分钟。
            day_start = interval["opened_at_sim"]
        elif interval["opened_by"] in ("restored", "allowance_renewed"):
            day_start = None
        if day_start is not None and "allowance_from_sim" in interval:
            interval["allowance_from_sim"] = day_start
        if refill_same_day and starts >= 2 and "day_allowance" in interval:
            interval["run_allowance"] = interval["day_allowance"]
        interval.pop("day_allowance", None)


class CompatibilityAndTamperTests(unittest.TestCase):
    def _same_day_restart(self):
        _, state, _, runtime = _spend_two()
        runtime.stop_cognition(wall=wall(12))
        runtime.start_cognition(N, wall=wall(13))
        return state.to_dict()

    def test_an_old_save_without_the_field_still_loads(self):
        payload = self._same_day_restart()
        _make_old(payload, refill_same_day=False)
        # 第二次 Start 的 run_allowance 是 1、又没写每日上限：旧规则下就是"Start(1)"。
        SessionState.from_dict(payload)

    def test_an_old_save_that_refilled_on_the_same_day_still_loads(self):
        payload = self._same_day_restart()
        _make_old(payload, refill_same_day=True)
        state = SessionState.from_dict(payload)
        self.assertEqual(_started(state)[-1].run_allowance, N)
        self.assertIsNone(_started(state)[-1].day_allowance)

    def test_a_start_that_forgets_the_day_usage_is_rejected(self):
        payload = self._same_day_restart()
        payload["cognition"]["intervals"][-1]["run_allowance"] = N
        with self.assertRaises(SessionStateError) as caught:
            SessionState.from_dict(payload)
        self.assertIn("这一天之前已用 2 次", str(caught.exception))

    def test_dropping_the_field_after_it_appeared_is_rejected(self):
        # ena 复审 P3：删掉新 Start 的每日上限、把额度改回整份，想退回旧规则。
        payload = self._same_day_restart()
        last = payload["cognition"]["intervals"][-1]
        del last["day_allowance"]
        last["run_allowance"] = N
        last["allowance_from_sim"] = last["opened_at_sim"]
        with self.assertRaises(SessionStateError) as caught:
            SessionState.from_dict(payload)
        self.assertIn("没写每日上限", str(caught.exception))

    def test_a_start_moved_back_a_day_cannot_renew_the_same_boundary_twice(self):
        # ena 复审 P2（第二轮）：05:00 已续额、05:10 用过一次，05:12 伪造一次 Start 把
        # 额度记回前一天，再接一次 05:00 续额——同一天就多出一整份。
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls)
        runtime.start_cognition(N, wall=wall())
        runtime.advance(605)  # 越过 05:00，续额
        _schedule(scheduler, "a", sim(610))
        runtime.advance(7)  # 05:12
        self.assertEqual(calls.n, 1)
        payload = state.to_dict()
        timeline = CognitionTimeline.from_dict(payload["cognition"])
        forged = timeline.started(
            log_length=len(state.agency),
            sim=sim(612),
            wall=wall(612).isoformat(),
            run_allowance=N,
            renewal=POLICY,
            day_allowance=N,
            day_start=DAY0,
        ).allowance_renewed(log_length=len(state.agency), sim=B1, wall=wall(612).isoformat())
        payload["cognition"] = forged.to_dict()
        with self.assertRaises(SessionStateError) as caught:
            SessionState.from_dict(payload)
        self.assertIn("早于此前已提交", str(caught.exception))

    def test_a_later_start_forged_into_a_non_renewing_shape_is_rejected(self):
        # ena 复审 P3（第二轮）：只把后面一次 Start 改成有限、不续额的形状，消耗就不进账。
        _, state, _, _ = _spend_two()
        payload = state.to_dict()
        timeline = CognitionTimeline.from_dict(payload["cognition"])
        forged = timeline.started(
            log_length=len(state.agency), sim=sim(12), wall=wall(12).isoformat(), run_allowance=N
        )
        payload["cognition"] = forged.to_dict()
        with self.assertRaises(SessionStateError) as caught:
            SessionState.from_dict(payload)
        self.assertIn("没写每日上限", str(caught.exception))

    def test_usage_under_any_authorization_counts_for_the_day(self):
        # 不续额形状的授权下用掉的认知也算进这一天（P3 的计账一半）。
        _, state, _, _ = _spend_two()
        records = state.agency.records()
        outbox = state.activation_outbox
        intervals = [
            replace(interval, renewal=None, allowance_from_sim=None, day_allowance=None)
            if interval.opened_by is K.STARTED
            else interval
            for interval in state.cognition.intervals
        ]
        used = allowance_used_on_day(
            intervals,
            records,
            lambda record: outbox.get(record.due_id).fired_at,
            renewal=POLICY,
            end=B1,
        )
        self.assertEqual(used, 2)

    def test_a_day_limit_below_the_allowance_is_rejected(self):
        payload = self._same_day_restart()
        payload["cognition"]["intervals"][-1]["day_allowance"] = 0
        with self.assertRaises((SessionStateError, ValueError)):
            SessionState.from_dict(copy.deepcopy(payload))

    def test_a_day_limit_without_a_renewal_policy_is_rejected(self):
        calls = _Calls()
        state, _, runtime = _new_world(calls, renewal=None)
        runtime.start_cognition(N, wall=wall())
        payload = state.to_dict()
        payload["cognition"]["intervals"][-1]["day_allowance"] = N
        with self.assertRaises((SessionStateError, ValueError)):
            SessionState.from_dict(payload)

    def test_a_non_renewing_world_still_refills_on_every_start(self):
        # 不续额的世界没有"世界日"：每次 Start 一份，行为不变。
        calls = _Calls()
        state, scheduler, runtime = _new_world(calls, renewal=None)
        runtime.start_cognition(1, wall=wall())
        _schedule(scheduler, "a", sim(10))
        runtime.advance(11)
        runtime.stop_cognition(wall=wall(11))
        runtime.start_cognition(1, wall=wall(12))
        current = state.cognition.current
        self.assertEqual((current.run_allowance, current.day_allowance), (1, None))
        self.assertNotIn("day_allowance", state.to_dict()["cognition"]["intervals"][-1])


class HttpTests(unittest.TestCase):
    def test_the_refusal_is_a_409_with_its_own_category(self):
        from pns.interfaces.persistent_worlds import _translate

        error = _translate(AllowanceSpentToday(408, 400), None, "w", "autonomy_start")
        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.detail["category"], "allowance_spent_today")


if __name__ == "__main__":
    unittest.main()
