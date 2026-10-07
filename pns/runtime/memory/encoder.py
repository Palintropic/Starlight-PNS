# pns/runtime/memory/encoder.py — 记忆的编码事务
#
# 编码器回答的问题只有一个：**这个角色刚刚感知到的东西，它留下什么？**
#
# 它不回答（写在这里免得以后被顺手加进来）：世界上发生了什么（提交边界）、
# 谁能感知到（曝光）、要不要行动（Agency）、此刻想起什么（Recall）。
#
# 四条硬约束：
#
#   1. **只认观察。** 输入必须是本会话观察日志里那一条 —— 手工拼出来的观察、
#      别人的观察、根本没被曝光的事件，一律长不出记忆。角色的记忆只能从它
#      自己的感知里来。
#   2. **写入落在事务里。** 记忆和它依据的观察同生共死：中途失败不留半条
#      记忆，也不留一份被写脏的存储。
#   3. **重复编码幂等。** 记忆的身份由 (拥有者, 源观察, 类别) 推导，重试
#      算出的是同一个 ID，第二次只会得到一条 SKIPPED_DUPLICATE。
#   4. **"没记住"是显式结果。** 每一条不编码都带理由码，因为"评估过，觉得
#      不值得记"和"根本没走到这一步"对下游是两件不同的事。
#
# 归属跟调度器、Agency 一样：存储归 SessionState 所有，编码器是它上面的服务，
# 一个会话只能绑一个。存档里的 memory 段就是那份存储。
from datetime import datetime
from typing import Dict, Optional, Sequence, Tuple

from pns.models.event import AUTHORITY_EVENT_TYPES, Event
from pns.models.memory import (
    MemoryError,
    MemoryRecord,
    MemoryStore,
    derive_memory_id,
)
from pns.models.observation import Observation
from pns.models.session import SessionState
from pns.models.world_state import WorldState
from pns.runtime.event_commit import commit_session_event
from pns.runtime.memory.encoding import (
    EncodingDecision,
    EncodingOutcome,
    MemoryBudget,
    draft_memories,
)
from pns.runtime.memory.recall import RecallQuery, score_memory

# WORLD-2 权威操作的类型值：观察里只带 type 字符串。
_AUTHORITY_TYPE_VALUES = frozenset(kind.value for kind in AUTHORITY_EVENT_TYPES)


class MemoryEncoderError(ValueError):
    """这次编码的前提就不成立（观察不属于本会话、会话没世界、绑了第二个编码器）。

    它跟 SKIPPED_* 是两类东西：SKIPPED_* 是"评估过，结论是不记"，会留下一条
    决策；MemoryEncoderError 是"这次编码的前提不成立"，什么都不留。
    """


