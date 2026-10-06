# pns/runtime/world2_replay.py — 恢复时从 WORLD-2 基准加后缀重放，与快照互验（设计 v3 §4.5）
#
# 用过 WORLD-2 的世界，名单、地点图、授予不再能从开局 origin 推出来。恢复时：
#
#   1. 世界历史里有任何一条 WORLD-2 操作，第一条就必须是带基准的地点扩展；
#   2. 基准本身要接得上：前缀条数与指纹是它之前那段历史，时钟是那一刻，日历没变，
#      名单就是 origin 的 resident，账本重放到它的序号恰好是它记下的已采用版本
#      （入住采用若被搬到基准之前，重放到那个序号时就会多出一项，在这里露出来）；
#   3. 从基准重建一份世界，把基准之后的**每一条**事件照提交边界的同一套校验与状态
#      效果重放一遍（_commit_event），推导出名单、地点图、授予、位置、频道在场、
#      活动；与恢复出来的快照逐项比；
#   4. 账本里的入住采用与历史里的入住事件一一对应（内容项、指纹、operation_id、先后），
#      而且都在基准之后。
#
# 任何一项不符都拒绝打开（fail closed），不自动修复，也不从快照重新生成基准。
# 从没用过 WORLD-2 的存档什么都不查（只要求账本里没有入住采用）。
#
# 保证范围如实收窄：基准以前的世界只按既有的 archive / ledger 校验；拥有整份存档
# 写权限、把基准、事件、账本、快照一致重写的人，这里防不住。
from typing import Dict, List, Tuple

from pns.models.content_ledger import ContentLedgerError, genesis_from_origin
from pns.models.event import AUTHORITY_EVENT_TYPES, EventType
from pns.models.event_store import EventStore
from pns.models.frozen import thaw_json_value
from pns.models.session import SessionState
from pns.models.world_state import WorldState
from pns.runtime.event_commit import _commit_event
from pns.runtime.formal_world import rhythm_subject
from pns.runtime.world2_seed import SeedError, activation_from_seed
from pns.runtime.world2_baseline import (
    WORLD_FIELDS,
    BaselineError,
    calendar_binding,
    check_rhythms_against_adopted,
    decode_baseline_rhythms,
    events_fingerprint,
)


class WorldReplayError(ValueError):
    """WORLD-2 的基准、后缀历史、账本与快照互相对不上：这份存档不能打开。"""


def verify_world2_history(state: SessionState) -> None:
    events = state.events.events()
    positions = [i for i, event in enumerate(events) if event.type in AUTHORITY_EVENT_TYPES]
    ledger = state.content
    if not positions:
        if ledger is not None and ledger.admissions:
            raise WorldReplayError("账本里有入住采用，世界历史里却没有任何 WORLD-2 操作")
        return
    if ledger is None or state.world_state is None:
        raise WorldReplayError("有 WORLD-2 操作的世界必须带世界状态与内容账本")

    start = positions[0]
    first = events[start]
    if first.type is not EventType.WORLD_LOCATIONS_EXTENDED or first.payload["baseline"] is None:
        raise WorldReplayError("世界上第一笔 WORLD-2 操作不是带重放基准的地点扩展")
    for index in positions[1:]:
        later = events[index]
        if later.type is EventType.WORLD_LOCATIONS_EXTENDED and later.payload["baseline"] is not None:
            raise WorldReplayError("世界历史里出现了第二份重放基准")

    baseline = thaw_json_value(first.payload["baseline"])
    try:
        rhythms = decode_baseline_rhythms(baseline)
    except BaselineError as e:
        raise WorldReplayError(str(e)) from None
    _check_anchoring(state, baseline, rhythms, start)

    world = _world_from(baseline)
    roster = list(baseline["roster"])
    admitted: List[Tuple[str, str, str]] = []
    seeds = []
    scratch = EventStore()
    for event in events[start:]:
        # 提交时 occurred_at 就是世界时钟；时钟推进不一定都有事件（安静分钟），
        # 所以重放按事件自己的时刻对齐时钟，不重放推进本身。
        world.clock = event.occurred_at
        try:
            _commit_event(world, scratch, event)
        except (ValueError, KeyError, TypeError) as e:
            raise WorldReplayError(
                f"从基准重放到事件 '{event.event_id}' 时不成立：{e}"
            ) from None
        if event.type is EventType.WORLD_RESIDENT_ADMITTED:
            payload = event.payload
            roster.append(payload["character_id"])
            admitted.append(
                (
                    rhythm_subject(payload["character_id"]),
                    payload["fingerprints"]["rhythm"],
                    payload["operation_id"],
                )
            )
            seeds.append(event)
    world.clock = state.world_state.clock

    if roster != list(state.characters):
        raise WorldReplayError("名单与从基准重放推出的不一致")
    replayed, snapshot = _comparable(world), _comparable(state.world_state)
    for name in replayed:
        if replayed[name] != snapshot[name]:
            raise WorldReplayError(f"快照里的 {name} 与从基准重放推出的不一致")
    recorded = [(a.subject, a.fingerprint, a.operation_id) for a in ledger.admissions]
    if recorded != admitted:
        raise WorldReplayError("账本里的入住采用与世界历史里的入住事件对不上")
    for event in seeds:
        _check_seed(state, event)


