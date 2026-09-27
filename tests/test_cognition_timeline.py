# tests/test_cognition_timeline.py — 认知时间线（WORLD-1 设计 §13–§14）。
#
# 守的线（对应 R4 §7 的反例清单）：
#   1. 判定是并集：当前区间原因 ∪ 所有覆盖该到期资格的 backlog 原因；
#   2. backlog 规范形：同一 cutoff 合并，cutoff 不倒退，条目非空；
#   3. 区间按 Agency 日志位置定位，取最右边界，零宽区间合法；
#   4. 转换规则：Start 清不掉故障、现实时钟落后、世界上限；恢复记 process_stopped；
#   5. 存档往返与篡改拒绝。
#
# 运行: python -m unittest discover -s tests -p test_cognition_timeline.py
import unittest
from datetime import datetime

from pns.models.cognition import (
    BacklogItem,
    CognitionCause as C,
    CognitionTimeline,
    CognitionTimelineError,
    TransitionKind,
    next_minute_after,
    unavailable_causes,
)

WALL = "2026-09-27T03:00:00+00:00"


def t(hour, minute=0):
    return datetime(2026, 9, 27, hour, minute)


def _open(log_length=0, sim=t(9)):
    return CognitionTimeline.open(log_length=log_length, sim=sim, wall=WALL)


def _restore(timeline, *, log_length, now):
    return timeline.restored(
        log_length=log_length,
        sim=now.replace(second=0, microsecond=0),
        wall=WALL,
        backlog_until=next_minute_after(now),
    )


def _start(timeline, *, log_length, now, allowance=None):
    return timeline.started(
        log_length=log_length,
        sim=now.replace(second=0, microsecond=0),
        wall=WALL,
        backlog_until=next_minute_after(now),
        run_allowance=allowance,
    )


class UnionJudgementTests(unittest.TestCase):
    def test_a_new_world_is_not_started(self):
        timeline = _open()
        self.assertEqual(unavailable_causes(timeline.current, t(9)), {C.NOT_STARTED})

    def test_restored_pending_due_keeps_process_stopped_while_not_started(self):
        # R4-1：恢复后的 not_started 不许把 process_stopped 盖掉。
        timeline = _open()
        timeline = _start(timeline, log_length=0, now=t(9))
        # 09:45 的 due 待决；09:50 checkpoint；12:00:20 恢复。
        timeline = _restore(timeline, log_length=3, now=datetime(2026, 9, 27, 12, 0, 20))
        self.assertEqual(
            unavailable_causes(timeline.current, t(9, 45)),
            {C.NOT_STARTED, C.PROCESS_STOPPED},
        )
        # 恢复之后才触发的，只有 not_started。
        self.assertEqual(
            unavailable_causes(timeline.current, t(12, 1)), {C.NOT_STARTED}
        )

    def test_restore_and_start_in_the_same_minute_merge_into_one_cutoff(self):
        # R4-2 反例 A。
        timeline = _open()
        timeline = _restore(timeline, log_length=0, now=datetime(2026, 9, 27, 12, 0, 10))
        timeline = _start(timeline, log_length=0, now=datetime(2026, 9, 27, 12, 0, 40))
        backlog = timeline.current.backlog
        self.assertEqual(len(backlog), 1)
        self.assertEqual(backlog[0].until_sim, t(12, 1))
        self.assertEqual(
            unavailable_causes(timeline.current, t(11)),
            {C.NOT_STARTED, C.PROCESS_STOPPED},
        )
        self.assertEqual(unavailable_causes(timeline.current, t(12, 1)), frozenset())

    def test_fault_start_clear_interleave_keeps_every_cause(self):
        # R4-2 反例 B：恢复补跑中故障 → Start → 故障解除，旧 due 命中全部原因。
        timeline = _open()
        timeline = _restore(timeline, log_length=0, now=t(12))
        timeline = timeline.fault_began(log_length=0, sim=t(12, 5), wall=WALL)
        timeline = _start(timeline, log_length=0, now=t(12, 10))
        self.assertEqual(timeline.current.causes, {C.FAULT})
        timeline = timeline.fault_cleared(
            log_length=0, sim=t(12, 20), wall=WALL, backlog_until=t(12, 21)
        )
        self.assertTrue(timeline.current.available)
        self.assertEqual(
            unavailable_causes(timeline.current, t(11)),
            {C.NOT_STARTED, C.PROCESS_STOPPED, C.FAULT},
        )
        self.assertEqual(
            unavailable_causes(timeline.current, t(12, 15)), {C.FAULT}
        )
        self.assertEqual(unavailable_causes(timeline.current, t(12, 21)), frozenset())

    def test_a_remaining_cause_does_not_hide_the_backlog(self):
        # R4 §2：故障解除后仍有世界上限，backlog 里的 fault 不能被遮住。
        timeline = _open()
        timeline = _start(timeline, log_length=0, now=t(9))
        timeline = timeline.world_action_cap(log_length=5, sim=t(9, 30), wall=WALL)
        timeline = timeline.fault_began(log_length=5, sim=t(10), wall=WALL)
        timeline = timeline.fault_cleared(
            log_length=5, sim=t(11), wall=WALL, backlog_until=t(11, 1)
        )
        self.assertEqual(
            unavailable_causes(timeline.current, t(10, 30)),
            {C.WORLD_ACTION_CAP, C.FAULT},
        )

    def test_the_cutoff_minute_itself_still_carries_the_cause(self):
        # 故障在 12:00:00 解除 → cutoff 12:01；fired_at == 12:00 仍带 fault。
        timeline = _start(_open(), log_length=0, now=t(9))
        timeline = timeline.fault_began(log_length=0, sim=t(10), wall=WALL)
        timeline = timeline.fault_cleared(
            log_length=0, sim=t(12), wall=WALL, backlog_until=next_minute_after(t(12))
        )
        self.assertEqual(unavailable_causes(timeline.current, t(12)), {C.FAULT})
        self.assertEqual(unavailable_causes(timeline.current, t(12, 1)), frozenset())


