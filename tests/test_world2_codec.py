# tests/test_world2_codec.py — WORLD-2 C4：存进世界历史的作息编码，以及日历指纹。
#
# 守的线（设计 v3 §4.5、复审 F3）：
#   1. 内容包里每一份作息（单表的 25 時、两表的 MMJ）都能编码、解码回同一个值，指纹不变；
#   2. 任何不是 encode_rhythm 亲手写出的形状都拒绝：缺键、多键、版本不对、段的键不对、
#      顺序被打乱、at 写成整数、未知活动、属于别人、YAML 写法；
#   3. 三组跨表日期（周五夜→周六、祝日前夜→祝日、跨午夜还没到当天第一段）上，解码出来的
#      作息与原作息给出完全相同的当前段、起点和下一段——codec 往返之外，日期行为也一致；
#   4. 日历指纹确定，并且对祝日表、收录范围、规则版本、周末定义的任何改动都敏感。
#
# 运行: python -m unittest discover -s tests -p test_world2_codec.py
import copy
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

from pns.runtime.formal_world import rhythm_fingerprint
from pns.runtime.reload import BOUNDARY
from pns.world import calendar
from pns.world.rhythm import (
    RHYTHM_CODEC_SCHEMA,
    RhythmError,
    decode_rhythm,
    encode_rhythm,
)

MMJ = ("airi", "minori", "haruka", "shizuku")


def _rhythms():
    return BOUNDARY.active().rhythms()


class RoundTripTests(unittest.TestCase):
    def test_every_pack_rhythm_round_trips_with_the_same_fingerprint(self):
        rhythms = _rhythms()
        self.assertGreaterEqual(len(rhythms), 8)
        for character_id, rhythm in rhythms.items():
            with self.subTest(character_id=character_id):
                encoded = encode_rhythm(rhythm)
                self.assertEqual(encoded["schema"], RHYTHM_CODEC_SCHEMA)
                decoded = decode_rhythm(copy.deepcopy(encoded), character_id=character_id)
                self.assertEqual(decoded, rhythm)
                self.assertEqual(rhythm_fingerprint(decoded), rhythm_fingerprint(rhythm))

    def test_two_table_rhythms_keep_both_tables(self):
        rhythms = _rhythms()
        for character_id in MMJ:
            with self.subTest(character_id=character_id):
                decoded = decode_rhythm(encode_rhythm(rhythms[character_id]))
                self.assertIsNotNone(decoded.rest_day_segments)
                self.assertEqual(
                    decoded.rest_day_segments, rhythms[character_id].rest_day_segments
                )


class RejectionTests(unittest.TestCase):
    def setUp(self):
        self.encoded = encode_rhythm(_rhythms()["airi"])

    def _mutated(self, mutate):
        payload = copy.deepcopy(self.encoded)
        mutate(payload)
        return payload

    def assert_rejected(self, payload, **kwargs):
        with self.assertRaises(RhythmError):
            decode_rhythm(payload, **kwargs)

    def test_rejects_wrong_or_missing_schema(self):
        self.assert_rejected(self._mutated(lambda p: p.pop("schema")))
        self.assert_rejected(self._mutated(lambda p: p.update(schema="pns.daily_rhythm/2")))

    def test_rejects_extra_top_level_keys(self):
        self.assert_rejected(self._mutated(lambda p: p.update(note="x")))

    def test_rejects_a_segment_with_missing_or_extra_keys(self):
        self.assert_rejected(self._mutated(lambda p: p["segments"][0].pop("source")))
        self.assert_rejected(self._mutated(lambda p: p["segments"][0].update(note="x")))

    def test_rejects_shuffled_segments(self):
        # DailyRhythm 会悄悄排好序；那就不再是写进去的那一份了。
        self.assert_rejected(self._mutated(lambda p: p["segments"].reverse()))

    def test_rejects_an_integer_minute(self):
        self.assert_rejected(self._mutated(lambda p: p["segments"][0].update(at=0)))

    def test_rejects_unknown_activity(self):
        self.assert_rejected(
            self._mutated(lambda p: p["segments"][1].update(activity="dancing"))
        )

    def test_rejects_someone_elses_rhythm(self):
        self.assert_rejected(copy.deepcopy(self.encoded), character_id="minori")

    def test_rejects_the_yaml_shape(self):
        rhythm = _rhythms()["airi"]
        yaml_shape = {
            "weekday": [s.to_dict() for s in rhythm.segments],
            "rest_day": [s.to_dict() for s in rhythm.rest_day_segments],
        }
        self.assert_rejected(yaml_shape)

    def test_rejects_non_mapping(self):
        self.assert_rejected([])
        self.assert_rejected(None)


