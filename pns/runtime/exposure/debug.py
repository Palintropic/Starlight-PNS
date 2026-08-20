# pns/runtime/exposure/debug.py — 只读的曝光解释通道
#
# 曝光出错的时候，光看"这个角色没收到"是查不出原因的。这里把判定证据摊开，
# 供测试和以后的调试 UI 读。
#
# 一条硬规则：这些数据**不进角色上下文**。它属于系统视角 —— 角色不该知道
# "有一件事我没被曝光到"，那本身就是它感知不到的信息。所以这个模块只有
# 读取函数，没有任何东西会把它的输出接到提示词渲染上。
from typing import Dict, Optional

from pns.models.session import SessionState


def explain_event(state: SessionState, event_id: str) -> Dict:
    """某条已提交事件的完整曝光判定报告（JSON 安全，可直接给 UI）。"""
    event = state.events.get(event_id)
    observers = set(state.observations.observers_of(event_id))
    return {
        "event_id": event_id,
        "type": event.type.value,
        "scope": event.scope.value,
        "actor_id": event.actor_id,
        "location_id": event.location_id,
        "channel_id": event.channel_id,
        "decisions": [
            {
                **decision.to_dict(),
                "observation_created": decision.character_id in observers,
            }
            for decision in state.exposures.for_event(event_id)
        ],
    }


def explain_character(
    state: SessionState, event_id: str, character_id: str
) -> Optional[Dict]:
    """单个角色对单条事件的判定；没判过就返回 None。"""
    decision = state.exposures.explain(event_id, character_id)
    return decision.to_dict() if decision is not None else None
