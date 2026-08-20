# pns/models/event.py — 客观发生事实的领域模型
#
# Event 回答的是一个问题：世界里发生了什么、什么时候、在哪里、传播边界到哪为止。
#
# 它刻意不回答另外两个问题：谁能感知到（那是 Exposure，P6）、角色记住了什么
# （那是 Memory，更后面）。LLM 的一次输出、一次尝试动作、一条观察都不自动
# 是事件 —— 只有被接受的发生才进入权威世界历史。
#
# 事件一旦构造出来就不可变：payload/provenance 在构造时深冻结，属性拿到的是
# 只读视图，to_dict() 才返回可以随便改的普通字典。这样事件历史不可能被下游
# 调用方从引用上改掉。
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from uuid import uuid4


class EventError(ValueError):
    """事件自身不合法（缺 ID、未知类型、scope 必填字段缺失、payload 非法等）。"""


class EventScope(str, Enum):
    """事件的传播边界。

    scope 不是传输方式：事件不是 WebSocket 消息，也不是频道推送。它只声明
    "这件事最远能传到哪"，具体谁真的感知到由后续的 Exposure 阶段决定。
    """

    PRIVATE = "private"
    PARTICIPANT = "participant"
    CHANNEL = "channel"
    LOCATION = "location"
    PUBLIC = "public"

    @classmethod
    def _missing_(cls, value):
        # 架构文档里 public 那一档写作 "public / ambient"，两者是同一档。
        if isinstance(value, str) and value.strip().lower() == "ambient":
            return cls.PUBLIC
        return None


class EventType(str, Enum):
    """本阶段能够完整校验并应用的事件类型。

    这里刻意保持很小：没有语义、没有状态效果的占位类型不进这个枚举。
    """

    DIALOGUE_SPOKEN = "dialogue.spoken"
    MESSAGE_SENT = "message.sent"
    PRESENCE_JOINED_CHANNEL = "presence.joined_channel"
    PRESENCE_LEFT_CHANNEL = "presence.left_channel"
    WORLD_TIME_ADVANCED = "world.time_advanced"
    CHARACTER_LOCATION_CHANGED = "character.location_changed"


# payload/provenance 只允许放 JSON 安全的值。这不是洁癖：任何别的对象都可能
# 是外部还持有引用的可变结构，放进事件就等于在不可变历史里开了个后门。
_SCALARS = (str, int, float, bool, type(None))


