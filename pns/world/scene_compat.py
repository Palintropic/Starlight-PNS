# pns/world/scene_compat.py — 遗留 scene → 初始 WorldState 的唯一兼容边界
#
# 这是整个运行时里唯一允许读 pns/world/scenes.py 推导世界状态的地方。
# scene 是作者写死的叙事 fixture，只在会话开始时投影一次；投影之后
# WorldState 就是权威，scene 里的 trigger/auto_next/auto_turns 不参与世界模型。
#
# 散文地名到 location_id 的映射写死在下面的 SCENE_WORLD_MAP 里 —— 运行时
# 不做任何模糊匹配。没有映射的场景直接报错，不会被悄悄放到别的地方去。
from dataclasses import dataclass, field
from datetime import date, datetime
import re
from typing import Dict, Iterable, Mapping, Optional, Tuple

from pns.models.channel import ChannelRegistry
from pns.models.location import LocationGraph
from pns.models.world_state import ActivityKind, WorldState, WorldStateError
from pns.world.channels import build_default_channel_registry
from pns.world.grants import CharacterGrants, install_grants
from pns.world.locations import build_default_location_graph


class SceneMappingError(ValueError):
    """遗留场景无法确定性地映射到位置/频道安排。"""


@dataclass(frozen=True)
class SceneWorldMapping:
    """一个遗留场景对应的初始世界安排。"""

    default_location_id: str
    character_locations: Mapping[str, str] = field(default_factory=dict)
    channel_ids: Tuple[str, ...] = ()
    # 只写场景明确成立的当前活动。没有证据就保持 unspecified，绝不从职业猜。
    character_activities: Mapping[str, ActivityKind] = field(default_factory=dict)
    # 场景把角色放进这些地点时，没有内容授予的人以访客（guest）身份在场。
    # 这是场景作者显式声明的初始条件，不是从关系推出来的权限；地点本身也必须
    # 接受 guest 身份，否则建世界失败。
    guest_locations: Tuple[str, ...] = ()


SCENE_WORLD_MAP: Dict[str, SceneWorldMapping] = {
    "gate": SceneWorldMapping(
        default_location_id="kamiyama_high_gate",
    ),
    "ena_room": SceneWorldMapping(
        default_location_id="ena_home_studio",
        guest_locations=("ena_home_studio",),
    ),
    "clothes_shop": SceneWorldMapping(
        default_location_id="clothing_store_floor",
        guest_locations=("clothing_store_floor",),
    ),
    # 遗留 scene 把 "各自房间·Nightcord 语音频道" 挤进一个 location 字符串里。
    # 拆开之后：每个人待在自己的物理房间，同时都在 nightcord 频道上。
    "nightcord": SceneWorldMapping(
        default_location_id="private_residence",
        character_locations={
            "ena": "ena_home_studio",
            "mizuki": "mizuki_home_room",
            # 真冬离家后暂住宵崎家，在奏的房间里创作和休息。
            "kanade": "kanade_home_room",
            "mafuyu": "kanade_home_room",
        },
        channel_ids=("nightcord",),
        character_activities={
            "ena": ActivityKind.ONLINE_CHATTING,
            "mizuki": ActivityKind.ONLINE_CHATTING,
            "kanade": ActivityKind.ONLINE_CHATTING,
            "mafuyu": ActivityKind.ONLINE_CHATTING,
        },
        guest_locations=("private_residence",),
    ),
}


def get_scene_mapping(scene_id: str) -> SceneWorldMapping:
    """取场景的世界映射；没有映射就报一个能照着修的错误。"""
    try:
        return SCENE_WORLD_MAP[scene_id]
    except KeyError:
        raise SceneMappingError(
            f"场景 '{scene_id}' 还没有世界映射，无法确定角色所在的地点。"
            f"请在 pns/world/scene_compat.py 的 SCENE_WORLD_MAP 里为它补一条映射"
            f"（可选场景：{'、'.join(sorted(SCENE_WORLD_MAP))}）。"
        ) from None


