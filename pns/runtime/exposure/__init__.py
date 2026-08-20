# pns/runtime/exposure — 曝光层
#
#     事件真相 → 曝光资格 → 观察投影 → （以后的）注意力 / 能动性 → 记忆
#
# 这一层只走前两步。它不判断角色在不在意、要不要回应、记不记得住，也不
# 调度谁下一个说话 —— 那些都在后面的阶段。
from pns.runtime.exposure.debug import explain_character, explain_event
from pns.runtime.exposure.projection import (
    observation_for,
    observations_for,
    perceived_content,
)
from pns.runtime.exposure.rules import (
    ExposureRuleError,
    candidate_characters,
    evaluate_event_exposure,
    evaluate_exposure,
)

__all__ = [
    "ExposureRuleError",
    "explain_event",
    "explain_character",
    "candidate_characters",
    "evaluate_exposure",
    "evaluate_event_exposure",
    "observation_for",
    "observations_for",
    "perceived_content",
]
