from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterator, List, Mapping, Optional, Sequence

from pns.models.activation import ActivationError
from pns.models.activation_outbox import ActivationOutbox, ActivationOutboxError
from pns.models.activation_queue import ActivationQueue, ActivationQueueError
from pns.models.event_store import EventStore
from pns.models.exposure import ExposureDecision, ExposureLog
from pns.models.observation import Observation, ObservationLog
from pns.models.world_state import WorldState


class SessionStateError(ValueError):
    """会话存档不自洽（各部分来自不同时刻、引用对不上、形状损坏等）。"""


@dataclass
class Turn:
    """A completed, persisted simulation turn with compatible projections."""

    turn_number: int
    character: str
    prompt: str
    response: str
    timestamp: str
    char_name: str = ""
    score: float = 0
    is_ooc: bool = False
    confidence: float = 0.0
    drift_type: str = ""
    reason: str = ""
    correction: Optional[str] = None
    correction_applied: Optional[str] = None
    needs_human_review: bool = False
    dimensions: Dict = field(default_factory=dict)
    dimensions_complete: bool = False
    methodology_version: str = ""
    scene_id: str = ""
    lore_tag: str = ""
    router_reference_status: str = ""
    generator_provider: str = ""
    generator_model: str = ""
    evaluator_provider: str = ""
    evaluator_model: str = ""

    def to_wire_dict(self) -> Dict:
        """Return the established WebSocket/history turn shape."""
        return {
            "turn": self.turn_number,
            "character": self.character,
            "char_name": self.char_name,
            "reply": self.response,
            "score": self.score,
            "is_ooc": self.is_ooc,
            "drift_type": self.drift_type,
            "reason": self.reason,
            "correction": self.correction,
            "needs_human_review": self.needs_human_review,
            # 深拷贝：投影不能把 Turn 的内部字典按引用交出去。
            "dimensions": deepcopy(self.dimensions),
            "dimensions_complete": self.dimensions_complete,
            "methodology_version": self.methodology_version,
            "generator_provider": self.generator_provider,
            "generator_model": self.generator_model,
            "evaluator_provider": self.evaluator_provider,
            "evaluator_model": self.evaluator_model,
        }

    def to_drift_record(self, session_id: str) -> Dict:
        """Return the established drift JSONL record shape."""
        return {
            "session_id": session_id,
            "turn": self.turn_number,
            "character": self.character,
            "char_name": self.char_name,
            "text": self.response,
            "drift_score": self.score,
            "confidence": self.confidence,
            "drift_type": self.drift_type,
            "reason": self.reason,
            "needs_human_review": self.needs_human_review,
            "correction": self.correction,
            "scene_id": self.scene_id,
            "lore_tag": self.lore_tag,
            "router_reference_status": self.router_reference_status,
            "dimensions": deepcopy(self.dimensions),
            "dimensions_complete": self.dimensions_complete,
            "methodology_version": self.methodology_version,
            "generator_provider": self.generator_provider,
            "generator_model": self.generator_model,
            "evaluator_provider": self.evaluator_provider,
            "evaluator_model": self.evaluator_model,
            "original_request": self.prompt,
            "correction_applied": self.correction_applied,
            "timestamp": self.timestamp,
        }

    def to_dict(self) -> Dict:
        """Serialize the complete authoritative turn state."""
        return {
            **self.to_wire_dict(),
            "prompt": self.prompt,
            "response": self.response,
            "timestamp": self.timestamp,
            "confidence": self.confidence,
            "correction_applied": self.correction_applied,
            "scene_id": self.scene_id,
            "lore_tag": self.lore_tag,
            "router_reference_status": self.router_reference_status,
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "Turn":
        """Rebuild a turn from to_dict(); the wire projection alone is not enough."""
        if not isinstance(payload, dict):
            raise SessionStateError("轮次记录必须是字典")
        for required in ("turn", "character", "prompt", "response", "timestamp"):
            if required not in payload:
                raise SessionStateError(f"轮次记录缺少必填字段: {required}")
        return cls(
            turn_number=payload["turn"],
            character=payload["character"],
            prompt=payload["prompt"],
            response=payload["response"],
            timestamp=payload["timestamp"],
            char_name=payload.get("char_name", ""),
            score=payload.get("score", 0),
            is_ooc=payload.get("is_ooc", False),
            confidence=payload.get("confidence", 0.0),
            drift_type=payload.get("drift_type", ""),
            reason=payload.get("reason", ""),
            correction=payload.get("correction"),
            correction_applied=payload.get("correction_applied"),
            needs_human_review=payload.get("needs_human_review", False),
            dimensions=deepcopy(payload.get("dimensions", {})),
            dimensions_complete=payload.get("dimensions_complete", False),
            methodology_version=payload.get("methodology_version", ""),
            scene_id=payload.get("scene_id", ""),
            lore_tag=payload.get("lore_tag", ""),
            router_reference_status=payload.get("router_reference_status", ""),
            generator_provider=payload.get("generator_provider", ""),
            generator_model=payload.get("generator_model", ""),
            evaluator_provider=payload.get("evaluator_provider", ""),
            evaluator_model=payload.get("evaluator_model", ""),
        )


@dataclass
class SessionState:
    """Authoritative mutable state for one simulation session."""

    session_id: str
    scene: str
    characters: List[str]
    turns: List[Turn] = field(default_factory=list)
    current_character_index: int = 0
    histories: Dict[str, List[Dict]] = field(default_factory=dict)
    pending_corrections: Dict[str, Optional[str]] = field(default_factory=dict)
    world_state: Optional[WorldState] = None
    # 客观世界历史。它不是 turns 的另一种写法：turns 是生成审计记录，
    # events 是"世界上发生过什么"。两者由 atomic_commit() 绑在一起提交。
    events: EventStore = field(default_factory=EventStore)
    # 角色主观感知。世界历史 ≠ 角色观察：一条事件只在角色确实感知得到时
    # 才会在这里留下一条投影，绝不按会话名单群发。
    observations: ObservationLog = field(default_factory=ObservationLog)
    # 曝光判定的解释日志，含拒绝。系统侧数据，只给测试和调试 UI 看，
    # 任何渲染角色上下文的路径都不许读它。
    exposures: ExposureLog = field(default_factory=ExposureLog)
    # 调度状态。它归会话所有，不归调度器所有：调度器是这份状态之上的服务，
    # 一个会话只能有一份。放在这里，会话存档就不可能出现"存了世界时钟却把
    # 队列和到期记录丢了"——那种存档恢复出来的世界是残缺的。
    activations: ActivationQueue = field(default_factory=ActivationQueue)
    activation_outbox: ActivationOutbox = field(default_factory=ActivationOutbox)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    status: str = "created"  # created / active / completed / paused / cancelled
    last_error: Optional[str] = None
    metadata: Dict = field(default_factory=dict)
    # 绑定在本会话上的调度器实例。类型刻意不写死：models 不许 import runtime，
    # 而"一个会话只有一份调度器"这件事必须由会话自己来判定，不能靠调用方自觉。
    # 它是运行时服务，不是状态，因此不进 to_dict()——存档里存的是 activations
    # 和 activation_outbox。
    scheduler: Optional[object] = field(default=None, repr=False, compare=False)

    def attach_world_state(self, world_state: WorldState) -> None:
        """绑定本会话唯一一份权威 WorldState（只允许一次）。

        运行时和 SessionState 拿到的必须是同一个对象，不允许各存一份副本。
        """
        if not isinstance(world_state, WorldState):
            raise TypeError("world_state 必须是 WorldState 实例")
        if self.world_state is not None:
            raise RuntimeError("SessionState 已经绑定过 WorldState")
        self.world_state = world_state

    def attach_scheduler(self, scheduler) -> None:
        """绑定本会话唯一一份调度器（只允许一次）。

        第二次绑定必须失败。两个调度器各自持有一份队列、却推进同一个时钟，
        产生的是两份互相看不见的"权威"排期：一份里的一次性激活触发之后，
        另一份仍然认为它还没触发。恢复存档也走同一个实例（就地替换它管理的
        队列与投递箱），不产生第二个并列实例。
        """
        if self.scheduler is not None:
            raise RuntimeError("SessionState 已经绑定过调度器")
        for required in ("schedule", "advance_by", "acknowledge"):
            if not callable(getattr(scheduler, required, None)):
                raise TypeError(f"调度器必须提供 {required}()")
        self.scheduler = scheduler

    def initialize_runtime(self, scene_trigger: str) -> None:
        """Initialize per-character runtime state exactly once."""
        if self.histories or self.pending_corrections:
            raise RuntimeError("SessionState runtime state has already been initialized")
        self.histories = {
            cid: [
                {
                    "role": "user",
                    "content": f"【场景】{scene_trigger}"
                    + ("\n请开始对话。" if index == 0 else ""),
                }
            ]
            for index, cid in enumerate(self.characters)
        }
        self.pending_corrections = {cid: None for cid in self.characters}

    def start(self) -> None:
        if self.status != "created":
            raise RuntimeError("SessionRuntime.run() 只能调用一次")
        self.status = "active"

    @property
    def current_character(self) -> str:
        return self.characters[self.current_character_index]

    def history_for(self, character: str) -> List[Dict]:
        return self.histories[character]

    def correction_for(self, character: str) -> Optional[str]:
        return self.pending_corrections[character]

    def record_observations(
        self,
        decisions: Sequence[ExposureDecision],
        observations: Sequence[Observation],
    ) -> None:
        """记录一次曝光判定的全部结果（判定日志 + 通过者的观察）。

        只应该由提交边界调用：观察必须和它所投影的那条事件同生共死，绕开
        atomic_commit() 单独写进来的观察回滚不掉。
        """
        for decision in decisions:
            self.exposures._append(decision)
        for observation in observations:
            self.observations._append(observation)

    def record_turn(
        self, turn: Turn, observations: Optional[Sequence[Observation]] = None
    ) -> None:
        """Commit a persisted turn and all session-level consequences.

        observations 给出的是这次发言真正被谁感知到了。给了就按它投影角色
        历史；给 None 是**遗留兼容路径**（把这句话抄进每个角色的历史），
        只保留给不经过世界模型的纯记录调用方。运行时不走那条路。
        """
        if turn.character not in self.characters:
            raise ValueError(f"Turn character is not part of session: {turn.character}")
        if bool(self.histories) != bool(self.pending_corrections):
            raise RuntimeError("SessionState runtime state is only partially initialized")
        expected_turn = len(self.turns) + 1
        if turn.turn_number != expected_turn:
            raise ValueError(
                f"Turn number must be {expected_turn}, got {turn.turn_number}"
            )
        self.turns.append(turn)

        # A SessionState may also be used as a simple persisted record without
        # live LLM histories. Runtime-created states initialize both mappings
        # and therefore continue through the live-state updates below.
        if not self.histories:
            return

        if turn.is_ooc:
            self.pending_corrections[turn.character] = turn.correction
        else:
            self.pending_corrections[turn.character] = None

        if observations is None:
            # 遗留全知投影：没有曝光信息可依据时，退回旧行为。
            line = f"{turn.char_name}：{turn.response}"
            self.histories[turn.character].append(
                {"role": "assistant", "content": line}
            )
            for other in self.characters:
                if other != turn.character:
                    self.histories[other].append({"role": "user", "content": line})
            return

        # 新路径：角色历史是**观察的投影**，不是按会话名单复制文本。
        # 没有观察的角色，历史里就不会多出这一行。
        for observation in observations:
            line = observation.render_line()
            if line is None:
                continue
            observer = observation.observer_id
            # 观察可能来自世界里存在、但不属于这个会话生成名单的角色。
            # 他们确实感知到了（观察日志里有记录），只是这个会话不为他们
            # 生成台词，所以没有对应的历史。用 characters 判断而不是用
            # histories 有没有这个键 —— 后者会把"状态被改坏了"一起吞掉。
            if observer not in self.characters:
                continue
            role = "assistant" if observer == turn.character else "user"
            self.histories[observer].append({"role": role, "content": line})

    def add_turn(self, turn: Turn) -> None:
        """Backward-compatible alias for the pre-Phase-3 public API."""
        self.record_turn(turn)

    @contextmanager
    def atomic_commit(self) -> Iterator["SessionState"]:
        """把一次提交里的会话可变状态绑成一个整体。

        覆盖：世界状态、事件历史、观察、曝光判定、轮次、角色历史、待纠正，
        以及调度器的排期队列与到期投递箱。块内任何一步抛异常，它们会一起回到
        进入时的样子：不会出现"世界改了但事件没记下"、"事件记下了但轮次没
        落地"、"时间走了但队列没动"、"队列摘了但到期记录没落箱"这些半提交
        状态中的任何一种。
        """
        world = self.world_state
        world_snapshot = (
            world.snapshot_mutable_state() if world is not None else None
        )
        events_length = len(self.events)
        observations_length = len(self.observations)
        exposures_length = len(self.exposures)
        turns_length = len(self.turns)
        history_lengths = {cid: len(items) for cid, items in self.histories.items()}
        corrections = dict(self.pending_corrections)
        # 引用和内容都要记：块内如果发生了存档恢复（restore_scheduler_archive
        # 会整个换掉这两个容器），只回滚内容会留下换过之后的那一份。
        activations = self.activations
        activations_snapshot = activations._snapshot()
        outbox = self.activation_outbox
        outbox_snapshot = outbox._snapshot()
        try:
            yield self
        except BaseException:
            if world_snapshot is not None:
                world.restore_mutable_state(world_snapshot)
            self.events._rollback_to(events_length)
            self.observations._rollback_to(observations_length)
            self.exposures._rollback_to(exposures_length)
            self.activations = activations
            activations._restore(activations_snapshot)
            self.activation_outbox = outbox
            outbox._restore(outbox_snapshot)
            del self.turns[turns_length:]
            for cid in list(self.histories):
                if cid in history_lengths:
                    del self.histories[cid][history_lengths[cid]:]
                else:
                    del self.histories[cid]
            self.pending_corrections.clear()
            self.pending_corrections.update(corrections)
            raise

    def advance_character(self) -> None:
        self.current_character_index = (
            self.current_character_index + 1
        ) % len(self.characters)

    def record_error(self, message: str) -> None:
        self.last_error = message

    def complete(self) -> None:
        self.status = "completed"

    def cancel(self) -> None:
        if self.status == "active":
            self.status = "cancelled"

    def final_stats(self) -> Dict:
        scores = [turn.score for turn in self.turns]
        avg_score = sum(scores) / len(scores) if scores else 0
        return {
            "total_turns": len(self.turns),
            "ooc_count": sum(turn.is_ooc for turn in self.turns),
            "corrections": sum(
                bool(turn.correction) for turn in self.turns if turn.is_ooc
            ),
            "avg_score": round(avg_score, 2),
            "max_score": max(scores) if scores else 0,
        }

    def get_conversation(self) -> str:
        return "\n".join(
            f"{turn.character}: {turn.response}" for turn in self.turns
        )

    # ── 调度存档 ────────────────────────────────────────────────────────
    def scheduler_archive(self) -> Dict:
        """调度状态的持久化形状：队列、到期投递箱，以及它们对应的那一刻时钟。

        这是**唯一**一处定义调度存档形状的地方 —— PersistentScheduler.to_dict()
        直接返回它。同一份状态有两种写法，迟早会出现一种存得下、另一种读不回来。
        """
        return {
            "session_id": self.session_id,
            "clock": (
                self.world_state.clock.isoformat()
                if self.world_state is not None
                else None
            ),
            "queue": self.activations.to_dict(),
            "outbox": self.activation_outbox.to_dict(),
        }

    def restore_scheduler_archive(self, payload) -> None:
        """按存档就地恢复队列与投递箱。

        就地替换而不是新建一份：会话是这两样东西的权威所有者，绑在它上面的
        调度器读的始终是 self.activations / self.activation_outbox，所以恢复
        之后不会出现"调度器还抱着旧队列"这种两份权威并存的状态。

        三类不自洽一律拒绝：会话对不上、时钟对不上、队列/投递箱与那一刻的
        时钟对不上（排期不在未来、到期记录发生在未来）。
        """
        if not isinstance(payload, Mapping):
            raise SessionStateError("调度存档必须是字典")
        if payload.get("session_id") != self.session_id:
            raise SessionStateError(
                f"调度存档属于会话 '{payload.get('session_id')}'，不能恢复进会话 "
                f"'{self.session_id}'"
            )

        clock = _parse_clock(payload.get("clock"), "调度存档的 clock")
        world_clock = (
            self.world_state.clock if self.world_state is not None else None
        )
        if clock != world_clock:
            raise SessionStateError(
                f"调度存档的时钟 {clock} 与世界时钟 {world_clock} 不一致"
            )

        raw_queue = payload.get("queue")
        raw_outbox = payload.get("outbox")
        if not isinstance(raw_queue, Mapping):
            # 少了就当空的，等于一份丢了内容的存档能安静地恢复成"什么都没排"。
            raise SessionStateError("调度存档缺少 queue")
        if not isinstance(raw_outbox, Mapping):
            raise SessionStateError("调度存档缺少 outbox")
        try:
            activations = ActivationQueue.from_dict(dict(raw_queue))
            outbox = ActivationOutbox.from_dict(dict(raw_outbox))
        except (ActivationQueueError, ActivationOutboxError, ActivationError) as e:
            raise SessionStateError(str(e)) from e

        _validate_schedule_against_clock(activations, outbox, clock)
        self.activations = activations
        self.activation_outbox = outbox

    @classmethod
    def from_dict(cls, payload: Dict) -> "SessionState":
        """从 to_dict() 的形状恢复一份权威会话状态。

        这是会话存档的生产路径：世界、事件历史、观察、曝光判定、排期队列和
        到期投递箱一起存、一起恢复、一起校验。刻意不允许缺件恢复 ——
        少了 scheduler 段就直接失败，而不是"世界时钟还在、队列却没了"地
        安静恢复成一份残缺的世界。

        跨部分的一致性也在这里判：事件/观察/判定不能发生在世界时钟之后，
        排期必须严格在时钟之后，到期记录必须不晚于时钟。把不同时刻的世界、
        事件和调度状态拼在一起，会得到一份每一部分单独看都合法、合起来却
        自相矛盾的存档。
        """
        if not isinstance(payload, Mapping):
            raise SessionStateError("会话存档必须是字典")
        for required in ("session_id", "scene", "characters", "scheduler"):
            if required not in payload:
                raise SessionStateError(f"会话存档缺少必填字段: {required}")

        characters = payload["characters"]
        if not isinstance(characters, (list, tuple)) or not characters:
            raise SessionStateError("characters 必须是非空数组")
        characters = list(characters)
        if not all(isinstance(cid, str) and cid for cid in characters):
            raise SessionStateError("characters 必须由非空角色 ID 组成")
        if len(set(characters)) != len(characters):
            raise SessionStateError("characters 不能包含重复角色")

        state = cls(
            session_id=payload["session_id"],
            scene=payload["scene"],
            characters=characters,
        )

        world_payload = payload.get("world_state") or {}
        if world_payload:
            state.attach_world_state(WorldState.from_dict(dict(world_payload)))
        clock = state.world_state.clock if state.world_state is not None else None

        entries = payload.get("turns", [])
        if not isinstance(entries, list):
            raise SessionStateError("turns 必须是数组")
        for expected, entry in enumerate(entries, start=1):
            turn = Turn.from_dict(entry)
            if turn.turn_number != expected:
                raise SessionStateError(
                    f"轮次编号不连续：第 {expected} 条是 {turn.turn_number}"
                )
            if turn.character not in characters:
                raise SessionStateError(
                    f"轮次引用了不属于本会话的角色: {turn.character}"
                )
            state.turns.append(turn)

        index = payload.get("current_character_index", 0)
        if isinstance(index, bool) or not isinstance(index, int):
            raise SessionStateError("current_character_index 必须是整数")
        if not 0 <= index < len(characters):
            raise SessionStateError(f"current_character_index 越界: {index}")
        state.current_character_index = index

        histories = payload.get("histories", {})
        corrections = payload.get("pending_corrections", {})
        if not isinstance(histories, Mapping) or not isinstance(corrections, Mapping):
            raise SessionStateError("histories 和 pending_corrections 必须是字典")
        if histories and set(histories) != set(characters):
            raise SessionStateError("histories 的角色集合与会话角色不一致")
        if corrections and set(corrections) != set(characters):
            raise SessionStateError("pending_corrections 的角色集合与会话角色不一致")
        if bool(histories) != bool(corrections):
            raise SessionStateError("histories 与 pending_corrections 必须同时存在")
        state.histories = {cid: list(deepcopy(items)) for cid, items in histories.items()}
        state.pending_corrections = dict(corrections)

        state.events = EventStore.from_dict(dict(payload.get("events", {})))
        state.observations = ObservationLog.from_dict(
            dict(payload.get("observations", {}))
        )
        state.exposures = ExposureLog.from_dict(dict(payload.get("exposures", {})))
        _validate_history_against_clock(state, clock)

        state.restore_scheduler_archive(payload["scheduler"])

        created_at = payload.get("created_at")
        if created_at is not None:
            state.created_at = created_at
        state.status = payload.get("status", "created")
        state.last_error = payload.get("last_error")
        state.metadata = deepcopy(dict(payload.get("metadata", {})))
        return state

    def to_dict(self) -> Dict:
        return {
            "session_id": self.session_id,
            "scene": self.scene,
            "characters": list(self.characters),
            "turns": [turn.to_dict() for turn in self.turns],
            "current_character_index": self.current_character_index,
            "current_character": self.current_character,
            # 全部深拷贝：序列化结果不能是内部可变状态的引用，否则调用方改一下
            # 返回的字典就等于改了权威状态。
            "histories": deepcopy(self.histories),
            "pending_corrections": dict(self.pending_corrections),
            "stats": self.final_stats(),
            "world_state": self.world_state.to_dict() if self.world_state else {},
            "events": self.events.to_dict(),
            "observations": self.observations.to_dict(),
            "exposures": self.exposures.to_dict(),
            "scheduler": self.scheduler_archive(),
            "created_at": self.created_at,
            "status": self.status,
            "last_error": self.last_error,
            "metadata": deepcopy(self.metadata),
        }


# ── 存档校验辅助 ────────────────────────────────────────────────────────
def _parse_clock(value, label: str) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        clock = value
    elif isinstance(value, str):
        try:
            clock = datetime.fromisoformat(value)
        except ValueError:
            raise SessionStateError(f"无法解析的{label}: {value!r}") from None
    else:
        raise SessionStateError(f"{label}必须是 ISO 时间字符串")
    if clock.tzinfo is not None:
        raise SessionStateError(f"{label}必须是 timezone-naive 的模拟时间")
    return clock


def _validate_schedule_against_clock(activations, outbox, clock) -> None:
    """排期与到期记录必须跟这一刻的时钟自洽。"""
    if clock is None:
        # 没有世界就没有模拟时间，也就不可能有排过的期或触发过的到期。
        if len(activations) or len(outbox):
            raise SessionStateError("没有世界状态的会话不能持有排期或到期记录")
        return
    for activation in activations.pending():
        if activation.due_at <= clock:
            raise SessionStateError(
                f"排期 '{activation.activation_id}' 的到期时间 "
                f"{activation.due_at.isoformat()} 不晚于世界时钟 "
                f"{clock.isoformat()}"
            )
    for record in outbox.records():
        if record.fired_at > clock:
            raise SessionStateError(
                f"到期记录 '{record.due_id}' 的触发时间 "
                f"{record.fired_at.isoformat()} 晚于世界时钟 {clock.isoformat()}"
            )


def _validate_history_against_clock(state: "SessionState", clock) -> None:
    """世界历史、观察和曝光判定都不能发生在世界时钟之后。"""
    if clock is None:
        if len(state.events) or len(state.observations) or len(state.exposures):
            raise SessionStateError("没有世界状态的会话不能持有事件、观察或曝光判定")
        return
    latest = state.events.latest()
    if latest is not None and latest.occurred_at > clock:
        raise SessionStateError(
            f"事件 '{latest.event_id}' 发生在世界时钟 {clock.isoformat()} 之后"
        )
    for observation in state.observations.observations():
        if observation.observed_at > clock:
            raise SessionStateError(
                f"观察 '{observation.source_event_id}' 晚于世界时钟 "
                f"{clock.isoformat()}"
            )
    for decision in state.exposures.decisions():
        if decision.evaluated_at > clock:
            raise SessionStateError(
                f"曝光判定 '{decision.event_id}' 晚于世界时钟 {clock.isoformat()}"
            )