class DateBehaviourTests(unittest.TestCase):
    """解码后的作息在跨表的日子上与原作息逐分钟一致。"""

    # (起点, 跨度)：周五夜→周六；周日夜→10-12 祝日→祝日夜→平日；
    # 平日表第一段之前（00:00–第一段）属于前一天最后一段。
    WINDOWS = (
        (datetime(2026, 10, 9, 20, 0), timedelta(hours=14)),
        (datetime(2026, 10, 11, 20, 0), timedelta(hours=38)),
        (datetime(2026, 10, 14, 0, 0), timedelta(hours=8)),
    )

    def test_decoded_rhythms_behave_identically_across_tables(self):
        rhythms = _rhythms()
        for character_id, rhythm in rhythms.items():
            decoded = decode_rhythm(encode_rhythm(rhythm))
            for start, span in self.WINDOWS:
                clock = start
                while clock < start + span:
                    with self.subTest(character_id=character_id, at=clock.isoformat()):
                        original = rhythm.occurrence_at(clock)
                        self.assertEqual(decoded.occurrence_at(clock), original)
                        self.assertEqual(
                            decoded.next_after(original[1]), rhythm.next_after(original[1])
                        )
                    clock += timedelta(minutes=15)

    def test_the_windows_really_switch_tables(self):
        # 不然上面那条在一张表上跑也会通过。
        airi = _rhythms()["airi"]
        self.assertIs(airi.table_for(date(2026, 10, 9)), airi.segments)
        self.assertIs(airi.table_for(date(2026, 10, 10)), airi.rest_day_segments)
        self.assertIs(airi.table_for(date(2026, 10, 12)), airi.rest_day_segments)
        self.assertIs(airi.table_for(date(2026, 10, 13)), airi.segments)


class CalendarFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.base = calendar.calendar_fingerprint()

    def test_is_deterministic(self):
        self.assertEqual(calendar.calendar_fingerprint(), self.base)
        self.assertEqual(len(self.base), 64)

    def test_changes_with_the_holiday_table(self):
        with patch.dict(calendar.JP_HOLIDAYS, {date(2026, 12, 24): "x"}):
            self.assertNotEqual(calendar.calendar_fingerprint(), self.base)
        with patch.dict(calendar.JP_HOLIDAYS, {date(2026, 10, 12): "改名"}):
            self.assertNotEqual(calendar.calendar_fingerprint(), self.base)

    def test_changes_with_the_known_range(self):
        with patch.object(calendar, "HOLIDAYS_KNOWN_UNTIL", date(2028, 12, 31)):
            self.assertNotEqual(calendar.calendar_fingerprint(), self.base)
        with patch.object(calendar, "HOLIDAYS_KNOWN_FROM", date(2025, 1, 1)):
            self.assertNotEqual(calendar.calendar_fingerprint(), self.base)

    def test_changes_with_the_rules(self):
        with patch.object(calendar, "CALENDAR_RULES_VERSION", 2):
            self.assertNotEqual(calendar.calendar_fingerprint(), self.base)
        with patch.object(calendar, "REST_WEEKDAYS", frozenset({6})):
            self.assertNotEqual(calendar.calendar_fingerprint(), self.base)

    def test_rest_weekdays_drive_day_kind(self):
        # 指纹覆盖的周末定义就是 day_kind 真正用的那一份。
        saturday = date(2026, 10, 10)
        self.assertIs(calendar.day_kind(saturday), calendar.DayKind.REST_DAY)
        with patch.object(calendar, "REST_WEEKDAYS", frozenset({6})):
            self.assertIs(calendar.day_kind(saturday), calendar.DayKind.WEEKDAY)


if __name__ == "__main__":
    unittest.main()
