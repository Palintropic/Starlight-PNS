# pns/world/grants.py — 角色包里声明的进入/加入授予
#
# 这个模块回答一个问题：**内容作者说"这个角色可以进哪些非公开地点、是哪些
# 频道的成员"，那份数据长什么样。**
#
# 它不回答：此刻谁在哪里（WorldState）、授予怎么进世界（建世界时由内容注册表
# 装入）、谁来执行（事件提交边界与 Agency 前置条件，都读 WorldState.may_enter /
# may_join）。
#
# 三条硬约束：
#
#   1. **授予只描述身份，不描述意愿或关系。** 条目只有地点、身份、频道三种
#      结构化字段；没有"因为关系好所以可以进"这种写法（Articles VIII–IX）。
#   2. **写错就在构建内容快照时拒绝。** 未知地点/频道、给公开地点写授予、身份与
#      地点声明的 access.role 对不上，都是内容错误，不留到提交那一刻。
#   3. **它是纯内容。** 不 import 运行时、不读磁盘，import 没有副作用。
from dataclasses import dataclass
from typing import FrozenSet, Mapping, Optional, Sequence, Tuple

from pns.models.location import access_admits, accepted_roles
from pns.world.rhythm import MINUTES_PER_DAY
from pns.world.routing import plan_route


class GrantError(ValueError):
    """这份授予声明本身不合法。"""


@dataclass(frozen=True)
class CharacterGrants:
    """一个角色的静态授予：非公开地点 → 身份，以及频道成员资格。"""

    character_id: str
    locations: Tuple[Tuple[str, str], ...] = ()
    channels: FrozenSet[str] = frozenset()

    def location_roles(self) -> Mapping[str, str]:
        return dict(self.locations)


_TOP_KEYS = frozenset({"locations", "channels"})
_LOCATION_KEYS = frozenset({"location_id", "role"})


def _require_list(value, label: str) -> Sequence:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise GrantError(f"{label} 必须是一个列表")
    return value


def parse_access_grants(
    payload, *, character_id: str, locations, channels
) -> Optional[CharacterGrants]:
    """把角色包 YAML 里的 `access_grants` 解析成一份被校验过的授予。

    没写就返回 None：没有授予的角色只能出现在公开地点，也不是任何频道的成员。
    `locations` / `channels` 是必填的 —— 忘了传就跳过校验，会让"写错构建就
    失败"退化成一次观察。
    """
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise GrantError(f"角色 '{character_id}' 的 access_grants 必须是字典")
    unknown = sorted(set(payload) - _TOP_KEYS)
    if unknown:
        raise GrantError(
            f"角色 '{character_id}' 的 access_grants 有多余字段：{'、'.join(unknown)}"
        )

    location_roles = {}
    for index, entry in enumerate(
        _require_list(payload.get("locations", []), "access_grants.locations")
    ):
        label = f"角色 '{character_id}' 的 access_grants.locations 第 {index + 1} 项"
        if not isinstance(entry, Mapping):
            raise GrantError(f"{label} 必须是字典")
        extra = sorted(set(entry) - _LOCATION_KEYS)
        if extra:
            raise GrantError(f"{label} 有多余字段：{'、'.join(extra)}")
        location_id = entry.get("location_id")
        role = entry.get("role")
        if not isinstance(location_id, str) or not locations.has(location_id):
            raise GrantError(f"{label} 引用了未知的 location_id: {location_id!r}")
        if not isinstance(role, str) or not role:
            raise GrantError(f"{label} 必须写明 role")
        access = locations.get(location_id).access
        if access.get("public") is True:
            # 公开地点不需要授予；写了多半是地点 id 写错了。
            raise GrantError(f"{label}：'{location_id}' 是公开地点，不需要授予")
        accepted = accepted_roles(access)
        if accepted is not None and role not in accepted:
            raise GrantError(
                f"{label}：'{location_id}' 接受的身份是 {'、'.join(accepted)}，"
                f"授予写的是 {role!r}"
            )
        if location_id in location_roles:
            raise GrantError(f"{label}：'{location_id}' 重复授予")
        location_roles[location_id] = role

    channel_ids = set()
    for index, channel_id in enumerate(
        _require_list(payload.get("channels", []), "access_grants.channels")
    ):
        if not isinstance(channel_id, str) or not channels.has(channel_id):
            raise GrantError(
                f"角色 '{character_id}' 的 access_grants.channels 第 {index + 1} 项"
                f"引用了未知的 channel_id: {channel_id!r}"
            )
        if channel_id in channel_ids:
            raise GrantError(
                f"角色 '{character_id}' 的频道 '{channel_id}' 重复授予"
            )
        channel_ids.add(channel_id)

    return CharacterGrants(
        character_id=character_id,
        locations=tuple(sorted(location_roles.items())),
        channels=frozenset(channel_ids),
    )


