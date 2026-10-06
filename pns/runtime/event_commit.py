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
from typing import Dict, Optional, Tuple

from pns.models.channel import ChannelKind
from pns.models.event import AUTHORITY_EVENT_TYPES, Event, EventScope, EventType
from pns.models.event_store import EventStore
from pns.models.location import access_admits
from pns.models.observation import Observation
from pns.models.session import SessionState, Turn
from pns.models.world_state import ActivityKind, WorldState
from pns.runtime.exposure import evaluate_event_exposure, observations_for
from pns.runtime.formal_world import grants_fingerprint, rhythm_fingerprint, rhythm_subject
from pns.runtime.world2_baseline import BaselineError, check_baseline_at_commit
from pns.runtime.world2_seed import SeedError, activation_from_seed
from pns.world.extension import ExtensionError, extended_graph, graph_fingerprint
from pns.world.grants import (
    CharacterGrants,
    GrantError,
    decode_grants,
    install_grants,
    validate_grants_against,
)
from pns.world.rhythm import DailyRhythm, RhythmError, decode_rhythm


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

    if event.occurred_at != world.clock:
        raise EventCommitError(
            f"事件 '{event.event_id}' 的时间 {event.occurred_at.isoformat()} "
            f"与当前世界时钟 {world.clock.isoformat()} 不一致"
        )

    # 授权在事实判断之前：一个无权进入/加入的角色，不管此刻在不在那里，都不能
    # 靠一条事件把自己放进去。授予来自世界的静态结构（见 WorldState.may_enter /
    # may_join），调用方是谁、为什么想去都不影响答案（Articles VIII–IX）。
    if event.type is EventType.PRESENCE_JOINED_CHANNEL and not world.may_join(
        event.actor_id, event.channel_id
    ):
        raise EventCommitError(
            f"角色 '{event.actor_id}' 不是频道 '{event.channel_id}' 的成员，不能加入"
        )
    if event.type is EventType.CHARACTER_LOCATION_CHANGED and not world.may_enter(
        event.actor_id, event.location_id
    ):
        raise EventCommitError(
            f"角色 '{event.actor_id}' 没有进入 '{event.location_id}' 的授予"
        )
    if event.type is EventType.PRESENCE_JOINED_CHANNEL and world.is_in_channel(
        event.actor_id, event.channel_id
    ):
        raise EventCommitError(
            f"角色 '{event.actor_id}' 已经在频道 '{event.channel_id}' 中"
        )
    if event.type is EventType.PRESENCE_LEFT_CHANNEL and not world.is_in_channel(
        event.actor_id, event.channel_id
    ):
        raise EventCommitError(
            f"角色 '{event.actor_id}' 不在频道 '{event.channel_id}' 中，不能离开"
        )
    if (
        event.type is EventType.CHARACTER_LOCATION_CHANGED
        and world.location_of(event.actor_id) == event.location_id
    ):
        raise EventCommitError(
            f"角色 '{event.actor_id}' 已经位于 '{event.location_id}'"
        )
    if event.type is EventType.CHARACTER_ACTIVITY_CHANGED:
        try:
            activity = ActivityKind(event.payload["activity"])
        except (KeyError, ValueError):
            raise EventCommitError(
                f"事件 '{event.event_id}' 引用了未知的角色活动"
            ) from None
        if world.activity_of(event.actor_id).kind is activity:
            raise EventCommitError(
                f"角色 '{event.actor_id}' 已经处于活动 '{activity.value}'"
            )
    if event.type is EventType.WORLD_LOCATIONS_EXTENDED:
        _extension_of(world, event)
    if event.type is EventType.WORLD_RESIDENT_ADMITTED:
        _admission_of(world, event)


# ── WORLD-2 的两种权威操作 ──────────────────────────────────────────────
#
# 校验与应用都从 payload 重新推导同一份结果（新图 / 作息与授予），所以"校验过的"
# 和"生效的"不可能是两样东西。这里只判"这一笔对这个世界是否成立"；入住窗口、
# 室友是否都睡着、前身扫描、账本与排期由维护入口在闸门内另行判定。
def _extension_of(world: WorldState, event: Event):
    payload = event.payload
    if payload["graph_before"] != graph_fingerprint(world.locations):
        raise EventCommitError("地点扩展的 graph_before 与世界当前的地点图不符")
    try:
        graph = extended_graph(
            world.locations, payload["locations"], payload["append_connections"]
        )
    except ExtensionError as e:
        raise EventCommitError(f"地点扩展不成立：{e}") from None
    if payload["graph_after"] != graph_fingerprint(graph):
        raise EventCommitError("地点扩展的 graph_after 与扩展结果不符")
    new_ids = set(graph.ids()) - set(world.locations.ids())
    for character_id, grants in world.location_grants.items():
        if new_ids & set(grants):
            raise EventCommitError(
                f"已有角色 '{character_id}' 持有新地点的授予：扩展不能改变已有居民的可进入范围"
            )
    return graph


