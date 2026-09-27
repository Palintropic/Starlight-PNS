# tests/test_clock_anchor.py — 模拟时间与现实时间的锚点（WORLD-1 设计 §2、§6）。
#
# 守的线：
#   1. 换算：anchor_now = sim_epoch + (wall - wall_epoch) × rate，时钟步目标向下取整；
#   2. 换倍率不跳、不丢秒；
#   3. 锚点随存档往返，只在事务里改、随事务回滚，没有认知时间线就没有锚点；
#   4. 运维账本的变化（Start、处置、锚点）让世界变脏。
#
# 运行: python -m unittest discover -s tests -p test_clock_anchor.py
import unittest
from datetime import datetime, timedelta, timezone

from grants_support import grant_everything
from pns.models.clock_anchor import ClockAnchor, ClockAnchorError
from pns.models.cognition import CognitionTimeline
from pns.models.session import SessionState, SessionStateError
from pns.models.world_state import WorldState
from pns.runtime.persistence.lifecycle import _fingerprint
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph

SIM = datetime(2026, 9, 27, 19, 0)
WALL = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)


def _state(timeline=True):
    world = WorldState(
        clock=SIM,
        locations=build_default_location_graph(),
        channels=build_default_channel_registry(),
    )
    grant_everything(world)
    world.place_character("mizuki", "mizuki_home_room")
    state = SessionState(session_id="s1", scene="gate", characters=["mizuki"])
    state.attach_world_state(world)
    if timeline:
        with state.atomic_commit():
            state.set_cognition(CognitionTimeline.open(log_length=0, sim=SIM, wall="w"))
    return state


class ConversionTests(unittest.TestCase):
    def test_one_to_one(self):
        anchor = ClockAnchor(SIM, WALL)
        self.assertEqual(anchor.sim_at(WALL + timedelta(minutes=90)), SIM + timedelta(minutes=90))
        self.assertEqual(
            anchor.minute_at(WALL + timedelta(minutes=5, seconds=59)),
            SIM + timedelta(minutes=5),
        )

    def test_a_wall_clock_before_the_epoch_gives_an_earlier_minute(self):
        # 现实时钟被往回拨：换算结果早于锚点，由 worker 判成 wall_clock_behind。
        anchor = ClockAnchor(SIM, WALL)
        self.assertEqual(anchor.minute_at(WALL - timedelta(minutes=30)), SIM - timedelta(minutes=30))

    def test_rate_and_rebase_keep_the_seconds(self):
        anchor = ClockAnchor(SIM, WALL, rate=60)
        later = WALL + timedelta(seconds=30, milliseconds=500)
        self.assertEqual(anchor.sim_at(later), SIM + timedelta(minutes=30, seconds=30))
        rebased = anchor.rebased(later, 1)
        self.assertEqual(rebased.sim_epoch, SIM + timedelta(minutes=30, seconds=30))
        self.assertEqual(
            rebased.minute_at(later + timedelta(seconds=30)), SIM + timedelta(minutes=31)
        )

    def test_wall_time_is_normalised_to_utc(self):
        tokyo = timezone(timedelta(hours=9))
        anchor = ClockAnchor(SIM, WALL.astimezone(tokyo))
        self.assertEqual(anchor.wall_epoch, WALL)
        self.assertIs(anchor.wall_epoch.tzinfo, timezone.utc)

    def test_bad_anchors_are_refused(self):
        for label, make in {
            "aware sim": lambda: ClockAnchor(SIM.replace(tzinfo=timezone.utc), WALL),
            "naive wall": lambda: ClockAnchor(SIM, WALL.replace(tzinfo=None)),
            "zero rate": lambda: ClockAnchor(SIM, WALL, 0),
            "negative rate": lambda: ClockAnchor(SIM, WALL, -1),
            "bool rate": lambda: ClockAnchor(SIM, WALL, True),
            "huge rate": lambda: ClockAnchor(SIM, WALL, 1e9),
        }.items():
            with self.subTest(label):
                with self.assertRaises(ClockAnchorError):
                    make()


class SessionAnchorTests(unittest.TestCase):
    def test_it_round_trips_with_the_archive(self):
        state = _state()
        anchor = ClockAnchor(SIM + timedelta(seconds=17), WALL, 1)
        with state.atomic_commit():
            state.set_anchor(anchor)
        restored = SessionState.from_dict(state.to_dict())
        self.assertEqual(restored.anchor, anchor)

    def test_it_only_changes_inside_a_transaction_and_rolls_back(self):
        state = _state()
        with self.assertRaises(SessionStateError):
            state.set_anchor(ClockAnchor(SIM, WALL))
        with self.assertRaises(RuntimeError):
            with state.atomic_commit():
                state.set_anchor(ClockAnchor(SIM, WALL))
                raise RuntimeError("step failed")
        self.assertIsNone(state.anchor)

    def test_no_anchor_without_a_timeline(self):
        state = _state(timeline=False)
        with self.assertRaises(SessionStateError):
            with state.atomic_commit():
                state.set_anchor(ClockAnchor(SIM, WALL))
        payload = _state().to_dict()
        payload["cognition"] = None
        payload["anchor"] = ClockAnchor(SIM, WALL).to_dict()
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(payload)

    def test_a_tampered_anchor_is_refused(self):
        payload = _state().to_dict()
        payload["anchor"] = {"sim_epoch": SIM.isoformat(), "wall_epoch": "yesterday", "rate": 1}
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(payload)


class OperationsMakeTheWorldDirtyTests(unittest.TestCase):
    def test_every_ledger_change_changes_the_fingerprint(self):
        # §6：只按了一次 Start、只记了一段处置、只换了锚点，世界都算变过。
        state = _state()
        changes = {
            "timeline": lambda: state.set_cognition(
                state.cognition.started(log_length=0, sim=SIM, wall="w", run_allowance=3)
            ),
            "disposition": lambda: state.add_rhythm_dispositions({"mizuki:12:00@x"}),
            "anchor": lambda: state.set_anchor(ClockAnchor(SIM, WALL)),
        }
        for label, change in changes.items():
            with self.subTest(label):
                before = _fingerprint(state)
                with state.atomic_commit():
                    change()
                self.assertNotEqual(_fingerprint(state), before)


if __name__ == "__main__":
    unittest.main()
