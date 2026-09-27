# pns/models/clock_anchor.py — 模拟时间与现实时间的锚点
#
# 一个打开的世界里，时间一直在走（WORLD-1 设计 §2）：此刻"应当"到了哪一分钟，
# 由锚点从现实时间换算出来。时钟 worker 把世界一步一步推到那里。
#
#   anchor_now = sim_epoch + (wall_now - wall_epoch) × rate
#
# 锚点属于 Operational History：它不进 WorldState，不写世界事件，不进任何
# resident 的上下文。它随存档保存，恢复时用存档里的锚点和此刻的 UTC 算出
# "离线期间走到了哪里"。
#
# 两条约束：
#
#   1. **sim_epoch 保留秒。** 换倍率时要把"当前精确模拟时刻"连同秒的余量带进
#      新锚点；取整会让时钟在每次换倍率时丢掉不到一分钟。
#   2. **wall_epoch 是带时区的 UTC。** 跨进程只有 UTC 可比；进程内由调用方用
#      monotonic 推算 wall_now（系统时钟被拨动不影响推进），见时钟 worker。
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Mapping

# 开发环境快进的上限。安全预算，不是审美：再快，补跑本身就追不上了。
MAX_RATE = 3600.0


class ClockAnchorError(ValueError):
    """锚点不合法。"""


def _rate(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClockAnchorError(f"rate 必须是数字，收到 {value!r}")
    rate = float(value)
    if not 0 < rate <= MAX_RATE:
        raise ClockAnchorError(f"rate 必须落在 (0, {MAX_RATE:g}]，收到 {value!r}")
    return rate


def _sim(value) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise ClockAnchorError(f"sim_epoch 不是合法时间: {value!r}") from None
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise ClockAnchorError("sim_epoch 必须是 timezone-naive 的模拟时间")
    return value


def _wall(value) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise ClockAnchorError(f"wall 时间不合法: {value!r}") from None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ClockAnchorError("wall 时间必须带时区（UTC）")
    return value.astimezone(timezone.utc)


def floor_minute(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)


@dataclass(frozen=True)
class ClockAnchor:
    sim_epoch: datetime
    wall_epoch: datetime
    rate: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "sim_epoch", _sim(self.sim_epoch))
        object.__setattr__(self, "wall_epoch", _wall(self.wall_epoch))
        object.__setattr__(self, "rate", _rate(self.rate))

    def sim_at(self, wall: datetime) -> datetime:
        """现实时刻 wall 对应的精确模拟时刻（含秒）。"""
        return self.sim_epoch + (_wall(wall) - self.wall_epoch) * self.rate

    def minute_at(self, wall: datetime) -> datetime:
        """时钟步的目标：wall 对应的模拟时刻向下取整到分钟。"""
        return floor_minute(self.sim_at(wall))

    def rebased(self, wall: datetime, rate) -> "ClockAnchor":
        """在 wall 这一刻换倍率：新锚点从此刻的精确模拟时刻起算，不跳、不丢秒。"""
        return ClockAnchor(self.sim_at(wall), wall, rate)

    def to_dict(self) -> Dict:
        return {
            "sim_epoch": self.sim_epoch.isoformat(),
            "wall_epoch": self.wall_epoch.isoformat(),
            "rate": self.rate,
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> "ClockAnchor":
        if not isinstance(payload, Mapping):
            raise ClockAnchorError("锚点必须是字典")
        try:
            return cls(payload["sim_epoch"], payload["wall_epoch"], payload["rate"])
        except KeyError as e:
            raise ClockAnchorError(f"锚点缺字段: {e}") from None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "MAX_RATE",
    "ClockAnchor",
    "ClockAnchorError",
    "floor_minute",
    "utc_now",
]
