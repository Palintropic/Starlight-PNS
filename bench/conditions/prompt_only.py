# bench/conditions/prompt_only.py — 条件 A：Prompt Only。
#
# 角色 prompt（由 call_character 统一附加）+ **全局**转录 + 探针问题。
# 转录不做任何可见性过滤：世界里发生过的每句话都在这个角色的上下文里，
# 包括它根本不在场的那些——这正是这个基线要暴露的问题。
from typing import Dict, List, Tuple

from bench.conditions.base import (
    DialogueEvent,
    question_message,
    render_line,
    scene_message,
)


def build_history(
    corpus: List[DialogueEvent],
    character: str,
    question: str,
    *,
    trigger: str = "",
) -> Tuple[List[Dict], Dict]:
    """返回 (history, info)。消息角色约定与运行时一致：角色自己的台词是
    assistant，别人的台词是 user，逐行 `名字：台词`。"""
    history: List[Dict] = []
    if trigger:
        history.append(scene_message(trigger))
    for event in corpus:
        role = "assistant" if event.speaker == character else "user"
        history.append({"role": role, "content": render_line(event)})
    history.append(question_message(question))
    return history, {"corpus_events": len(corpus)}
