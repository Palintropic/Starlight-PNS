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
        # 回滚用的整体替换也是写入口：它能换掉时钟、位置、频道和授予（审查 F4）。
        "world._restore_mutable_state": lambda: world._restore_mutable_state(
            dict(world.snapshot_mutable_state(), clock=datetime(2026, 9, 27, 23, 0))
        ),
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

    def test_a_history_read_is_not_a_write_handle(self):
        # R2-F3：history_for() 交出的若是内部列表，fence 之后照样能写会话。
        state, _scheduler = _state()
        state.initialize_runtime("开场")
        state.fence("closed: test")
        before = state.to_dict()
        state.history_for("mizuki").append({"role": "assistant", "content": "late"})
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
            "world._restore_mutable_state",
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

    def test_the_static_registries_are_frozen_once_attached(self):
        # 位置图与频道表随存档走、会话期间不变；挂上会话之后它们的 add() 不能
        # 再是一条绕过栅栏的写入口。
        state, _scheduler = _state()
        world = state.world_state
        before = state.to_dict()
        location = next(iter(world.locations))
        channel = next(iter(world.channels))
        for name, action in {
            "locations.add": lambda: world.locations.add(
                type(location).from_dict(dict(location.to_dict(), location_id="new_place"))
            ),
            "channels.add": lambda: world.channels.add(
                type(channel).from_dict(dict(channel.to_dict(), channel_id="new_channel"))
            ),
        }.items():
            with self.subTest(action=name):
                with self.assertRaisesRegex(ValueError, "已经挂在会话上"):
                    action()
        self.assertEqual(state.to_dict(), before)

    def test_restores_in_any_order_cannot_publish_a_bare_acknowledgement(self):
        # R2-F2：先恢复（空的）Agency、再恢复带裸确认的投递箱，两步各自都过；
        # 拼出来的状态加载时会被拒绝，所以也不许发布成 live。
        source, scheduler = _state()
        (due,) = scheduler.advance_by(10).due
        scheduler.acknowledge(due.due_id)
        archive = source.to_dict()

        state = SessionState(session_id="s1", scene="gate", characters=["mizuki", "ena"])
        state.attach_world_state(WorldState.from_dict(archive["world_state"]))
        state.restore_agency_archive(state.agency_archive())
        state.restore_scheduler_archive(archive["scheduler"])
        with self.assertRaisesRegex(SessionStateError, "没有任何 Agency 记录"):
            state.publish()
        self.assertEqual(state.phase, "building")

    def test_a_consistent_assembly_still_publishes(self):
        state, scheduler = _state()
        scheduler.advance_by(5)
        state.publish()
        self.assertEqual(state.phase, "live")

    def test_a_world_state_belongs_to_one_session(self):
        state, _scheduler = _state()
        other = SessionState(session_id="s2", scene="gate", characters=["mizuki"])
        with self.assertRaises(RuntimeError):
            other.attach_world_state(state.world_state)


if __name__ == "__main__":
    unittest.main()
