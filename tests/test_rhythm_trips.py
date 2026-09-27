# tests/test_rhythm_trips.py — 作息的行程与频道（WORLD-1 设计 §4、§12.5–§12.6）。
#
# 内容侧（构建内容快照时就拒绝）：
#   1. 作息声明的频道必须存在，而且角色是它的成员；
#   2. 一整天（含跨零点）的每一次换地方，路都走得通、时间排得下；
#   3. commuting 不能写进作息表：路上的时间由行程算出来。
# 运行时（逐边界推进）：
#   4. 按时出门、准点到；每一跳只到相邻地点，间隔不少于路程；晚出门就晚到；
#   5. 段内别人做的决定压过行程；走不到的段记一次处置，之后不再重试；
#   6. 频道进出由作息管理，只在段开始时发生；
#   7. next_boundary_after 与规划同源：给出的每个时刻都真的会发生点什么。
#
# 运行: python -m unittest discover -s tests -p test_rhythm_trips.py
import unittest
from datetime import datetime, timedelta

from grants_support import grant_everything
from pns.models.event import Event, EventScope, EventType
from pns.models.session import SessionState
from pns.models.world_state import ActivityKind, WorldState
from pns.runtime.autonomy.audit import ScriptedAuditor
from pns.runtime.autonomy.coordinator import AutonomousRuntime, AutonomyError
from pns.runtime.event_commit import commit_session_event
from pns.runtime.rhythm import RhythmDirector
from pns.world.channels import build_default_channel_registry
from pns.world.grants import (
    GrantError,
    parse_access_grants,
    require_rhythm_channels_joinable,
    require_rhythm_trips_fit,
)
from pns.world.locations import build_default_location_graph
from pns.world.rhythm import (
    DailyRhythm,
    RhythmError,
    RhythmSegment,
    parse_daily_rhythm,
    parse_day_minute,
)

LOCATIONS = build_default_location_graph()
CHANNELS = build_default_channel_registry()

MIZUKI_GRANTS = parse_access_grants(
    {
        "locations": [
            {"location_id": "mizuki_home", "role": "household"},
            {"location_id": "mizuki_home_room", "role": "household"},
            {"location_id": "kamiyama_high", "role": "student"},
            {"location_id": "clothing_store_floor", "role": "staff"},
        ],
        "channels": ["nightcord"],
    },
    character_id="mizuki",
    locations=LOCATIONS,
    channels=CHANNELS,
)


def _rhythm(*entries):
    return parse_daily_rhythm(
        [dict(entry, source="inferred") for entry in entries],
        character_id="mizuki",
        locations=LOCATIONS,
        channels=CHANNELS,
    )


class ContentChannelTests(unittest.TestCase):
    def test_an_unknown_channel_is_rejected_at_parse(self):
        with self.assertRaises(RhythmError):
            _rhythm({"at": "01:00", "activity": "online_chatting", "channel_id": "discord"})

    def test_a_non_member_channel_is_rejected_at_build(self):
        rhythm = _rhythm(
            {"at": "01:00", "activity": "online_chatting", "channel_id": "nightcord"},
            {"at": "04:00", "activity": "resting"},
        )
        require_rhythm_channels_joinable(rhythm, MIZUKI_GRANTS)
        with self.assertRaises(GrantError):
            require_rhythm_channels_joinable(rhythm, None)

    def test_segments_differing_only_by_channel_are_distinct(self):
        _rhythm(
            {"at": "01:00", "activity": "online_chatting", "channel_id": "nightcord"},
            {"at": "04:00", "activity": "online_chatting"},
        )

    def test_commuting_is_not_an_authored_activity(self):
        with self.assertRaises(RhythmError):
            _rhythm({"at": "08:00", "activity": "commuting"})