def may_enter(grants: Optional[CharacterGrants], location) -> bool:
    """跟 WorldState.may_enter 同一条规则（access_admits），在内容快照这一侧求值。"""
    role = None if grants is None else grants.location_roles().get(location.location_id)
    return access_admits(location.access, role)


def require_rhythm_is_enterable(rhythm, grants, locations) -> None:
    """作息表里每一段的地点，这个角色都必须有资格进入。

    否则那一段会在到点提交时才被拒绝 —— 那时已经没人在看屏幕了。
    """
    if rhythm is None:
        return
    for segment in rhythm.all_segments:
        if segment.location_id is None:
            continue
        if not may_enter(grants, locations.get(segment.location_id)):
            raise GrantError(
                f"角色 '{rhythm.character_id}' 的作息表 {segment.label} 那一段去 "
                f"'{segment.location_id}'，但没有进入它的授予"
            )


def require_rhythm_channels_joinable(rhythm, grants) -> None:
    """作息表里每一段声明的频道，这个角色都必须是成员（R2-11）。

    否则 01:00 那一刻的加入会被提交边界拒绝，整个时钟步回滚、反复失败，
    一条内容错误就能把世界时钟卡死。
    """
    if rhythm is None:
        return
    members = grants.channels if grants is not None else frozenset()
    for segment in rhythm.all_segments:
        if segment.channel_id is not None and segment.channel_id not in members:
            raise GrantError(
                f"角色 '{rhythm.character_id}' 的作息表 {segment.label} 那一段在频道 "
                f"'{segment.channel_id}' 里，但不是它的成员"
            )


def require_rhythm_trips_fit(rhythm, grants, locations) -> None:
    """每一次换地方（含跨零点接回次日第一段），路都走得通、时间也排得下。

    没写地点的段沿用上一段的地点。相邻两段地点不同，就必须有一条只经过可进入
    地点的路线，而且路上的时间不超过两段起点之差：否则按时出发的行程会跟上一
    个行程重叠（R2-9）。按时运行的作息因此不会出现两个同时进行的行程。

    有休息日表时，平日接休息日、休息日接平日的那几次换地方同样要检查：把"前一天、
    这一天、后一天"各取哪张表的每一种组合都走一遍，前一天只用来确定这一天第一段
    沿用的地点。
    """
    if rhythm is None:
        return
    if not any(segment.location_id is not None for segment in rhythm.all_segments):
        return
    tables = rhythm.tables
    for before in tables:
        for today in tables:
            for after in tables:
                _check_day_trips(rhythm.character_id, before, today, after, grants, locations)


def _check_day_trips(character_id, before, today, after, grants, locations) -> None:
    # 前一天的最后一个已知地点，是这一天第一段没写地点时沿用的地点。
    last = None
    for segment in before:
        if segment.location_id is not None:
            last = segment.location_id
    timeline = [(segment.at, segment) for segment in today]
    timeline.append((MINUTES_PER_DAY + after[0].at, after[0]))
    where = []
    for _, segment in timeline:
        if segment.location_id is not None:
            last = segment.location_id
        where.append(last)
    for index in range(len(today)):
        here, there = where[index], where[index + 1]
        if here is None or there is None or here == there:
            continue
        (start, current), (end, following) = timeline[index], timeline[index + 1]
        gap = end - start
        route = plan_route(
            locations,
            lambda location_id: may_enter(grants, locations.get(location_id)),
            here,
            there,
        )
        if route is None:
            raise GrantError(
                f"角色 '{character_id}' 的作息表从 {current.label} 的 '{here}' 到 "
                f"{following.label} 的 '{there}' 没有它走得通的路"
            )
        if route.total_minutes > gap:
            raise GrantError(
                f"角色 '{character_id}' 的作息表从 {current.label} 到 {following.label} "
                f"只有 {gap} 分钟，路上却要 {route.total_minutes} 分钟"
            )


def install_grants(world, grants: CharacterGrants) -> None:
    """建世界时把一个角色的授予装进 WorldState。"""
    for location_id, role in grants.locations:
        world._grant_location(grants.character_id, location_id, role)
    for channel_id in sorted(grants.channels):
        world._grant_channel(grants.character_id, channel_id)


__all__ = [
    "CharacterGrants",
    "GrantError",
    "install_grants",
    "may_enter",
    "parse_access_grants",
    "require_rhythm_channels_joinable",
    "require_rhythm_is_enterable",
    "require_rhythm_trips_fit",
]
