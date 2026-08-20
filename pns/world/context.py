# pns/world/context.py — WorldState → 提示词文本的投影层
#
# 这里只读 WorldState，只产出字符串。规则是单向的：结构化状态决定文本，
# 文本永远不会变回状态。所以这个模块没有任何写操作。
from datetime import datetime
from typing import Optional

from pns.models.world_state import WorldState

# 从时钟推导出的口语时段标签。原来它是 scene 上手写的 day_phase 字段，
# 现在由时间本身决定 —— 时间推进后标签会自己跟上，不会停留在旧场景上。
_DAY_PHASES = (
    (5, "早上"),
    (11, "中午"),
    (14, "下午"),
    (17, "傍晚"),
    (19, "晚上"),
    (23, "深夜"),
)


def day_phase_label(clock: datetime) -> str:
    label = "深夜"
    for start_hour, phase in _DAY_PHASES:
        if clock.hour >= start_hour:
            label = phase
    return label


def render_clock(clock: datetime) -> str:
    """渲染成遗留提示词里那种 '傍晚 17:30'。"""
    return f"{day_phase_label(clock)} {clock.strftime('%H:%M')}"


def render_location(world: WorldState, location_id: str) -> str:
    return world.locations.get(location_id).display


def render_session_location(world: WorldState) -> str:
    """整个会话层面的地点摘要：所有人同处一地就是那一处，否则逐个列出。"""
    location_ids = []
    for location_id in world.character_locations.values():
        if location_id not in location_ids:
            location_ids.append(location_id)

    if len(location_ids) == 1:
        places = render_location(world, location_ids[0])
    elif location_ids:
        places = " / ".join(world.locations.get(lid).name for lid in location_ids)
    else:
        places = "未定位"

    channel_names = [
        world.channels.get(channel_id).display
        for channel_id, members in sorted(world.channel_members.items())
        if members
    ]
    if channel_names:
        places = f"{places} · {'、'.join(channel_names)}"
    return places


def render_environment(world: WorldState, location_id: Optional[str]) -> str:
    if location_id is None:
        return ""
    environment = world.environment_of(location_id)
    return str(environment.get("weather", ""))


def render_world_context(world: WorldState, character_id: Optional[str] = None) -> str:
    """角色视角的世界上下文，替代原来的 get_world_state_str(scene)。

    形状保持与遗留提示词一致（时间/地点/天气），另外在角色确实挂着线上
    频道时补一段频道信息 —— 物理位置和线上在场是两件独立的事。
    """
    location_id = world.location_of(character_id) if character_id else None

    if location_id is not None:
        place = render_location(world, location_id)
        environment = render_environment(world, location_id)
    else:
        place = render_session_location(world)
        environment = ""
        for candidate in world.character_locations.values():
            environment = render_environment(world, candidate)
            if environment:
                break

    context = f"时间：{render_clock(world.clock)}，地点：{place}"
    if environment:
        context += f"，天气/环境：{environment}"

    if character_id:
        channels = world.channels_for(character_id)
        if channels:
            names = "、".join(world.channels.get(cid).display for cid in channels)
            context += f"，在线频道：{names}"
    return context
