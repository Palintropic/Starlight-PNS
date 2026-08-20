# pns/runtime/exposure/rules.py — 确定性曝光判定
#
# 一条规则，一句话：**事件的 scope 就是传播边界**。谁真的感知到，由 scope
# 那一档对应的世界状态推导出来 —— 不看会话选了谁，不看事件里顺手记下的
# 旁观者名单，不看 Router 打了几分。
#
# 判定顺序是刻意的，写在这里免得以后被人调换：
#
#   1. 自己做的事           → 一律自观察（架构文档 §19：自动作不走外部感知）
#   2. 世界里没有这个角色   → 不可感知
#   3. 睡着了               → 不可感知（在忙不算，忙不等于世界没发生过）
#   4. 按 scope 判边界
#
# 关于 event.participants 的一条重要判断（P6 复核 P5 时确认）：
# dialogue_event_for_turn 会把"提交那一刻在场/在频道里的人"记进
# participants。那是有价值的历史事实，但它**不是**曝光依据 —— 把它当依据
# 会产生两个源头：一个人后来离开了地点，快照里却还留着他的名字，重新判定
# 就会把事件泄给一个已经不在场的角色。所以只有 private / participant 这两档
# （participants 在语义上就是"被点名的人"）才认这个字段；channel / location /
# public 一律回世界状态现算。
from typing import Optional, Tuple

from pns.models.event import Event, EventScope
from pns.models.exposure import ExposureDecision, ExposureReason
from pns.models.world_state import Availability, WorldState

# participants 在这两档里表示"被授权/被点名的人"，是权威的曝光依据。
_PARTICIPANT_AUTHORITATIVE_SCOPES = (EventScope.PRIVATE, EventScope.PARTICIPANT)


class ExposureRuleError(ValueError):
    """无法对这条事件做曝光判定（参数类型不对等）。"""


def _audible_from(world: WorldState, location_id: str) -> Tuple[str, ...]:
    """地点元数据声明的"在哪些别的地点也听得见这里"。

    默认是空的 —— 房间默认是封闭的。要让声音传出去必须在 Location.perception
    里显式声明，而不是靠父子包含关系自动推导：默认漏出去比默认听不见危险。
    """
    if not world.locations.has(location_id):
        return ()
    audible = world.locations.get(location_id).perception.get("audible_from", ())
    if isinstance(audible, str) or not isinstance(audible, (list, tuple, set)):
        return ()
    return tuple(str(item) for item in audible)


def _location_reason(
    world: WorldState, event_location_id: Optional[str], character_id: str
) -> Tuple[ExposureReason, dict]:
    """物理地点这一档的判定，location 与 public 两处共用。"""
    where = world.location_of(character_id)
    detail = {
        "event_location_id": event_location_id,
        "character_location_id": where,
    }
    if event_location_id is None:
        return ExposureReason.WRONG_LOCATION, detail
    if where == event_location_id:
        return ExposureReason.SAME_LOCATION, detail
    if where is not None and where in _audible_from(world, event_location_id):
        return ExposureReason.AUDIBLE_FROM, detail
    return ExposureReason.WRONG_LOCATION, detail