class MemoryEncoder:
    """一个会话里唯一一份记忆编码服务。

    记忆的 schema 与编码算法属于 **cold update**：它们是运行时逻辑，不是内容
    配置。没有任何构造它的路径读磁盘配置，ContentRegistry 也没有任何字段能碰到
    它、它的存储或它的预算 —— P7 的重载换掉的是配置快照，动不了一份已经存在的
    记忆。
    """

    def __init__(
        self,
        state: SessionState,
        budget: Optional[MemoryBudget] = None,
        name: str = "declared-rules-v2",
    ) -> None:
        if not isinstance(state, SessionState):
            raise MemoryEncoderError("记忆编码器必须绑定在一个 SessionState 上")
        if not isinstance(state.world_state, WorldState):
            raise MemoryEncoderError("记忆编码器绑定的会话还没有权威 WorldState")

        budget = budget if budget is not None else MemoryBudget()
        if not isinstance(budget, MemoryBudget):
            raise MemoryEncoderError("budget 必须是 MemoryBudget")

        # 这里刻意**没有**任何影响资格判断的外部输入（比如一张角色别名表）：
        # 只有编码那一刻才知道的信号，存档恢复时重算不出来，于是那一类记忆的
        # 资格就变成"存档说了算"。资格规则的输入必须全部来自观察本身。
        self._state = state
        self._budget = budget
        self._name = name
        try:
            state.attach_memory(self)
        except (RuntimeError, TypeError) as e:
            raise MemoryEncoderError(str(e)) from e

    # ── 读 ──────────────────────────────────────────────────────────────
    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def session_id(self) -> str:
        return self._state.session_id

    @property
    def world(self) -> WorldState:
        return self._state.world_state

    @property
    def clock(self) -> datetime:
        """当前模拟时间。权威值始终在 WorldState 上，这里不另存一份。"""
        return self._state.world_state.clock

    @property
    def store(self) -> MemoryStore:
        """本会话的记忆存储。权威副本在 SessionState 上，这里不缓存引用 ——
        存档恢复会就地换掉它。"""
        return self._state.memories

    @property
    def budget(self) -> MemoryBudget:
        return self._budget

    @property
    def name(self) -> str:
        return self._name

    # ── 编码（事务） ────────────────────────────────────────────────────
    def encode(
        self, observations: Sequence[Observation]
    ) -> Tuple[EncodingDecision, ...]:
        """把一批观察编码成记忆。整批落在一个事务里。

        观察必须是本会话观察日志里的那一条（逐字段相等）。这道闸拦的是三种
        真实错法：手工拼一条没人观察到的"观察"、把别的会话的观察塞进来、
        把某个字段改过的观察塞进来 —— 三种都会让角色记住它感知不到的东西。
        """
        candidates = tuple(observations)
        for observation in candidates:
            self._require_observation(observation)

        state = self._state
        encoded_at = self.clock
        decisions = []
        with state.atomic_commit():
            for observation in candidates:
                decisions.extend(self._encode_one(observation, encoded_at))
        return tuple(decisions)

    def encode_event(self, event_id: str) -> Tuple[EncodingDecision, ...]:
        """把某条已提交事件产生的全部观察编码掉（每个观察者各记各的）。"""
        if not isinstance(event_id, str) or not event_id:
            raise MemoryEncoderError("event_id 必须是非空字符串")
        return self.encode(self._state.observations.for_event(event_id))

    def commit_and_encode(self, event: Event) -> Tuple[Dict, Tuple[EncodingDecision, ...]]:
        """提交一条事件并把它产生的观察一并编码 —— 一个原子边界。

        这是"记忆写入与被接受的观察/编码步骤处在同一个原子边界里"的可调用
        形式：编码失败，事件、观察、曝光判定一起回滚，世界当这次提交没发生过。
        """
        state = self._state
        with state.atomic_commit():
            projection = commit_session_event(state, event)
            decisions = self.encode(state.observations.for_event(event.event_id))
        return projection, decisions

    # ── 单条观察 ────────────────────────────────────────────────────────
    def _encode_one(
        self, observation: Observation, encoded_at: datetime
    ) -> Tuple[EncodingDecision, ...]:
        owner = observation.observer_id
        observation_id = observation.observation_id
        if observation.perceived.get("type") in _AUTHORITY_TYPE_VALUES:
            # WORLD-2 权威操作不会被曝光，正常走不到这里；按类型再拦一道，不靠"碰巧没有观察"。
            return (
                EncodingDecision(
                    observation_id=observation_id,
                    owner_id=owner,
                    outcome=EncodingOutcome.SKIPPED_NOT_ELIGIBLE,
                    detail={"reason": "authority_operation"},
                ),
            )
        drafts = draft_memories(observation)
        if not drafts:
            # 显式的"不记"：白名单外的观察类型（比如时钟前进这种系统心跳），
            # 或者一条规则都没触发。
            return (
                EncodingDecision(
                    observation_id=observation_id,
                    owner_id=owner,
                    outcome=EncodingOutcome.SKIPPED_NOT_ELIGIBLE,
                    detail={
                        "reason": "no_rule_matched",
                        "type": observation.perceived.get("type"),
                    },
                ),
            )

        limit = self._budget.max_records_per_observation
        kept, dropped = drafts[:limit], drafts[limit:]
        decisions = []
        for draft in kept:
            decisions.append(self._encode_draft(observation, draft, encoded_at))
        for draft in dropped:
            # 超过一条观察的上限：按持久度从低到高丢。丢掉一条短时痕迹的代价
            # 比丢掉一条承诺小得多，所以规则表本身就是按持久度排的。
            decisions.append(
                EncodingDecision(
                    observation_id=observation_id,
                    owner_id=owner,
                    outcome=EncodingOutcome.SKIPPED_BUDGET,
                    memory_class=draft.memory_class,
                    detail={
                        "reason": "max_records_per_observation",
                        "limit": limit,
                        "drafted": len(drafts),
                    },
                )
            )
        return tuple(decisions)

    def _encode_draft(
        self, observation: Observation, draft, encoded_at: datetime
    ) -> EncodingDecision:
        owner = observation.observer_id
        observation_id = observation.observation_id
        memory_id = derive_memory_id(
            owner, observation.source_event_id, draft.memory_class
        )

        def skipped(outcome, detail) -> EncodingDecision:
            return EncodingDecision(
                observation_id=observation_id,
                owner_id=owner,
                outcome=outcome,
                memory_class=draft.memory_class,
                detail=detail,
            )

        store = self.store
        if store.has(memory_id):
            # 重复编码 / 重试：身份由字段推导，所以第二次算出的是同一个 ID。
            return skipped(
                EncodingOutcome.SKIPPED_DUPLICATE, {"memory_id": memory_id}
            )
        if draft.fact is not None:
            fact, value = draft.fact
            if self._known_fact(owner, fact) == value:
                # 这条世界事实已经知道了，取值没变 —— 再存一条只会让同一个
                # 事实有两份副本。取值变了才是新事实。
                return skipped(
                    EncodingOutcome.SKIPPED_KNOWN_FACT, {"fact": fact, "value": value}
                )
        if len(store) >= self._budget.max_records_per_session:
            return skipped(
                EncodingOutcome.SKIPPED_BUDGET,
                {
                    "reason": "max_records_per_session",
                    "limit": self._budget.max_records_per_session,
                },
            )
        forgotten = None
        if len(store.for_owner(owner)) >= self._budget.max_records_per_owner:
            forgotten = self._make_room(owner, observation.source_event_id, encoded_at)
            if forgotten is None:
                return skipped(
                    EncodingOutcome.SKIPPED_BUDGET,
                    {
                        "reason": "max_records_per_owner",
                        "limit": self._budget.max_records_per_owner,
                    },
                )

        record = MemoryRecord(
            owner_id=owner,
            memory_class=draft.memory_class,
            source_event_id=observation.source_event_id,
            observed_at=observation.observed_at,
            encoded_at=encoded_at,
            content=draft.content,
            salience=draft.salience,
            provenance={
                "kind": "observation",
                "encoder": self._name,
                # 这个角色**自己**是通过哪条通道感知到的。它是系统侧簿记，
                # 提示投影不会碰它；它也不是拒绝信息 —— 拒绝根本不会走到这里。
                "reason": observation.reason.value,
            },
        )
        try:
            self._state.record_memories((record,))
        except MemoryError as e:
            raise MemoryEncoderError(str(e)) from e
        detail = {"salience": draft.salience}
        if forgotten is not None:
            # 忘掉了哪一条要留在决策里："为了记住这条忘了那条"是一次真实的
            # 状态变化，不能悄悄发生。
            detail["forgot"] = forgotten
        return EncodingDecision(
            observation_id=observation_id,
            owner_id=owner,
            outcome=EncodingOutcome.ENCODED,
            memory_class=draft.memory_class,
            memory_id=memory_id,
            detail=detail,
        )

    def _make_room(
        self, owner_id: str, source_event_id: str, now: datetime
    ) -> Optional[str]:
        """这个角色的记忆满了：忘掉一条最不可能再想起来的，返回它的 ID。

        顺序：
          1. 已经衰减的短时痕迹，旧的先忘。衰减只随时钟单调前进，它们对此刻
             及以后的**任何**召回都已经不存在了，忘掉它们召回结果一个字都不变。
          2. 其余非固定类别里，按"此刻不带线索的召回"排在最后的那条（得分
             最低，同分时更旧的先忘）—— 也就是这个角色最不可能想起来的那件事。
        承诺和身份（固定类别）永远不忘。同一条观察刚长出来的兄弟记忆也不忘：
        为了记住一件事的某一面，忘掉它的另一面，没有意义。
        一条可忘的都没有就返回 None，调用方给出显式的 SKIPPED_BUDGET。
        """
        query = RecallQuery(owner_id=owner_id, now=now)

        def rank(record):
            if record.is_decayed_at(now):
                return (0, 0, record.encoded_at, record.memory_id)
            return (1, score_memory(record, query), record.encoded_at, record.memory_id)

        candidates = [
            record
            for record in self.store.for_owner(owner_id)
            if not record.pinned and record.source_event_id != source_event_id
        ]
        if not candidates:
            return None
        victim = min(candidates, key=rank)
        # 跟追加走同一道写闸：只读的会话连遗忘也不许发生。
        self._state.require_writable()
        self.store._forget(victim.memory_id)
        return victim.memory_id

    def _known_fact(self, owner_id: str, fact: str) -> Optional[str]:
        """这个角色目前记着的这条世界事实的取值；不知道就是 None。

        从存储推导，不另存一张事实表：两份状态迟早会对不上，而存档里只有一份。
        """
        value = None
        for record in self.store.for_owner(owner_id):
            content = record.content
            if content.get("kind") == "world_fact" and content.get("fact") == fact:
                value = content.get("value")
        return value

    # ── 输入校验 ────────────────────────────────────────────────────────
    def _require_observation(self, observation) -> None:
        if not isinstance(observation, Observation):
            raise MemoryEncoderError("只能编码 Observation")
        stored = self._state.observations.find(
            observation.observer_id, observation.source_event_id
        )
        if stored is None:
            raise MemoryEncoderError(
                f"观察 '{observation.observation_id}' 不在本会话的观察日志里"
            )
        if stored != observation:
            raise MemoryEncoderError(
                f"观察 '{observation.observation_id}' 与观察日志里那条不一致"
            )

    # ── 调试投影 ────────────────────────────────────────────────────────
    def debug_projection(self) -> Dict:
        """只读的记忆状态投影（JSON 安全），供测试和调试 UI 读。

        跟曝光的解释通道、Agency 的审计投影同一条规矩：这些是系统视角的数据，
        不进任何角色的上下文或提示词。
        """
        store = self.store
        return {
            "session_id": self.session_id,
            "clock": self.clock.isoformat(),
            "encoder": self._name,
            "budget": self._budget.to_dict(),
            "records": len(store),
            "owners": list(store.owners()),
            "classes": {
                memory_class.value: len(store.for_class(memory_class))
                for memory_class in sorted(
                    {record.memory_class for record in store.records()},
                    key=lambda c: c.value,
                )
            },
        }


__all__ = ["MemoryEncoder", "MemoryEncoderError"]
