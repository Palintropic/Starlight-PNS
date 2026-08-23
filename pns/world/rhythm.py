# pns/world/rhythm.py — 作者写下来的日常作息表
#
# 这个模块回答一个问题：**内容作者说"这个角色一天是怎么过的"，那份数据长什么样。**
#
# 它不回答：现在几点（WorldState 的时钟）、这条作息什么时候被应用（调度器推进
# 时间之后，见 pns/runtime/rhythm.py）、应用之后世界怎么变（事件提交边界）。
# 所以这里没有任何可变状态，也没有任何写世界的能力 —— 它只是一张被校验过的表。
#
# 四条硬约束：
#
#   1. **一天被完全覆盖，没有缝。** 一段从它的起始分钟一直管到下一段起始那一刻，
#      最后一段跨过零点接回第一段。所以"此刻属于哪一段"永远有答案，不会出现
#      一个没人认领的时刻，也不需要一条"默认活动"的兜底规则去掩盖漏写。
#   2. **活动是闭集，地点必须在位置图里。** 活动会进角色提示词与 Router 事实，
#      自由文本等于给内容作者开了一条提示注入通道；地点要变成
#      character.location_changed 的落点，一个不存在的 id 会在提交那一刻才炸。
#      两样都在构建内容快照时校验，一条不过整份内容作废。
#   3. **段里没有散文。** 没有 note、没有 description、没有 label —— 那些迟早
#      会被某一版提示词渲染出来，而这张表的每一个字段都是要变成世界事实的。
#   4. **它是纯内容。** 这个模块不 import 运行时、不 import 会话、不读磁盘，
#      import 它没有任何副作用。
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Mapping, Optional, Sequence, Tuple

from pns.models.world_state import ActivityKind

MINUTES_PER_DAY = 24 * 60

# 一份作息表最多几段。上界是安全预算不是审美：每一段边界都会变成一次事件提交，
# 一张写了几百段的表会让世界每分钟都在提交状态变更。
MAX_SEGMENTS = 48


class RhythmError(ValueError):
    """这份作息表本身不合法（时间越界、重复、未知活动/地点、多余字段等）。"""


def _require_minute(value, label: str) -> int:
    # bool 是 int 的子类：True 当成 00:01 会让一个明显写错的配置跑起来。
    if isinstance(value, bool) or not isinstance(value, int):
        raise RhythmError(f"{label} 必须是整数分钟，收到 {value!r}")
    if not 0 <= value < MINUTES_PER_DAY:
        raise RhythmError(f"{label} 必须落在 0–{MINUTES_PER_DAY - 1}，收到 {value}")
    return value


# 严格的 HH:MM：两位小时、两位分钟，ASCII 数字，前后不许有别的东西。
#
# 每一处收紧都对应一种真的会被写出来、而且会被悄悄接受成别的意思的写法：
# `str.isdigit()` 对全角"０８"为真、`int()` 也认它；`"1:2"` 和 `"001:02"` 会被
# 解析成 01:02；`"08:0"` 会被解析成 08:00。一张作息表读错一位就是角色在错误的
# 时刻换地方，而且没有任何迹象 —— 所以这里宁可拒绝，不猜。
_DAY_MINUTE_RE = re.compile(r"([0-9]{2}):([0-9]{2})")


def parse_day_minute(value, label: str = "at") -> int:
    """把作者写的 "HH:MM"（或整数分钟）解析成当日分钟数。

    只接受严格的 HH:MM：两位小时、两位分钟、ASCII 数字，没有空白、没有前缀、
    没有别的分隔符。"傍晚 17:30" 那种带前缀的写法属于遗留 scene 的显示字符串，
    不该出现在结构化作息表里。
    """
    if isinstance(value, str):
        match = _DAY_MINUTE_RE.fullmatch(value)
        if match is None:
            raise RhythmError(f"{label} 必须是严格的 HH:MM，收到 {value!r}")
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise RhythmError(f"{label} 超出范围：{value!r}")
        return hour * 60 + minute
    return _require_minute(value, label)


