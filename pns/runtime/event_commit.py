# pns/runtime/event_commit.py — 事件提交边界
#
# 这是运行时里唯一一处"接受一个事件并让它改变世界"的地方。所有想改
# WorldState 的路径都必须经过这里，不存在绕开事件直接改状态的第二条路。
#
# 提交分成两个阶段，顺序是刻意的：
#
#   阶段一  只校验，不改任何状态（引用完整性 + 能不能追加）
#   阶段二  只改状态（应用状态效果 → 追加事件），出任何岔子整体回滚
#
# 于是"世界改了但事件没记下"和"事件记下了但世界没改"都不可能发生。
#
# 另一条边界同样重要：只有被接受的结果才走到这里。生成失败、判分失败、
# 漂移记录写盘失败的候选输出留在审计/错误历史里，永远不进世界历史。
from typing import Dict, Optional

from pns.models.channel import ChannelKind
from pns.models.event import Event, EventScope, EventType
from pns.models.event_store import EventStore
from pns.models.session import SessionState, Turn
from pns.models.world_state import WorldState


class EventCommitError(ValueError):
    """事件无法在这个世界里被提交（引用不存在、缺少可应用的状态效果等）。"""


# ── 阶段一：校验 ────────────────────────────────────────────────────────
def validate_against_world(world: WorldState, event: Event) -> None:
    """事件里的每个标识符都必须在这个世界里真实存在。

    Event 自己只能校验形状（scope 必填字段、类型必填字段）；它不认识具体
    世界，所以"这个角色/地点/频道到底存不存在"只能在这里判。
    """
    if not isinstance(world, WorldState):
        raise EventCommitError("提交事件前必须先绑定权威 WorldState")
    if not isinstance(event, Event):
        raise EventCommitError("只能提交 Event")

    known = set(world.known_characters())
    references = list(event.participants)
    if event.actor_id is not None:
        references.append(event.actor_id)
    for character_id in references:
        if character_id not in known:
            raise EventCommitError(
                f"事件 '{event.event_id}' 引用了世界里不存在的角色: {character_id}"
            )

    if event.location_id is not None and not world.locations.has(event.location_id):
        raise EventCommitError(
            f"事件 '{event.event_id}' 引用了未知的 location_id: {event.location_id}"
        )
    if event.channel_id is not None and not world.channels.has(event.channel_id):
        raise EventCommitError(
            f"事件 '{event.event_id}' 引用了未知的 channel_id: {event.channel_id}"
        )


# ── 阶段二：状态效果 ────────────────────────────────────────────────────
#
# 发言类事件刻意没有状态效果：说话是一次"发生"，不是一次世界状态变更。
# 事件不必改状态才算事件 —— 它记录的是发生本身。
def _apply_nothing(world: WorldState, event: Event) -> None:
    return


def _apply_joined_channel(world: WorldState, event: Event) -> None:
    world.join_channel(event.actor_id, event.channel_id)


def _apply_left_channel(world: WorldState, event: Event) -> None:
    world.leave_channel(event.actor_id, event.channel_id)


def _apply_time_advanced(world: WorldState, event: Event) -> None:
    world.advance_time(int(event.payload["minutes"]))


def _apply_location_changed(world: WorldState, event: Event) -> None:
    world.place_character(event.actor_id, event.location_id)


_APPLY = {
    EventType.DIALOGUE_SPOKEN: _apply_nothing,
    EventType.MESSAGE_SENT: _apply_nothing,
    EventType.PRESENCE_JOINED_CHANNEL: _apply_joined_channel,
    EventType.PRESENCE_LEFT_CHANNEL: _apply_left_channel,
    EventType.WORLD_TIME_ADVANCED: _apply_time_advanced,
    EventType.CHARACTER_LOCATION_CHANGED: _apply_location_changed,
}


def apply_event(world: WorldState, event: Event) -> None:
    """把事件声明的状态效果作用到世界上。

    payload 永远不会被当成"要写进世界状态的字典"—— 每种类型走各自写死的
    状态效果，任意 payload 键改不动 WorldState。
    """
    handler = _APPLY.get(event.type)
    if handler is None:
        raise EventCommitError(
            f"事件类型 {event.type.value} 还没有已实现的状态效果，不能提交"
        )
    handler(world, event)