def _decide(world: WorldState, event: Event, character_id: str):
    """返回 (理由码, detail)；纯函数，不碰任何状态。"""
    # 1. 自动作：不走外部感知通道。
    if event.actor_id is not None and character_id == event.actor_id:
        return ExposureReason.SELF_ACTION, {"scope": event.scope.value}

    # 2. 世界里没有这个角色。会话选了谁不算数 —— 选中不等于在场。
    if character_id not in world.known_characters():
        return ExposureReason.UNKNOWN_CHARACTER, {"scope": event.scope.value}

    # 3. 可用性：只有真的挡住感知的那一档才挡。
    availability = world.availability_of(character_id)
    if availability is Availability.ASLEEP:
        return ExposureReason.UNAVAILABLE, {
            "scope": event.scope.value,
            "availability": availability.value,
        }

    scope = event.scope
    base = {"scope": scope.value, "availability": availability.value}

    # 4. 按 scope 判边界。
    if scope in _PARTICIPANT_AUTHORITATIVE_SCOPES:
        detail = {**base, "participants": list(event.participants)}
        if character_id in event.participants:
            return ExposureReason.EXPLICIT_PARTICIPANT, detail
        return (
            ExposureReason.PRIVATE_SCOPE_DENIED
            if scope is EventScope.PRIVATE
            else ExposureReason.NOT_A_PARTICIPANT
        ), detail

    if scope is EventScope.CHANNEL:
        detail = {**base, "channel_id": event.channel_id}
        if event.channel_id is not None and world.is_in_channel(
            character_id, event.channel_id
        ):
            return ExposureReason.CHANNEL_MEMBER, detail
        return ExposureReason.NO_CHANNEL_ACCESS, detail

    if scope is EventScope.LOCATION:
        reason, detail = _location_reason(world, event.location_id, character_id)
        return reason, {**base, **detail}

    # public / ambient：公开只意味着**有可能**被看到，不等于自动知道。
    # 本阶段用一条最简单的确定性规则：当下就在感知范围内（同频道或同地点）
    # 才算感知到；不在范围内的判为"还没撞上"，留给以后的 feed / 注意力层，
    # 而不是现在就一次性广播给全世界。
    detail = {**base, "channel_id": event.channel_id}
    if event.channel_id is not None and world.is_in_channel(
        character_id, event.channel_id
    ):
        # via 记下"是从哪个通道撞上的"，投影层据此决定这条观察里该出现
        # 频道还是地点 —— 不能两个都给。
        return ExposureReason.PUBLIC_VISIBLE, {**detail, "via": "channel"}
    if event.location_id is not None:
        reason, where_detail = _location_reason(world, event.location_id, character_id)
        detail = {**detail, **where_detail}
        if reason in (ExposureReason.SAME_LOCATION, ExposureReason.AUDIBLE_FROM):
            return ExposureReason.PUBLIC_VISIBLE, {**detail, "via": "location"}
    return ExposureReason.PUBLIC_NOT_PERCEIVED, detail


def evaluate_exposure(
    world: WorldState, event: Event, character_id: str
) -> ExposureDecision:
    """判定单个角色对单条已提交事件的感知资格。

    纯函数：同一个事件 + 同一份世界快照永远产出相等的 ExposureDecision。
    """
    if not isinstance(world, WorldState):
        raise ExposureRuleError("曝光判定需要权威 WorldState")
    if not isinstance(event, Event):
        raise ExposureRuleError("只能对 Event 做曝光判定")
    if not isinstance(character_id, str) or not character_id:
        raise ExposureRuleError("character_id 必须是非空字符串")

    reason, detail = _decide(world, event, character_id)
    return ExposureDecision(
        event_id=event.event_id,
        character_id=character_id,
        reason=reason,
        # 判定属于这条事件，时间也必须跟事件走。提交状态效果可能已经推进
        # world.clock；若读取当前时钟，world.time_advanced 会让同一条事件的
        # Event 与 Exposure/Observation 出现两个时间口径。
        evaluated_at=event.occurred_at,
        detail=detail,
    )


def candidate_characters(world: WorldState, event: Event) -> Tuple[str, ...]:
    """要为这条事件逐个判定的候选角色。

    口径是"世界当前认识的角色"，跟事件提交校验用的是同一个 —— 刻意不用
    SessionState.characters：被选进一个会话不代表在场，那正是这一层要拆掉的
    全知假设。事件的 actor 一定在其中（提交校验已经保证），显式点名的
    participant 也补进来，免得一个私密事件的收件人因为还没落地就漏判。
    """
    candidates = set(world.known_characters())
    if event.actor_id is not None:
        candidates.add(event.actor_id)
    candidates.update(event.participants)
    return tuple(sorted(candidates))


def evaluate_event_exposure(
    world: WorldState, event: Event
) -> Tuple[ExposureDecision, ...]:
    """对所有候选角色逐个判定，按角色 ID 排序返回（顺序也是确定的）。"""
    return tuple(
        evaluate_exposure(world, event, character_id)
        for character_id in candidate_characters(world, event)
    )
