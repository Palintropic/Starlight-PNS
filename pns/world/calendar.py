# pns/world/calendar.py — 世界日历：哪一天是平日，哪一天是休息日
#
# 这个模块回答一个问题：**某一个模拟日期，在这个世界里是不是休息日。** 休息日 =
# 周六、周日、日本的国民の祝日・休日。作息表按它选用平日表还是休息日表
# （见 pns/world/rhythm.py）。
#
# 祝日不是按规则推算出来的：春分、秋分每年由内阁府在前一年二月公布，振替休日与
# 国民の休日也随之变化。所以这里放的是官方公布的那份表，原样抄录：
#
#   来源：内閣府「国民の祝日について」 https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv
#   取得：2026-10-03，文件 sha256 cec37a743c96995cdb9cb52b685c9003634682a9b0e1a640a6b9b96881fe964a
#   收录：2026-01-01 至 2027-12-31（该文件公布到 2027 年底）
#
# 收录范围之外的日期只认周末，不猜祝日。范围之外的世界会把祝日当平日过——这是
# 已知的、看得见的限制（`holidays_known`），不是悄悄的默认：每年内阁府公布次年
# 祝日后更新这张表并延长 `HOLIDAYS_KNOWN_UNTIL`。
#
# 学校的长假（暑假、寒假、春假）不在这里：那是每所学校各自的安排，不是国家日历。
#
# 纯内容：不 import 运行时、不读磁盘，import 没有副作用。
from datetime import date
from enum import Enum
from typing import Dict

# 内阁府 syukujitsu.csv 中 2026、2027 两年的全部条目，名称原样保留。
_JP_HOLIDAYS = (
    (date(2026, 1, 1), "元日"),
    (date(2026, 1, 12), "成人の日"),
    (date(2026, 2, 11), "建国記念の日"),
    (date(2026, 2, 23), "天皇誕生日"),
    (date(2026, 3, 20), "春分の日"),
    (date(2026, 4, 29), "昭和の日"),
    (date(2026, 5, 3), "憲法記念日"),
    (date(2026, 5, 4), "みどりの日"),
    (date(2026, 5, 5), "こどもの日"),
    (date(2026, 5, 6), "休日"),
    (date(2026, 7, 20), "海の日"),
    (date(2026, 8, 11), "山の日"),
    (date(2026, 9, 21), "敬老の日"),
    (date(2026, 9, 22), "休日"),
    (date(2026, 9, 23), "秋分の日"),
    (date(2026, 10, 12), "スポーツの日"),
    (date(2026, 11, 3), "文化の日"),
    (date(2026, 11, 23), "勤労感謝の日"),
    (date(2027, 1, 1), "元日"),
    (date(2027, 1, 11), "成人の日"),
    (date(2027, 2, 11), "建国記念の日"),
    (date(2027, 2, 23), "天皇誕生日"),
    (date(2027, 3, 21), "春分の日"),
    (date(2027, 3, 22), "休日"),
    (date(2027, 4, 29), "昭和の日"),
    (date(2027, 5, 3), "憲法記念日"),
    (date(2027, 5, 4), "みどりの日"),
    (date(2027, 5, 5), "こどもの日"),
    (date(2027, 7, 19), "海の日"),
    (date(2027, 8, 11), "山の日"),
    (date(2027, 9, 20), "敬老の日"),
    (date(2027, 9, 23), "秋分の日"),
    (date(2027, 10, 11), "スポーツの日"),
    (date(2027, 11, 3), "文化の日"),
    (date(2027, 11, 23), "勤労感謝の日"),
)

JP_HOLIDAYS: Dict[date, str] = dict(_JP_HOLIDAYS)
HOLIDAYS_KNOWN_FROM = date(2026, 1, 1)
HOLIDAYS_KNOWN_UNTIL = date(2027, 12, 31)


class DayKind(str, Enum):
    WEEKDAY = "weekday"
    REST_DAY = "rest_day"


def holidays_known(day: date) -> bool:
    """这一天的祝日是否在收录范围内。范围之外 `day_kind` 只认周末。"""
    return HOLIDAYS_KNOWN_FROM <= day <= HOLIDAYS_KNOWN_UNTIL


def holiday_name(day: date):
    """这一天的祝日名称（内阁府原文）；不是祝日或不在收录范围内返回 None。"""
    return JP_HOLIDAYS.get(day)


def day_kind(day: date) -> DayKind:
    """周六、周日、收录范围内的祝日是休息日；其余是平日。"""
    if not isinstance(day, date):
        raise TypeError(f"day_kind 只认 date，收到 {day!r}")
    if day.weekday() >= 5 or day in JP_HOLIDAYS:
        return DayKind.REST_DAY
    return DayKind.WEEKDAY


__all__ = [
    "DayKind",
    "HOLIDAYS_KNOWN_FROM",
    "HOLIDAYS_KNOWN_UNTIL",
    "JP_HOLIDAYS",
    "day_kind",
    "holiday_name",
    "holidays_known",
]
