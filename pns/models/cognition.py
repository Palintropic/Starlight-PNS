# pns/models/cognition.py — 认知不可用的原因
#
# 世界时间与确定性作息在世界打开期间一直走；需要模型的认知（Agency 决策、生成、
# 判分）只在"认知可用"时发生。不可用时到期的激活以 REJECTED_UNAVAILABLE 收尾，
# 记录里写明**全部**生效的原因。
#
# 这些原因属于 Operational History（Article XIII）：它们解释的是"这一刻为什么
# 没有做决定"，不是任何 resident 的经历，也不会进入观察、记忆或提示词。
from enum import Enum
from typing import Iterable, Tuple


class CognitionCause(str, Enum):
    """认知不可用的原因。闭集；一个区间可以同时有多个。"""

    NOT_STARTED = "not_started"  # 世界打开后操作员还没有 Start
    OPERATOR_PAUSED = "operator_paused"  # 操作员 Stop
    RUN_BUDGET_EXHAUSTED = "run_budget_exhausted"  # 单次 Start 的额度用完
    WORLD_ACTION_CAP = "world_action_cap"  # 世界一生的动作上限到顶
    PROCESS_STOPPED = "process_stopped"  # 进程停机；恢复前后没来得及决定
    FAULT = "fault"  # 时钟 worker 故障期间及其补跑
    WALL_CLOCK_BEHIND = "wall_clock_behind"  # 现实时钟晚于存档时钟


class CognitionCauseError(ValueError):
    """原因集合不合法（空、未知值、重复）。"""


def normalize_causes(causes: Iterable) -> Tuple[CognitionCause, ...]:
    """校验并规范化一组原因：非空、闭集内、去重后排序。"""
    if isinstance(causes, (str, bytes)):
        raise CognitionCauseError("原因必须是一组值，不是单个字符串")
    try:
        items = [CognitionCause(cause) for cause in causes]
    except (TypeError, ValueError):
        raise CognitionCauseError(f"未知的认知不可用原因: {causes!r}") from None
    if not items:
        raise CognitionCauseError("认知不可用必须至少有一个原因")
    if len(set(items)) != len(items):
        raise CognitionCauseError(f"认知不可用原因重复: {causes!r}")
    return tuple(sorted(items, key=lambda cause: cause.value))


__all__ = ["CognitionCause", "CognitionCauseError", "normalize_causes"]