def _admission_of(world: WorldState, event: Event) -> Tuple[DailyRhythm, CharacterGrants]:
    payload = event.payload
    character_id = payload["character_id"]
    if not isinstance(character_id, str) or not character_id:
        raise EventCommitError("入住的 character_id 必须是非空字符串")
    if (
        character_id in world.known_characters()
        or character_id in world.location_grants
        or character_id in world.channel_grants
    ):
        raise EventCommitError(f"世界里已经有 '{character_id}' 的记录，不能首次入住")
    try:
        rhythm = decode_rhythm(payload["rhythm"], character_id=character_id)
        grants = decode_grants(payload["grants"], character_id=character_id)
        validate_grants_against(grants, world.locations, world.channels)
    except (RhythmError, GrantError) as e:
        raise EventCommitError(f"入住的作息或授予不成立：{e}") from None
    fingerprints = payload["fingerprints"]
    expected = {"rhythm": rhythm_fingerprint(rhythm), "grants": grants_fingerprint(grants)}
    if not hasattr(fingerprints, "keys") or dict(fingerprints) != expected:
        raise EventCommitError("入住 payload 的指纹与其中的作息 / 授予不符")

    location_id = payload["location_id"]
    if not isinstance(location_id, str) or not world.locations.has(location_id):
        raise EventCommitError(f"入住地点不存在：{location_id!r}")
    if not access_admits(
        world.locations.get(location_id).access, grants.location_roles().get(location_id)
    ):
        raise EventCommitError(f"'{character_id}' 的授予不允许她出现在 '{location_id}'")
    try:
        activity = ActivityKind(payload["activity"])
    except ValueError:
        raise EventCommitError(f"入住活动未知：{payload['activity']!r}") from None
    channel_id = payload["channel_id"]
    if channel_id is not None and (
        not isinstance(channel_id, str)
        or not world.channels.has(channel_id)
        or channel_id not in grants.channels
    ):
        raise EventCommitError(f"入住频道不成立：{channel_id!r}")

    # 初始状态必须就是作息表此刻那一段：入住不是一次"临时安排"。
    segment = rhythm.segment_at(event.occurred_at)
    if (segment.location_id, segment.activity, segment.channel_id) != (
        location_id,
        activity,
        channel_id,
    ):
        raise EventCommitError("入住的地点 / 活动 / 频道与她此刻的作息段不一致")

    roommates = payload["roommates"]
    if (
        not isinstance(roommates, (list, tuple))
        or not all(isinstance(item, str) and item for item in roommates)
        or len(set(roommates)) != len(roommates)
        or character_id in roommates
    ):
        raise EventCommitError("入住的 roommates 必须是不含本人、不重复的角色 id 列表")
    try:
        activation_from_seed(
            payload["seed"], character_id=character_id, admitted_at=event.occurred_at
        )
    except SeedError as e:
        raise EventCommitError(f"入住的排期种子不成立：{e}") from None
    return rhythm, grants


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


def _apply_activity_changed(world: WorldState, event: Event) -> None:
    world.set_activity(event.actor_id, event.payload["activity"])


def _apply_locations_extended(world: WorldState, event: Event) -> None:
    world._replace_locations(_extension_of(world, event))


def _apply_resident_admitted(world: WorldState, event: Event) -> None:
    # 顺序固定：先装授予（放置要靠它），再放置、设活动、入频道。
    _, grants = _admission_of(world, event)
    character_id = event.payload["character_id"]
    install_grants(world, grants)
    world.place_character(character_id, event.payload["location_id"])
    world.set_activity(character_id, event.payload["activity"])
    if event.payload["channel_id"] is not None:
        world.join_channel(character_id, event.payload["channel_id"])


