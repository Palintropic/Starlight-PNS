# pns/runtime/memory/encoding.py — 观察 → 记忆草稿
#
# 这一层唯一的职责是**把资格规则跑一遍，产出还没落地的草稿**：给定角色自己的
# 一条观察，它值不值得记住、记成哪几类、内容是什么、显著度多少。它拿不到会话、
# 拿不到事件历史、拿不到曝光判定日志 —— 只认一条 Observation。
#
# 资格、内容、显著度这三样的**定义**都不在这里，而在 pns/models/memory.py：
# 存档恢复也要照同一份声明重判一遍，而模型层不许反向依赖运行时。一份声明两处
# 用，验证就不可能比构造更松（跟 P9 的 agency_event_fields 同一条规矩）。
#
# 三条规矩：
#
#   1. 白名单。只有登记过的观察类型才可能长出记忆；没登记的一律显式不编码，
#      而不是"顺手记一条"。新事件类型默认什么都不记，要记必须显式加一行。
#   2. "没记住"是显式结果。不编码要留下一条带理由的决策，因为"评估过，觉得
#      不值得记"和"根本没走到这一步"对下游是两件不同的事。
#   3. 台词不整段抄，短台词也不例外。内容里存的是结构描述加一段有上限的片段
#      （pns/models/memory.py 的 memory_fragment），精确原文留在世界历史里供
#      审计 —— 那是两种数据产品（架构文档 §18）。
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Mapping, Optional, Tuple

from pns.models.memory import (
    COMMITMENT_MARKERS,
    ENCODABLE_TYPES,
    EPISODIC_THRESHOLD,
    EncodingSignals,
    MemoryClass,
    derived_salience,
    eligible_classes,
    memory_content,
    read_signals,
    world_fact,
)
from pns.models.observation import Observation


class EncodingError(ValueError):
    """这次编码的前提就不成立（观察不属于本会话、预算非法等）。"""


@dataclass(frozen=True)
class MemoryBudget:
    """编码侧的显式上限。每一项都有一条真实分支盯着。

    这里**没有**"片段留多长"这一项，而且不该有：它是存档格式的一部分
    （恢复时要用同一条规则重新推导内容来核对），一个调用方能调的旋钮会让
    昨天的存档在今天变成"损坏"。它是 pns/models/memory.py 的模型层常量。
    """

    # 一条观察最多长出几条记忆。超了按持久度从高到低保留（规则表本身就是
    # 按持久度排的）。默认 5 —— 规则表下一条观察真的可能长出的最多条数，
    # 所以默认配置不会悄悄丢掉东西；要收紧就显式传一个更小的预算。
    max_records_per_observation: int = 5
    # 一个会话累计最多存多少条记忆。计数**从存储推导**，不另存计数器：
    # 计数器会在存档往返之后归零，于是恢复出来的会话能把上限再用一遍。
    max_records_per_session: int = 512

    def __post_init__(self) -> None:
        for name in ("max_records_per_observation", "max_records_per_session"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise EncodingError(f"{name} 必须是整数，收到 {value!r}")
            if value <= 0:
                raise EncodingError(f"{name} 必须大于 0，收到 {value}")

    def to_dict(self) -> Dict:
        return {
            "max_records_per_observation": self.max_records_per_observation,
            "max_records_per_session": self.max_records_per_session,
        }


class EncodingOutcome(str, Enum):
    """一次编码尝试的结果码。闭集，每个码都对应一条真实分支。

      ENCODED               记下来了。
      SKIPPED_NOT_ELIGIBLE  评估过，不值得记（或者这类观察本来就不进记忆）。
      SKIPPED_DUPLICATE     这条观察的这一类记忆已经存在 —— 重复编码/重试幂等。
      SKIPPED_KNOWN_FACT    这条世界事实已经知道了，取值没变。
      SKIPPED_BUDGET        触到了显式声明的上限。
    """

    ENCODED = "encoded"
    SKIPPED_NOT_ELIGIBLE = "skipped_not_eligible"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    SKIPPED_KNOWN_FACT = "skipped_known_fact"
    SKIPPED_BUDGET = "skipped_budget"

    @property
    def encoded(self) -> bool:
        return self is EncodingOutcome.ENCODED


@dataclass(frozen=True)
class EncodingDecision:
    """一次"这条观察要不要记成这一类"的结论，含显式的不记。

    它是**系统侧审计**，不是角色经验：没有任何路径把它送进上下文或提示词。
    """

    observation_id: str
    owner_id: str
    outcome: EncodingOutcome
    memory_class: Optional[MemoryClass] = None
    memory_id: Optional[str] = None
    detail: Mapping = field(default_factory=dict)

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        try:
            set_(self, "outcome", EncodingOutcome(self.outcome))
        except ValueError:
            raise EncodingError(f"未知的编码结果码: {self.outcome!r}") from None
        if self.memory_class is not None:
            set_(self, "memory_class", MemoryClass(self.memory_class))
        if self.outcome.encoded and self.memory_id is None:
            raise EncodingError("encoded 决策必须指向它写下的那条记忆")
        if not self.outcome.encoded and self.memory_id is not None:
            raise EncodingError(f"{self.outcome.value} 决策不能指向任何记忆")
        set_(self, "detail", dict(self.detail))

    def to_dict(self) -> Dict:
        return {
            "observation_id": self.observation_id,
            "owner_id": self.owner_id,
            "outcome": self.outcome.value,
            "memory_class": (
                self.memory_class.value if self.memory_class is not None else None
            ),
            "memory_id": self.memory_id,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class MemoryDraft:
    """还没落地的一条记忆：类别 + 内容 + 显著度（+ 世界事实的身份）。"""

    memory_class: MemoryClass
    content: Dict
    salience: int
    fact: Optional[Tuple[str, str]] = None


def draft_memories(observation: Observation) -> Tuple[MemoryDraft, ...]:
    """这条观察该长出哪几条记忆，按持久度从高到低。纯函数，不碰任何状态。

    资格、内容、显著度全部走模型层那份声明 —— 编码器与存档校验读的是同一套
    规则，所以"存档里这条记忆当初有没有资格产生"是可以重算的。
    """
    return tuple(
        MemoryDraft(
            memory_class=memory_class,
            content=memory_content(memory_class, observation),
            salience=derived_salience(observation),
            fact=world_fact(observation)
            if memory_class is MemoryClass.SEMANTIC
            else None,
        )
        for memory_class in eligible_classes(observation)
    )


__all__ = [
    "COMMITMENT_MARKERS",
    "ENCODABLE_TYPES",
    "EPISODIC_THRESHOLD",
    "EncodingDecision",
    "EncodingError",
    "EncodingOutcome",
    "EncodingSignals",
    "MemoryBudget",
    "MemoryDraft",
    "draft_memories",
    "derived_salience",
    "eligible_classes",
    "read_signals",
]
