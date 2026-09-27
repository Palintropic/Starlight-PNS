# tests/test_session_fence.py — SessionState 的生命周期阶段与写栅栏（WORLD-1 设计 §13.4 / §14.4）。
#
# 守的线（R4 §4 那张表）：
#   1. fence 之后，每一个受支持的公开写方法都失败 —— 会话的、调度器的、世界状态的；
#   2. 快照块里是只读的，块内写入失败；
#   3. 发布之后不能再整段恢复存档或重绑服务；
#   4. 阶段只能前进。
#
# 运行: python -m unittest discover -s tests -p test_session_fence.py
import unittest
from datetime import datetime, timedelta

from grants_support import grant_everything
from pns.models.activation import ActivationKind, ScheduledActivation
from pns.models.cognition import CognitionTimeline
from pns.models.session import (
    SessionFencedError,
    SessionState,
    SessionStateError,
    TransactionBoundaryError,
    Turn,
)
from pns.models.world_state import WorldState
from pns.runtime.scheduler import PersistentScheduler
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph

CLOCK = datetime(2026, 9, 27, 19, 0)


def _state():
    world = WorldState(
        clock=CLOCK,
        locations=build_default_location_graph(),
        channels=build_default_channel_registry(),
    )
    grant_everything(world)
    world.place_character("mizuki", "mizuki_home_room")
    world.place_character("ena", "ena_home_studio")
    state = SessionState(session_id="s1", scene="gate", characters=["mizuki", "ena"])
    state.attach_world_state(world)
    scheduler = PersistentScheduler(state)
    scheduler.schedule(
        ScheduledActivation(
            activation_id="wake",
            kind=ActivationKind.CHARACTER_ACTIVATION,
            due_at=CLOCK + timedelta(minutes=10),
            character_id="mizuki",
        )
    )
    return state, scheduler


def _writes(state, scheduler):
    """R4 §4 列出的每一个受支持的写入口。"""
    world = state.world_state
    return {
        "initialize_runtime": lambda: state.initialize_runtime("开场"),
        "start": state.start,
        "advance_character": state.advance_character,
        "record_error": lambda: state.record_error("late"),
        "complete": state.complete,
        "cancel": state.cancel,
        "record_observations": lambda: state.record_observations((), ()),
        "record_memories": lambda: state.record_memories(()),
        "add_turn": lambda: state.add_turn(
            Turn(
                turn_number=1,
                character="mizuki",
                prompt="p",
                response="…",
                timestamp="2026-09-27T19:00:00",
            )
        ),
        "atomic_commit": lambda: state.atomic_commit().__enter__(),
        "set_cognition": lambda: state.set_cognition(
            CognitionTimeline.open(log_length=0, sim=CLOCK, wall="w")
        ),
        "scheduler.schedule": lambda: scheduler.schedule(
            ScheduledActivation(
                activation_id="late",
                kind=ActivationKind.CHARACTER_ACTIVATION,
                due_at=CLOCK + timedelta(minutes=30),
                character_id="ena",
            )
        ),
        "scheduler.cancel": lambda: scheduler.cancel("wake"),
        "scheduler.acknowledge": lambda: scheduler.acknowledge("nope"),
        "scheduler.advance_by": lambda: scheduler.advance_by(5),
        "world.advance_time": lambda: world.advance_time(5),
        "world.place_character": lambda: world.place_character("mizuki", "city_streets"),
        "world.remove_character": lambda: world.remove_character("ena"),
        "world.join_channel": lambda: world.join_channel("mizuki", "nightcord"),
        "world.leave_channel": lambda: world.leave_channel("mizuki", "nightcord"),
        "world.set_availability": lambda: world.set_availability("mizuki", "busy"),
        "world.set_activity": lambda: world.set_activity("mizuki", "idle"),
        "world.set_environment": lambda: world.set_environment(
            "city_streets", {"weather": "雨"}
        ),
        "world._grant_location": lambda: world._grant_location(
            "mizuki", "ena_home", "guest"
        ),
        "world._grant_channel": lambda: world._grant_channel("mizuki", "nightcord"),
    }


class FenceTests(unittest.TestCase):
    def test_every_supported_write_fails_after_the_fence(self):
        state, scheduler = _state()
        state.publish()
        state.fence("closed: test")
        before = state.to_dict()
        for name, write in _writes(state, scheduler).items():
            with self.subTest(write=name):
                with self.assertRaises(SessionFencedError):
                    write()
        self.assertEqual(state.to_dict(), before)

    def test_reads_still_work_after_the_fence(self):
        state, _scheduler = _state()
        state.fence("closed: test")
        self.assertEqual(state.world_state.location_of("mizuki"), "mizuki_home_room")
        state.to_dict()

    def test_no_new_snapshot_after_the_fence(self):
        state, _scheduler = _state()
        state.fence("closed: test")
        with self.assertRaises(SessionFencedError):
            with state.snapshot_boundary():
                pass

    def test_the_fence_waits_for_a_running_transaction_and_refuses_from_inside(self):
        state, _scheduler = _state()
        with state.atomic_commit():
            with self.assertRaises(TransactionBoundaryError):
                state.fence("from inside")
        self.assertFalse(state.fenced)

    def test_phases_only_move_forward(self):
        state, _scheduler = _state()
        self.assertEqual(state.phase, "building")
        state.publish()
        self.assertEqual(state.phase, "live")
        state.fence("closed: test")
        with self.assertRaises(SessionFencedError):
            state.publish()
        self.assertEqual(state.phase, "fenced")


class ReadOnlySnapshotTests(unittest.TestCase):
    def test_writes_inside_a_snapshot_block_fail(self):
        state, scheduler = _state()
        writes = _writes(state, scheduler)
        for name in (
            "record_error",
            "scheduler.schedule",
            "world.set_environment",
            "world.place_character",
        ):
            with self.subTest(write=name):
                with state.snapshot_boundary():
                    with self.assertRaises(SessionFencedError):
                        writes[name]()

    def test_writes_work_again_after_the_block(self):
        state, _scheduler = _state()
        with state.snapshot_boundary():
            state.to_dict()
        state.record_error("fine")
        self.assertEqual(state.last_error, "fine")


class BuildingOnlyTests(unittest.TestCase):
    def test_a_live_session_cannot_swap_archives_or_rebind(self):
        state, _scheduler = _state()
        archive = state.to_dict()
        state.publish()
        for name, action in {
            "restore_scheduler_archive": lambda: state.restore_scheduler_archive(
                archive["scheduler"]
            ),
            "restore_agency_archive": lambda: state.restore_agency_archive(
                archive["agency"]
            ),
            "restore_memory_archive": lambda: state.restore_memory_archive(
                archive["memory"]
            ),
            "attach_agency": lambda: state.attach_agency(object()),
            "attach_memory": lambda: state.attach_memory(object()),
            "attach_autonomy": lambda: state.attach_autonomy(object()),
        }.items():
            with self.subTest(action=name):
                with self.assertRaises(SessionStateError):
                    action()

    def test_a_world_state_belongs_to_one_session(self):
        state, _scheduler = _state()
        other = SessionState(session_id="s2", scene="gate", characters=["mizuki"])
        with self.assertRaises(RuntimeError):
            other.attach_world_state(state.world_state)


if __name__ == "__main__":
    unittest.main()