def _check_seed(state: SessionState, event) -> None:
    """新居民的周期激活还在队列里，而且只是按自己的周期往前推过。

    周期激活触发后换成下一次（相位不变，见 ScheduledActivation.next_occurrence），
    运行期没有别的入口会摘掉它。所以：在、只在一处、身份与节律原样、到期时刻是
    初次到期加整数个周期。
    """
    character_id = event.payload["character_id"]
    try:
        seeded = activation_from_seed(
            event.payload["seed"], character_id=character_id, admitted_at=event.occurred_at
        )
    except SeedError as e:
        raise WorldReplayError(f"入住事件的排期种子不成立：{e}") from None
    queue = state.activations
    if not queue.has(seeded.activation_id):
        raise WorldReplayError(f"新居民 '{character_id}' 有入住记录，队列里却没有她的种子排期")
    current = queue.get(seeded.activation_id)
    if (current.kind, current.character_id, current.interval_minutes, dict(current.payload)) != (
        seeded.kind,
        seeded.character_id,
        seeded.interval_minutes,
        dict(seeded.payload),
    ):
        raise WorldReplayError(f"新居民 '{character_id}' 的种子排期与入住记录不符")
    recurring = [
        a.activation_id
        for a in queue.for_character(character_id)
        if a.kind is seeded.kind and a.is_recurring
    ]
    if recurring != [seeded.activation_id]:
        raise WorldReplayError(f"新居民 '{character_id}' 的周期激活不止种子一条（双播）")
    elapsed = (current.due_at - seeded.due_at).total_seconds() / 60
    if elapsed < 0 or elapsed % seeded.interval_minutes:
        raise WorldReplayError(f"新居民 '{character_id}' 的种子排期不在自己的周期相位上")


def _check_anchoring(state: SessionState, baseline: Dict, rhythms, start: int) -> None:
    """基准接得上它之前的世界：前缀、时刻、日历、origin、账本。"""
    events = state.events.events()
    if baseline["prefix"] != {"length": start, "fingerprint": events_fingerprint(events[:start])}:
        raise WorldReplayError("重放基准记下的事件前缀与它之前的历史不符")
    if baseline["clock"] != events[start].occurred_at.isoformat():
        raise WorldReplayError("重放基准的时钟不是第一笔 WORLD-2 操作的时刻")
    if baseline["calendar"] != calendar_binding():
        # 选表行为变了：同一份作息会在不同的日子换表。要明确迁移，不能悄悄重放。
        raise WorldReplayError("日历与重放基准记下的不一致（需要明确迁移）")
    origin = state.world_state.metadata.get("origin")
    roster = baseline["roster"]
    if not isinstance(roster, list) or not isinstance(origin, dict):
        raise WorldReplayError("重放基准的名单或开局来源形状不对")
    if roster != list(origin.get("residents") or ()):
        raise WorldReplayError("重放基准的名单不是开局来源里的 resident")
    if set(rhythms) != set(roster):
        raise WorldReplayError("重放基准里的作息与名单不一致")
    ledger_part = baseline["ledger"]
    if not isinstance(ledger_part, dict) or set(ledger_part) != {"next_seq", "adopted"}:
        raise WorldReplayError("重放基准的账本字段不对")
    ledger = state.content
    try:
        before = ledger.adopted_before(genesis_from_origin(origin), ledger_part["next_seq"])
    except ContentLedgerError as e:
        raise WorldReplayError(f"重放基准的账本序号不成立：{e}") from None
    if before != ledger_part["adopted"]:
        raise WorldReplayError("账本重放到基准的序号时，已采用版本与基准记下的不同")
    try:
        check_rhythms_against_adopted(rhythms, ledger_part["adopted"])
    except BaselineError as e:
        raise WorldReplayError(str(e)) from None


def _world_from(baseline: Dict) -> WorldState:
    world_part = baseline["world"]
    if not isinstance(world_part, dict) or set(world_part) != set(WORLD_FIELDS):
        raise WorldReplayError("重放基准的世界状态字段不对")
    try:
        world = WorldState.from_dict(
            {
                "clock": baseline["clock"],
                "locations": baseline["locations"],
                "channels": baseline["channels"],
                **world_part,
            }
        )
        world.validate()
    except (ValueError, KeyError, TypeError) as e:
        raise WorldReplayError(f"重放基准里的世界状态不成立：{e}") from None
    return world


def _comparable(world: WorldState) -> Dict:
    snapshot = world.to_dict()
    result = {name: snapshot[name] for name in ("locations", "channels") + WORLD_FIELDS}
    # 空的在场名单与没有这一项是同一个状态。
    result["channel_members"] = {
        channel_id: members for channel_id, members in snapshot["channel_members"].items() if members
    }
    return result


__all__ = ["WorldReplayError", "verify_world2_history"]
