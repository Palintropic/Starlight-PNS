# pns/world/__init__.py
from .facts import WORLD_FACTS
from .scenes import SCENES, DEFAULT_SCENE
from .characters import get_character_prompt, get_character_prompt_compat, list_characters, get_character_metadata, get_available_pairs

__all__ = [
    'WORLD_FACTS', 'SCENES', 'DEFAULT_SCENE',
    'get_character_prompt', 'list_characters', 'get_character_metadata', 'get_available_pairs',
    'get_world_state_str', 'get_character_system',
]


def get_world_state_str(scene: dict) -> str:
    return (
        f"时间：{scene['time']}，"
        f"地点：{scene['location']}，"
        f"天气/环境：{scene['weather']}"
    )


def get_character_system(character_id: str, scene: dict, compat: bool = False) -> str:
    """获取任意角色的system prompt，compat=True时优先用兼容版（如果存在）"""
    world_state = get_world_state_str(scene)
    if compat:
        compat_prompt = get_character_prompt_compat(character_id)
        if compat_prompt is not None:
            return compat_prompt.format(world_state=world_state)
    prompt = get_character_prompt(character_id)  # 从 .characters 动态取
    return prompt.format(world_state=world_state)
