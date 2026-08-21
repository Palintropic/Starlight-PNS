# tests/test_scheduler.py — P8 持久化调度器的不变量。
#
# 盯住的东西按"错了会怎样"排：
#   1. 队列顺序是确定的，而且不是"碰巧按插入顺序"（同刻到期按登记顺序，
#      不按 ID 字典序，序列化一圈回来仍然一样）
#   2. 时间只能通过 world.time_advanced 事件推进，绝不存在没记进世界历史的
#      时钟变更
#   3. 一次推进是一个事务：时钟、事件、曝光/观察、队列、到期记录同生共死
#   4. 一次性激活至多触发一次，取消语义幂等，周期激活不漂相位、不悄悄少跑
#   5. 存档能原样恢复，损坏的存档响亮地失败
#   6. 调度状态是会话私有的运行时权威状态：会话之间不串，配置重载动不了
#   7. 研究会话的确定性 round robin 一点没变
#
# 运行: python -m unittest tests.test_scheduler -v
import ast
import inspect
import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from pns.models.activation import (
    ActivationDue,
    ActivationError,
    ActivationKind,
    ScheduledActivation,
    new_activation_id,
)
from pns.models.activation_queue import ActivationQueue, ActivationQueueError
from pns.models.event import EventType
from pns.models.event_store import EventStore
from pns.models.exposure import ExposureLog
from pns.models.session import SessionState
from pns.models.world_state import Availability, WorldState
from pns.runtime import scheduler as scheduler_mod
from pns.runtime.scheduler import (
    PersistentScheduler,
    SchedulerError,
    TickResult,
)
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph

CLOCK = datetime(2026, 8, 21, 23, 50)


def _world(clock=CLOCK):
    world = WorldState(
        clock=clock,
        locations=build_default_location_graph(),
        channels=build_default_channel_registry(),
    )
    world.place_character("mizuki", "mizuki_home_room")
    world.place_character("ena", "ena_home_studio")
    return world


def _session(world=None, session_id="s1"):
    world = _world() if world is None else world
    state = SessionState(session_id=session_id, scene="gate", characters=["mizuki", "ena"])
    state.attach_world_state(world)
    state.initialize_runtime("开场")
    return state


def _activation(activation_id="a1", *, minutes_ahead=20, clock=CLOCK, **overrides):
    fields = {
        "activation_id": activation_id,
        "kind": ActivationKind.CHARACTER_ACTIVATION,
        "due_at": clock + timedelta(minutes=minutes_ahead),
        "character_id": "mizuki",
    }
    fields.update(overrides)
    return ScheduledActivation(**fields)


def _scheduler(state=None):
    return PersistentScheduler(state if state is not None else _session())


def _fingerprint(scheduler):
    """调度器 + 会话的全部相关状态。回滚测试拿它做前后比对。"""
    state = scheduler.state
    return {
        "clock": scheduler.clock,
        "events": [event.event_id for event in state.events],
        "observations": len(state.observations),
        "exposures": len(state.exposures),
        "turns": len(state.turns),
        "queue": scheduler.queue.to_dict(),
    }


class ScheduledActivationShapeTests(unittest.TestCase):
    """激活自身的形状校验 —— 脏值不能排进队列。"""

    def test_rejects_missing_or_empty_identity(self):
        for bad in ("", None, 7):
            with self.subTest(activation_id=bad), self.assertRaises(ActivationError):
                _activation(bad)

    def test_rejects_unknown_kind(self):
        with self.assertRaises(ActivationError):
            _activation(kind="character.teleports")

    def test_rejects_timezone_aware_due_at(self):
        """带时区的时间跟 WorldState.clock 不是一个口径，比较结果没有意义。"""
        from datetime import timezone

        aware = datetime(2026, 8, 22, 0, 10, tzinfo=timezone.utc)
        with self.assertRaises(ActivationError) as ctx:
            _activation(due_at=aware)
        self.assertIn("naive", str(ctx.exception))

    def test_rejects_non_datetime_due_at(self):
        for bad in ("2026-08-22T00:10:00", 1755000000, None):
            with self.subTest(due_at=bad), self.assertRaises(ActivationError):
                _activation(due_at=bad)

    def test_rejects_sub_minute_precision(self):
        """模拟时钟只落在整分钟上，带秒的到期时间永远不会被正好命中。"""
        with self.assertRaises(ActivationError):
            _activation(due_at=datetime(2026, 8, 22, 0, 10, 30))
        with self.assertRaises(ActivationError):
            _activation(due_at=datetime(2026, 8, 22, 0, 10, 0, 1))

    def test_character_activation_requires_a_character(self):
        with self.assertRaises(ActivationError):
            _activation(character_id=None)
        with self.assertRaises(ActivationError):
            _activation(character_id="")

    def test_rejects_impossible_recurrence_values(self):
        for bad in (0, -1, -1440, True, 1.5, "1440", object()):
            with self.subTest(interval=bad), self.assertRaises(ActivationError):
                _activation(interval_minutes=bad)

    def test_payload_is_frozen_at_construction(self):
        source = {"note": "起床", "tags": ["morning"]}
        activation = _activation(payload=source)
        source["note"] = "改掉了"
        source["tags"].append("mutated")

        self.assertEqual(activation.payload["note"], "起床")
        self.assertEqual(list(activation.payload["tags"]), ["morning"])
        with self.assertRaises(TypeError):
            activation.payload["note"] = "也改不动"

        projection = activation.to_dict()
        projection["payload"]["note"] = "投影可以随便改"
        self.assertEqual(activation.payload["note"], "起床")

    def test_rejects_non_json_payload(self):
        with self.assertRaises(ActivationError):
            _activation(payload={"when": datetime.now()})
        with self.assertRaises(ActivationError):
            _activation(payload=["not", "a", "dict"])

    def test_round_trips_through_dict(self):
        activation = _activation(payload={"note": "起床"}, interval_minutes=1440)
        self.assertEqual(ScheduledActivation.from_dict(activation.to_dict()), activation)

    def test_from_dict_rejects_incomplete_or_corrupt_payloads(self):
        good = _activation().to_dict()
        for missing in ("activation_id", "kind", "due_at"):
            broken = dict(good)
            broken.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ActivationError):
                ScheduledActivation.from_dict(broken)
        with self.assertRaises(ActivationError):
            ScheduledActivation.from_dict({**good, "due_at": "not-a-time"})
        with self.assertRaises(ActivationError):
            ScheduledActivation.from_dict("不是字典")

    def test_generated_ids_are_unique(self):
        self.assertNotEqual(new_activation_id(), new_activation_id())


