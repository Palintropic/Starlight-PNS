# tests/test_rest_days.py — 平日表与休息日表（周末、日本祝日）。
#
# 1. 日历：周六周日、内阁府表里的祝日是休息日；收录范围外只认周末。
# 2. 查询：哪一天用哪张表；跨零点那一段属于它开始的那一天；下一段可能在第二天的另一张表。
# 3. 内容：分表写法必须两张都写；两张一样的表拒绝；只写一张的作息指纹不变。
# 4. 校验：平日接休息日、休息日接平日的换地方同样要走得通、来得及。
# 5. 运行时：周五照常上学，周六不去学校；周日夜里一段管到周一平日表开始。
#
# 运行: python -m unittest discover -s tests -p test_rest_days.py
import unittest
from datetime import date, datetime, timedelta, timezone

from grants_support import grant_everything
from pns.models.event import EventType
from pns.models.session import SessionState
from pns.models.world_state import ActivityKind, WorldState
from pns.runtime.autonomy.audit import ScriptedAuditor
from pns.runtime.autonomy.coordinator import AutonomousRuntime
from pns.runtime.formal_world import YOAKE_MAE, formal_session_state, rhythm_fingerprint
from pns.runtime.reload import BOUNDARY
from pns.runtime.rhythm import RhythmDirector
from pns.world.calendar import (
    HOLIDAYS_KNOWN_UNTIL,
    DayKind,
    day_kind,
    holiday_name,
    holidays_known,
)
from pns.world.channels import build_default_channel_registry
from pns.world.grants import GrantError, parse_access_grants, require_rhythm_trips_fit
from pns.world.locations import build_default_location_graph
from pns.world.rhythm import DailyRhythm, RhythmError, RhythmSegment, parse_daily_rhythm, parse_day_minute

LOCATIONS = build_default_location_graph()
CHANNELS = build_default_channel_registry()


def _seg(at, activity, location_id=None, channel_id=None):
    return RhythmSegment(
        at=parse_day_minute(at), activity=activity, location_id=location_id, channel_id=channel_id
    )


WEEKDAY = (
    _seg("01:00", ActivityKind.ONLINE_CHATTING, "kanade_home_room", "nightcord"),
    _seg("02:00", ActivityKind.RESTING, "kanade_home_room"),
    _seg("08:10", ActivityKind.STUDYING, "miyamasuzaka_girls"),
    _seg("19:00", ActivityKind.EATING, "kanade_home"),
)
REST_DAY = (
    _seg("01:00", ActivityKind.ONLINE_CHATTING, "kanade_home_room", "nightcord"),
    _seg("02:00", ActivityKind.RESTING, "kanade_home_room"),
    _seg("09:00", ActivityKind.STUDYING, "kanade_home_room"),
    _seg("19:00", ActivityKind.EATING, "kanade_home"),
)
MAFUYU = DailyRhythm(character_id="mafuyu", segments=WEEKDAY, rest_day_segments=REST_DAY)

# 2026 年 10 月：2 日周五，3 日周六，4 日周日，5 日周一，12 日周一是スポーツの日。
FRI, SAT, SUN, MON, SPORTS_DAY = (date(2026, 10, d) for d in (2, 3, 4, 5, 12))


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute)


class CalendarTests(unittest.TestCase):
    def test_weekends_are_rest_days(self):
        self.assertIs(day_kind(FRI), DayKind.WEEKDAY)
        self.assertIs(day_kind(SAT), DayKind.REST_DAY)
        self.assertIs(day_kind(SUN), DayKind.REST_DAY)
        self.assertIs(day_kind(MON), DayKind.WEEKDAY)

    def test_national_holidays_from_the_cabinet_office_table_are_rest_days(self):
        self.assertIs(day_kind(SPORTS_DAY), DayKind.REST_DAY)
        self.assertEqual(holiday_name(SPORTS_DAY), "スポーツの日")
        # 振替休日与国民の休日是表里的"休日"，同样休息。
        self.assertIs(day_kind(date(2026, 9, 22)), DayKind.REST_DAY)
        self.assertIs(day_kind(date(2027, 3, 22)), DayKind.REST_DAY)

    def test_outside_the_published_range_only_weekends_count(self):
        new_year = date(2028, 1, 3)  # 周一；2028 年的祝日还没公布
        self.assertFalse(holidays_known(new_year))
        self.assertIs(day_kind(new_year), DayKind.WEEKDAY)
        self.assertTrue(holidays_known(HOLIDAYS_KNOWN_UNTIL))


