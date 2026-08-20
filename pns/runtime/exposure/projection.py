# pns/runtime/exposure/projection.py — 已提交事件 → 角色观察
#
# 这一层唯一的职责是**删减**：把一条客观事件砍成"这个角色确实感知得到的
# 那一部分"。所以它是白名单式的 —— 每种事件类型显式列出哪些 payload 键
# 可以被感知，没列的一律不进观察。黑名单迟早会漏。
#
# 三类信息永远不进观察：
#
#   provenance      哪个模型生成的、Router 打了几分、是不是 OOC。
#                   架构文档 §15：系统过程不自动等于角色经验 —— 角色不该
#                   知道"我说的话被一个看不见的评估器打了 8 分"。
#   correlation/causation_id
#                   会话与因果链编号，系统侧簿记。
#   旁观者名单       channel / location 档里的 participants 是提交那一刻的
#                   在场快照；把它给出去等于告诉一个路人整个频道的成员表。
from typing import Dict, Optional, Sequence, Tuple

from pns.models.event import Event, EventScope, EventType
from pns.models.exposure import ExposureDecision, ExposureReason
from pns.models.observation import Observation

# 每种事件类型里，角色感知得到的 payload 键。没登记的类型给空元组 ——
# 新类型默认什么都不透出，要透出必须显式加一行。
_PERCEIVED_PAYLOAD_KEYS: Dict[EventType, Tuple[str, ...]] = {
    EventType.DIALOGUE_SPOKEN: ("text", "char_name"),
    EventType.MESSAGE_SENT: ("text", "char_name"),
    EventType.PRESENCE_JOINED_CHANNEL: (),
    EventType.PRESENCE_LEFT_CHANNEL: (),
    EventType.WORLD_TIME_ADVANCED: ("minutes",),
    EventType.CHARACTER_LOCATION_CHANGED: (),
}

# participants 只有在这两档里才表示"被点名的人"，也只有这两档的收件人
# 有资格知道同批收件人是谁。
_PARTICIPANT_VISIBLE_SCOPES = (EventScope.PRIVATE, EventScope.PARTICIPANT)


def _anchor(event: Event, decision: ExposureDecision) -> Dict:
    """这条观察里可以出现的"在哪"。

    角色只感知得到自己那条通道：从频道里听见的人不会因此知道说话的人
    坐在哪个房间；在同一个房间听见的人也不会因此知道对方开着哪个频道。
    """
    reason = decision.reason
    if reason is ExposureReason.SELF_ACTION:
        # 自己知道自己人在哪、往哪个频道说的。
        return {"location_id": event.location_id, "channel_id": event.channel_id}
    if reason is ExposureReason.CHANNEL_MEMBER:
        return {"channel_id": event.channel_id}
    if reason in (ExposureReason.SAME_LOCATION, ExposureReason.AUDIBLE_FROM):
        return {"location_id": event.location_id}
    if reason is ExposureReason.EXPLICIT_PARTICIPANT:
        if event.channel_id is not None:
            return {"channel_id": event.channel_id}
        return {"location_id": event.location_id}
    if reason is ExposureReason.PUBLIC_VISIBLE:
        if decision.detail.get("via") == "channel":
            return {"channel_id": event.channel_id}
        return {"location_id": event.location_id}
    return {}


def perceived_content(event: Event, decision: ExposureDecision) -> Dict:
    """构造这条观察里的可感知内容。"""
    perceived: Dict = {"type": event.type.value, "actor_id": event.actor_id}
    for key in _PERCEIVED_PAYLOAD_KEYS.get(event.type, ()):
        if key in event.payload:
            perceived[key] = event.payload[key]
    perceived.update(_anchor(event, decision))
    if event.scope in _PARTICIPANT_VISIBLE_SCOPES:
        perceived["participants"] = list(event.participants)
    return perceived


def observation_for(event: Event, decision: ExposureDecision) -> Optional[Observation]:
    """判定通过就生成观察，否则返回 None。

    判定和投影分成两步，是为了让"没曝光"这件事只有一种表达方式：没有观察。
    不存在"生成了一条标着未曝光的观察"这种中间态。
    """
    if decision.event_id != event.event_id:
        raise ValueError(
            f"决策 '{decision.event_id}' 与事件 '{event.event_id}' 不是同一条"
        )
    if not decision.exposed:
        return None
    return Observation(
        source_event_id=event.event_id,
        observer_id=decision.character_id,
        reason=decision.reason,
        observed_at=decision.evaluated_at,
        perceived=perceived_content(event, decision),
    )


def observations_for(
    event: Event, decisions: Sequence[ExposureDecision]
) -> Tuple[Observation, ...]:
    """一批判定里所有通过的那些的观察，顺序与判定顺序一致。"""
    made = (observation_for(event, decision) for decision in decisions)
    return tuple(observation for observation in made if observation is not None)