class RecurrenceMathTests(unittest.TestCase):
    """周期推算：不漂相位、不跳日期、跨过去的次数是明说的。"""

    def test_next_occurrence_keeps_the_original_phase(self):
        """反例：next = now + interval 会让"每天 07:00"漂成"每天 07:13"。"""
        daily = _activation(due_at=datetime(2026, 8, 22, 7, 0), interval_minutes=1440)
        following, missed = daily.next_occurrence(datetime(2026, 8, 22, 7, 13))
        self.assertEqual(following.due_at, datetime(2026, 8, 23, 7, 0))
        self.assertEqual(missed, 0)

    def test_next_occurrence_is_strictly_after_an_exact_hit(self):
        daily = _activation(due_at=datetime(2026, 8, 22, 7, 0), interval_minutes=1440)
        following, missed = daily.next_occurrence(datetime(2026, 8, 22, 7, 0))
        self.assertEqual(following.due_at, datetime(2026, 8, 23, 7, 0))
        self.assertEqual(missed, 0)

    def test_skipped_occurrences_are_counted_not_dropped(self):
        daily = _activation(due_at=datetime(2026, 8, 22, 7, 0), interval_minutes=1440)
        following, missed = daily.next_occurrence(datetime(2026, 8, 25, 7, 13))
        self.assertEqual(following.due_at, datetime(2026, 8, 26, 7, 0))
        self.assertEqual(missed, 3)

    def test_recurrence_crosses_midnight_without_losing_a_day(self):
        hourly = _activation(due_at=datetime(2026, 8, 21, 23, 30), interval_minutes=60)
        following, missed = hourly.next_occurrence(datetime(2026, 8, 21, 23, 45))
        self.assertEqual(following.due_at, datetime(2026, 8, 22, 0, 30))
        self.assertEqual(missed, 0)

    def test_recurrence_crosses_a_year_and_a_leap_day(self):
        daily = _activation(due_at=datetime(2027, 12, 31, 23, 30), interval_minutes=1440)
        following, _ = daily.next_occurrence(datetime(2027, 12, 31, 23, 30))
        self.assertEqual(following.due_at, datetime(2028, 1, 1, 23, 30))

        leap = _activation(due_at=datetime(2028, 2, 28, 7, 0), interval_minutes=1440)
        following, _ = leap.next_occurrence(datetime(2028, 2, 28, 7, 0))
        self.assertEqual(following.due_at, datetime(2028, 2, 29, 7, 0))

    def test_one_shot_has_no_next_occurrence(self):
        with self.assertRaises(ActivationError):
            _activation().next_occurrence(CLOCK + timedelta(minutes=30))

    def test_overflow_past_the_representable_range_fails_loudly(self):
        edge = _activation(
            due_at=datetime.max.replace(second=0, microsecond=0) - timedelta(minutes=1),
            interval_minutes=1440,
        )
        with self.assertRaises(ActivationError):
            edge.next_occurrence(edge.due_at)


class QueueOrderTests(unittest.TestCase):
    """队列顺序必须是显式的、可复现的。"""

    def test_orders_by_due_time_regardless_of_insertion_order(self):
        queue = ActivationQueue()
        queue._append(_activation("late", minutes_ahead=90))
        queue._append(_activation("soon", minutes_ahead=15))
        queue._append(_activation("middle", minutes_ahead=40))
        self.assertEqual(
            [a.activation_id for a in queue.pending()], ["soon", "middle", "late"]
        )

    def test_equal_due_times_break_ties_by_registration_not_by_id(self):
        """反例：按 activation_id 排序的实现会把 'aa' 排到 'zz' 前面。"""
        queue = ActivationQueue()
        queue._append(_activation("zz", minutes_ahead=30))
        queue._append(_activation("aa", minutes_ahead=30))
        queue._append(_activation("mm", minutes_ahead=30))
        self.assertEqual([a.activation_id for a in queue.pending()], ["zz", "aa", "mm"])

    def test_order_survives_serialization(self):
        queue = ActivationQueue()
        queue._append(_activation("zz", minutes_ahead=30))
        queue._append(_activation("aa", minutes_ahead=30))
        queue._append(_activation("early", minutes_ahead=5))
        restored = ActivationQueue.from_dict(queue.to_dict())
        self.assertEqual(
            [a.activation_id for a in restored.pending()],
            [a.activation_id for a in queue.pending()],
        )
        self.assertEqual(restored.to_dict(), queue.to_dict())

    def test_rescheduling_keeps_the_registration_tie_breaker(self):
        """周期激活重排之后，它在同刻到期里的位置仍然由登记顺序决定。"""
        queue = ActivationQueue()
        queue._append(_activation("first", minutes_ahead=30, interval_minutes=60))
        queue._append(_activation("second", minutes_ahead=90))
        following, _ = queue.get("first").next_occurrence(
            CLOCK + timedelta(minutes=30)
        )
        queue._reschedule(following)
        # 重排后两条都落在 +90，先登记的仍然在前 —— 重排不重新发号。
        self.assertEqual(queue.get("first").due_at, queue.get("second").due_at)
        self.assertEqual(queue.sequence_of("first"), 0)
        self.assertEqual(
            [a.activation_id for a in queue.pending()], ["first", "second"]
        )

    def test_duplicate_ids_are_rejected_before_any_mutation(self):
        queue = ActivationQueue()
        queue._append(_activation("dup", minutes_ahead=30))
        before = queue.to_dict()
        with self.assertRaises(ActivationQueueError):
            queue._append(_activation("dup", minutes_ahead=90))
        self.assertEqual(queue.to_dict(), before)
        self.assertEqual(len(queue), 1)

    def test_read_surface_never_hands_out_the_internal_container(self):
        queue = ActivationQueue([_activation("a", minutes_ahead=10)])
        pending = queue.pending()
        self.assertIsInstance(pending, tuple)
        projection = queue.to_dict()
        projection["activations"].clear()
        self.assertEqual(len(queue), 1)

    def test_unknown_ids_fail_loudly(self):
        queue = ActivationQueue()
        for call in (queue.get, queue.sequence_of, queue._remove):
            with self.subTest(call=call.__name__), self.assertRaises(ActivationQueueError):
                call("nope")
        self.assertFalse(queue.has("nope"))