class QueryTests(unittest.TestCase):
    def test_each_day_uses_its_own_table(self):
        self.assertEqual(MAFUYU.segment_at(at(FRI, 10)).location_id, "miyamasuzaka_girls")
        self.assertEqual(MAFUYU.segment_at(at(SAT, 10)).location_id, "kanade_home_room")
        self.assertEqual(MAFUYU.segment_at(at(SPORTS_DAY, 10)).location_id, "kanade_home_room")

    def test_the_segment_across_midnight_belongs_to_the_day_it_started(self):
        # 周日 19:00 那一段一直管到周一 01:00；它的起点在周日。
        segment, start = MAFUYU.occurrence_at(at(MON, 0, 30))
        self.assertEqual(segment, REST_DAY[-1])
        self.assertEqual(start, at(SUN, 19))

    def test_the_next_segment_can_be_in_the_other_table(self):
        # 周五 19:00 之后是周六的第一段（休息日表），不是平日表的第一段。
        following, start = MAFUYU.next_after(at(FRI, 19))
        self.assertEqual((following, start), (REST_DAY[0], at(SAT, 1)))
        # 周六 02:00 之后是休息日表的 09:00，不是平日表的 08:10。
        following, start = MAFUYU.next_after(at(SAT, 2))
        self.assertEqual((following, start), (REST_DAY[2], at(SAT, 9)))
        # 周日 19:00 之后接回周一的平日表。
        following, start = MAFUYU.next_after(at(SUN, 19))
        self.assertEqual((following, start), (WEEKDAY[0], at(MON, 1)))

    def test_the_next_day_starts_from_its_own_first_segment(self):
        # 两张表的第一段不同：周五最后一段之后是周六休息日表的第一段（10:00），
        # 不是平日表的第一段（06:30）；周日之后反过来。
        weekday = (
            _seg("06:30", ActivityKind.EATING, "kanade_home"),
            _seg("08:10", ActivityKind.STUDYING, "miyamasuzaka_girls"),
            _seg("19:00", ActivityKind.RESTING, "kanade_home_room"),
        )
        rest = (
            _seg("10:00", ActivityKind.EATING, "kanade_home"),
            _seg("11:00", ActivityKind.STUDYING, "kanade_home_room"),
            _seg("21:00", ActivityKind.RESTING, "kanade_home_room"),
        )
        rhythm = DailyRhythm(character_id="mafuyu", segments=weekday, rest_day_segments=rest)
        self.assertEqual(rhythm.next_after(at(FRI, 19)), (rest[0], at(SAT, 10)))
        self.assertEqual(rhythm.next_after(at(SUN, 21)), (weekday[0], at(MON, 6, 30)))
        # 周六 08:00 仍在周五 19:00 那一段里（休息日表 10:00 才开始）。
        self.assertEqual(rhythm.occurrence_at(at(SAT, 8)), (weekday[-1], at(FRI, 19)))


