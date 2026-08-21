# pns/runtime/memory/recall.py — 记忆 → 此刻想起什么
#
# 召回回答的问题只有一个：**这个角色在这个当下，从自己存下来的记忆里想起
# 哪些？**
#
# 它不回答：记住了什么（那是编码，已经发生过了）、感知得到什么（曝光）、
# 世界上发生了什么（事件历史）。
#
# 四条硬约束：
#
#   1. **角色作用域。** 输入只能是某一个角色自己的记忆，混进别人的一条就
#      直接抛错。别人的私有记忆没有任何路径能被召回出来。
#   2. **只读。** 召回一个字节都不改：换个问法问十遍，存储逐字节不变。
#      "提示词换个问法"永远不该改写已经存下来的记忆。
#   3. **确定性。** 打分、排序、预算全是整数与显式规则，同一 (记忆, 查询)
#      重复调用、存档往返之后结果完全相同。没有随机、没有模型、没有字典
#      迭代顺序参与。
#   4. **有显式预算。** 返回条数、每类条数、留给永久类别的位置都有上限，
#      截断显式标记出来 —— "没想起来"不能悄悄伪装成"没有过这段记忆"。
#
# 衰减在这一层实现，而且**只在这一层**：过期的短时记忆原样躺在存储里，只是
# 不再进入召回。编码与召回分开的意思就是这个 —— 存下来的东西不会因为问法
# 变了而被改写。
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from pns.models.memory import MemoryClass, MemoryRecord, MemoryStore
from pns.models.session import SessionState


class RecallError(ValueError):
    """这次召回的前提就不成立（记忆不属于这个角色、预算非法等）。"""


@dataclass(frozen=True)
class RecallBudget:
    """召回侧的显式上限。每一项都有一条真实分支盯着。"""

    # 一次召回最多返回几条。
    max_items: int = 8
    # 同一个类别最多占几条 —— 免得八条全是同一类痕迹。
    max_per_class: int = 4
    # 给永不衰减的那两类（承诺 / 身份）预留的位置。预算再紧也挤不掉它们。
    max_pinned: int = 4

    def __post_init__(self) -> None:
        for name in ("max_items", "max_per_class", "max_pinned"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise RecallError(f"{name} 必须是整数，收到 {value!r}")
            if value <= 0:
                raise RecallError(f"{name} 必须大于 0，收到 {value}")

    def to_dict(self) -> Dict:
        return {
            "max_items": self.max_items,
            "max_per_class": self.max_per_class,
            "max_pinned": self.max_pinned,
        }


@dataclass(frozen=True)
class RecallQuery:
    """一次召回的上下文。

    "此刻想起什么"依赖当下：谁在问、什么时候问、话题是什么、跟谁有关。同一批
    记忆换一个查询会想起不同的东西 —— 这正是记忆与召回必须分开的原因。
    """

    owner_id: str
    now: datetime
    # 话题线索：命中记忆内容就加分。大小写不敏感的子串匹配，确定性。
    cues: Tuple[str, ...] = ()
    # 跟谁有关：命中就加分（关系记忆、承诺记忆里的对象）。
    about_id: Optional[str] = None
    # 只要这几类；None = 不限。
    classes: Optional[Tuple[MemoryClass, ...]] = None

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        if not isinstance(self.owner_id, str) or not self.owner_id:
            raise RecallError("owner_id 必须是非空字符串")
        if not isinstance(self.now, datetime):
            raise RecallError("now 必须是 datetime（模拟时钟时间）")
        if self.now.tzinfo is not None:
            raise RecallError("now 必须是 timezone-naive 的模拟时间")
        if isinstance(self.cues, (str, bytes)):
            raise RecallError("cues 必须是字符串序列，不是一个字符串")
        set_(self, "cues", tuple(str(cue) for cue in self.cues if str(cue)))
        if self.about_id is not None and (
            not isinstance(self.about_id, str) or not self.about_id
        ):
            raise RecallError("about_id 必须是非空字符串或 None")
        if self.classes is not None:
            set_(
                self,
                "classes",
                tuple(MemoryClass(memory_class) for memory_class in self.classes),
            )

    def to_dict(self) -> Dict:
        return {
            "owner_id": self.owner_id,
            "now": self.now.isoformat(),
            "cues": list(self.cues),
            "about_id": self.about_id,
            "classes": (
                [memory_class.value for memory_class in self.classes]
                if self.classes is not None
                else None
            ),
        }


@dataclass(frozen=True)
class ScoredMemory:
    """一条被考虑过的记忆和它的得分。得分是系统侧数据，不进提示词。"""

    record: MemoryRecord
    score: int

    def to_dict(self) -> Dict:
        return {"score": self.score, **self.record.to_dict()}


@dataclass(frozen=True)
class RecallResult:
    """一次召回的完整结果，含"考虑过多少、丢掉多少"。

    truncated 是显式的：预算截断过的召回必须能被看出来，否则"没想起来"和
    "没有过这段记忆"就分不开，而这两者对下游是完全不同的事实。
    """

    query: RecallQuery
    memories: Tuple[ScoredMemory, ...] = ()
    considered: int = 0
    decayed: int = 0
    truncated: bool = False

    @property
    def records(self) -> Tuple[MemoryRecord, ...]:
        return tuple(scored.record for scored in self.memories)

    def __len__(self) -> int:
        return len(self.memories)

    def to_dict(self) -> Dict:
        """系统侧投影（含 ID 与得分），给测试和调试 UI 用。

        提示词要的是另一份更窄的投影，见 pns/runtime/memory/projection.py。
        """
        return {
            "query": self.query.to_dict(),
            "memories": [scored.to_dict() for scored in self.memories],
            "considered": self.considered,
            "decayed": self.decayed,
            "truncated": self.truncated,
        }


# ── 打分 ────────────────────────────────────────────────────────────────
#
# 全整数：浮点数会让"两次召回结果完全相同"这条不变量依赖平台细节。
_CUE_BONUS = 15
_CUE_BONUS_CAP = 45
_ABOUT_BONUS = 25


def _content_text(record: MemoryRecord) -> str:
    """记忆内容里可供线索匹配的那些文字。"""
    parts: List[str] = []

    def walk(value) -> None:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict) or hasattr(value, "items"):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(record.content)
    return " ".join(parts).lower()


