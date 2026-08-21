# pns/runtime/agency/effects.py — 被接受的提案 → 一条事件
#
# 这是 Agency 和 P5 提交边界之间**唯一**的接合处。一条提案要变成世界真相，
# 只有这一条路：在这里被翻译成 Event，然后交给 commit_session_event()。
#
# 翻译规则全部来自目录声明，不来自提案自己：
#
#   事件类型 / 传播 scope   目录说了算，提案没有字段能影响它
#   落点（地点 / 频道）      由目标类型决定，目标本身已经过前置条件校验
#   payload                只搬目录声明过的键
#
# 于是"任意字典改不动 WorldState"这句话有两道锁：提案构造时未声明的键就被
# 拒了，这里再按目录过一遍。第二道锁不是冗余 —— 它保证的是"就算以后有人
# 造出一条绕过构造校验的提案，也搬不出多余的键"。
from typing import Dict, Optional, Tuple

from pns.models.action import ActionId, ActionProposal, TargetKind
from pns.models.activation import ActivationDue
from pns.models.event import Event, EventScope
from pns.models.event_store import EventStore
from pns.models.world_state import WorldState


class AgencyEffectError(ValueError):
    """提案翻译不成事件（目标类型没覆盖、落点算不出来）。"""


def _anchor(
    world: WorldState, proposal: ActionProposal
) -> Tuple[Optional[str], Optional[str]]:
    """算出这条动作落在哪个地点 / 哪个频道上。"""
    definition = proposal.definition
    kind = definition.target_kind
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
    """提交那一刻的在场快照。

    这是历史事实，不是访问规则：曝光层对 channel / location 档从不拿
    participants 当授权依据（P6 已经写死了这一条），它在这两档里只用来说明
    "被接受的时候屋里/频道里有谁"。

    移动动作不给名单：它的 location_id 是**目的地**，而状态效果还没应用，
    此刻那份名单马上就会作废，写下一份注定过期的花名册不如不写。
    """
    if proposal.action_id is ActionId.MOVE_TO:
        return ()
    if channel_id is not None:
        return tuple(world.channel_participants(channel_id))
    if location_id is not None:
        return tuple(world.characters_at(location_id))
    return ()


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

    这个函数是纯的：它只读世界和事件历史来算落点与因果链，不改任何东西。
    事件能不能被接受，由 P5 的提交边界再判一次（引用完整性、时钟一致、
    状态转移是否可能）—— Agency 不越过它，也不重复它。
    """
    if not isinstance(proposal, ActionProposal):
        raise AgencyEffectError("只能翻译 ActionProposal")
    if not isinstance(world, WorldState):
        raise AgencyEffectError("翻译提案需要一份权威 WorldState")

    definition = proposal.definition
    location_id, channel_id = _anchor(world, proposal)
    participants = _participants(world, proposal, channel_id, location_id)

    scope = definition.event_scope
    if scope in (EventScope.PRIVATE, EventScope.PARTICIPANT):
        # 目录里现在没有这两档的动作；真加了的话，参与者名单必须由动作显式
        # 声明，而不是从在场快照里推 —— 那两档的 participants 是授权依据。
        raise AgencyEffectError(
            f"动作 '{definition.action_id.value}' 声明的 scope {scope.value} "
            "还没有已实现的参与者语义"
        )

    latest = store.latest()
    return Event(
        event_id=proposal.derived_event_id(session_id),
        type=definition.event_type,
        occurred_at=world.clock,
        scope=scope,
        actor_id=proposal.character_id,
        participants=participants,
        location_id=location_id,
        channel_id=channel_id,
        payload=proposal.event_payload(),
        # 系统侧簿记：这条事件是哪次到期、哪条提案、哪个策略产出的。
        # provenance 从不进任何角色的观察（P6 的投影白名单挡着），所以角色
        # 不会因此知道"我这一步是被某个策略选出来的"。
        provenance={
            "kind": "agency",
            "session_id": session_id,
            "due_id": due.due_id,
            "activation_id": due.activation_id,
            "proposal_id": proposal.proposal_id,
            "action_id": definition.action_id.value,
            "policy": policy,
        },
        causation_id=latest.event_id if latest is not None else None,
        correlation_id=session_id,
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
        "payload_keys": sorted(proposal.event_payload()),
    }


__all__ = ["AgencyEffectError", "debug_projection", "event_for_proposal"]