class ContentTripFeasibilityTests(unittest.TestCase):
    def test_the_shipped_mizuki_day_fits(self):
        rhythm = _rhythm(
            {"at": "01:00", "activity": "online_chatting", "location_id": "mizuki_home_room"},
            {"at": "04:00", "activity": "resting", "location_id": "mizuki_home_room"},
            {"at": "12:00", "activity": "studying", "location_id": "kamiyama_high"},
            {"at": "15:00", "activity": "working_part_time", "location_id": "clothing_store_floor"},
            {"at": "19:00", "activity": "idle", "location_id": "mizuki_home_room"},
        )
        require_rhythm_trips_fit(rhythm, MIZUKI_GRANTS, LOCATIONS)

    def test_a_trip_longer_than_the_gap_is_rejected(self):
        # 房间 → 教室要 16 分钟，两段之间只有 10 分钟。
        rhythm = _rhythm(
            {"at": "07:50", "activity": "idle", "location_id": "mizuki_home_room"},
            {"at": "08:00", "activity": "studying", "location_id": "kamiyama_high"},
        )
        with self.assertRaises(GrantError) as caught:
            require_rhythm_trips_fit(rhythm, MIZUKI_GRANTS, LOCATIONS)
        self.assertIn("16", str(caught.exception))

    def test_the_wrap_around_midnight_is_checked_too(self):
        # 23:55 在学校，次日 00:05 要回到房间：跨零点只有 10 分钟。
        rhythm = _rhythm(
            {"at": "00:05", "activity": "resting", "location_id": "mizuki_home_room"},
            {"at": "23:55", "activity": "studying", "location_id": "kamiyama_high"},
        )
        with self.assertRaises(GrantError):
            require_rhythm_trips_fit(rhythm, MIZUKI_GRANTS, LOCATIONS)

    def test_an_unlocated_segment_keeps_the_previous_place(self):
        # 07:50 没写地点 = 仍在房间；到 08:00 的学校只有 10 分钟，照样排不下。
        rhythm = _rhythm(
            {"at": "07:00", "activity": "resting", "location_id": "mizuki_home_room"},
            {"at": "07:50", "activity": "idle"},
            {"at": "08:00", "activity": "studying", "location_id": "kamiyama_high"},
        )
        with self.assertRaises(GrantError):
            require_rhythm_trips_fit(rhythm, MIZUKI_GRANTS, LOCATIONS)

    def test_a_route_only_through_forbidden_places_is_rejected(self):
        # 没有学生授予：教室根本进不去，路也就不存在。
        no_school = parse_access_grants(
            {"locations": [{"location_id": "mizuki_home_room", "role": "household"},
                           {"location_id": "mizuki_home", "role": "household"}]},
            character_id="mizuki",
            locations=LOCATIONS,
            channels=CHANNELS,
        )
        rhythm = _rhythm(
            {"at": "07:00", "activity": "resting", "location_id": "mizuki_home_room"},
            {"at": "12:00", "activity": "studying", "location_id": "kamiyama_high"},
        )
        with self.assertRaises(GrantError):
            require_rhythm_trips_fit(rhythm, no_school, LOCATIONS)


def _segment(at, activity, location_id=None, channel_id=None):
    return RhythmSegment(
        at=parse_day_minute(at),
        activity=activity,
        location_id=location_id,
        channel_id=channel_id,
    )


SCHOOL_DAY = DailyRhythm(
    character_id="mizuki",
    segments=(
        _segment("01:00", ActivityKind.ONLINE_CHATTING, "mizuki_home_room", "nightcord"),
        _segment("04:00", ActivityKind.RESTING, "mizuki_home_room"),
        _segment("12:00", ActivityKind.STUDYING, "kamiyama_high"),
        _segment("15:00", ActivityKind.WORKING_PART_TIME, "clothing_store_floor"),
        _segment("19:00", ActivityKind.IDLE, "mizuki_home_room"),
    ),
)


def _runtime(clock, rhythm=SCHOOL_DAY, *, home_only=False, place="mizuki_home_room", align=True):
    world = WorldState(
        clock=clock,
        locations=build_default_location_graph(),
        channels=build_default_channel_registry(),
    )
    if home_only:
        world._grant_location("mizuki", "mizuki_home_room", "household")
        world._grant_location("mizuki", "mizuki_home", "household")
    else:
        grant_everything(world)
    world.place_character("mizuki", place)
    state = SessionState(session_id="s1", scene="gate", characters=["mizuki"])
    state.attach_world_state(world)
    state.initialize_runtime("开场")
    runtime = AutonomousRuntime(
        state, auditor=ScriptedAuditor(), rhythm=RhythmDirector({"mizuki": rhythm})
    )
    runtime.start()
    if align:
        # 跟正式世界的 bootstrap 一样：开局就按作息对齐（见 test_daily_rhythm._rig）。
        runtime.apply_rhythm()
    return state, runtime


def _of(state, kind):
    return [event for event in state.events.events() if event.type is kind]


def t(hour, minute=0, day=27):
    return datetime(2026, 9, day, hour, minute)


