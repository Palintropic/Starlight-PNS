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

from pns.models.location import access_admits


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
        required = access.get("role")
        if required is not None and role != required:
            raise GrantError(
                f"{label}：'{location_id}' 要求身份 {required!r}，授予写的是 {role!r}"
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
    for segment in rhythm.segments:
        if segment.location_id is None:
            continue
        if not may_enter(grants, locations.get(segment.location_id)):
            raise GrantError(
                f"角色 '{rhythm.character_id}' 的作息表 {segment.label} 那一段去 "
                f"'{segment.location_id}'，但没有进入它的授予"
            )


def install_grants(world, grants: CharacterGrants) -> None:
    """建世界时把一个角色的授予装进 WorldState。"""
    for location_id, role in grants.locations:
        world.grant_location(grants.character_id, location_id, role)
    for channel_id in sorted(grants.channels):
        world.grant_channel(grants.character_id, channel_id)


__all__ = [
    "CharacterGrants",
    "GrantError",
    "install_grants",
    "may_enter",
    "parse_access_grants",
    "require_rhythm_is_enterable",
]