# ── 提交 ────────────────────────────────────────────────────────────────
def commit_event(world: WorldState, store: EventStore, event: Event) -> Dict:
    """接受一个事件：应用状态效果并追加到世界历史，两者同生共死。

    返回一份稳定投影（事件的完整公开形状 + 它在世界历史里的序号），供下游
    使用；下游拿到的是新的可变结构，改它影响不到已提交的事件。
    """
    if not isinstance(store, EventStore):
        raise EventCommitError("世界历史必须是 EventStore")

    validate_against_world(world, event)
    store.check_can_append(event)

    snapshot = world.snapshot_mutable_state()
    length = len(store)
    try:
        apply_event(world, event)
        sequence = store.append(event)
    except BaseException:
        world.restore_mutable_state(snapshot)
        store.rollback_to(length)
        raise

    return {"sequence": sequence, **event.to_dict()}


def commit_session_event(state: SessionState, event: Event) -> Dict:
    """在一个会话里提交事件，失败时连会话状态一起回滚。"""
    with state.atomic_commit():
        return commit_event(state.world_state, state.events, event)


def commit_dialogue(state: SessionState, turn: Turn, event: Event) -> Dict:
    """提交一次被接受的发言：世界历史里的事件 + 生成审计里的 Turn。

    两者要么都落地，要么都不落地 —— 不允许出现一条没有对应事件的 turn，
    也不允许出现一条没有对应生成记录的发言事件。
    """
    with state.atomic_commit():
        projection = commit_event(state.world_state, state.events, event)
        state.record_turn(turn)
    return projection


# ── 从生成记录构造发言事件 ──────────────────────────────────────────────
def _primary_channel(world: WorldState, character_id: str) -> Optional[str]:
    """角色说话时优先落在哪个频道上；不挂频道就返回 None。"""
    channels = world.channels_for(character_id)
    return channels[0] if channels else None


def dialogue_event_for_turn(
    world: WorldState,
    store: EventStore,
    session_id: str,
    turn: Turn,
) -> Event:
    """把一次被接受的角色发言表示成事件。

    落点由权威世界状态推导，不看遗留 scene 的散文地名：角色挂着线上频道
    就是频道事件，否则就是所在物理地点的事件。scope 是传播边界，谁真的
    听见由后续的 Exposure 阶段决定，这里不做。
    """
    actor = turn.character
    location_id = world.location_of(actor)
    channel_id = _primary_channel(world, actor)

    if channel_id is not None:
        channel = world.channels.get(channel_id)
        event_type = (
            EventType.MESSAGE_SENT
            if channel.kind is ChannelKind.TEXT
            else EventType.DIALOGUE_SPOKEN
        )
        scope = EventScope.CHANNEL
        participants = world.channel_participants(channel_id)
    elif location_id is not None:
        event_type = EventType.DIALOGUE_SPOKEN
        scope = EventScope.LOCATION
        participants = world.characters_at(location_id)
    else:
        raise EventCommitError(
            f"角色 '{actor}' 既不在任何地点也不在任何频道，无法提交发言事件"
        )

    latest = store.latest()
    return Event(
        event_id=f"{session_id}:t{turn.turn_number}:dialogue",
        type=event_type,
        occurred_at=world.clock,
        scope=scope,
        actor_id=actor,
        participants=participants,
        location_id=location_id,
        channel_id=channel_id,
        payload={"text": turn.response, "char_name": turn.char_name},
        # 回指对应的生成记录：事件说"世界里发生了这句话"，provenance 说
        # "这句话是哪次模型生成、被哪个 Router 判过分之后被接受的"。
        provenance={
            "kind": "generation",
            "session_id": session_id,
            "turn_number": turn.turn_number,
            "recorded_at": turn.timestamp,
            "generator_provider": turn.generator_provider,
            "generator_model": turn.generator_model,
            "evaluator_provider": turn.evaluator_provider,
            "evaluator_model": turn.evaluator_model,
            "drift_score": turn.score,
            "is_ooc": turn.is_ooc,
            "router_reference_status": turn.router_reference_status,
        },
        causation_id=latest.event_id if latest is not None else None,
        correlation_id=session_id,
    )


def project_turn_message(committed: Dict, turn: Turn) -> Dict:
    """遗留 turn 消息 = 已提交事件 + 生成记录的投影。

    形状保持不变（客户端读的字段一个没动），只额外带上 event_id，让这条
    消息能被追回到它所投影的那条世界历史事件。消息本身不是权威存储。
    """
    return {
        "type": "turn",
        **turn.to_wire_dict(),
        "event_id": committed["event_id"],
    }