_APPLY = {
    EventType.DIALOGUE_SPOKEN: _apply_nothing,
    EventType.MESSAGE_SENT: _apply_nothing,
    EventType.PRESENCE_JOINED_CHANNEL: _apply_joined_channel,
    EventType.PRESENCE_LEFT_CHANNEL: _apply_left_channel,
    EventType.WORLD_TIME_ADVANCED: _apply_time_advanced,
    EventType.CHARACTER_LOCATION_CHANGED: _apply_location_changed,
    EventType.CHARACTER_ACTIVITY_CHANGED: _apply_activity_changed,
    EventType.WORLD_LOCATIONS_EXTENDED: _apply_locations_extended,
    EventType.WORLD_RESIDENT_ADMITTED: _apply_resident_admitted,
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


# ── 作息的身份 ──────────────────────────────────────────────────────────
#
# 作息凭 provenance 认领"本段自己做的决定"（见 pns/runtime/rhythm.py）。这份
# 身份只能由作息自己的提交入口签发：别的事件带上它，一个外部决定就会被当成
# 作息自己的，行程接着往下走，外部决定压过作息的契约随之失效。
RHYTHM_PROVENANCE_KIND = "daily_rhythm"
RHYTHM_RESERVED_PROVENANCE = frozenset({"segment_key", "trip_leg"})


def claims_rhythm(provenance) -> bool:
    """这份 provenance 是否带着作息的身份（种类或保留字段）。"""
    provenance = provenance or {}
    return provenance.get("kind") == RHYTHM_PROVENANCE_KIND or any(
        key in provenance for key in RHYTHM_RESERVED_PROVENANCE
    )


def _refuse_rhythm_identity(event: Event) -> None:
    if isinstance(event, Event) and claims_rhythm(event.provenance):
        raise EventCommitError(
            "只有作息自己能提交带作息 provenance（kind/segment_key/trip_leg）的事件"
        )
    if isinstance(event, Event) and event.type in AUTHORITY_EVENT_TYPES:
        # 公共提交入口（Agency、模型输出、HTTP 改活动）提不出权威操作。
        raise EventCommitError(
            f"{event.type.value} 只能经世界的维护入口提交（commit_authority_event）"
        )


# ── 提交 ────────────────────────────────────────────────────────────────
def commit_event(world: WorldState, store: EventStore, event: Event) -> Dict:
    """接受一个事件：应用状态效果并追加到世界历史，两者同生共死。

    返回一份稳定投影（事件的完整公开形状 + 它在世界历史里的序号），供下游
    使用；下游拿到的是新的可变结构，改它影响不到已提交的事件。
    """
    _refuse_rhythm_identity(event)
    return _commit_event(world, store, event)


def _commit_event(world: WorldState, store: EventStore, event: Event) -> Dict:
    if not isinstance(store, EventStore):
        raise EventCommitError("世界历史必须是 EventStore")

    validate_against_world(world, event)
    store._check_can_append(event)

    snapshot = world.snapshot_mutable_state()
    length = len(store)
    try:
        apply_event(world, event)
        sequence = store._append(event)
    except BaseException:
        world._restore_mutable_state(snapshot)
        store._rollback_to(length)
        raise

    return {"sequence": sequence, **event.to_dict()}


# ── 阶段三：曝光 ────────────────────────────────────────────────────────
#
# 事件被接受之后，逐个候选角色判定"能不能感知到"，通过的才生成观察。这一步
# 在提交边界里做，而不是留给调用方，因为它必须满足两条：
#
#   1. 进了会话的已提交事件没有一条能跳过曝光判定 —— 跳过就等于回到全知。
#      （commit_event() 本身只认 world + store 这一对，没有会话可以承载观察，
#      所以它不做曝光；会话路径只有下面两个入口，都走判定。）
#   2. 判定结果和事件同生共死 —— 提交失败时观察不能留下。
#
# 判定跑在**状态效果应用之后**：事件被接受之后的世界才是它发生的那个世界。
# 于是"某人进了房间"这件事由房间里现在的人感知到。反过来，"某人离开了"
# 目前只有一条事件、一个落点，原地的人感知不到离开 —— 那需要一条独立的
# 离开事件，属于后续阶段，不在这里偷偷补。
def _record_exposure(state: SessionState, event: Event) -> Tuple[Observation, ...]:
    decisions = evaluate_event_exposure(state.world_state, event)
    observations = observations_for(event, decisions)
    state.record_observations(decisions, observations)
    return observations


def commit_session_event(state: SessionState, event: Event) -> Dict:
    """在一个会话里提交事件，失败时连会话状态一起回滚。"""
    _refuse_rhythm_identity(event)
    return _commit_session_event(state, event)


def _commit_rhythm_event(state: SessionState, event: Event) -> Dict:
    """作息自己的提交入口：只收带作息身份的事件。只给作息的时钟步用。"""
    if not isinstance(event, Event) or event.provenance.get("kind") != RHYTHM_PROVENANCE_KIND:
        raise EventCommitError("作息的提交入口只收作息事件")
    return _commit_session_event(state, event)


def _commit_session_event(state: SessionState, event: Event) -> Dict:
    with state.atomic_commit():
        projection = _commit_event(state.world_state, state.events, event)
        _record_exposure(state, event)
    return projection


def commit_authority_event(state: SessionState, event: Event) -> Dict:
    """WORLD-2 的权威操作提交入口。**只给世界的维护入口用**（C8 的入住服务）。

    跟会话事件同一个原子边界：世界状态效果 + 追加历史 + 会话侧效果（入住时把她加进
    名单、开空的历史槽位）同生共死。**不做曝光**：这两种操作没有任何人感知得到，连一条
    "没感知到"的判定都不写。调用方可以把它放进自己更大的事务里（账本、排期、导演发布），
    嵌套事务一起回滚。
    """
    if not isinstance(event, Event) or event.type not in AUTHORITY_EVENT_TYPES:
        raise EventCommitError("权威提交入口只收 WORLD-2 的权威操作")
    if claims_rhythm(event.provenance):
        raise EventCommitError("权威操作不能带作息 provenance")
    if state.content is None:
        raise EventCommitError("WORLD-2 的权威操作只用于带内容账本的正式世界")
    with state.atomic_commit():
        _check_baseline_order(state, event)
        projection = _commit_event(state.world_state, state.events, event)
        if event.type is EventType.WORLD_RESIDENT_ADMITTED:
            payload = event.payload
            state._admit_character(payload["character_id"])
            _schedule_seed(state, event)
            # 入住采用：她的作息作为一项新内容进账本，与已有记录、决定共用一条序号。
            state.set_content(
                state.content.admitted(
                    rhythm_subject(payload["character_id"]),
                    payload["fingerprints"]["rhythm"],
                    operation_id=payload["operation_id"],
                )
            )
    return projection


def _schedule_seed(state: SessionState, event: Event) -> None:
    """排入新居民的周期激活。旧居民的排期一条不动；同名排期已在队列里就拒绝。"""
    activation = activation_from_seed(
        event.payload["seed"],
        character_id=event.payload["character_id"],
        admitted_at=event.occurred_at,
    )
    queue = state.activations
    try:
        queue._check_can_append(activation)
    except ValueError as e:
        raise EventCommitError(f"新居民的排期排不进去：{e}") from None
    queue._append(activation)


def _check_baseline_order(state: SessionState, event: Event) -> None:
    """世界上第一笔 WORLD-2 操作必须是带基准的地点扩展；之后谁都不再带基准。

    operation_id 在全部 WORLD-2 事件里唯一：同一个 id 只能对应一个提交结果，
    查询与重试才认得出是哪一笔（扩展不进账本，账本互验管不到它）。
    """
    operation_id = event.payload["operation_id"]
    if any(
        e.type in AUTHORITY_EVENT_TYPES and e.payload["operation_id"] == operation_id
        for e in state.events
    ):
        raise EventCommitError(f"operation_id '{operation_id}' 已经被另一笔 WORLD-2 操作用过")
    first = not any(e.type in AUTHORITY_EVENT_TYPES for e in state.events)
    if event.type is EventType.WORLD_RESIDENT_ADMITTED:
        if first:
            raise EventCommitError("世界上第一笔 WORLD-2 操作必须是带重放基准的地点扩展")
        return
    baseline = event.payload["baseline"]
    if not first:
        if baseline is not None:
            raise EventCommitError("重放基准只在世界上第一笔 WORLD-2 操作里记一次")
        return
    if baseline is None:
        raise EventCommitError("世界上第一笔 WORLD-2 操作必须带重放基准")
    try:
        check_baseline_at_commit(state, baseline)
    except BaselineError as e:
        raise EventCommitError(f"重放基准不成立：{e}") from None


def commit_dialogue(state: SessionState, turn: Turn, event: Event) -> Dict:
    """提交一次被接受的发言：世界历史里的事件 + 生成审计里的 Turn。

    三者要么都落地，要么都不落地 —— 不允许出现一条没有对应事件的 turn，
    不允许出现一条没有对应生成记录的发言事件，也不允许出现一条没被任何人
    感知过、却已经抄进所有人历史的台词。
    """
    with state.atomic_commit():
        projection = commit_event(state.world_state, state.events, event)
        observations = _record_exposure(state, event)
        state.record_turn(turn, observations)
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
