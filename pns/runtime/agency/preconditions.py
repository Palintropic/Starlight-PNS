# pns/runtime/agency/preconditions.py — 动作前置条件的求值
#
# 目录（pns/models/action.py）声明每个动作需要哪些前置条件；这里是那些条件
# 真正读 WorldState 得出答案的地方。分法跟曝光一样：码在 models，规则在
# runtime。
#
# 两条规矩：
#
#   1. 每个 Precondition 成员必须在这里有且只有一个求值器。少一个，声明它的
#      动作就会带着一条从来没被判过的条件跑；多一个没被枚举覆盖的求值器，
#      说明有条件是从别处偷偷加进来的。有测试盯着这个映射跟枚举完全相等。
#   2. 求值是**纯函数**：只读世界，不改世界。同一份世界快照必须给出同一个
#      答案 —— 提交那一刻要拿它重判一遍，判出不同结果只能是因为世界变了。
from typing import Callable, Dict, Optional, Tuple

from pns.models.action import (
    ActionId,
    LegalAction,
    Precondition,
    TargetKind,
    action_definition,
    catalogue_ids,
)
from pns.models.world_state import Availability, WorldState


def _actor_known(world: WorldState, actor: str, target: Optional[str]) -> bool:
    # 口径跟事件提交边界完全一样：世界认识谁，不看会话选了谁。
    return actor in world.known_characters()


def _actor_awake(world: WorldState, actor: str, target: Optional[str]) -> bool:
    # busy 不拦：忙着不等于不能行动，那是"要不要现在动"的判断，属于策略。
    # asleep 拦：睡着的角色不会自己发起动作。
    return world.availability_of(actor) is not Availability.ASLEEP


def _actor_has_location(world: WorldState, actor: str, target: Optional[str]) -> bool:
    return world.location_of(actor) is not None


def _target_location_exists(
    world: WorldState, actor: str, target: Optional[str]
) -> bool:
    return target is not None and world.locations.has(target)


def _target_location_is_elsewhere(
    world: WorldState, actor: str, target: Optional[str]
) -> bool:
    return target is not None and world.location_of(actor) != target


def _target_location_reachable(
    world: WorldState, actor: str, target: Optional[str]
) -> bool:
    """目标必须是当前所在地在位置图上的直接邻居。

    只认一步，不做寻路：一次动作就是一次移动。多步路线属于 Planner 的长程
    分解，那不在本阶段。
    """
    current = world.location_of(actor)
    if current is None or target is None:
        return False
    if not world.locations.has(current) or not world.locations.has(target):
        return False
    return target in world.locations.neighbors(current)


def _target_channel_exists(
    world: WorldState, actor: str, target: Optional[str]
) -> bool:
    return target is not None and world.channels.has(target)


def _actor_in_target_channel(
    world: WorldState, actor: str, target: Optional[str]
) -> bool:
    return target is not None and world.is_in_channel(actor, target)


def _actor_not_in_target_channel(
    world: WorldState, actor: str, target: Optional[str]
) -> bool:
    return target is not None and not world.is_in_channel(actor, target)


_EVALUATORS: Dict[
    Precondition, Callable[[WorldState, str, Optional[str]], bool]
] = {
    Precondition.ACTOR_KNOWN: _actor_known,
    Precondition.ACTOR_AWAKE: _actor_awake,
    Precondition.ACTOR_HAS_LOCATION: _actor_has_location,
    Precondition.TARGET_LOCATION_EXISTS: _target_location_exists,
    Precondition.TARGET_LOCATION_IS_ELSEWHERE: _target_location_is_elsewhere,
    Precondition.TARGET_LOCATION_REACHABLE: _target_location_reachable,
    Precondition.TARGET_CHANNEL_EXISTS: _target_channel_exists,
    Precondition.ACTOR_IN_TARGET_CHANNEL: _actor_in_target_channel,
    Precondition.ACTOR_NOT_IN_TARGET_CHANNEL: _actor_not_in_target_channel,
}


def evaluators() -> Dict[Precondition, Callable]:
    """只读副本，给覆盖面测试用。"""
    return dict(_EVALUATORS)


def failed_preconditions(
    world: WorldState, character_id: str, action_id, target_id: Optional[str]
) -> Tuple[Precondition, ...]:
    """按声明顺序返回没通过的前置条件；全过就是空元组。

    返回全部失败项而不是第一条：一条提案为什么不合法，说全了才是可查的事实。
    """
    definition = action_definition(action_id)
    failed = []
    for precondition in definition.preconditions:
        evaluate = _EVALUATORS.get(precondition)
        if evaluate is None:
            # 声明了一条没有求值器的条件 = 这条条件从来没被判过。
            raise KeyError(
                f"前置条件 {precondition.value} 没有求值器，动作 "
                f"'{definition.action_id.value}' 不能被判定"
            )
        if not evaluate(world, character_id, target_id):
            failed.append(precondition)
    return tuple(failed)


def is_legal(
    world: WorldState, character_id: str, action_id, target_id: Optional[str]
) -> bool:
    return not failed_preconditions(world, character_id, action_id, target_id)


def _candidate_targets(
    world: WorldState, kind: TargetKind
) -> Tuple[Optional[str], ...]:
    if kind is TargetKind.NONE:
        return (None,)
    if kind is TargetKind.LOCATION:
        return tuple(world.locations.ids())
    if kind is TargetKind.CHANNEL:
        return tuple(world.channels.ids())
    raise KeyError(f"未知的目标类型: {kind!r}")


def legal_actions(
    world: WorldState, character_id: str, limit: Optional[int] = None
) -> Tuple[Tuple[LegalAction, ...], bool]:
    """枚举这个角色此刻能合法做的一切，返回 (合法动作, 是否被截断)。

    枚举方式刻意是"目录 × 候选目标，逐个跑前置条件"，而不是每个动作各写一套
    快捷筛选：那会变成两套判断，迟早出现"枚举里有但提交时被拒"或者反过来
    "明明合法却枚举不出来"。等式「枚举结果 == 前置条件全过的组合」有测试盯着。

    顺序是确定的（动作 ID、然后目标 ID），不依赖字典迭代顺序。limit 是安全
    预算：超了就在排序之后截断，并把截断这件事如实告诉调用方 —— 悄悄少给
    几个选项，会让策略"没选"和"没得选"混成一件事。
    """
    found = []
    for action_id in catalogue_ids():
        definition = action_definition(action_id)
        for target in _candidate_targets(world, definition.target_kind):
            if is_legal(world, character_id, action_id, target):
                found.append(LegalAction(action_id=action_id, target_id=target))
    found.sort(key=lambda legal: legal.sort_key)
    if limit is not None and len(found) > limit:
        return tuple(found[:limit]), True
    return tuple(found), False


__all__ = [
    "ActionId",
    "evaluators",
    "failed_preconditions",
    "is_legal",
    "legal_actions",
]
