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
from pns.models.world_state import WorldState
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph


class SceneMappingError(ValueError):
    """遗留场景无法确定性地映射到位置/频道安排。"""


@dataclass(frozen=True)
class SceneWorldMapping:
    """一个遗留场景对应的初始世界安排。"""

    default_location_id: str
    character_locations: Mapping[str, str] = field(default_factory=dict)
    channel_ids: Tuple[str, ...] = ()


SCENE_WORLD_MAP: Dict[str, SceneWorldMapping] = {
    "gate": SceneWorldMapping(
        default_location_id="kamiyama_high_gate",
    ),
    "ena_room": SceneWorldMapping(
        default_location_id="ena_home_studio",
    ),
    "clothes_shop": SceneWorldMapping(
        default_location_id="clothing_store_floor",
    ),
    # 遗留 scene 把 "各自房间·Nightcord 语音频道" 挤进一个 location 字符串里。
    # 拆开之后：每个人待在自己的物理房间，同时都在 nightcord 频道上。
    "nightcord": SceneWorldMapping(
        default_location_id="private_residence",
        character_locations={
            "ena": "ena_home_studio",
            "mizuki": "mizuki_home_room",
        },
        channel_ids=("nightcord",),
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
) -> WorldState:
    """把一个遗留 scene 投影成会话的初始 WorldState。"""
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
        location_id = mapping.character_locations.get(
            character_id, mapping.default_location_id
        )
        world.place_character(character_id, location_id)

    for channel_id in mapping.channel_ids:
        for character_id in character_ids:
            world.join_channel(character_id, channel_id)

    weather = scene.get("weather")
    if weather is not None and not isinstance(weather, str):
        raise SceneMappingError(f"场景 weather 必须是字符串，收到 {weather!r}")
    if weather:
        for location_id in set(world.character_locations.values()):
            world.set_environment(location_id, {"weather": weather})

    return world
