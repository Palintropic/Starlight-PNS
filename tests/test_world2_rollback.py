# tests/test_world2_rollback.py — WORLD-2 C3：提交失败时，名单与地点图 / 频道表一起回滚。
#
# 守的线（设计 v3 §4.3；复审附录 A 的两个 probe 在 main 上分别证明了这两处会漏）：
#   1. 事务里给名单追加一人、再抛异常：名单（连同轮转位置）回到原样，引用也还是原来那个；
#   2. 事务里整体换掉地点图或频道表、再抛异常：换回旧引用，旧图本身没被动过；
#   3. 正常提交时以上改动都保留——回滚只在失败时发生；
#   4. histories / pending_corrections 已有的回滚照旧（不重写，只确认没被这次改坏）。
#
# 运行: python -m unittest discover -s tests -p test_world2_rollback.py
import unittest
from datetime import datetime

from pns.models.channel import ChannelRegistry
from pns.models.location import Connection, Location, LocationGraph
from pns.models.session import SessionState
from pns.models.world_state import WorldState


class _Injected(RuntimeError):
    pass


def _graph() -> LocationGraph:
    return LocationGraph(
        [
            Location("street", "street", access={"public": True}),
            Location(
                "home",
                "home",
                parent_id="street",
                connections=(Connection("street", 5),),
                access={"public": True},
            ),
        ]
    )


def _state() -> SessionState:
    state = SessionState("w2-rollback", "probe", ["a", "b"])
    state.attach_world_state(WorldState(clock=datetime(2026, 10, 6), locations=_graph()))
    return state


class RosterRollbackTests(unittest.TestCase):
    def test_failed_commit_restores_the_roster(self):
        state = _state()
        roster = state.characters
        with self.assertRaises(_Injected):
            with state.atomic_commit():
                state.characters.append("c")
                raise _Injected()
        self.assertEqual(state.characters, ["a", "b"])
        self.assertIs(state.characters, roster)

    def test_failed_commit_restores_a_replaced_roster(self):
        state = _state()
        roster = state.characters
        with self.assertRaises(_Injected):
            with state.atomic_commit():
                state.characters = ["a", "b", "c"]
                raise _Injected()
        self.assertIs(state.characters, roster)
        self.assertEqual(state.characters, ["a", "b"])

    def test_failed_commit_restores_the_turn_index(self):
        state = _state()
        with self.assertRaises(_Injected):
            with state.atomic_commit():
                state.characters.append("c")
                state.current_character_index = 2
                raise _Injected()
        self.assertEqual(state.current_character_index, 0)

    def test_successful_commit_keeps_the_new_roster(self):
        state = _state()
        with state.atomic_commit():
            state.characters.append("c")
        self.assertEqual(state.characters, ["a", "b", "c"])

    def test_history_slots_still_roll_back(self):
        state = _state()
        before = {cid: list(items) for cid, items in state.histories.items()}
        with self.assertRaises(_Injected):
            with state.atomic_commit():
                state.characters.append("c")
                state.histories["c"] = []
                state.pending_corrections["c"] = None
                raise _Injected()
        self.assertEqual({cid: list(items) for cid, items in state.histories.items()}, before)
        self.assertNotIn("c", state.pending_corrections)


class GraphRollbackTests(unittest.TestCase):
    def test_failed_commit_restores_the_location_graph_reference(self):
        state = _state()
        world = state.world_state
        old = world.locations
        old_dict = old.to_dict()
        with self.assertRaises(_Injected):
            with state.atomic_commit():
                world.locations = LocationGraph([Location("other", "other")])
                raise _Injected()
        self.assertIs(world.locations, old)
        self.assertEqual(world.locations.to_dict(), old_dict)

    def test_failed_commit_restores_the_channel_registry_reference(self):
        state = _state()
        world = state.world_state
        old = world.channels
        with self.assertRaises(_Injected):
            with state.atomic_commit():
                world.channels = ChannelRegistry()
                raise _Injected()
        self.assertIs(world.channels, old)

    def test_successful_commit_keeps_the_new_graph(self):
        state = _state()
        world = state.world_state
        new = LocationGraph([Location("other", "other")])
        with state.atomic_commit():
            world.locations = new
        self.assertIs(world.locations, new)

    def test_world_level_snapshot_covers_the_graph_too(self):
        # event_commit 用的是同一对快照 / 恢复，不经 SessionState 也要成立。
        world = state_world = _state().world_state
        old = state_world.locations
        snapshot = world.snapshot_mutable_state()
        world.locations = LocationGraph([Location("other", "other")])
        world._restore_mutable_state(snapshot)
        self.assertIs(world.locations, old)


if __name__ == "__main__":
    unittest.main()