class SchedulingValidationTests(unittest.TestCase):
    """排期的每一次拒绝都必须发生在任何状态变更之前。"""

    def setUp(self):
        self.scheduler = _scheduler()

    def _assert_queue_untouched(self, before):
        self.assertEqual(self.scheduler.queue.to_dict(), before)

    def test_rejects_scheduling_in_the_past(self):
        before = self.scheduler.queue.to_dict()
        with self.assertRaises(SchedulerError) as ctx:
            self.scheduler.schedule(_activation("past", minutes_ahead=-10))
        self.assertIn("不晚于当前模拟时钟", str(ctx.exception))
        self._assert_queue_untouched(before)

    def test_rejects_scheduling_at_exactly_the_current_clock(self):
        """恰好等于当前时刻的激活没有任何推进能触发它 —— 那是个说不清的中间态。"""
        before = self.scheduler.queue.to_dict()
        with self.assertRaises(SchedulerError):
            self.scheduler.schedule(_activation("now", minutes_ahead=0))
        self._assert_queue_untouched(before)

    def test_rejects_characters_the_world_does_not_know(self):
        before = self.scheduler.queue.to_dict()
        with self.assertRaises(SchedulerError) as ctx:
            self.scheduler.schedule(_activation("ghost", character_id="kanade"))
        self.assertIn("kanade", str(ctx.exception))
        self._assert_queue_untouched(before)

    def test_session_membership_is_not_the_test_for_existence(self):
        """口径是"世界认识谁"，不是"会话选了谁" —— 跟事件提交边界保持一致。"""
        world = _world()
        world.place_character("kanade", "private_residence")
        scheduler = _scheduler(_session(world))
        self.assertNotIn("kanade", scheduler.state.characters)
        scheduler.schedule(_activation("guest", character_id="kanade"))
        self.assertTrue(scheduler.queue.has("guest"))

    def test_rejects_duplicate_ids_without_touching_the_existing_entry(self):
        self.scheduler.schedule(_activation("dup", minutes_ahead=30))
        before = self.scheduler.queue.to_dict()
        with self.assertRaises(SchedulerError):
            self.scheduler.schedule(_activation("dup", minutes_ahead=90))
        self._assert_queue_untouched(before)
        self.assertEqual(
            self.scheduler.queue.get("dup").due_at, CLOCK + timedelta(minutes=30)
        )

    def test_rejects_anything_that_is_not_a_scheduled_activation(self):
        before = self.scheduler.queue.to_dict()
        for bad in ({"activation_id": "x"}, "x", None, 7):
            with self.subTest(value=bad), self.assertRaises(SchedulerError):
                self.scheduler.schedule(bad)
        self._assert_queue_untouched(before)

    def test_requires_a_session_with_an_authoritative_world(self):
        with self.assertRaises(SchedulerError):
            PersistentScheduler(object())
        bare = SessionState(session_id="s0", scene="gate", characters=["mizuki", "ena"])
        with self.assertRaises(SchedulerError):
            PersistentScheduler(bare)

    def test_a_prebuilt_queue_with_stale_items_is_rejected_at_construction(self):
        queue = ActivationQueue([_activation("stale", minutes_ahead=-30)])
        with self.assertRaises(SchedulerError):
            PersistentScheduler(_session(), queue=queue)

    def test_preview_is_read_only(self):
        self.scheduler.schedule(_activation("soon", minutes_ahead=10))
        self.scheduler.schedule(_activation("later", minutes_ahead=120))
        before = _fingerprint(self.scheduler)
        preview = self.scheduler.preview_due(CLOCK + timedelta(minutes=60))
        self.assertEqual([a.activation_id for a in preview], ["soon"])
        self.assertEqual(_fingerprint(self.scheduler), before)
        with self.assertRaises(SchedulerError):
            self.scheduler.preview_due("2026-08-22T00:10:00")


class TimeAdvanceEventTests(unittest.TestCase):
    """时间只能通过 world.time_advanced 事件推进。"""

    def setUp(self):
        self.state = _session()
        self.scheduler = PersistentScheduler(self.state)

    def test_advancing_commits_exactly_one_time_event(self):
        result = self.scheduler.advance_by(25)

        self.assertIsInstance(result, TickResult)
        self.assertEqual(len(self.state.events), 1)
        event = self.state.events.latest()
        self.assertIs(event.type, EventType.WORLD_TIME_ADVANCED)
        # 事件的时间是它发生的那一刻（推进之前），不是推进之后的时钟。
        self.assertEqual(event.occurred_at, CLOCK)
        self.assertEqual(event.payload["minutes"], 25)
        self.assertIsNone(event.actor_id)
        self.assertEqual(self.state.world_state.clock, CLOCK + timedelta(minutes=25))
        self.assertEqual(result.from_clock, CLOCK)
        self.assertEqual(result.to_clock, CLOCK + timedelta(minutes=25))
        self.assertEqual(result.event["sequence"], 0)

    def test_the_clock_never_moves_without_an_event(self):
        """反例：直接调用 world.advance_time() 的实现会让这两个数字对不上。"""
        for minutes in (10, 5, 1440):
            self.scheduler.advance_by(minutes)
        advanced = self.state.events.by_type(EventType.WORLD_TIME_ADVANCED)
        self.assertEqual(len(advanced), 3)
        self.assertEqual(
            sum(event.payload["minutes"] for event in advanced),
            (self.state.world_state.clock - CLOCK) // timedelta(minutes=1),
        )

    def test_time_events_are_uniquely_identified_and_causally_chained(self):
        first = self.scheduler.advance_by(10)
        second = self.scheduler.advance_by(10)
        self.assertNotEqual(first.event["event_id"], second.event["event_id"])
        self.assertEqual(second.event["causation_id"], first.event["event_id"])
        self.assertEqual(second.event["correlation_id"], self.state.session_id)

    def test_the_scheduler_module_never_mutates_the_clock_directly(self):
        """静态检查：调度器里不允许出现 advance_time() 或对 clock 的赋值。"""
        source = inspect.getsource(scheduler_mod)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                self.assertNotEqual(
                    node.attr, "advance_time", "调度器不能绕过事件推进时钟"
                )
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute):
                        self.assertNotEqual(
                            target.attr, "clock", "调度器不能直接给时钟赋值"
                        )

    def test_a_clock_tick_becomes_nobody_observation(self):
        """时间推进不是任何角色感知得到的事 —— 它没有落点，谁也撞不上。"""
        self.state.world_state.set_availability("ena", Availability.ASLEEP)
        self.scheduler.advance_by(30)

        self.assertEqual(len(self.state.observations), 0)
        decisions = self.state.exposures.for_event(
            self.state.events.latest().event_id
        )
        self.assertEqual(
            sorted(d.character_id for d in decisions), ["ena", "mizuki"]
        )
        self.assertEqual([d.exposed for d in decisions], [False, False])

    def test_a_tick_produces_no_dialogue_and_no_turn(self):
        """调度器不生成台词、不记轮次 —— 那是 P9 和生成层的事。"""
        self.scheduler.schedule(_activation("wake", minutes_ahead=10))
        result = self.scheduler.advance_by(10)

        self.assertEqual(len(self.state.turns), 0)
        self.assertEqual(
            [event.type for event in self.state.events],
            [EventType.WORLD_TIME_ADVANCED],
        )
        due = result.due[0]
        self.assertIsInstance(due, ActivationDue)
        self.assertNotIn("text", due.to_dict())
        self.assertEqual(dict(due.payload), {})

    def test_the_scheduler_does_not_import_the_generation_stack(self):
        forbidden = ("pns.logic.router", "pns.logic.simulation", "pns.world.characters")
        source = inspect.getsource(scheduler_mod)
        for module in forbidden:
            self.assertNotIn(module, source)

    def test_rejects_non_positive_or_non_integer_minutes(self):
        before = _fingerprint(self.scheduler)
        for bad in (0, -1, -1440, True, 1.5, "10", None):
            with self.subTest(minutes=bad), self.assertRaises(SchedulerError):
                self.scheduler.advance_by(bad)
        self.assertEqual(_fingerprint(self.scheduler), before)

    def test_advance_to_rejects_backwards_equal_and_ragged_targets(self):
        before = _fingerprint(self.scheduler)
        for bad in (
            CLOCK - timedelta(minutes=1),
            CLOCK,
            CLOCK + timedelta(seconds=90),
            "2026-08-22T00:10:00",
        ):
            with self.subTest(target=bad), self.assertRaises(SchedulerError):
                self.scheduler.advance_to(bad)
        self.assertEqual(_fingerprint(self.scheduler), before)

    def test_advance_to_lands_exactly_on_the_target(self):
        target = datetime(2026, 8, 22, 6, 30)
        result = self.scheduler.advance_to(target)
        self.assertEqual(self.state.world_state.clock, target)
        self.assertEqual(result.minutes, 400)
        self.assertEqual(self.state.events.latest().payload["minutes"], 400)

    def test_advancing_past_the_representable_range_fails_without_mutation(self):
        state = _session(_world(clock=datetime.max.replace(second=0, microsecond=0) - timedelta(minutes=1)))
        scheduler = PersistentScheduler(state)
        before = _fingerprint(scheduler)
        with self.assertRaises(SchedulerError):
            scheduler.advance_by(10)
        self.assertEqual(_fingerprint(scheduler), before)


