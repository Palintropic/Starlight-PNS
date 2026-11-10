# bench/conditions/pns.py — 条件 C：PNS 本体。
#
# 这个条件不许走捷径：语料逐条经运行时的提交边界（commit_dialogue）进
# 一个真实的 SessionState，曝光判定在提交时跑完，角色历史是观察的投影。
# 被问角色的历史里有没有目标事件那一行，完全由世界的落位与曝光规则决定
# ——这正是 BENCH-1A 要测的机制本身，所以这里不许出现任何手工过滤。
#
# 接缝（如实记录）：真实 KL-001 语料来自 yoake-mae 持久世界的固定快照
# （revision 98），落位与频道成员资格都在快照里。本模块目前只支持
# "静态落位 + 地点作用域"的重放：placements 由场景/夹具给全，语料事件
# 必须发生在说话者当时的落位。把持久世界快照直接恢复成 SessionState
# （走 formal_world / persistence 的恢复路径）是后续决定，见 bench/README。
from datetime import datetime
from typing import Dict, List, Tuple

from bench.conditions.base import DialogueEvent, question_message
from pns.models.session import SessionState, Turn
from pns.runtime.event_commit import commit_dialogue, dialogue_event_for_turn
from pns.models.world_state import WorldState
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph


class BenchWorldError(ValueError):
    """语料/落位与世界规则对不上。benchmark 的世界搭不起来就不许开跑。"""


def _grant_everything(world: WorldState, character_ids) -> None:
    """给夹具角色发齐静态授予。模式与 tests/grants_support.py 相同：
    放人/进频道走 WorldState 的授权检查，夹具的职责是把授权备齐，
    授权本身不是 BENCH-1A 的被测对象（它有自己的测试覆盖）。"""
    from pns.models.location import accepted_roles

    for character_id in character_ids:
        for location in world.locations:
            if location.access.get("public") is True:
                continue
            accepted = accepted_roles(location.access)
            role = "fixture" if accepted is None else accepted[0]
            world._grant_location(character_id, location.location_id, role)
        for channel_id in world.channels.ids():
            world._grant_channel(character_id, channel_id)


def build_session_state(
    *,
    session_id: str,
    characters: List[str],
    corpus: List[DialogueEvent],
    placements: Dict[str, str],
    clock: datetime,
    trigger: str = "",
) -> SessionState:
    """把语料逐条提交进一个新 SessionState，返回提交完的状态。

    每个角色的历史（state.history_for）就是曝光投影的结果；提交过程与
    研究会话完全共用同一条路径（dialogue_event_for_turn + commit_dialogue）。
    """
    missing = [c for c in characters if c not in placements]
    if missing:
        raise BenchWorldError(f"落位缺失的角色：{missing}")
    world = WorldState(
        clock=clock,
        locations=build_default_location_graph(),
        channels=build_default_channel_registry(),
    )
    _grant_everything(world, characters)
    for character_id in characters:
        world.place_character(character_id, placements[character_id])

    state = SessionState(
        session_id=session_id, scene="bench-kl", characters=list(characters)
    )
    state.attach_world_state(world)
    state.initialize_runtime(trigger)

    for number, event in enumerate(corpus, start=1):
        if event.channel_id is not None:
            raise BenchWorldError(
                "阶段一的 PNS 条件只重放地点作用域的语料；频道作用域事件"
                "需要快照恢复接缝（见 bench/README 的后续决定）"
            )
        expected = placements.get(event.speaker)
        if event.location_id is not None and event.location_id != expected:
            raise BenchWorldError(
                f"语料第 {number} 条：{event.speaker} 的落位是 {expected}，"
                f"事件却发生在 {event.location_id}（静态落位重放对不上）"
            )
        turn = Turn(
            turn_number=number,
            character=event.speaker,
            prompt="",
            response=event.text,
            timestamp=clock.isoformat(),
            char_name=event.speaker_name,
        )
        world_event = dialogue_event_for_turn(world, state.events, session_id, turn)
        commit_dialogue(state, turn, world_event)
    return state


def build_history(
    state: SessionState, character: str, question: str
) -> Tuple[List[Dict], Dict]:
    """被问角色在真实运行时会拿到的历史 + 探针问题。只读，不改状态：
    探针回答不提交进世界——BENCH-1A 测的是一次问答，不是续写世界。"""
    history = state.history_for(character)
    history.append(question_message(question))
    return history, {"history_lines": len(history) - 1}


def world_fingerprint(state: SessionState) -> str:
    """世界状态指纹（事件序列的哈希）：运行前后各算一次，证明探针问答
    没有改动世界——manifest 里 before/after 必须相等。"""
    import hashlib
    import json

    ids = [event.event_id for event in state.events]
    payload = json.dumps(ids, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