def _recency_bonus(record: MemoryRecord, now: datetime) -> int:
    minutes = max(0, int((now - record.encoded_at).total_seconds() // 60))
    if minutes <= 10:
        return 30
    if minutes <= 60:
        return 20
    if minutes <= 1440:
        return 10
    return 0


def score_memory(record: MemoryRecord, query: RecallQuery) -> int:
    """一条记忆在这次查询下的得分。确定性、可手算。"""
    score = record.memory_class.recall_weight + record.salience
    score += _recency_bonus(record, query.now)
    if query.cues:
        text = _content_text(record)
        hits = sum(1 for cue in query.cues if cue.lower() in text)
        score += min(hits * _CUE_BONUS, _CUE_BONUS_CAP)
    if query.about_id is not None:
        content = record.content
        if query.about_id in (content.get("about"), content.get("by")):
            score += _ABOUT_BONUS
    return score


def _order_key(scored: ScoredMemory, now: datetime):
    """总序：分高的在前，同分的新的在前，再同就按 ID —— 没有并列。"""
    record = scored.record
    return (
        -scored.score,
        (now - record.encoded_at),
        record.memory_id,
    )


def recall(
    memories: Sequence[MemoryRecord],
    query: RecallQuery,
    budget: Optional[RecallBudget] = None,
) -> RecallResult:
    """从**这个角色自己的**记忆里召回此刻想得起来的那些。

    memories 必须只包含 query.owner_id 的记忆。这是刻意的接口形状：这个函数
    拿不到存储，也就没办法自己去别人的记忆里翻，"只喂它自己的记忆"这件事在
    调用点是显式的、可审查的一行（跟 build_agency_context 同一条规矩）。
    """
    if not isinstance(query, RecallQuery):
        raise RecallError("query 必须是 RecallQuery")
    budget = budget if budget is not None else RecallBudget()
    if not isinstance(budget, RecallBudget):
        raise RecallError("budget 必须是 RecallBudget")

    eligible: List[ScoredMemory] = []
    decayed = 0
    considered = 0
    for record in memories:
        if not isinstance(record, MemoryRecord):
            raise RecallError("memories 只能包含 MemoryRecord")
        if record.owner_id != query.owner_id:
            # 别人的记忆混进来就是一次泄漏，而且是最难发现的那种。
            raise RecallError(
                f"记忆 '{record.memory_id}' 属于 '{record.owner_id}'，"
                f"不能进 '{query.owner_id}' 的召回"
            )
        considered += 1
        if record.encoded_at > query.now:
            # 还没编码就想起来了 —— 那不是记忆，那是两段状态被拼在了一起。
            # 这里不抛错（查询时刻由调用方给），但也绝不返回它。
            continue
        if query.classes is not None and record.memory_class not in query.classes:
            continue
        if record.is_decayed_at(query.now):
            decayed += 1
            continue
        eligible.append(ScoredMemory(record=record, score=score_memory(record, query)))

    ordered = sorted(eligible, key=lambda scored: _order_key(scored, query.now))
    selected = _apply_budget(ordered, budget)
    return RecallResult(
        query=query,
        memories=tuple(sorted(selected, key=lambda s: _order_key(s, query.now))),
        considered=considered,
        decayed=decayed,
        truncated=len(selected) < len(eligible),
    )


def _apply_budget(
    ordered: Sequence[ScoredMemory], budget: RecallBudget
) -> List[ScoredMemory]:
    """两遍选取：先给永久类别留位置，再按分数填满剩下的。"""
    selected: List[ScoredMemory] = []
    per_class: Dict[MemoryClass, int] = {}
    pinned_taken = 0

    def take(scored: ScoredMemory) -> None:
        nonlocal pinned_taken
        selected.append(scored)
        memory_class = scored.record.memory_class
        per_class[memory_class] = per_class.get(memory_class, 0) + 1
        if memory_class.pinned:
            pinned_taken += 1

    def has_room(scored: ScoredMemory) -> bool:
        memory_class = scored.record.memory_class
        return (
            len(selected) < budget.max_items
            and per_class.get(memory_class, 0) < budget.max_per_class
        )

    for scored in ordered:
        if not scored.record.pinned:
            continue
        if pinned_taken >= budget.max_pinned:
            break
        if has_room(scored):
            take(scored)

    chosen = {id(scored) for scored in selected}
    for scored in ordered:
        if id(scored) in chosen:
            continue
        if len(selected) >= budget.max_items:
            break
        if has_room(scored):
            take(scored)
    return selected


class MemoryRecall:
    """会话之上的只读召回服务。

    刻意不绑定会话（不走 attach_*）：它一个字节都不写，多来几份也不会产生
    两份互相看不见的权威。收窄成角色作用域的那一行就在 recall() 里。
    """

    def __init__(self, state: SessionState, budget: Optional[RecallBudget] = None):
        if not isinstance(state, SessionState):
            raise RecallError("召回服务必须绑定在一个 SessionState 上")
        self._state = state
        self._budget = budget if budget is not None else RecallBudget()
        if not isinstance(self._budget, RecallBudget):
            raise RecallError("budget 必须是 RecallBudget")

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def budget(self) -> RecallBudget:
        return self._budget

    @property
    def store(self) -> MemoryStore:
        return self._state.memories

    def now(self) -> datetime:
        world = self._state.world_state
        if world is None:
            raise RecallError("没有世界状态的会话没有模拟时间，无法召回")
        return world.clock

    def query_for(
        self,
        owner_id: str,
        *,
        cues: Sequence[str] = (),
        about_id: Optional[str] = None,
        classes: Optional[Sequence[MemoryClass]] = None,
        now: Optional[datetime] = None,
    ) -> RecallQuery:
        return RecallQuery(
            owner_id=owner_id,
            now=now if now is not None else self.now(),
            cues=tuple(cues),
            about_id=about_id,
            classes=tuple(classes) if classes is not None else None,
        )

    def recall(self, query: RecallQuery, budget: Optional[RecallBudget] = None):
        """这个角色此刻想起什么。

        取数就这一行：store.for_owner(query.owner_id)。角色作用域的收窄是一个
        显式的、可以一眼审查完的调用点，不是"记得别读全量"。
        """
        if not isinstance(query, RecallQuery):
            raise RecallError("query 必须是 RecallQuery")
        return recall(
            self.store.for_owner(query.owner_id),
            query,
            budget if budget is not None else self._budget,
        )

    def recall_for(self, owner_id: str, **kwargs) -> RecallResult:
        """query_for() + recall()，日常路径走这个。"""
        budget = kwargs.pop("budget", None)
        return self.recall(self.query_for(owner_id, **kwargs), budget)


__all__ = [
    "MemoryRecall",
    "RecallBudget",
    "RecallError",
    "RecallQuery",
    "RecallResult",
    "ScoredMemory",
    "recall",
    "score_memory",
]
