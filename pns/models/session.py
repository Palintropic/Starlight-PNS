from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Sequence

from pns.models.event_store import EventStore
from pns.models.exposure import ExposureDecision, ExposureLog
from pns.models.observation import Observation, ObservationLog
from pns.models.world_state import WorldState


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
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    status: str = "created"  # created / active / completed / paused / cancelled
    last_error: Optional[str] = None
    metadata: Dict = field(default_factory=dict)

    def attach_world_state(self, world_state: WorldState) -> None:
        """绑定本会话唯一一份权威 WorldState（只允许一次）。

        运行时和 SessionState 拿到的必须是同一个对象，不允许各存一份副本。
        """
        if not isinstance(world_state, WorldState):
            raise TypeError("world_state 必须是 WorldState 实例")
        if self.world_state is not None:
            raise RuntimeError("SessionState 已经绑定过 WorldState")
        self.world_state = world_state

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
        """把一次提交里的世界状态、事件历史、轮次和角色历史绑成一个整体。

        块内任何一步抛异常，这四者都会一起回到进入时的样子：不会出现
        "世界改了但事件没记下"、"事件记下了但轮次没落地" 这种半提交状态。
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
        try:
            yield self
        except BaseException:
            if world_snapshot is not None:
                world.restore_mutable_state(world_snapshot)
            self.events._rollback_to(events_length)
            self.observations._rollback_to(observations_length)
            self.exposures._rollback_to(exposures_length)
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
            "created_at": self.created_at,
            "status": self.status,
            "last_error": self.last_error,
            "metadata": deepcopy(self.metadata),
        }
