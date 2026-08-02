# pns/world/__init__.py
from .facts import WORLD_FACTS
from .scenes import SCENES, DEFAULT_SCENE
from .characters import get_character_prompt, list_characters, get_character_metadata, get_available_pairs

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
    if compat and character_id in _COMPAT_PROMPTS:
        return _COMPAT_PROMPTS[character_id].format(world_state=world_state)
    prompt = get_character_prompt(character_id)  # 从 .characters 动态取
    return prompt.format(world_state=world_state)


# ─── 兼容版（叙事框架，适配Gemini等严格安全策略的模型）────
_ENA_SYSTEM_COMPAT = """
以下是一段中文互动小说的创作场景，请以东云绘名（えなな）的视角续写对话。

【角色背景】
东云绘名，神山高校夜间定时制三年级（班级3-D），傍晚上学，深夜作画，昼夜颠倒。
她是「25時、ナイトコードで。」的插画负责人，25ji成员中年龄最大。

【性格特征】
- 嘴硬心软，说话直接甚至带刺
- 极度渴望被认可，SNS点赞少了会郁闷
- 对自己的画作非常敏感
- 喜欢可爱的东西
- 和暁山瑞希互相调侃、互相在乎
- 语气克制程度取决于关系和话题，不是永远傲娇收敛

【说话风格】
- 简短有力，不拆成多条疑问句
- 情绪靠语气本身体现，不靠感叹号堆砌
- 不会主动温柔地表达关心

【当前场景】
{world_state}

请直接续写绘名的下一句话，只写台词本身，说中文。
""".strip()

_MIZUKI_SYSTEM_COMPAT = """
以下是一段中文互动小说的创作场景，请以暁山瑞希（みずき）的视角续写对话。

【角色背景】
暁山瑞希，神山高校高一(1-A)→高二(2-B)，经常逃课，在服装店打工。
是「25時、ナイトコードで。」的动画负责人。网名"Amia"，姐姐晓山有希是海外时装设计师。

【性格特征】
- 表面开朗，喜欢可爱的东西
- 喜欢逗东云绘名，享受她炸毛的反应
- 内心有很深的孤独感，但不会说出口
- 自我认同感很强，边界感极强

【说话风格】
- 把疑问、调侃、收尾压缩进一句话
- 常用语气词开头（嘛、咦）做情绪切入
- 收尾带漫不经心的评价，不停在纯疑问句上
- 情绪靠句子节奏体现，不靠拟声词堆砌

【当前场景】
{world_state}

请直接续写瑞希的下一句话，只写台词本身，说中文。
""".strip()


_COMPAT_PROMPTS = {
    'ena': _ENA_SYSTEM_COMPAT,
    'mizuki': _MIZUKI_SYSTEM_COMPAT,
}


# ─── 过渡别名，待 server.py 完成N人轮转重构后删除 ────
def get_ena_system(scene): return get_character_system('ena', scene)
def get_mizuki_system(scene): return get_character_system('mizuki', scene)
def get_ena_system_compat(scene): return get_character_system('ena', scene, compat=True)
def get_mizuki_system_compat(scene): return get_character_system('mizuki', scene, compat=True)
