# pns/world/__init__.py
from .facts import WORLD_FACTS
from .scenes import SCENES, DEFAULT_SCENE
from .characters import get_character_prompt, get_character_prompt_compat, get_character_constitution, list_characters, get_character_metadata, get_available_pairs

__all__ = [
    'WORLD_FACTS', 'SCENES', 'DEFAULT_SCENE',
    'get_character_prompt', 'get_character_constitution', 'list_characters', 'get_character_metadata', 'get_available_pairs',
    'get_world_state_str', 'get_character_system',
]


def get_world_state_str(scene: dict) -> str:
    return (
        f"时间：{scene['time']}，"
        f"地点：{scene['location']}，"
        f"天气/环境：{scene['weather']}"
    )


def get_character_system(character_id: str, scene: dict, compat: bool = False) -> str:
    """获取任意角色的 system prompt，并附加该角色已声明的宪法。"""
    world_state = get_world_state_str(scene)
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
