# pns/world/__init__.py
from .facts import WORLD_FACTS
from .scenes import SCENES, DEFAULT_SCENE
from .channels import DEFAULT_CHANNELS, build_default_channel_registry
from .context import render_clock, render_session_location, render_world_context
from .locations import DEFAULT_LOCATIONS, build_default_location_graph
from .scene_compat import (
    SCENE_WORLD_MAP,
    SceneMappingError,
    build_initial_world_state,
    get_scene_mapping,
)
from .characters import get_character_prompt, get_character_prompt_compat, get_character_constitution, list_characters, get_character_metadata, get_available_pairs
from pns.models.world_state import WorldState

__all__ = [
    'WORLD_FACTS', 'SCENES', 'DEFAULT_SCENE',
    'DEFAULT_LOCATIONS', 'build_default_location_graph',
    'DEFAULT_CHANNELS', 'build_default_channel_registry',
    'SCENE_WORLD_MAP', 'SceneMappingError', 'build_initial_world_state', 'get_scene_mapping',
    'render_clock', 'render_session_location', 'render_world_context',
    'get_character_prompt', 'get_character_constitution', 'list_characters', 'get_character_metadata', 'get_available_pairs',
    'get_world_state_str', 'get_character_system',
]


def get_world_state_str(scene: dict) -> str:
    """遗留 scene 的世界上下文渲染。

    仅保留给还没迁移的调用方；新代码走 render_world_context(WorldState, …)。
    """
    return (
        f"时间：{scene['time']}，"
        f"地点：{scene['location']}，"
        f"天气/环境：{scene['weather']}"
    )


def _render_context(context, character_id: str) -> str:
    """把权威 WorldState（或遗留 scene dict）渲染成提示词里的世界上下文。"""
    if isinstance(context, WorldState):
        return render_world_context(context, character_id)
    return get_world_state_str(context)


def get_character_system(character_id: str, context, compat: bool = False) -> str:
    """获取任意角色的 system prompt，并附加该角色已声明的宪法。

    context 可以是权威的 WorldState（新路径），也可以是遗留 scene dict
    （兼容路径）。无论哪种，产出的都只是当前状态的一次投影 —— 提示词文本
    本身不构成世界状态。
    """
    world_state = _render_context(context, character_id)
    prompt = None
    if compat:
        prompt = get_character_prompt_compat(character_id)
    if prompt is None:
        prompt = get_character_prompt(character_id)  # 从 .characters 动态取

    system_prompt = prompt.format(world_state=world_state)
    constitution = get_character_constitution(character_id)
    if constitution is not None:
        system_prompt = (
            f"【角色宪法：生成回复后用于自我检查】\n{constitution}"
            f"\n\n【角色事实、当前场景与输出要求】\n{system_prompt}"
        )
    return system_prompt