class ContentTests(unittest.TestCase):
    def _parse(self, payload):
        return parse_daily_rhythm(payload, character_id="mafuyu", locations=LOCATIONS, channels=CHANNELS)

    @staticmethod
    def _yaml(table):
        return [
            {
                "at": f"{s.at // 60:02d}:{s.at % 60:02d}",
                "activity": s.activity.value,
                "location_id": s.location_id,
                "channel_id": s.channel_id,
                "source": "inferred",
            }
            for s in table
        ]

    def test_the_split_form_parses_both_tables(self):
        rhythm = self._parse({"weekday": self._yaml(WEEKDAY), "rest_day": self._yaml(REST_DAY)})
        self.assertEqual(rhythm.segments, WEEKDAY)
        self.assertEqual(rhythm.rest_day_segments, REST_DAY)

    def test_the_split_form_needs_exactly_both_tables(self):
        for payload in (
            {"weekday": self._yaml(WEEKDAY)},
            {"rest_day": self._yaml(REST_DAY)},
            {"weekday": self._yaml(WEEKDAY), "rest_day": self._yaml(REST_DAY), "holiday": []},
        ):
            with self.subTest(keys=sorted(payload)):
                with self.assertRaises(RhythmError):
                    self._parse(payload)

    def test_two_identical_tables_are_rejected(self):
        with self.assertRaises(RhythmError):
            DailyRhythm(character_id="mafuyu", segments=WEEKDAY, rest_day_segments=WEEKDAY)

    def test_a_single_table_keeps_its_fingerprint(self):
        # 只写一张表的作息，序列化与引入休息日之前一致：已采用它的世界不多出一条冲突。
        single = DailyRhythm(character_id="mafuyu", segments=WEEKDAY)
        self.assertNotIn("rest_day_segments", single.to_dict())
        self.assertNotEqual(rhythm_fingerprint(single), rhythm_fingerprint(MAFUYU))


class ChangeoverTripTests(unittest.TestCase):
    GRANTS = parse_access_grants(
        {
            "locations": [
                {"location_id": "kanade_home", "role": "household"},
                {"location_id": "kanade_home_room", "role": "household"},
                {"location_id": "miyamasuzaka_girls", "role": "student"},
            ],
            "channels": ["nightcord"],
        },
        character_id="mafuyu",
        locations=LOCATIONS,
        channels=CHANNELS,
    )

    def test_a_valid_pair_of_tables_fits(self):
        require_rhythm_trips_fit(MAFUYU, self.GRANTS, LOCATIONS)

    def test_a_changeover_without_time_to_travel_is_rejected(self):
        # 平日表最后一段在学校（23:59 起），休息日表 00:00 就要在宵崎家：
        # 两张表各自都没问题，只有"周五接周六"这一次走不及。
        weekday = WEEKDAY[:-1] + (_seg("23:59", ActivityKind.STUDYING, "miyamasuzaka_girls"),)
        weekday = tuple(s for s in weekday if s.at != parse_day_minute("08:10"))
        rest = (_seg("00:00", ActivityKind.RESTING, "kanade_home_room"),) + REST_DAY[2:]
        rhythm = DailyRhythm(character_id="mafuyu", segments=weekday, rest_day_segments=rest)
        require_rhythm_trips_fit(DailyRhythm(character_id="mafuyu", segments=rest), self.GRANTS, LOCATIONS)
        with self.assertRaises(GrantError):
            require_rhythm_trips_fit(rhythm, self.GRANTS, LOCATIONS)


def _runtime(clock):
    world = WorldState(clock=clock, locations=build_default_location_graph(), channels=build_default_channel_registry())
    grant_everything(world)
    world.place_character("mafuyu", "kanade_home_room")
    state = SessionState(session_id="s1", scene="gate", characters=["mafuyu"])
    state.attach_world_state(world)
    state.initialize_runtime("开场")
    runtime = AutonomousRuntime(state, auditor=ScriptedAuditor(), rhythm=RhythmDirector({"mafuyu": MAFUYU}))
    runtime.start()
    runtime.apply_rhythm()
    return state, runtime


def _arrivals(state):
    return [
        (e.location_id, e.occurred_at)
        for e in state.events.events()
        if e.type is EventType.CHARACTER_LOCATION_CHANGED and e.location_id in ("miyamasuzaka_girls", "kanade_home_room")
    ]


