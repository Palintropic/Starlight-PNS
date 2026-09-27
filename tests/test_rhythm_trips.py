# tests/test_rhythm_trips.py — 作息的行程与频道（WORLD-1 设计 §4、§12.5–§12.6）。
#
# 本文件先守内容侧（构建内容快照时就拒绝）：
#   1. 作息声明的频道必须存在，而且角色是它的成员；
#   2. 一整天（含跨零点）的每一次换地方，路都走得通、时间排得下；
#   3. commuting 不能写进作息表：路上的时间由行程算出来。
#
# 运行: python -m unittest discover -s tests -p test_rhythm_trips.py
import unittest

from pns.world.channels import build_default_channel_registry
from pns.world.grants import (
    GrantError,
    parse_access_grants,
    require_rhythm_channels_joinable,
    require_rhythm_trips_fit,
)
from pns.world.locations import build_default_location_graph
from pns.world.rhythm import RhythmError, parse_daily_rhythm

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


if __name__ == "__main__":
    unittest.main()
