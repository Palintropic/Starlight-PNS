# tests/test_event_store_recent.py — EventStore.recent_non_clock 的索引。
#
# 看板每几秒要一次"最近 N 条非时间推进事件"。索引必须跟逐条倒着扫出来的结果
# 一模一样：追加、回滚、从存档恢复之后都是。
import unittest
from datetime import datetime, timedelta

from pns.models.event import Event, EventScope, EventType
from pns.models.event_store import EventStore, EventStoreError

T0 = datetime(2026, 11, 5, 8, 0)


def _tick(n: int) -> Event:
    return Event(
        type=EventType.WORLD_TIME_ADVANCED,
        occurred_at=T0 + timedelta(minutes=n),
        scope=EventScope.PUBLIC,
        payload={"minutes": 1},
        event_id=f"tick-{n}",
    )


def _said(n: int, text: str) -> Event:
    return Event(
        type=EventType.DIALOGUE_SPOKEN,
        occurred_at=T0 + timedelta(minutes=n),
        scope=EventScope.LOCATION,
        actor_id="mizuki",
        participants=("mizuki",),
        location_id="mizuki_room",
        payload={"text": text},
        event_id=f"said-{n}-{text}",
    )


def _brute_force(store: EventStore, limit: int):
    out = []
    events = store.events()
    for seq in range(len(events) - 1, -1, -1):
        if events[seq].type is EventType.WORLD_TIME_ADVANCED:
            continue
        out.append((seq, events[seq]))
        if len(out) >= limit:
            break
    return tuple(reversed(out))


def _history():
    events = []
    for n in range(1, 41):
        events.append(_tick(n))
        if n % 7 == 0:
            events.append(_said(n, "a"))
            events.append(_said(n, "b"))
    return events


class RecentNonClockTests(unittest.TestCase):
    def test_matches_a_backwards_scan_for_every_limit(self):
        store = EventStore(_history())
        # 对照是 overview 原来那段倒着扫的循环；接口要求 limit >= 1，0 单独看
        for limit in range(1, 15):
            with self.subTest(limit=limit):
                self.assertEqual(store.recent_non_clock(limit), _brute_force(store, limit))
        self.assertEqual(store.recent_non_clock(0), ())

    def test_sequences_are_positions_in_the_whole_history(self):
        store = EventStore(_history())
        for seq, event in store.recent_non_clock(100):
            self.assertIs(store.events()[seq], event)
            self.assertEqual(store.sequence_of(event.event_id), seq)

    def test_rollback_drops_indexed_events_past_the_cut(self):
        store = EventStore(_history())
        keep = len(store)
        store._append(_tick(41))
        store._append(_said(41, "late"))
        store._append(_tick(42))
        self.assertEqual(store.recent_non_clock(1)[0][1].event_id, "said-41-late")

        store._rollback_to(keep)
        self.assertEqual(store.recent_non_clock(50), _brute_force(store, 50))
        # 回滚后还能接着追加，索引不留洞
        store._append(_said(43, "after"))
        self.assertEqual(store.recent_non_clock(50), _brute_force(store, 50))

    def test_rollback_to_the_middle_of_a_run(self):
        store = EventStore(_history())
        cut = store.sequence_of("said-21-b")  # 同一分钟两句之间切开
        store._rollback_to(cut)
        self.assertEqual(store.recent_non_clock(50), _brute_force(store, 50))
        self.assertEqual(store.recent_non_clock(1)[0][1].event_id, "said-21-a")

    def test_restored_store_rebuilds_the_index(self):
        store = EventStore(_history())
        restored = EventStore.from_dict(store.to_dict())
        self.assertEqual(
            [(seq, e.event_id) for seq, e in restored.recent_non_clock(50)],
            [(seq, e.event_id) for seq, e in store.recent_non_clock(50)],
        )

    def test_only_clock_events_gives_nothing(self):
        store = EventStore([_tick(n) for n in range(1, 10)])
        self.assertEqual(store.recent_non_clock(5), ())

    def test_first_event(self):
        self.assertIsNone(EventStore().first())
        store = EventStore(_history())
        self.assertEqual(store.first().event_id, "tick-1")

    def test_limit_must_be_a_non_negative_int(self):
        store = EventStore(_history())
        for bad in (-1, True, 1.5, "3"):
            with self.subTest(bad=bad), self.assertRaises(EventStoreError):
                store.recent_non_clock(bad)


if __name__ == "__main__":
    unittest.main()