class DueSemanticsTests(unittest.TestCase):
    """哪些排期在这一次推进里到期，以及到期之后队列变成什么样。"""

    def setUp(self):
        self.state = _session()
        self.scheduler = PersistentScheduler(self.state)

    def test_only_activations_at_or_before_the_target_fire(self):
        self.scheduler.schedule(_activation("soon", minutes_ahead=10))
        self.scheduler.schedule(_activation("edge", minutes_ahead=30, character_id="ena"))
        self.scheduler.schedule(_activation("later", minutes_ahead=31))

        result = self.scheduler.advance_by(30)
        self.assertEqual(result.due_ids, ("soon", "edge"))
        self.assertEqual([a.activation_id for a in self.scheduler.pending()], ["later"])

    def test_due_records_follow_queue_order(self):
        self.scheduler.schedule(_activation("zz", minutes_ahead=10))
        self.scheduler.schedule(_activation("aa", minutes_ahead=10, character_id="ena"))
        self.scheduler.schedule(_activation("early", minutes_ahead=5))
        result = self.scheduler.advance_by(10)
        self.assertEqual(result.due_ids, ("early", "zz", "aa"))
        self.assertEqual([r.sequence for r in result.due], [2, 0, 1])

    def test_a_one_shot_fires_at_most_once(self):
        self.scheduler.schedule(_activation("once", minutes_ahead=10))
        first = self.scheduler.advance_by(10)
        self.assertEqual(first.due_ids, ("once",))
        self.assertFalse(self.scheduler.queue.has("once"))

        second = self.scheduler.advance_by(600)
        third = self.scheduler.advance_by(60 * 24 * 30)
        self.assertEqual(second.due_ids, ())
        self.assertEqual(third.due_ids, ())
        self.assertIsNone(first.due[0].next_due_at)

    def test_due_records_carry_the_scheduled_time_not_only_the_firing_time(self):
        """反例：把 due_at 直接写成当前时钟的实现会丢掉"迟了多久"。"""
        self.scheduler.schedule(_activation("late", minutes_ahead=10))
        result = self.scheduler.advance_by(45)
        record = result.due[0]
        self.assertEqual(record.due_at, CLOCK + timedelta(minutes=10))
        self.assertEqual(record.fired_at, CLOCK + timedelta(minutes=45))
        self.assertEqual(record.character_id, "mizuki")
        self.assertIs(record.kind, ActivationKind.CHARACTER_ACTIVATION)

    def test_advancing_across_midnight_fires_and_carries_the_date(self):
        self.scheduler.schedule(
            _activation("after_midnight", due_at=datetime(2026, 8, 22, 0, 10))
        )
        result = self.scheduler.advance_by(25)
        self.assertEqual(result.due_ids, ("after_midnight",))
        self.assertEqual(self.state.world_state.date, "2026-08-22")
        self.assertEqual(self.state.world_state.time, "00:15")

    def test_advancing_across_a_year_boundary_fires_everything_in_between(self):
        state = _session(_world(clock=datetime(2026, 12, 31, 23, 50)))
        scheduler = PersistentScheduler(state)
        scheduler.schedule(_activation("newyear", due_at=datetime(2027, 1, 1, 0, 0)))
        scheduler.schedule(
            _activation("january", due_at=datetime(2027, 1, 2, 9, 0), character_id="ena")
        )
        result = scheduler.advance_to(datetime(2027, 1, 2, 9, 0))
        self.assertEqual(result.due_ids, ("newyear", "january"))
        self.assertEqual(state.world_state.date, "2027-01-02")
        # 10 分钟到零点 + 24 小时 + 9 小时
        self.assertEqual(result.minutes, 10 + 1440 + 540)

    def test_a_recurring_activation_keeps_its_phase_across_days(self):
        self.scheduler.schedule(
            _activation(
                "morning", due_at=datetime(2026, 8, 22, 7, 0), interval_minutes=1440
            )
        )
        first = self.scheduler.advance_to(datetime(2026, 8, 22, 7, 13))
        self.assertEqual(first.due_ids, ("morning",))
        self.assertEqual(first.due[0].missed_occurrences, 0)
        self.assertEqual(self.scheduler.next_due_at(), datetime(2026, 8, 23, 7, 0))

        second = self.scheduler.advance_to(datetime(2026, 8, 23, 7, 30))
        self.assertEqual(second.due[0].due_at, datetime(2026, 8, 23, 7, 0))
        self.assertEqual(self.scheduler.next_due_at(), datetime(2026, 8, 24, 7, 0))

    def test_a_long_jump_coalesces_and_reports_what_it_skipped(self):
        """跨过去的次数必须是明说的 —— 少跑可以，悄悄少跑不行。"""
        self.scheduler.schedule(
            _activation(
                "morning", due_at=datetime(2026, 8, 22, 7, 0), interval_minutes=1440
            )
        )
        result = self.scheduler.advance_to(datetime(2026, 8, 25, 7, 0))
        self.assertEqual(len(result.due), 1)
        record = result.due[0]
        self.assertEqual(record.due_at, datetime(2026, 8, 22, 7, 0))
        self.assertEqual(record.missed_occurrences, 3)
        self.assertEqual(record.next_due_at, datetime(2026, 8, 26, 7, 0))
        self.assertEqual(self.scheduler.next_due_at(), datetime(2026, 8, 26, 7, 0))

    def test_a_recurring_activation_never_fires_twice_in_one_tick(self):
        self.scheduler.schedule(_activation("hourly", minutes_ahead=10, interval_minutes=60))
        result = self.scheduler.advance_by(600)
        self.assertEqual(result.due_ids, ("hourly",))
        self.assertGreater(self.scheduler.next_due_at(), self.scheduler.clock)

    def test_advance_to_next_due_lands_on_the_next_activation(self):
        self.scheduler.schedule(_activation("soon", minutes_ahead=17))
        self.scheduler.schedule(_activation("later", minutes_ahead=90))
        result = self.scheduler.advance_to_next_due()
        self.assertEqual(result.minutes, 17)
        self.assertEqual(result.due_ids, ("soon",))
        self.assertEqual(self.scheduler.clock, CLOCK + timedelta(minutes=17))

    def test_advance_to_next_due_on_an_empty_queue_changes_nothing(self):
        before = _fingerprint(self.scheduler)
        self.assertIsNone(self.scheduler.advance_to_next_due())
        self.assertEqual(_fingerprint(self.scheduler), before)

    def test_debug_projection_is_json_safe_and_read_only(self):
        self.scheduler.schedule(_activation("soon", minutes_ahead=20, interval_minutes=60))
        before = _fingerprint(self.scheduler)
        projection = self.scheduler.debug_projection()
        self.assertEqual(projection["pending"], 1)
        self.assertEqual(projection["next_due_at"], "2026-08-22T00:10:00")
        self.assertEqual(projection["queue"][0]["due_in_minutes"], 20)
        self.assertEqual(projection["time_advanced_events"], 0)
        projection["queue"].clear()
        self.assertEqual(_fingerprint(self.scheduler), before)