class TripRuntimeTests(unittest.TestCase):
    def test_on_time_trip_departs_and_arrives_exactly(self):
        state, runtime = _runtime(t(11, 30))
        runtime.advance(40)  # → 12:10
        commuting = [
            e for e in _of(state, EventType.CHARACTER_ACTIVITY_CHANGED)
            if e.payload["activity"] == "commuting"
        ]
        self.assertEqual([e.occurred_at for e in commuting], [t(11, 44)])
        hops = _of(state, EventType.CHARACTER_LOCATION_CHANGED)
        self.assertEqual(
            [(e.location_id, e.occurred_at) for e in hops],
            [
                ("mizuki_home", t(11, 45)),
                ("city_streets", t(11, 57)),
                ("kamiyama_high_gate", t(11, 58)),
                ("kamiyama_high", t(12, 0)),
            ],
        )
        self.assertIs(state.world_state.activity_of("mizuki").kind, ActivityKind.STUDYING)
        self.assertEqual(state.world_state.activity_of("mizuki").since, t(12, 0))

    def test_a_late_start_arrives_late_without_compressing_the_road(self):
        # 世界在出发时刻之后才开始走：从现在出发，每一跳照样要走满路程。
        state, runtime = _runtime(t(11, 50), align=False)
        runtime.advance(30)  # → 12:20
        hops = _of(state, EventType.CHARACTER_LOCATION_CHANGED)
        departed = next(
            e.occurred_at for e in _of(state, EventType.CHARACTER_ACTIVITY_CHANGED)
            if e.payload["activity"] == "commuting"
        )
        self.assertEqual(departed, t(11, 51))  # 逾期就在下一分钟出门
        self.assertEqual(hops[-1].location_id, "kamiyama_high")
        self.assertEqual(hops[-1].occurred_at, departed + timedelta(minutes=16))

    def test_every_hop_is_adjacent_and_takes_its_time(self):
        state, runtime = _runtime(t(11, 0))
        runtime.advance(9 * 60)  # 走完上学、打工、回家三趟
        graph = state.world_state.locations
        position, since = "mizuki_home_room", None
        for hop in _of(state, EventType.CHARACTER_LOCATION_CHANGED):
            minutes = graph.travel_minutes(position, hop.location_id)
            self.assertIsNotNone(minutes, f"{position} → {hop.location_id} 是瞬移")
            if since is not None and hop.provenance["trip_leg"]:
                self.assertGreaterEqual(hop.occurred_at - since, timedelta(minutes=minutes))
            position, since = hop.location_id, hop.occurred_at
        self.assertEqual(position, "mizuki_home_room")

    def test_a_move_by_someone_else_mid_trip_abandons_the_trip(self):
        state, runtime = _runtime(t(11, 30))
        runtime.advance(20)  # 11:50：走在 city_streets 之前
        self.assertEqual(state.world_state.location_of("mizuki"), "mizuki_home")
        commit_session_event(
            state,
            Event(
                event_id="agency-move",
                type=EventType.CHARACTER_LOCATION_CHANGED,
                occurred_at=state.world_state.clock,
                scope=EventScope.LOCATION,
                actor_id="mizuki",
                location_id="mizuki_home_room",
            ),
        )
        runtime.advance(60)  # 12:50
        self.assertEqual(state.world_state.location_of("mizuki"), "mizuki_home_room")

    def _commuting_key(self, state):
        commuting = [
            e for e in _of(state, EventType.CHARACTER_ACTIVITY_CHANGED)
            if e.payload["activity"] == "commuting"
        ]
        return commuting[-1].provenance["segment_key"]

    def _idle(self, state, provenance):
        return Event(
            event_id="operator-idle",
            type=EventType.CHARACTER_ACTIVITY_CHANGED,
            occurred_at=state.world_state.clock,
            scope=EventScope.PRIVATE,
            actor_id="mizuki",
            payload={"activity": ActivityKind.IDLE.value},
            provenance=provenance,
        )

    def test_an_operator_event_cannot_borrow_the_rhythm_identity(self):
        # 审查 F6：外部事件带上本段的 segment_key，作息就会把它当成自己的决定、
        # 把行程继续走下去。受支持的外部入口直接拒绝作息的 provenance。
        state, runtime = _runtime(t(11, 30))
        runtime.advance(14)  # 11:44 出门
        key = self._commuting_key(state)
        before = len(state.events)
        for label, provenance in {
            "segment_key": {"segment_key": key},
            "kind": {"kind": "daily_rhythm"},
            "trip_leg": {"trip_leg": 0},
        }.items():
            with self.subTest(label):
                with self.assertRaises(AutonomyError):
                    runtime.commit_external_event(self._idle(state, provenance))
        self.assertEqual(len(state.events), before)

    def test_only_rhythm_events_carry_a_segment_key(self):
        # 入口之外（直接提交）带了 key、却不是作息事件的，仍然是外部决定：
        # 行程停下，角色留在原地。
        state, runtime = _runtime(t(11, 30))
        runtime.advance(14)  # 11:44 出门，还在 mizuki_home_room
        key = self._commuting_key(state)
        commit_session_event(state, self._idle(state, {"segment_key": key}))
        runtime.advance(1)  # 11:45：作息自己的话这一刻走到 mizuki_home
        self.assertEqual(state.world_state.location_of("mizuki"), "mizuki_home_room")
        self.assertIs(state.world_state.activity_of("mizuki").kind, ActivityKind.IDLE)

    def test_an_unreachable_segment_is_recorded_once_and_not_retried(self):
        # 没有学生授予：到不了学校。这一段沉默，记一次处置，不瞬移、不反复记。
        state, runtime = _runtime(t(11, 30), home_only=True)
        runtime.advance(60)  # → 12:30
        self.assertEqual(state.world_state.location_of("mizuki"), "mizuki_home_room")
        self.assertEqual(
            state.rhythm_dispositions, {"mizuki:12:00@2026-09-27T12:00:00"}
        )
        self.assertEqual(_of(state, EventType.CHARACTER_LOCATION_CHANGED), [])
        runtime.advance(60)
        self.assertEqual(len(state.rhythm_dispositions), 1)
        restored = SessionState.from_dict(state.to_dict())
        self.assertEqual(restored.rhythm_dispositions, state.rhythm_dispositions)

    def test_a_restore_mid_trip_continues_from_where_she_is(self):
        state, runtime = _runtime(t(11, 30))
        runtime.advance(20)  # 11:50 在 mizuki_home，路走了一跳
        restored = SessionState.from_dict(state.to_dict())
        again = AutonomousRuntime(
            restored, auditor=ScriptedAuditor(), rhythm=RhythmDirector({"mizuki": SCHOOL_DAY})
        )
        again.start()
        again.advance(20)  # 12:10
        hops = _of(restored, EventType.CHARACTER_LOCATION_CHANGED)
        self.assertEqual(
            [e.location_id for e in hops],
            ["mizuki_home", "city_streets", "kamiyama_high_gate", "kamiyama_high"],
        )
        commuting = [
            e for e in _of(restored, EventType.CHARACTER_ACTIVITY_CHANGED)
            if e.payload["activity"] == "commuting"
        ]
        self.assertEqual(len(commuting), 1, "恢复之后不会再出一次门")


