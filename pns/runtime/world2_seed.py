# pns/runtime/world2_seed.py — 入住时的首次排期种子（WORLD-2 设计 v3 §4.7，复审 F5）
#
# 开局播种按传入列表的 index 错开首次到期；四次单人入住各传 [新人]，index 恒为 0，
# 四人会同一分钟首发。所以入住用**稳定序位**：序位由入住请求带入（MMJ 一批四人是
# 0–3），连同算出来的种子一起写进入住事件的 payload。重试不重算，恢复只核对。
#
# 种子是一份类型化的记录，提交边界据它排入一条周期激活：
#
#   algorithm            种子算法版本
#   activation_id        seed.activation:<角色>（与开局播种同一个 ID 规则）
#   ordinal              稳定序位
#   first_delay_minutes  ┐
#   stagger_minutes      ├ 服务器侧节律（ActivationCadence 的同名字段）
#   interval_minutes     ┘
#   cue                  角色可见的提示（没有就是 None）
#   due_at               首次到期 = 入住时刻 + first_delay + ordinal × stagger
#
# 首次到期严格晚于入住时刻，不补播入住之前的分钟。旧居民的排期一条不动。
#
# 这个模块不在 pns.runtime.autonomy 包里：事件提交层要用它，而那个包的 __init__
# 会带出协调器，协调器又 import 事件提交层。
from datetime import datetime, timedelta
from typing import Dict, Mapping, Optional

from pns.models.activation import ActivationKind, ScheduledActivation
from pns.models.frozen import thaw_json_value

# 播种出来的激活 ID 前缀。确定性 —— 同一个角色在同一个世界里只可能有一条。
SEED_PREFIX = "seed.activation"

# 各项节律的上界。它们是**安全预算**，不是审美：一个手滑写成 100000 的周期
# 会让世界永远不动，一个写成 0 的周期会让它每分钟都在花钱。
MAX_INTERVAL_MINUTES = 7 * 24 * 60
MAX_FIRST_DELAY_MINUTES = 7 * 24 * 60
MAX_STAGGER_MINUTES = 24 * 60
# 稳定序位的上界：一次入住批次里的人数，远小于它。
MAX_ADMISSION_ORDINAL = 63

ADMISSION_SEED_ALGORITHM = "pns.admission_seed/1"
_SEED_KEYS = frozenset(
    {
        "algorithm",
        "activation_id",
        "ordinal",
        "first_delay_minutes",
        "stagger_minutes",
        "interval_minutes",
        "cue",
        "due_at",
    }
)


class SeedError(ValueError):
    """入住种子不成立。"""


def seed_activation_id(character_id: str) -> str:
    """这个角色在这个世界里那条周期排期的 ID。确定性，所以撞车会被发现。"""
    if not isinstance(character_id, str) or not character_id:
        raise SeedError("character_id 必须是非空字符串")
    return f"{SEED_PREFIX}:{character_id}"


def _bounded(value, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SeedError(f"{label} 必须是整数，收到 {value!r}")
    if not low <= value <= high:
        raise SeedError(f"{label} 必须落在 {low}–{high}，收到 {value}")
    return value


def admission_seed(
    character_id: str,
    *,
    ordinal: int,
    admitted_at: datetime,
    first_delay_minutes: int,
    stagger_minutes: int,
    interval_minutes: int,
    cue: Optional[str] = None,
) -> Dict:
    """按稳定序位算出一位新居民的种子。调用方（入住服务）算一次、写进 payload。"""
    seed = {
        "algorithm": ADMISSION_SEED_ALGORITHM,
        "activation_id": seed_activation_id(character_id),
        "ordinal": ordinal,
        "first_delay_minutes": first_delay_minutes,
        "stagger_minutes": stagger_minutes,
        "interval_minutes": interval_minutes,
        "cue": cue,
        "due_at": None,
    }
    _check_numbers(seed)
    seed["due_at"] = _first_due(seed, admitted_at).isoformat()
    activation_from_seed(seed, character_id=character_id, admitted_at=admitted_at)
    return seed


def _check_numbers(seed: Mapping) -> None:
    _bounded(seed["ordinal"], "ordinal", 0, MAX_ADMISSION_ORDINAL)
    _bounded(seed["first_delay_minutes"], "first_delay_minutes", 1, MAX_FIRST_DELAY_MINUTES)
    _bounded(seed["stagger_minutes"], "stagger_minutes", 1, MAX_STAGGER_MINUTES)
    _bounded(seed["interval_minutes"], "interval_minutes", 1, MAX_INTERVAL_MINUTES)


def _first_due(seed: Mapping, admitted_at: datetime) -> datetime:
    return admitted_at + timedelta(
        minutes=seed["first_delay_minutes"] + seed["ordinal"] * seed["stagger_minutes"]
    )


def activation_from_seed(
    seed, *, character_id: str, admitted_at: datetime
) -> ScheduledActivation:
    """严格解码一份种子，交出它要排入的那条周期激活。

    种子必须正是这个角色、这个入住时刻按它自己的序位与节律算出来的那一份：
    首次到期由算法推出，不接受任何别的值。
    """
    seed = thaw_json_value(seed)
    if not isinstance(seed, dict) or set(seed) != _SEED_KEYS:
        raise SeedError("入住种子的字段不对")
    if seed["algorithm"] != ADMISSION_SEED_ALGORITHM:
        raise SeedError(f"不认识的种子算法: {seed['algorithm']!r}")
    if seed["activation_id"] != seed_activation_id(character_id):
        raise SeedError("入住种子的 activation_id 不是这个角色的")
    _check_numbers(seed)
    cue = seed["cue"]
    if cue is not None:
        # 运行时这个包早已加载完，延迟引用不会形成循环（见文件头）。
        from pns.runtime.autonomy.context import MAX_CUE_CHARS

        if not isinstance(cue, str) or not cue.strip() or len(cue) > MAX_CUE_CHARS:
            raise SeedError("入住种子的 cue 必须是不超长的非空字符串，或者 None")
    if admitted_at.second or admitted_at.microsecond:
        raise SeedError("入住时刻不在整分钟上，排期无法确定")
    due_at = _first_due(seed, admitted_at)
    if seed["due_at"] != due_at.isoformat():
        raise SeedError("入住种子的首次到期不是按序位与节律算出来的")
    return ScheduledActivation(
        activation_id=seed["activation_id"],
        kind=ActivationKind.CHARACTER_ACTIVATION,
        due_at=due_at,
        character_id=character_id,
        interval_minutes=seed["interval_minutes"],
        payload={"cue": cue} if cue else {},
    )


__all__ = [
    "ADMISSION_SEED_ALGORITHM",
    "MAX_ADMISSION_ORDINAL",
    "MAX_FIRST_DELAY_MINUTES",
    "MAX_INTERVAL_MINUTES",
    "MAX_STAGGER_MINUTES",
    "SEED_PREFIX",
    "SeedError",
    "activation_from_seed",
    "admission_seed",
    "seed_activation_id",
]
