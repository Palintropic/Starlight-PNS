# pns/runtime/memory/encoding.py — 观察 → 记忆资格与草稿
#
# 这一层唯一的职责是**判定与收窄**：给定角色自己的一条观察，它值不值得记住、
# 记成哪几类、内容是什么。它拿不到会话、拿不到事件历史、拿不到曝光判定日志 ——
# 只认一条 Observation。
#
# 三条规矩：
#
#   1. 白名单。只有登记过的事件类型才可能长出记忆；没登记的一律显式不编码，
#      而不是"顺手记一条"。新事件类型默认什么都不记，要记必须显式加一行。
#   2. "没记住"是显式结果。不编码要留下一条带理由的决策，因为"评估过，觉得
#      不值得记"和"根本没走到这一步"对下游是两件不同的事。
#   3. 台词不整段抄。内容里存的是摘要（pns/models/memory.py 的 memory_gist），
#      精确原文留在世界历史里供审计 —— 那是两种数据产品（架构文档 §18）。
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Mapping, Optional, Sequence, Tuple

from pns.models.exposure import ExposureReason
from pns.models.memory import (
    MemoryClass,
    MemoryError,
    memory_content,
    world_fact,
)
from pns.models.observation import Observation


class EncodingError(ValueError):
    """这次编码的前提就不成立（观察不属于本会话、预算非法等）。"""


# 能长出记忆的观察类型。跟曝光投影的 payload 白名单同一条规矩：没登记的类型
# 什么都不记。world.time_advanced 刻意不在里面 —— 时钟前进是系统心跳，不是
# 角色经验；调度器每推进一次就产出一条，记下来只会淹掉真正的记忆。
_ENCODABLE_TYPES = (
    "dialogue.spoken",
    "message.sent",
    "presence.joined_channel",
    "presence.left_channel",
    "character.location_changed",
)

_UTTERANCE_TYPES = ("dialogue.spoken", "message.sent")

# 承诺检测是一张**声明出来的标记表**，不是模型判断。它刻意浅：确定性、可测、
# 不需要任何外部调用。代价写在这里，免得以后被当成语义理解：换个说法就漏，
# 引用别人的话也会命中。真正的语义判定要等生成层接进来之后再谈。
COMMITMENT_MARKERS = (
    "约定",
    "答应",
    "保证",
    "一定会",
    "说好了",
    "約束",
    "必ず",
    "promise",
    "i will",
    "i'll",
)

# 情节记忆的门槛。低于它的观察只留短时痕迹 —— 听见路人说了句话，两小时后
# 想不起来是正常的。
EPISODIC_THRESHOLD = 20