def _parse_time(value: str) -> Tuple[int, int]:
    # Accept both the legacy display form ("傍晚 17:30") and a plain HH:MM
    # value, but reject trailing/embedded text rather than guessing.
    match = re.fullmatch(r"(?:[^0-9]*\s)?(\d{1,2}):(\d{2})\s*", value or "")
    if match is None:
        raise SceneMappingError(
            f"场景时间必须是 HH:MM 或时段加 HH:MM，收到 {value!r}"
        ) from None
    hour, minute = map(int, match.groups())
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise SceneMappingError(f"场景时间超出范围: {value!r}")
    return hour, minute


def build_initial_world_state(
    scene: Mapping,
    character_ids: Iterable[str],
    *,
    start_date: Optional[date] = None,
    locations: Optional[LocationGraph] = None,
    channels: Optional[ChannelRegistry] = None,
    grants: Optional[Mapping[str, CharacterGrants]] = None,
) -> WorldState:
    """把一个遗留 scene 投影成会话的初始 WorldState。

    顺序是刻意的：先装内容授予，再装场景声明的访客身份与频道成员资格，最后才
    放人。放人走 WorldState 的授权检查，所以场景没声明、内容也没授予的在场
    会让建世界失败，而不是造出一个"人在那里却无权在那里"的世界。
    """
    scene_id = scene.get("id") if isinstance(scene, Mapping) else None
    if not scene_id:
        raise SceneMappingError("遗留场景缺少 id，无法建立初始世界状态")

    mapping = get_scene_mapping(scene_id)
    hour, minute = _parse_time(scene.get("time"))
    day = start_date or date.today()

    world = WorldState(
        clock=datetime(day.year, day.month, day.day, hour, minute),
        locations=(
            locations if locations is not None else build_default_location_graph()
        ),
        channels=channels if channels is not None else build_default_channel_registry(),
        metadata={
            # 来源信息，仅供追溯和遗留投影；不是世界真相，运行时不读它做判断。
            "origin": {
                "kind": "legacy_scene",
                "scene_id": scene_id,
                "label": scene.get("label", ""),
                "trigger": scene.get("trigger", ""),
                "lore_tag": scene.get("lore_tag", ""),
            }
        },
    )

    character_ids = list(character_ids)
    for character_id in character_ids:
        content = (grants or {}).get(character_id)
        if content is not None:
            install_grants(world, content)

    scene_grants = []
    placements = {}
    for character_id in character_ids:
        location_id = mapping.character_locations.get(
            character_id, mapping.default_location_id
        )
        placements[character_id] = location_id
        if (
            not world.may_enter(character_id, location_id)
            and location_id in mapping.guest_locations
        ):
            world._grant_location(character_id, location_id, "guest")
            scene_grants.append(
                {"character_id": character_id, "location_id": location_id, "role": "guest"}
            )
    for channel_id in mapping.channel_ids:
        for character_id in character_ids:
            if not world.may_join(character_id, channel_id):
                world._grant_channel(character_id, channel_id)
                scene_grants.append(
                    {"character_id": character_id, "channel_id": channel_id}
                )
    # 哪些授予是场景给的，留在来源信息里：它们不是内容包声明的身份。
    world.metadata["origin"]["scene_grants"] = scene_grants

    try:
        for character_id, location_id in placements.items():
            world.place_character(character_id, location_id)
        for channel_id in mapping.channel_ids:
            for character_id in character_ids:
                world.join_channel(character_id, channel_id)
    except WorldStateError as e:
        raise SceneMappingError(f"场景 '{scene_id}' 的初始安排没有授予支撑：{e}") from e

    for character_id in character_ids:
        activity = mapping.character_activities.get(character_id)
        if activity is not None:
            world.set_activity(character_id, activity)

    weather = scene.get("weather")
    if weather is not None and not isinstance(weather, str):
        raise SceneMappingError(f"场景 weather 必须是字符串，收到 {weather!r}")
    if weather:
        for location_id in set(world.character_locations.values()):
            world.set_environment(location_id, {"weather": weather})

    return world