class CancellationTests(unittest.TestCase):
    """取消是显式的，而且幂等。"""

    def setUp(self):
        self.state = _session()
        self.scheduler = PersistentScheduler(self.state)

    def test_cancel_removes_once_and_then_reports_nothing_to_do(self):
        self.scheduler.schedule(_activation("drop", minutes_ahead=10))
        self.assertTrue(self.scheduler.cancel("drop"))
        self.assertFalse(self.scheduler.cancel("drop"))
        self.assertFalse(self.scheduler.cancel("drop"))
        self.assertFalse(self.scheduler.queue.has("drop"))

    def test_a_cancelled_activation_never_fires(self):
        self.scheduler.schedule(_activation("drop", minutes_ahead=10))
        self.scheduler.schedule(_activation("keep", minutes_ahead=10, character_id="ena"))
        self.scheduler.cancel("drop")
        result = self.scheduler.advance_by(60)
        self.assertEqual(result.due_ids, ("keep",))

    def test_cancelling_a_fired_one_shot_reports_false(self):
        self.scheduler.schedule(_activation("once", minutes_ahead=10))
        self.scheduler.advance_by(10)
        self.assertFalse(self.scheduler.cancel("once"))

    def test_cancelling_a_recurring_activation_stops_the_series(self):
        self.scheduler.schedule(_activation("hourly", minutes_ahead=10, interval_minutes=60))
        self.scheduler.advance_by(10)
        self.assertTrue(self.scheduler.cancel("hourly"))
        self.assertEqual(self.scheduler.advance_by(60 * 48).due_ids, ())

    def test_cancel_rejects_a_missing_identifier(self):
        for bad in ("", None, 7):
            with self.subTest(activation_id=bad), self.assertRaises(SchedulerError):
                self.scheduler.cancel(bad)

    def test_cancel_does_not_rewrite_the_rest_of_the_queue(self):
        self.scheduler.schedule(_activation("a", minutes_ahead=10))
        self.scheduler.schedule(_activation("b", minutes_ahead=10, character_id="ena"))
        self.scheduler.schedule(_activation("c", minutes_ahead=10))
        self.scheduler.cancel("b")
        self.assertEqual([a.activation_id for a in self.scheduler.pending()], ["a", "c"])
        self.assertEqual(self.scheduler.queue.sequence_of("c"), 2)