def format_day_minute(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


@dataclass(frozen=True)
class RhythmSegment:
    """一天里的一段：从 `at` 起，这个角色在 `location_id` 做 `activity`。

    段没有结束时间：结束由**下一段的开始**定义（最后一段跨零点接回第一段）。
    写两个端点就会出现"上一段的结束"和"下一段的开始"两个可以互相矛盾的答案。
    """

    at: int
    activity: ActivityKind
    location_id: Optional[str] = None

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "at", _require_minute(self.at, "at"))
        try:
            set_(self, "activity", ActivityKind(self.activity))
        except ValueError:
            raise RhythmError(f"未知的角色活动: {self.activity!r}") from None
        if self.activity is ActivityKind.UNSPECIFIED:
            # 一段作息存在的意义是**声明一个事实**：这个角色在这段时间里在做
            # 什么。unspecified 声明的是"没有事实"，而那跟不给这个角色写这一段
            # 是同一个意思 —— 区别只在于前者看起来像作者想说点什么。所以这里
            # 拒绝，而不是接受一个什么都没说的段。
            #
            # （运行时那边不需要这条禁令：作息表是否已经为当前时段说过话，由
            # 世界历史回答，跟活动是不是 unspecified 无关。这是一条内容规则。）
            raise RhythmError(
                "作息表里的活动不能是 unspecified —— 不想声明这一段就别写这一段"
            )
        if self.location_id is not None:
            if not isinstance(self.location_id, str) or not self.location_id:
                raise RhythmError("location_id 必须是非空字符串，或者干脆不写")

    @property
    def label(self) -> str:
        return format_day_minute(self.at)

    def to_dict(self) -> Dict:
        return {
            "at": format_day_minute(self.at),
            "activity": self.activity.value,
            "location_id": self.location_id,
        }


@dataclass(frozen=True)
class DailyRhythm:
    """一个角色被作者写下来的一天。

    时间口径跟 WorldState.clock 完全一致：timezone-naive 的模拟时间。带时区的
    时钟一律拒绝 —— 两种口径混着算"这一段是什么时候开始的"，要么抛 TypeError，
    要么悄悄偏几个小时。
    """

    character_id: str
    segments: Tuple[RhythmSegment, ...]

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        if not isinstance(self.character_id, str) or not self.character_id:
            raise RhythmError("character_id 必须是非空字符串")
        segments = tuple(self.segments)
        if not segments:
            raise RhythmError(f"角色 '{self.character_id}' 的作息表不能是空的")
        if len(segments) > MAX_SEGMENTS:
            raise RhythmError(
                f"角色 '{self.character_id}' 的作息表最多 {MAX_SEGMENTS} 段，"
                f"收到 {len(segments)}"
            )
        for segment in segments:
            if not isinstance(segment, RhythmSegment):
                raise RhythmError("作息表里只能放 RhythmSegment")
        segments = tuple(sorted(segments, key=lambda item: item.at))
        for previous, current in zip(segments, segments[1:]):
            if previous.at == current.at:
                # 同一分钟两段，等于"此刻属于哪一段"有两个答案。排序也解决不了，
                # 因为两个答案都合法 —— 所以拒绝，不静默取其一。
                if previous == current:
                    raise RhythmError(
                        f"角色 '{self.character_id}' 的作息表里 "
                        f"{format_day_minute(current.at)} 有重复的段"
                    )
                raise RhythmError(
                    f"角色 '{self.character_id}' 的作息表里 "
                    f"{format_day_minute(current.at)} 有两段互相冲突的安排"
                )
            if (previous.activity, previous.location_id) == (
                current.activity,
                current.location_id,
            ):
                # 相邻两段完全一样，中间那道边界什么都不会发生。它不是错误的
                # 世界，但它是一条写错了的内容（多半是想改却漏改了一项），
                # 所以在这里就说出来，而不是让作者以为世界在那一刻变了。
                raise RhythmError(
                    f"角色 '{self.character_id}' 的作息表里 "
                    f"{format_day_minute(previous.at)} 与 "
                    f"{format_day_minute(current.at)} 两段完全相同，应当合并"
                )
        set_(self, "segments", segments)

    # ── 查询 ────────────────────────────────────────────────────────────
    def segment_at(self, clock: datetime) -> RhythmSegment:
        """此刻属于哪一段。恒有答案 —— 一天被完全覆盖。"""
        minute = self._day_minute(clock)
        current = self.segments[-1]  # 第一段之前属于跨零点的最后一段
        for segment in self.segments:
            if segment.at <= minute:
                current = segment
            else:
                break
        return current

    def segment_started_at(self, clock: datetime) -> datetime:
        """此刻这一段是从哪个**绝对**时刻开始的。

        跨零点那一段的起点在昨天，所以这里要减一天 —— 不减的话，"当前活动是不是
        这一段开始之后才设的"会在每天零点到第一段之间恒成立，于是作息表在那段
        时间里永远不敢说话。
        """
        segment = self.segment_at(clock)
        minute = self._day_minute(clock)
        day: date = clock.date()
        start = datetime.combine(day, datetime.min.time()) + timedelta(
            minutes=segment.at
        )
        if segment.at > minute:
            start -= timedelta(days=1)
        return start

    def _day_minute(self, clock: datetime) -> int:
        if not isinstance(clock, datetime):
            raise RhythmError("作息表只认 datetime（模拟时钟时间）")
        if clock.tzinfo is not None:
            raise RhythmError(
                f"作息表只认 timezone-naive 的模拟时间，收到带时区的 {clock!r}"
            )
        return clock.hour * 60 + clock.minute

    def to_dict(self) -> Dict:
        return {
            "character_id": self.character_id,
            "segments": [segment.to_dict() for segment in self.segments],
        }


