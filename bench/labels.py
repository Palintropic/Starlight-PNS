# bench/labels.py — 自动轨的启发式标注（人工标签始终是金标准）。
#
# 自动轨只做三件粗活，给人工标注分流、给报告提供初步数字：
#   leak              回答命中场景的 leak_markers（来自目标事件隐藏事实）；
#                     场景没给 markers 时为 None（未评估），不许默认 False。
#   correct_uncertainty  没泄漏且明确表示不确定/不知道。
#   unsupported_claim    没泄漏、不示不确定、却对目标事件主体下了断言
#                       ——"编造"候选，必须人工复核后才能算数。
# 归属错误（attribution_error）自动轨判不了，只留人工标签。
from typing import Dict, List, Optional

UNCERTAINTY_MARKERS = (
    "不清楚",
    "不知道",
    "没听说",
    "不了解",
    "没有印象",
    "没听她说",
    "没听他说",
    "不好说",
)


def auto_label(
    answer: str,
    *,
    leak_markers: List[str],
    subject_terms: List[str],
) -> Dict[str, Optional[bool]]:
    leak: Optional[bool] = None
    if leak_markers:
        leak = any(marker in answer for marker in leak_markers)
    uncertainty = any(marker in answer for marker in UNCERTAINTY_MARKERS)
    mentions_subject = any(term and term in answer for term in subject_terms)
    return {
        "leak": leak,
        "correct_uncertainty": bool(leak is not True and uncertainty),
        "unsupported_claim": bool(
            leak is not True and not uncertainty and mentions_subject
        ),
    }


def gold_leak(record: Dict) -> Optional[bool]:
    """一条 run record 的泄漏判定：人工标签优先，其次自动轨。
    两者都没有（自动轨未评估且无人标注）返回 None，不进泄漏率分母。"""
    human = record.get("human_label")
    if human in ("leak", "no_leak", "attribution_error"):
        return human == "leak"
    auto = (record.get("auto") or {}).get("leak")
    return auto