class AtomicTickTests(unittest.TestCase):
    """一次推进的每一个变更步骤失败时，整件事都必须像没发生过。"""

    def setUp(self):
        self.state = _session()
        self.scheduler = PersistentScheduler(self.state)
        self.scheduler.schedule(_activation("once", minutes_ahead=10))
        self.scheduler.schedule(
            _activation("hourly", minutes_ahead=20, interval_minutes=60)
        )
        self.before = _fingerprint(self.scheduler)

    def _assert_nothing_happened(self):
        self.assertEqual(_fingerprint(self.scheduler), self.before)

    def test_rolls_back_when_the_event_cannot_be_appended(self):
        with patch.object(EventStore, "_append", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.scheduler.advance_by(30)
        self._assert_nothing_happened()

    def test_rolls_back_when_the_state_effect_fails(self):
        with patch.object(WorldState, "advance_time", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.scheduler.advance_by(30)
        self._assert_nothing_happened()

    def test_rolls_back_when_exposure_recording_fails(self):
        # 时钟推进不产生观察，但每个候选角色都会留下一条判定记录，
        # 判定这一步失败同样必须让整次推进作废。
        with patch.object(ExposureLog, "_append", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.scheduler.advance_by(30)
        self._assert_nothing_happened()

    def test_rolls_back_the_committed_event_when_the_queue_step_fails(self):
        """队列这一步失败时，已经提交的时间事件也必须跟着消失。"""
        with patch.object(
            PersistentScheduler, "_apply_due", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.scheduler.advance_by(30)
        self._assert_nothing_happened()

    def test_rolls_back_when_removing_a_fired_one_shot_fails(self):
        with patch.object(
            ActivationQueue, "_remove", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.scheduler.advance_by(30)
        self._assert_nothing_happened()

    def test_rolls_back_when_rescheduling_a_recurring_activation_fails(self):
        with patch.object(
            ActivationQueue, "_reschedule", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.scheduler.advance_by(30)
        self._assert_nothing_happened()

    def test_a_clock_that_lands_somewhere_else_aborts_the_whole_tick(self):
        """状态效果没把时钟落在目标上 —— 宁可整体作废，也不按错的时间触发。"""

        def wrong(self, minutes=10):
            self.clock = self.clock + timedelta(minutes=minutes + 7)
            return self.clock

        with patch.object(WorldState, "advance_time", wrong):
            with self.assertRaises(SchedulerError):
                self.scheduler.advance_by(30)
        self._assert_nothing_happened()

    def test_a_failed_tick_leaves_no_trace_on_the_next_one(self):
        """回滚不是"看起来一样"：后面一次成功推进的结果必须逐字节相同。"""
        clean = PersistentScheduler(_session())
        clean.schedule(_activation("once", minutes_ahead=10))
        clean.schedule(_activation("hourly", minutes_ahead=20, interval_minutes=60))
        expected = clean.advance_by(30)

        with patch.object(EventStore, "_append", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.scheduler.advance_by(30)
        actual = self.scheduler.advance_by(30)

        self.assertEqual(actual.to_dict(), expected.to_dict())
        self.assertEqual(_fingerprint(self.scheduler), _fingerprint(clean))

    def test_a_rejected_event_shape_never_reaches_the_queue(self):
        """事件层面的拒绝也不能留下半个动过的队列。"""
        other = _session(session_id="s2")
        # 世界历史里塞一条更晚的事件，下一次推进会因为"世界历史不能倒流"被拒。
        scheduler = PersistentScheduler(other)
        scheduler.schedule(_activation("once", minutes_ahead=10))
        before = _fingerprint(scheduler)
        with patch.object(
            EventStore, "_check_can_append", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                scheduler.advance_by(30)
        self.assertEqual(_fingerprint(scheduler), before)


class SerializationTests(unittest.TestCase):
    """存档能原样恢复；损坏的存档必须响亮地失败。"""

    def setUp(self):
        self.state = _session()
        self.scheduler = PersistentScheduler(self.state)
        self.scheduler.schedule(_activation("zz", minutes_ahead=30))
        self.scheduler.schedule(_activation("aa", minutes_ahead=30, character_id="ena"))
        self.scheduler.schedule(_activation("daily", minutes_ahead=90, interval_minutes=1440))
        self.archive = self.scheduler.to_dict()

    def _restore(self, archive):
        return PersistentScheduler.restore(self.state, archive)

    def _corrupt(self, mutate):
        import copy

        archive = copy.deepcopy(self.archive)
        mutate(archive)
        return archive

    def test_round_trips_through_constructors_and_validation(self):
        restored = self._restore(self.archive)
        self.assertEqual(restored.to_dict(), self.archive)
        self.assertEqual(
            [a.activation_id for a in restored.pending()],
            [a.activation_id for a in self.scheduler.pending()],
        )
        self.assertEqual(restored.queue.sequence_of("daily"), 2)

    def test_a_restored_queue_keeps_numbering_where_it_left_off(self):
        restored = self._restore(self.archive)
        restored.cancel("zz")
        sequence = restored.schedule(_activation("fresh", minutes_ahead=45))
        self.assertEqual(sequence, 3)
        self.assertEqual(
            [a.activation_id for a in restored.pending()], ["aa", "fresh", "daily"]
        )

    def test_a_restored_scheduler_still_ticks_identically(self):
        restored = self._restore(self.archive)
        expected = self.scheduler.advance_by(30)
        # 两个调度器绑在同一个会话上，先跑的那次已经改了世界；这里只比对
        # 恢复出来的队列会算出同一批到期。
        self.assertEqual(
            [record.activation_id for record in expected.due], ["zz", "aa"]
        )
        self.assertEqual(
            [a.activation_id for a in restored.preview_due(CLOCK + timedelta(minutes=30))],
            ["zz", "aa"],
        )

    def test_rejects_an_archive_from_another_session(self):
        other = _session(session_id="s2")
        with self.assertRaises(SchedulerError) as ctx:
            PersistentScheduler.restore(other, self.archive)
        self.assertIn("s2", str(ctx.exception))

    def test_rejects_a_clock_that_disagrees_with_the_world(self):
        self.state.world_state.advance_time(5)
        with self.assertRaises(SchedulerError) as ctx:
            self._restore(self.archive)
        self.assertIn("不一致", str(ctx.exception))

    def test_rejects_a_missing_or_unparsable_clock(self):
        for value in (None, "", "昨天", 1755000000):
            archive = self._corrupt(lambda a, v=value: a.__setitem__("clock", v))
            with self.subTest(clock=value), self.assertRaises(SchedulerError):
                self._restore(archive)

    def test_rejects_an_activation_that_is_not_in_the_future(self):
        """存档里出现早于持久化时钟的激活 = 一条永远不会被触发的僵尸排期。"""
        for offset in (-30, 0):
            archive = self._corrupt(
                lambda a, o=offset: a["queue"]["activations"][0].__setitem__(
                    "due_at", (CLOCK + timedelta(minutes=o)).isoformat()
                )
            )
            with self.subTest(offset=offset), self.assertRaises(SchedulerError):
                self._restore(archive)

    def test_rejects_a_reordered_queue(self):
        def swap(archive):
            entries = archive["queue"]["activations"]
            entries[0], entries[1] = entries[1], entries[0]

        with self.assertRaises(SchedulerError) as ctx:
            self._restore(self._corrupt(swap))
        self.assertIn("顺序", str(ctx.exception))

    def test_rejects_duplicate_ids_and_duplicate_sequences(self):
        def duplicate_id(archive):
            archive["queue"]["activations"][1]["activation_id"] = "zz"

        def duplicate_sequence(archive):
            archive["queue"]["activations"][1]["sequence"] = 0

        for mutate in (duplicate_id, duplicate_sequence):
            with self.subTest(mutate=mutate.__name__), self.assertRaises(SchedulerError):
                self._restore(self._corrupt(mutate))

    def test_rejects_corrupt_sequence_numbers(self):
        for value in (None, "0", -1, True, 1.5):
            archive = self._corrupt(
                lambda a, v=value: a["queue"]["activations"][0].__setitem__("sequence", v)
            )
            with self.subTest(sequence=value), self.assertRaises(SchedulerError):
                self._restore(archive)

    def test_rejects_a_missing_sequence(self):
        archive = self._corrupt(
            lambda a: a["queue"]["activations"][0].pop("sequence")
        )
        with self.assertRaises(SchedulerError):
            self._restore(archive)

    def test_rejects_a_next_sequence_that_would_collide(self):
        archive = self._corrupt(
            lambda a: a["queue"].__setitem__("next_sequence", 1)
        )
        with self.assertRaises(SchedulerError):
            self._restore(archive)
        archive = self._corrupt(
            lambda a: a["queue"].__setitem__("next_sequence", "3")
        )
        with self.assertRaises(SchedulerError):
            self._restore(archive)

    def test_rejects_a_corrupt_activation_shape(self):
        archive = self._corrupt(
            lambda a: a["queue"]["activations"][0].__setitem__("kind", "character.dances")
        )
        with self.assertRaises(SchedulerError):
            self._restore(archive)

    def test_rejects_archives_that_are_not_dictionaries(self):
        for bad in ("存档", None, ["a"]):
            with self.subTest(archive=bad), self.assertRaises(SchedulerError):
                self._restore(bad)
        with self.assertRaises(SchedulerError):
            self._restore({**self.archive, "queue": "不是队列"})


class SessionIsolationTests(unittest.TestCase):
    """调度状态是会话私有的运行时状态，不是进程级的东西。"""

    def test_two_sessions_do_not_share_a_queue_or_a_clock(self):
        first = PersistentScheduler(_session(session_id="s1"))
        second = PersistentScheduler(_session(session_id="s2"))

        first.schedule(_activation("only_in_first", minutes_ahead=10))
        self.assertEqual(len(second.queue), 0)

        first.advance_by(30)
        self.assertEqual(second.clock, CLOCK)
        self.assertEqual(len(second.state.events), 0)
        self.assertIsNot(first.queue, second.queue)

    def test_a_queue_cannot_be_carried_from_one_session_into_another(self):
        first = PersistentScheduler(_session(session_id="s1"))
        first.schedule(_activation("private", minutes_ahead=10))
        with self.assertRaises(SchedulerError):
            PersistentScheduler.restore(_session(session_id="s2"), first.to_dict())

    def test_the_module_holds_no_process_level_scheduler(self):
        instances = [
            name
            for name, value in vars(scheduler_mod).items()
            if isinstance(value, (PersistentScheduler, ActivationQueue))
        ]
        self.assertEqual(instances, [])


# ── 与既有运行时的边界 ──────────────────────────────────────────────────
import tempfile  # noqa: E402  （下面这几组用例才需要，放在这里免得污染上面）

from pns.runtime.content_registry import ContentRegistry  # noqa: E402
from pns.runtime.reload import ConfigBoundary, SessionSupervisor  # noqa: E402
from pns.runtime.session_runtime import SessionRuntime  # noqa: E402


async def _reply(client, character, history, world, model, *args, **kwargs):
    return f"reply-from-{character}"


async def _judge(*args, **kwargs):
    return {"drift_score": 1, "is_ooc": False, "evaluator_model": "test-judge"}


class RuntimeSessionTestBase:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.history_dir = tmp / "history"
        self.drift_file = tmp / "drift.jsonl"
        self.drift_file.parent.mkdir(parents=True, exist_ok=True)
        self.drift_file.touch()
        self._env_backup = dict(os.environ)
        self._patches = [
            patch("pns.runtime.session_runtime.router_mod._get_api_key", return_value="test-key"),
            patch("pns.runtime.session_runtime.router_mod.create_client", return_value=object()),
        ]
        for p in self._patches:
            p.start()
        self.supervisor = SessionSupervisor()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.environ.clear()
        os.environ.update(self._env_backup)
        self._tmp.cleanup()

    def _create(self, registry=None, **params):
        base = {"characters": ["mizuki", "ena"], "max_turns": 4, "api_delay": 0}
        base.update(params)
        return SessionRuntime.create(
            base,
            registry=registry,
            supervisor=self.supervisor,
            history_dir=self.history_dir,
            drift_scores_file=self.drift_file,
        )


class RoundRobinUnchangedTests(RuntimeSessionTestBase, unittest.IsolatedAsyncioTestCase):
    """确定性研究会话一点没变：轮转顺序照旧，时间不会被偷偷推进。"""

    async def test_a_research_session_still_alternates_deterministically(self):
        runtime = self._create()
        clock_before = runtime.world.clock
        with patch("pns.runtime.session_runtime.call_character_async", _reply), \
             patch("pns.runtime.session_runtime.judge_async", _judge):
            messages = [m async for m in runtime.run()]

        turns = [m["character"] for m in messages if m["type"] == "turn"]
        self.assertEqual(turns, ["mizuki", "ena", "mizuki", "ena"])
        self.assertEqual(
            [m["type"] for m in messages][:4], ["start", "generating", "judging", "turn"]
        )
        # 调度器存在、可用，但研究会话不调用它：时钟一分钟都没走。
        self.assertEqual(runtime.world.clock, clock_before)
        self.assertEqual(
            runtime.state.events.by_type(EventType.WORLD_TIME_ADVANCED), ()
        )
        self.assertEqual(len(runtime.state.events), len(runtime.state.turns))
        self.assertEqual(len(runtime.scheduler.queue), 0)
        self.assertIs(runtime.scheduler.state, runtime.state)

    async def test_each_session_runtime_owns_its_own_scheduler(self):
        first = self._create()
        second = self._create()
        try:
            self.assertIsNot(first.scheduler, second.scheduler)
            first.scheduler.schedule(
                _activation(
                    "only_first",
                    due_at=first.world.clock + timedelta(minutes=30),
                )
            )
            self.assertEqual(len(second.scheduler.queue), 0)
            first.scheduler.advance_by(30)
            self.assertNotEqual(first.world.clock, second.world.clock)
            self.assertEqual(len(second.state.events), 0)
        finally:
            first.close()
            second.close()

    async def test_a_scheduler_tick_coexists_with_the_round_robin_history(self):
        """手动推进时间不会打断轮转，也不会给谁多写一行角色历史。"""
        runtime = self._create(max_turns=2)
        with patch("pns.runtime.session_runtime.call_character_async", _reply), \
             patch("pns.runtime.session_runtime.judge_async", _judge):
            messages = [m async for m in runtime.run()]
        history_before = {
            cid: len(items) for cid, items in runtime.state.histories.items()
        }
        runtime.scheduler.advance_by(60)
        self.assertEqual(
            {cid: len(items) for cid, items in runtime.state.histories.items()},
            history_before,
        )
        self.assertEqual(len([m for m in messages if m["type"] == "turn"]), 2)


class ReloadCannotTouchSchedulerStateTests(RuntimeSessionTestBase, unittest.TestCase):
    """P7 的配置重载改不了一个已经存在的队列或时钟。"""

    def setUp(self):
        super().setUp()
        self.boundary = ConfigBoundary(self.supervisor, stop_timeout=0.5)
        self.runtime = self._create(registry=self.boundary.active())
        self.scheduler = self.runtime.scheduler
        clock = self.runtime.world.clock
        self.scheduler.schedule(
            _activation("morning", due_at=clock + timedelta(minutes=30))
        )
        self.scheduler.schedule(
            _activation(
                "hourly",
                due_at=clock + timedelta(minutes=90),
                interval_minutes=60,
                character_id="ena",
            )
        )
        self.scheduler.advance_by(45)
        self.before = _fingerprint(self.scheduler)

    def tearDown(self):
        self.runtime.close()
        super().tearDown()

    def test_a_successful_reload_leaves_the_queue_and_the_clock_alone(self):
        self.runtime.close()  # 让重载能等到 idle，从而真的完成一次切换
        old_registry = self.boundary.active()

        result = self.boundary.reload()

        self.assertEqual(result.status, "ok")
        self.assertIsNot(self.boundary.active(), old_registry)
        self.assertIs(self.runtime.scheduler, self.scheduler)
        self.assertEqual(_fingerprint(self.scheduler), self.before)
        self.assertEqual(
            [a.activation_id for a in self.scheduler.pending()], ["hourly"]
        )

    def test_a_failed_reload_leaves_the_queue_and_the_clock_alone(self):
        result = self.boundary.reload()  # 会话还活着 → 等不到 idle → 失败

        self.assertEqual(result.status, "failed")
        self.assertIsNotNone(self.runtime.stop_reason)
        self.assertEqual(_fingerprint(self.scheduler), self.before)

    def test_the_registry_carries_no_scheduler_state(self):
        forbidden = {"scheduler", "queue", "activations", "schedule", "clock"}
        fields = {f.name for f in ContentRegistry.__dataclass_fields__.values()}
        self.assertEqual(fields & forbidden, set())

    def test_the_registry_exposes_no_way_to_schedule_or_advance(self):
        writers = [
            name
            for name in dir(ContentRegistry)
            if name.startswith(("schedule", "advance", "cancel", "tick"))
        ]
        self.assertEqual(writers, [])


class AdversarialEdgeTests(unittest.TestCase):
    """自查里真正把手伸进去掰过的那几处边缘。"""

    def test_payload_reaches_the_due_record_and_stays_frozen(self):
        scheduler = _scheduler()
        scheduler.schedule(
            _activation("note", minutes_ahead=10, payload={"note": "起床", "tags": ["a"]})
        )
        record = scheduler.advance_by(10).due[0]
        self.assertEqual(record.to_dict()["payload"], {"note": "起床", "tags": ["a"]})
        with self.assertRaises(TypeError):
            record.payload["note"] = "改不动"

    def test_an_activation_still_fires_after_its_character_left_the_world(self):
        """排期时角色在，到期时不在了 —— 记录照出，由下游复核，不安静丢掉。"""
        scheduler = _scheduler()
        scheduler.schedule(_activation("gone", minutes_ahead=10))
        scheduler.state.world_state.remove_character("mizuki")
        result = scheduler.advance_by(10)
        self.assertEqual(result.due_ids, ("gone",))
        self.assertEqual(result.due[0].character_id, "mizuki")
        self.assertNotIn("mizuki", scheduler.world.known_characters())

    def test_a_clock_that_is_not_on_a_whole_minute_still_reaches_the_next_due(self):
        state = _session(_world(clock=datetime(2026, 8, 21, 23, 50, 30)))
        scheduler = PersistentScheduler(state)
        scheduler.schedule(_activation("x", due_at=datetime(2026, 8, 22, 0, 10)))
        result = scheduler.advance_to_next_due()
        self.assertEqual(result.minutes, 20)
        self.assertGreaterEqual(scheduler.clock, datetime(2026, 8, 22, 0, 10))
        self.assertEqual(result.due_ids, ("x",))

    def test_restore_rejects_an_archive_that_lost_its_queue(self):
        scheduler = _scheduler()
        archive = scheduler.to_dict()
        archive.pop("queue")
        with self.assertRaises(SchedulerError):
            PersistentScheduler.restore(scheduler.state, archive)

    def test_two_schedulers_on_one_session_do_not_collide_on_event_ids(self):
        state = _session()
        first = PersistentScheduler(state)
        second = PersistentScheduler(state)
        first.advance_by(10)
        second.advance_by(10)
        ids = [event.event_id for event in state.events]
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(state.world_state.clock, CLOCK + timedelta(minutes=20))

    def test_works_on_a_session_state_without_generation_histories(self):
        """调度器只需要权威世界状态，不需要一个正在生成的会话。"""
        state = SessionState(session_id="bare", scene="gate", characters=["mizuki", "ena"])
        state.attach_world_state(_world())
        scheduler = PersistentScheduler(state)
        scheduler.schedule(_activation("tick", minutes_ahead=10))
        self.assertEqual(scheduler.advance_by(10).due_ids, ("tick",))
        self.assertEqual(len(state.events), 1)

    def test_the_tick_result_projection_cannot_edit_committed_history(self):
        scheduler = _scheduler()
        result = scheduler.advance_by(10)
        result.event["payload"]["minutes"] = 999
        result.to_dict()["due"].clear()
        self.assertEqual(scheduler.state.events.latest().payload["minutes"], 10)

    def test_restore_rejects_timezone_aware_times_in_the_archive(self):
        scheduler = _scheduler()
        scheduler.schedule(_activation("a", minutes_ahead=30))
        archive = scheduler.to_dict()
        aware = dict(archive)
        aware["queue"] = {
            **archive["queue"],
            "activations": [
                {**archive["queue"]["activations"][0], "due_at": "2026-08-22T00:20:00+09:00"}
            ],
        }
        with self.assertRaises(SchedulerError):
            PersistentScheduler.restore(scheduler.state, aware)

        shifted = {**archive, "clock": "2026-08-21T23:50:00+09:00"}
        with self.assertRaises(SchedulerError):
            PersistentScheduler.restore(scheduler.state, shifted)

    def test_the_queue_invariant_holds_across_a_long_run(self):
        """跑一串长短不一的推进，每一步之后队列都不能留下已经过期的激活。"""
        state = _session()
        scheduler = PersistentScheduler(state)
        scheduler.schedule(_activation("once", minutes_ahead=7))
        scheduler.schedule(_activation("hourly", minutes_ahead=15, interval_minutes=60))
        scheduler.schedule(
            _activation(
                "daily",
                due_at=datetime(2026, 8, 22, 7, 0),
                interval_minutes=1440,
                character_id="ena",
            )
        )

        total = 0
        fired = []
        for minutes in (3, 5, 40, 1, 720, 90, 2880, 17, 1440, 60):
            result = scheduler.advance_by(minutes)
            total += minutes
            fired.extend(result.due_ids)
            for activation in scheduler.pending():
                self.assertGreater(activation.due_at, scheduler.clock)
            # 每一步的存档都必须能原样恢复回来。
            self.assertEqual(
                PersistentScheduler.restore(state, scheduler.to_dict()).to_dict(),
                scheduler.to_dict(),
            )

        self.assertEqual(scheduler.clock, CLOCK + timedelta(minutes=total))
        self.assertEqual(
            len(state.events.by_type(EventType.WORLD_TIME_ADVANCED)), 10
        )
        self.assertEqual(
            sum(
                event.payload["minutes"]
                for event in state.events.by_type(EventType.WORLD_TIME_ADVANCED)
            ),
            total,
        )
        self.assertEqual(fired.count("once"), 1)
        self.assertEqual(len(scheduler.queue), 2)