class TransitionRuleTests(unittest.TestCase):
    def test_start_clears_only_operator_causes(self):
        timeline = _start(_open(), log_length=0, now=t(9))
        for add in (
            lambda tl: tl.world_action_cap(log_length=1, sim=t(9, 5), wall=WALL),
            lambda tl: tl.fault_began(log_length=1, sim=t(9, 5), wall=WALL),
            lambda tl: tl.wall_clock_behind(log_length=1, sim=t(9, 5), wall=WALL),
        ):
            with self.subTest(add=add):
                blocked = add(timeline)
                blocked = blocked.stopped(log_length=1, sim=t(9, 6), wall=WALL)
                restarted = _start(blocked, log_length=1, now=t(9, 7))
                self.assertFalse(restarted.current.available)
                self.assertNotIn(C.OPERATOR_PAUSED, restarted.current.causes)

    def test_restore_keeps_the_world_cap_and_drops_process_local_causes(self):
        timeline = _start(_open(), log_length=0, now=t(9))
        timeline = timeline.world_action_cap(log_length=4, sim=t(9, 5), wall=WALL)
        timeline = timeline.fault_began(log_length=4, sim=t(9, 6), wall=WALL)
        timeline = _restore(timeline, log_length=4, now=t(12))
        self.assertEqual(
            timeline.current.causes, {C.NOT_STARTED, C.WORLD_ACTION_CAP}
        )
        self.assertIsNone(timeline.current.run_allowance)

    def test_start_sets_the_run_allowance(self):
        timeline = _start(_open(), log_length=0, now=t(9), allowance=3)
        self.assertEqual(timeline.current.run_allowance, 3)
        timeline = timeline.stopped(log_length=2, sim=t(9, 5), wall=WALL)
        self.assertEqual(timeline.current.run_allowance, 3)

    def test_a_transition_cannot_move_backwards_in_the_log(self):
        timeline = _start(_open(log_length=5), log_length=5, now=t(9))
        with self.assertRaises(CognitionTimelineError):
            timeline.stopped(log_length=4, sim=t(9, 5), wall=WALL)

    def test_a_backlog_cutoff_cannot_move_backwards(self):
        timeline = _restore(_open(), log_length=0, now=t(12))
        with self.assertRaises(CognitionTimelineError):
            timeline.fault_cleared(
                log_length=0, sim=t(12), wall=WALL, backlog_until=t(11)
            )

    def test_timelines_are_immutable_values(self):
        timeline = _open()
        started = _start(timeline, log_length=0, now=t(9))
        self.assertEqual(len(timeline.intervals), 1)
        self.assertEqual(len(started.intervals), 2)
        with self.assertRaises(Exception):
            timeline.intervals = ()