class ChannelRuntimeTests(unittest.TestCase):
    def test_nightcord_is_joined_at_one_and_left_at_four(self):
        state, runtime = _runtime(t(0, 30))
        runtime.advance(4 * 60)  # → 04:30
        joins = _of(state, EventType.PRESENCE_JOINED_CHANNEL)
        leaves = _of(state, EventType.PRESENCE_LEFT_CHANNEL)
        self.assertEqual([(e.channel_id, e.occurred_at) for e in joins], [("nightcord", t(1))])
        self.assertEqual([(e.channel_id, e.occurred_at) for e in leaves], [("nightcord", t(4))])
        self.assertFalse(state.world_state.is_in_channel("mizuki", "nightcord"))

    def test_the_rhythm_does_not_touch_channels_it_does_not_manage(self):
        rhythm = DailyRhythm(
            character_id="mizuki",
            segments=(
                _segment("08:00", ActivityKind.IDLE, "mizuki_home_room"),
                _segment("20:00", ActivityKind.RESTING, "mizuki_home_room"),
            ),
        )
        state, runtime = _runtime(t(7, 30), rhythm)
        state.world_state.join_channel("mizuki", "nightcord")
        runtime.advance(60)
        self.assertTrue(state.world_state.is_in_channel("mizuki", "nightcord"))


class BoundaryTests(unittest.TestCase):
    def test_the_next_boundaries_are_departure_hops_and_segment_starts(self):
        state, runtime = _runtime(t(11, 30))
        director = runtime.rhythm
        seen = []
        for _ in range(6):
            boundary = director.next_boundary_after(
                state.world_state, state.events, dispositions=state.rhythm_dispositions
            )
            seen.append(boundary)
            runtime.advance(int((boundary - state.world_state.clock).total_seconds() // 60))
        self.assertEqual(
            seen, [t(11, 44), t(11, 45), t(11, 57), t(11, 58), t(12, 0), t(14, 48)]
        )

    def test_every_boundary_step_commits_something(self):
        # 同源：边界不是空跑。每一个非终点的时钟步都真的提交了作息事件。
        state, runtime = _runtime(t(11, 30))
        report = runtime.advance(3 * 60)  # → 14:30；15:00 那趟 14:48 才出发
        # 12:00 那一趟：出门（commuting）+ 四跳 + 开始上课。
        self.assertEqual(len(report["rhythm_events"]), 1 + 4 + 1)
        steps = _of(state, EventType.WORLD_TIME_ADVANCED)
        # time_advanced 盖的是推进**之前**的时刻。各步依次推进到：11:44 出门、
        # 11:45/11:57/11:58/12:00 四跳（12:00 同时开始上课），最后一步到 14:30。
        self.assertEqual(
            [step.occurred_at for step in steps],
            [t(11, 30), t(11, 44), t(11, 45), t(11, 57), t(11, 58), t(12, 0)],
        )
        self.assertEqual(state.world_state.clock, t(14, 30))


if __name__ == "__main__":
    unittest.main()
