# pns/runtime/agency/effects.py — 被接受的提案 → 一条事件
#
# 这是 Agency 和 P5 提交边界之间**唯一**的接合处。一条提案要变成世界真相，
# 只有这一条路：在这里被翻译成 Event，然后交给 commit_session_event()。
#
# 事件长什么样不由这里决定，由目录声明决定 —— 具体的字段构造在
# `pns.models.action.agency_event_fields()`，会话存档恢复时的核对走的是同一
# 段代码的反方向。这个模块只负责它自己那份**世界相关**的输入：
#
#   落点        由目标类型决定；无目标的动作落在角色当时所在的地点
#   在场名单     由目录声明的来源决定，取提交那一刻的快照
#   causation   世界历史里的上一条事件
#
# 于是"任意字典改不动 WorldState"有两道锁：提案构造时未声明的键就被拒了，
# `event_payload()` 再按目录过一遍；而"事件内容必须是这条提案产出的"这件事，
# 构造和校验共用同一份声明，松不下来。
from typing import Dict, Optional, Tuple

from pns.models.action import (
    ActionProposal,
    ParticipantSource,
    TargetKind,
    agency_event_fields,
)
from pns.models.activation import ActivationDue
from pns.models.event import Event
from pns.models.event_store import EventStore
from pns.models.world_state import WorldState


class AgencyEffectError(ValueError):
    """提案翻译不成事件（目标类型没覆盖、落点算不出来）。"""


def _anchor(
    world: WorldState, proposal: ActionProposal
) -> Tuple[Optional[str], Optional[str]]:
    """算出这条动作落在哪个地点 / 哪个频道上。"""
    kind = proposal.definition.target_kind
    if kind is TargetKind.CHANNEL:
        # 频道动作不带地点：从频道里发生的事不透出发起者坐在哪个房间。
        return None, proposal.target_id
    if kind is TargetKind.LOCATION:
        return proposal.target_id, None
    if kind is TargetKind.NONE:
        location_id = world.location_of(proposal.character_id)
        if location_id is None:
            # 前置条件 ACTOR_HAS_LOCATION 应该已经拦下了；走到这里说明有人
            # 绕过了校验通道。
            raise AgencyEffectError(
                f"角色 '{proposal.character_id}' 不在任何地点，动作 "
                f"'{proposal.action_id.value}' 没有落点"
            )
        return location_id, None
    raise AgencyEffectError(f"未知的目标类型: {kind!r}")


def _participants(
    world: WorldState, proposal: ActionProposal, channel_id, location_id
) -> Tuple[str, ...]:
    """提交那一刻的在场快照，来源由目录声明。

    这是历史事实，不是访问规则：曝光层对 channel / location 档从不拿
    participants 当授权依据（P6 已经写死了这一条），它在这两档里只用来说明
    "被接受的时候屋里/频道里有谁"。
    """
    source = proposal.definition.participants_from
    if source is ParticipantSource.NONE:
        return ()
    if source is ParticipantSource.CHANNEL_MEMBERS:
        if channel_id is None:
            raise AgencyEffectError(
                f"动作 '{proposal.action_id.value}' 声明从频道成员取在场名单，"
                "却没有频道落点"
            )
        return tuple(world.channel_participants(channel_id))
    if source is ParticipantSource.LOCATION_OCCUPANTS:
        if location_id is None:
            raise AgencyEffectError(
                f"动作 '{proposal.action_id.value}' 声明从在场者取名单，"
                "却没有地点落点"
            )
        return tuple(world.characters_at(location_id))
    raise AgencyEffectError(f"未知的在场名单来源: {source!r}")


def event_for_proposal(
    world: WorldState,
    store: EventStore,
    session_id: str,
    due: ActivationDue,
    proposal: ActionProposal,
    *,
    policy: str = "",
) -> Event:
    """把一条已经通过校验的提案表示成一条待提交事件。

    这个函数是纯的：它只读世界和事件历史来算落点、在场名单与因果链，不改任何
    东西。事件能不能被接受，由 P5 的提交边界再判一次（引用完整性、时钟一致、
    状态转移是否可能）—— Agency 不越过它，也不重复它。

    需要台词的动作在这里就走不通：`agency_event_fields()` 直接拒绝，因为生成
    与 Router 判分链还没接进来。那不是可配置的，是这一阶段没有那条路径。
    """
    if not isinstance(proposal, ActionProposal):
        raise AgencyEffectError("只能翻译 ActionProposal")
    if not isinstance(world, WorldState):
        raise AgencyEffectError("翻译提案需要一份权威 WorldState")

    location_id, channel_id = _anchor(world, proposal)
    participants = _participants(world, proposal, channel_id, location_id)
    latest = store.latest()
    return Event(
        **agency_event_fields(
            session_id,
            due,
            proposal,
            occurred_at=world.clock,
            location_id=location_id,
            channel_id=channel_id,
            participants=participants,
            policy=policy,
        ),
        causation_id=latest.event_id if latest is not None else None,
    )


def debug_projection(world: WorldState, proposal: ActionProposal) -> Dict:
    """只读投影，给调试用：这条提案会落成什么样的事件骨架。"""
    location_id, channel_id = _anchor(world, proposal)
    definition = proposal.definition
    return {
        "action_id": definition.action_id.value,
        "event_type": definition.event_type.value,
        "event_scope": definition.event_scope.value,
        "location_id": location_id,
        "channel_id": channel_id,
        "participants_from": definition.participants_from.value,
        "payload_keys": sorted(proposal.event_payload()),
        "committable": not definition.requires_authored_text,
    }


__all__ = ["AgencyEffectError", "debug_projection", "event_for_proposal"]
