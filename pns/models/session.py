from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

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
            "dimensions": self.dimensions,
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
            "dimensions": self.dimensions,
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

    def record_turn(self, turn: Turn) -> None:
        """Commit a persisted turn and all session-level consequences."""
        if turn.character not in self.histories:
            raise ValueError(f"Turn character is not part of session: {turn.character}")
        expected_turn = len(self.turns) + 1
        if turn.turn_number != expected_turn:
            raise ValueError(
                f"Turn number must be {expected_turn}, got {turn.turn_number}"
            )
        self.turns.append(turn)

        if turn.is_ooc:
            self.pending_corrections[turn.character] = turn.correction
        else:
            self.pending_corrections[turn.character] = None

        line = f"{turn.char_name}：{turn.response}"
        self.histories[turn.character].append({"role": "assistant", "content": line})
        for other in self.characters:
            if other != turn.character:
                self.histories[other].append({"role": "user", "content": line})

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
            "histories": self.histories,
            "pending_corrections": self.pending_corrections,
            "stats": self.final_stats(),
            "world_state": self.world_state.to_dict() if self.world_state else {},
            "created_at": self.created_at,
            "status": self.status,
            "last_error": self.last_error,
            "metadata": self.metadata,
        }