@dataclass(frozen=True)
class MemoryBudget:
    """编码侧的显式上限。每一项都有一条真实分支盯着。

    这里**没有**"摘要多长"这一项，而且不该有：摘要长度是存档格式的一部分
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


@dataclass(frozen=True)
class EncodingSignals:
    """从一条观察里读出来的编码信号。全部确定性。"""

    actor_id: Optional[str]
    is_self: bool
    is_utterance: bool
    addressed: bool
    has_commitment: bool
    encodable: bool

    @property
    def salience(self) -> int:
        """显著度：0..100 的整数，用整数是为了排序完全确定。

        它是编码策略的标量，不是关于世界的断言 —— 所以存档恢复只校验范围，
        不拿它去跟观察对质（跟 P9 里 policy 字符串同一条处理）。
        """
        score = 0
        if self.is_self:
            score += 40
        if self.addressed:
            score += 30
        if self.has_commitment:
            score += 20
        score += 10 if self.is_utterance else 5
        return max(0, min(100, score))


def _mentions_owner(text, owner_id: str, aliases: Sequence[str]) -> bool:
    if not isinstance(text, str) or not text:
        return False
    lowered = text.lower()
    names = [owner_id, *aliases]
    return any(
        isinstance(name, str) and name and name.lower() in lowered for name in names
    )


def read_signals(
    observation: Observation, owner_aliases: Sequence[str] = ()
) -> EncodingSignals:
    """把一条观察读成编码信号。

    owner_aliases 由调用方显式给出（角色的显示名之类）。它只影响"这句话是不是
    冲着我说的"这个判断，进而影响触发与显著度，**不进记忆内容** —— 于是存档
    恢复时不需要知道当初用的是哪张别名表，也能一字不差地重新推导内容。
    """
    if not isinstance(observation, Observation):
        raise EncodingError("只能从 Observation 读取编码信号")
    perceived = observation.perceived
    kind = perceived.get("type")
    actor = perceived.get("actor_id")
    owner = observation.observer_id
    is_utterance = kind in _UTTERANCE_TYPES
    is_self = observation.reason is ExposureReason.SELF_ACTION
    text = perceived.get("text")
    participants = perceived.get("participants") or ()
    addressed = bool(
        actor
        and actor != owner
        and (
            _mentions_owner(text, owner, owner_aliases)
            or (owner in tuple(participants))
        )
    )
    has_commitment = bool(
        is_utterance
        and isinstance(text, str)
        and any(marker in text.lower() for marker in COMMITMENT_MARKERS)
    )
    return EncodingSignals(
        actor_id=actor,
        is_self=is_self,
        is_utterance=is_utterance,
        addressed=addressed,
        has_commitment=has_commitment,
        encodable=kind in _ENCODABLE_TYPES,
    )


# ── 规则 ────────────────────────────────────────────────────────────────
#
# 顺序就是持久度顺序：一条观察长出的记忆超过上限时，从后往前丢。丢掉一条
# 短时痕迹的代价，比丢掉一条承诺小得多。
def _wants_commitment(observation, s: EncodingSignals) -> bool:
    return bool(s.is_utterance and s.has_commitment and s.actor_id)


def _wants_identity(observation, s: EncodingSignals) -> bool:
    # 两种身份相关的经验：我自己说出口的承诺，以及别人冲着我说的话。
    if s.is_self and s.has_commitment:
        return True
    return bool(s.addressed and s.actor_id)


def _wants_relational(observation, s: EncodingSignals) -> bool:
    # 关系记忆问的是"这个人对我做了什么"，所以只在互动确实指向我时才产生；
    # 旁听到的一句话不会自动变成一条关系记忆。
    return bool(s.addressed and s.actor_id and s.actor_id != observation.observer_id)


def _wants_semantic(observation, s: EncodingSignals) -> bool:
    return world_fact(observation) is not None


def _wants_episodic(observation, s: EncodingSignals) -> bool:
    return s.salience >= EPISODIC_THRESHOLD


def _wants_working(observation, s: EncodingSignals) -> bool:
    return True  # 白名单内的观察都留一条会过期的短时痕迹


_RULES: Tuple[Tuple[MemoryClass, object], ...] = (
    (MemoryClass.COMMITMENT, _wants_commitment),
    (MemoryClass.IDENTITY, _wants_identity),
    (MemoryClass.RELATIONAL, _wants_relational),
    (MemoryClass.SEMANTIC, _wants_semantic),
    (MemoryClass.EPISODIC, _wants_episodic),
    (MemoryClass.WORKING, _wants_working),
)

# 每个类别都必须有一条规则 —— 没有规则的类别是个空标签。用显式 raise 而不是
# assert：assert 在 -O 下会被剥掉，而这条是结构约束，不是调试断言。
if {memory_class for memory_class, _ in _RULES} != set(MemoryClass):
    raise EncodingError("每一个记忆类别都必须有一条编码规则")


def draft_memories(
    observation: Observation, owner_aliases: Sequence[str] = ()
) -> Tuple[MemoryDraft, ...]:
    """这条观察该长出哪几条记忆，按持久度从高到低。纯函数，不碰任何状态。"""
    signals = read_signals(observation, owner_aliases)
    if not signals.encodable:
        return ()
    salience = signals.salience
    drafts = []
    for memory_class, wants in _RULES:
        if not wants(observation, signals):
            continue
        try:
            content = memory_content(memory_class, observation)
        except MemoryError:
            # 规则说要，内容却推导不出来（比如关系记忆碰上没有对方的观察）。
            # 这是"不记"，不是崩溃 —— 但它不该悄悄发生，所以规则和内容推导
            # 两边的条件是对齐的，走到这里说明有一边被改松了。
            continue
        drafts.append(
            MemoryDraft(
                memory_class=memory_class,
                content=content,
                salience=salience,
                fact=world_fact(observation)
                if memory_class is MemoryClass.SEMANTIC
                else None,
            )
        )
    return tuple(drafts)


__all__ = [
    "COMMITMENT_MARKERS",
    "EPISODIC_THRESHOLD",
    "EncodingDecision",
    "EncodingError",
    "EncodingOutcome",
    "EncodingSignals",
    "MemoryBudget",
    "MemoryDraft",
    "draft_memories",
    "read_signals",
]