class RuntimeTests(unittest.TestCase):
    def test_friday_goes_to_school_and_saturday_stays_home(self):
        state, runtime = _runtime(at(FRI, 6))
        runtime.advance(36 * 60)  # 周五 06:00 → 周六 18:00
        schools = [when for where, when in _arrivals(state) if where == "miyamasuzaka_girls"]
        self.assertEqual(schools, [at(FRI, 8, 10)])
        self.assertEqual(state.world_state.location_of("mafuyu"), "kanade_home_room")
        self.assertIs(state.world_state.activity_of("mafuyu").kind, ActivityKind.STUDYING)
        self.assertEqual(state.world_state.activity_of("mafuyu").since, at(SAT, 9))

    def test_a_national_holiday_on_a_monday_is_a_rest_day(self):
        state, runtime = _runtime(at(date(2026, 10, 11), 20))  # スポーツの日前夜（周日）
        runtime.advance(24 * 60)  # → 10/12 20:00
        self.assertEqual(
            [where for where, _ in _arrivals(state) if where == "miyamasuzaka_girls"], []
        )

    def test_sunday_night_hands_over_to_monday_school(self):
        state, runtime = _runtime(at(SUN, 20))
        runtime.advance(14 * 60)  # → 周一 10:00
        self.assertEqual(state.world_state.location_of("mafuyu"), "miyamasuzaka_girls")
        self.assertEqual(state.world_state.activity_of("mafuyu").since, at(MON, 8, 10))


class RealContentWeekendTests(unittest.TestCase):
    """yoake-mae 的四人，用仓库里的真实作息，从周四 19:00 逐分钟走到周一 10:00。"""

    def test_the_four_follow_the_right_table_and_never_share_a_sleeping_room(self):
        registry = BOUNDARY.active()
        thursday = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)  # 东京 19:00
        state = formal_session_state(YOAKE_MAE, registry, session_id="s", wall=thursday)
        state.initialize_runtime("开场")
        runtime = AutonomousRuntime(
            state, auditor=ScriptedAuditor(), rhythm=RhythmDirector(registry.rhythms())
        )
        runtime.start()
        ws = state.world_state
        end = datetime(2026, 10, 5, 10, 0)
        at_school = {}
        while ws.clock < end:
            runtime.advance(1)
            for character_id in YOAKE_MAE.residents:
                if ws.activity_of(character_id).kind is ActivityKind.COMMUTING:
                    continue
                segment = registry.rhythm(character_id).segment_at(ws.clock)
                self.assertEqual(
                    (ws.location_of(character_id), ws.activity_of(character_id).kind),
                    (segment.location_id, segment.activity),
                    f"{ws.clock:%a %H:%M} {character_id}",
                )
                if ws.location_of(character_id) in ("miyamasuzaka_girls", "kamiyama_high"):
                    at_school.setdefault(character_id, set()).add(ws.clock.date())
            # ena 审查 CONTENT-2 §3 疑点 3，两个方向都要成立，平日与休息日都要成立。
            for sleeper, other in (("mafuyu", "kanade"), ("kanade", "mafuyu")):
                if ws.activity_of(sleeper).kind is ActivityKind.RESTING:
                    self.assertNotEqual(
                        ws.location_of(other), ws.location_of(sleeper),
                        f"{ws.clock:%a %H:%M} {other} 进了 {sleeper} 睡觉的房间",
                    )
        weekdays = {date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5)}
        for character_id, days in at_school.items():
            with self.subTest(character_id=character_id):
                self.assertTrue(days <= weekdays, f"{character_id} 休息日去了学校：{sorted(days)}")
        self.assertIn(date(2026, 10, 2), at_school["mafuyu"])
        self.assertIn(date(2026, 10, 5), at_school["mafuyu"])


if __name__ == "__main__":
    unittest.main()