class LocatorTests(unittest.TestCase):
    def test_the_rightmost_zero_width_boundary_wins(self):
        # R4-6：Stop、Start 之间没有记录，产生零宽区间；位置 10 属于最后一个。
        timeline = _start(_open(log_length=10), log_length=10, now=t(9))
        timeline = timeline.stopped(log_length=10, sim=t(9, 1), wall=WALL)
        timeline = _start(timeline, log_length=10, now=t(9, 2))
        self.assertEqual(timeline.locate(10).index, 3)
        self.assertEqual(timeline.locate(99).index, 3)

    def test_positions_before_the_timeline_have_no_interval(self):
        timeline = _open(log_length=10)
        self.assertIsNone(timeline.locate(9))
        self.assertEqual(timeline.locate(10).index, 0)

    def test_positions_fall_into_their_own_interval(self):
        timeline = _start(_open(), log_length=0, now=t(9))
        timeline = timeline.run_budget_exhausted(log_length=41, sim=t(12), wall=WALL)
        self.assertEqual(timeline.locate(40).index, 1)
        self.assertTrue(timeline.locate(40).available)
        self.assertEqual(timeline.locate(41).causes, {C.RUN_BUDGET_EXHAUSTED})


class ArchiveTests(unittest.TestCase):
    def _busy(self):
        timeline = _restore(_open(), log_length=0, now=datetime(2026, 9, 27, 12, 0, 10))
        timeline = _start(timeline, log_length=0, now=datetime(2026, 9, 27, 12, 0, 40), allowance=5)
        timeline = timeline.fault_began(log_length=3, sim=t(12, 5), wall=WALL)
        return timeline.fault_cleared(
            log_length=4, sim=t(12, 9), wall=WALL, backlog_until=t(12, 10)
        )

    def test_it_round_trips(self):
        timeline = self._busy()
        self.assertEqual(CognitionTimeline.from_dict(timeline.to_dict()), timeline)

    def test_tampering_is_refused(self):
        def tamper(mutate):
            payload = self._busy().to_dict()
            mutate(payload)
            return payload

        cases = {
            "index gap": lambda p: p["intervals"][1].update(index=5),
            "log goes back": lambda p: p["intervals"][4].update(from_log=2),
            "sim goes back": lambda p: p["intervals"][3].update(
                opened_at_sim="2026-09-27T08:00:00"
            ),
            "unknown cause": lambda p: p["intervals"][0].update(causes=["bored"]),
            "unknown transition": lambda p: p["intervals"][0].update(opened_by="magic"),
            "empty backlog causes": lambda p: p["intervals"][-1]["backlog"][0].update(
                causes=[]
            ),
            "duplicate cutoff": lambda p: p["intervals"][-1]["backlog"].append(
                dict(p["intervals"][-1]["backlog"][-1])
            ),
            "backlog rewritten later": lambda p: p["intervals"][-1]["backlog"][0].update(
                causes=["fault"]
            ),
            "seconds in a cutoff": lambda p: p["intervals"][-1]["backlog"][0].update(
                until_sim="2026-09-27T12:01:30"
            ),
            "negative allowance": lambda p: p["intervals"][2].update(run_allowance=-1),
            "empty timeline": lambda p: p.update(intervals=[]),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                with self.assertRaises(CognitionTimelineError):
                    CognitionTimeline.from_dict(tamper(mutate))

    def test_backlog_items_validate_themselves(self):
        with self.assertRaises(CognitionTimelineError):
            BacklogItem(t(12), frozenset())
        with self.assertRaises(CognitionTimelineError):
            BacklogItem(datetime(2026, 9, 27, 12, 0, 5), {C.FAULT})

    def test_transition_kinds_are_a_closed_set(self):
        self.assertEqual(
            {kind.value for kind in TransitionKind},
            {
                "opened",
                "restored",
                "started",
                "stopped",
                "run_budget_exhausted",
                "world_action_cap",
                "fault_began",
                "fault_cleared",
                "wall_clock_behind",
                "wall_clock_caught_up",
            },
        )


if __name__ == "__main__":
    unittest.main()