def _freeze(value, *, path: str = "payload"):
    """把嵌套结构深冻结成只读视图；遇到不安全的值直接报错。"""
    if isinstance(value, _SCALARS):
        return value
    if isinstance(value, Mapping):
        frozen = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise EventError(f"{path} 的键必须是字符串，收到 {key!r}")
            frozen[key] = _freeze(item, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(
            _freeze(item, path=f"{path}[{index}]") for index, item in enumerate(value)
        )
    raise EventError(
        f"{path} 只能包含 JSON 安全的值（字符串/数字/布尔/None/字典/列表），"
        f"收到 {type(value).__name__}"
    )


def _thaw(value):
    """把冻结视图还原成普通可变结构，供序列化与外部使用。"""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def new_event_id(prefix: str = "evt") -> str:
    """给没有天然稳定 ID 的事件生成一个。"""
    return f"{prefix}_{uuid4().hex}"


def _require_id(value, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise EventError(f"{label} 必须是非空字符串")
    return value


def _optional_id(value, label: str) -> Optional[str]:
    if value is None:
        return None
    return _require_id(value, label)


@dataclass(frozen=True)
class Event:
    """一次被接受的客观发生。"""

    event_id: str
    type: EventType
    occurred_at: datetime
    scope: EventScope
    actor_id: Optional[str] = None
    participants: Tuple[str, ...] = ()
    location_id: Optional[str] = None
    channel_id: Optional[str] = None
    payload: Mapping = field(default_factory=dict)
    provenance: Mapping = field(default_factory=dict)
    causation_id: Optional[str] = None
    correlation_id: Optional[str] = None

    def __post_init__(self) -> None:
        set_ = object.__setattr__

        set_(self, "event_id", _require_id(self.event_id, "event_id"))

        try:
            set_(self, "type", EventType(self.type))
        except ValueError:
            raise EventError(f"未知的事件类型: {self.type!r}") from None
        try:
            set_(self, "scope", EventScope(self.scope))
        except ValueError:
            raise EventError(f"未知的事件 scope: {self.scope!r}") from None

        if not isinstance(self.occurred_at, datetime):
            raise EventError("occurred_at 必须是 datetime（模拟时钟时间）")

        set_(self, "actor_id", _optional_id(self.actor_id, "actor_id"))
        set_(self, "location_id", _optional_id(self.location_id, "location_id"))
        set_(self, "channel_id", _optional_id(self.channel_id, "channel_id"))
        set_(self, "causation_id", _optional_id(self.causation_id, "causation_id"))
        set_(self, "correlation_id", _optional_id(self.correlation_id, "correlation_id"))

        set_(self, "participants", self._normalize_participants(self.participants))

        if not isinstance(self.payload, Mapping):
            raise EventError("payload 必须是字典")
        if not isinstance(self.provenance, Mapping):
            raise EventError("provenance 必须是字典")
        set_(self, "payload", _freeze(self.payload, path="payload"))
        set_(self, "provenance", _freeze(self.provenance, path="provenance"))

        self._validate_scope()
        self._validate_type()

    def __hash__(self) -> int:
        # 冻结后的 payload/provenance 本身不可哈希，而事件的身份本来就是
        # event_id（世界历史里它是唯一的）。按 ID 哈希，免得 set()/dict()
        # 装事件时抛出一个跟真实问题无关的 TypeError。
        return hash(self.event_id)

    @staticmethod
    def _normalize_participants(participants) -> Tuple[str, ...]:
        if isinstance(participants, (str, bytes)) or not isinstance(
            participants, Iterable
        ):
            raise EventError("participants 必须是角色 ID 的序列")
        normalized: List[str] = []
        for participant in participants:
            _require_id(participant, "participant")
            if participant in normalized:
                raise EventError(f"participants 里出现重复角色: {participant}")
            normalized.append(participant)
        return tuple(normalized)

    # ── 校验 ────────────────────────────────────────────────────────────
    def _validate_scope(self) -> None:
        """每档 scope 各自的必填字段 —— 没有边界的事件不算声明了边界。"""
        if self.scope is EventScope.PRIVATE and self.actor_id is None:
            raise EventError("private 事件必须有 actor_id")
        if self.scope is EventScope.PARTICIPANT and not self.participants:
            raise EventError("participant 事件必须至少有一个 participant")
        if self.scope is EventScope.CHANNEL and self.channel_id is None:
            raise EventError("channel 事件必须有 channel_id")
        if self.scope is EventScope.LOCATION and self.location_id is None:
            raise EventError("location 事件必须有 location_id")

    def _validate_type(self) -> None:
        if self.type is EventType.DIALOGUE_SPOKEN:
            self._require_actor()
            self._require_text()
            if self.location_id is None and self.channel_id is None:
                raise EventError(
                    "dialogue.spoken 必须落在某个 location_id 或 channel_id 上"
                )
        elif self.type is EventType.MESSAGE_SENT:
            self._require_actor()
            self._require_text()
            if self.channel_id is None:
                raise EventError("message.sent 必须有 channel_id")
        elif self.type in (
            EventType.PRESENCE_JOINED_CHANNEL,
            EventType.PRESENCE_LEFT_CHANNEL,
        ):
            self._require_actor()
            if self.channel_id is None:
                raise EventError(f"{self.type.value} 必须有 channel_id")
        elif self.type is EventType.WORLD_TIME_ADVANCED:
            if self.actor_id is not None:
                raise EventError("world.time_advanced 是世界事件，不能有 actor_id")
            minutes = self.payload.get("minutes")
            if isinstance(minutes, bool) or not isinstance(minutes, int):
                raise EventError("world.time_advanced 的 payload.minutes 必须是整数")
            if minutes < 0:
                raise EventError("world.time_advanced 的 payload.minutes 不能为负")
        elif self.type is EventType.CHARACTER_LOCATION_CHANGED:
            self._require_actor()
            if self.location_id is None:
                raise EventError("character.location_changed 必须有目标 location_id")

    def _require_actor(self) -> None:
        if self.actor_id is None:
            raise EventError(f"{self.type.value} 必须有 actor_id")

    def _require_text(self) -> None:
        text = self.payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise EventError(f"{self.type.value} 的 payload.text 必须是非空字符串")

    # ── 序列化 ──────────────────────────────────────────────────────────
    def to_dict(self) -> Dict:
        """完整公开形状；返回值是全新的可变结构，改它影响不到事件本身。"""
        return {
            "event_id": self.event_id,
            "type": self.type.value,
            "occurred_at": self.occurred_at.isoformat(),
            "scope": self.scope.value,
            "actor_id": self.actor_id,
            "participants": list(self.participants),
            "location_id": self.location_id,
            "channel_id": self.channel_id,
            "payload": _thaw(self.payload),
            "provenance": _thaw(self.provenance),
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "Event":
        return cls(
            event_id=payload["event_id"],
            type=payload["type"],
            occurred_at=datetime.fromisoformat(payload["occurred_at"]),
            scope=payload["scope"],
            actor_id=payload.get("actor_id"),
            participants=tuple(payload.get("participants", ())),
            location_id=payload.get("location_id"),
            channel_id=payload.get("channel_id"),
            payload=payload.get("payload", {}),
            provenance=payload.get("provenance", {}),
            causation_id=payload.get("causation_id"),
            correlation_id=payload.get("correlation_id"),
        )