# 作息表条目里允许出现的键。白名单 —— 多写一个键就是拒绝，因为多出来的那个
# 键最可能是一句散文，而这张表的每一项都要变成世界事实。
_SEGMENT_KEYS = frozenset({"at", "activity", "location_id"})


def parse_daily_rhythm(
    payload,
    *,
    character_id: str,
    locations=None,
) -> Optional[DailyRhythm]:
    """把角色包 YAML 里的 `daily_rhythm` 解析成一份被校验过的作息表。

    没写就返回 None —— 没有作息表是正常的（作息表是逐个角色补的内容），
    不是错误。写了但写错了，就抛 RhythmError，由调用方决定整份内容作废。

    `locations` 给了就校验每个 location_id 真的存在。它是 cold 结构，作息表是
    可重载内容，所以这道校验必须在**构建内容快照**时做完：等到某天凌晨提交
    事件时才发现地点不存在，那时已经没人在看屏幕了。
    """
    if payload is None:
        return None
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        raise RhythmError(
            f"角色 '{character_id}' 的 daily_rhythm 必须是一组时间段"
        )

    segments = []
    for index, entry in enumerate(payload):
        if not isinstance(entry, Mapping):
            raise RhythmError(
                f"角色 '{character_id}' 的 daily_rhythm 第 {index + 1} 项必须是字典"
            )
        unknown = sorted(set(entry) - _SEGMENT_KEYS)
        if unknown:
            raise RhythmError(
                f"角色 '{character_id}' 的 daily_rhythm 第 {index + 1} 项有多余字段："
                f"{'、'.join(unknown)}（只接受 {'、'.join(sorted(_SEGMENT_KEYS))}）"
            )
        if "at" not in entry or "activity" not in entry:
            raise RhythmError(
                f"角色 '{character_id}' 的 daily_rhythm 第 {index + 1} 项缺少 at 或 activity"
            )
        location_id = entry.get("location_id")
        if location_id is not None and locations is not None:
            if not isinstance(location_id, str) or not locations.has(location_id):
                raise RhythmError(
                    f"角色 '{character_id}' 的作息表引用了未知的 location_id: "
                    f"{location_id!r}"
                )
        segments.append(
            RhythmSegment(
                at=parse_day_minute(
                    entry["at"], f"角色 '{character_id}' 作息表第 {index + 1} 项的 at"
                ),
                activity=entry["activity"],
                location_id=location_id,
            )
        )
    return DailyRhythm(character_id=character_id, segments=tuple(segments))


__all__ = [
    "MAX_SEGMENTS",
    "MINUTES_PER_DAY",
    "DailyRhythm",
    "RhythmError",
    "RhythmSegment",
    "format_day_minute",
    "parse_daily_rhythm",
    "parse_day_minute",
]
