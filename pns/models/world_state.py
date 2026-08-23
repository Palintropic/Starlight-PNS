# pns/models/world_state.py — 会话期间可变世界现实的权威表示
#
# P4 起 WorldState 取代 scene 成为运行时的世界真相：scene 只在初始化时
# 通过 pns/world/scene_compat.py 投影进来一次，之后不再参与。
#
# 这里刻意不再有 current_scene —— 世界里可以同时存在处于不同地点的角色，
# "当前场景" 不是世界的属性。角色一律用稳定 ID 作 key，不用显示名。
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Set

from pns.models.channel import ChannelRegistry
from pns.models.location import LocationGraph


class WorldStateError(ValueError):
    """对世界状态的非法操作（未知地点/频道、非法角色 ID、时间倒流等）。"""


class Availability(str, Enum):
    """角色当前对外界的感知能力。

    这是 AgentState（后续阶段）的一个最小前身，只放"是否还能感知到外界"
    这一件事 —— 目标、活动、计划都不在这里。

    三档的区别是刻意的，因为它们在曝光里的后果不同：

      AVAILABLE  正常感知。
      BUSY       在忙，但仍然感知得到。忙不等于世界没发生过；要不要理会
                 是 Attention/Agency 的事，不是感知资格的事。
      ASLEEP     感知不到外界。自己的动作仍然自观察（睡着的人不会发言，
                 真发言了那就是醒着）。
    """

    AVAILABLE = "available"
    BUSY = "busy"
    ASLEEP = "asleep"


class ActivityKind(str, Enum):
    """角色此刻正在进行的、可作为世界事实引用的活动。

    这是一个闭集而不是自由文本：活动会进入生成与 Router 提示，允许任意字符串
    等于给调用方开了一条提示注入通道。没有可靠事实时必须使用 UNSPECIFIED，
    不能从角色职业或地点猜一个“很可能”的活动。
    """

    UNSPECIFIED = "unspecified"
    IDLE = "idle"
    RESTING = "resting"
    STUDYING = "studying"
    WORKING_PART_TIME = "working_part_time"
    DRAWING = "drawing"
    COMPOSING = "composing"
    EDITING_VIDEO = "editing_video"
    ONLINE_CHATTING = "online_chatting"


@dataclass(frozen=True)
class CharacterActivity:
    kind: ActivityKind
    since: datetime

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        try:
            set_(self, "kind", ActivityKind(self.kind))
        except ValueError:
            raise WorldStateError(f"未知的角色活动: {self.kind!r}") from None
        if not isinstance(self.since, datetime):
            raise WorldStateError("角色活动的 since 必须是 datetime")

    def to_dict(self) -> Dict:
        return {"kind": self.kind.value, "since": self.since.isoformat()}

    @classmethod
    def from_dict(cls, payload: Dict) -> "CharacterActivity":
        if not isinstance(payload, dict):
            raise WorldStateError("角色活动必须是字典")
        return cls(
            kind=payload["kind"],
            since=datetime.fromisoformat(payload["since"]),
        )


@dataclass
class WorldState:
    """一个会话里唯一一份可变世界状态。"""

    clock: datetime
    locations: LocationGraph = field(default_factory=LocationGraph)
    channels: ChannelRegistry = field(default_factory=ChannelRegistry)
    character_locations: Dict[str, str] = field(default_factory=dict)
    channel_members: Dict[str, Set[str]] = field(default_factory=dict)
    # 只存偏离默认值的角色：没有条目就是 AVAILABLE。
    character_availability: Dict[str, Availability] = field(default_factory=dict)
    character_activities: Dict[str, CharacterActivity] = field(default_factory=dict)
    location_state: Dict[str, Dict] = field(default_factory=dict)
    metadata: Dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Own and validate all mutable state supplied at construction time."""
        if not isinstance(self.clock, datetime):
            raise WorldStateError("clock 必须是 datetime")
        if not isinstance(self.locations, LocationGraph):
            raise WorldStateError("locations 必须是 LocationGraph")
        if not isinstance(self.channels, ChannelRegistry):
            raise WorldStateError("channels 必须是 ChannelRegistry")

        self.character_locations = dict(self.character_locations)
        self.channel_members = {
            channel_id: set(members)
            for channel_id, members in self.channel_members.items()
        }
        self.character_availability = {
            character_id: Availability(value)
            for character_id, value in self.character_availability.items()
        }
        self.character_activities = {
            character_id: (
                activity
                if isinstance(activity, CharacterActivity)
                else CharacterActivity.from_dict(activity)
            )
            for character_id, activity in self.character_activities.items()
        }
        self.location_state = deepcopy(self.location_state)
        self.metadata = deepcopy(self.metadata)
        self.validate()

    def validate(self) -> None:
        """Reject state that bypassed the public mutation methods."""
        self.locations.validate()
        for character_id, location_id in self.character_locations.items():
            self._require_character_id(character_id)
            if not self.locations.has(location_id):
                raise WorldStateError(
                    f"角色 '{character_id}' 引用了未知的 location_id: {location_id}"
                )
        for channel_id, members in self.channel_members.items():
            if not self.channels.has(channel_id):
                raise WorldStateError(f"频道成员引用了未知的 channel_id: {channel_id}")
            for character_id in members:
                self._require_character_id(character_id)
        for character_id, availability in self.character_availability.items():
            self._require_character_id(character_id)
            if not isinstance(availability, Availability):
                # 脏值会让"睡着了感知不到"静默失效 —— 这类失败必须是响亮的。
                raise WorldStateError(
                    f"角色 '{character_id}' 的可用性必须是 Availability，"
                    f"收到 {availability!r}"
                )
        known = set(self.character_locations)
        for members in self.channel_members.values():
            known.update(members)
        for character_id, activity in self.character_activities.items():
            self._require_character_id(character_id)
            if character_id not in known:
                raise WorldStateError(
                    f"角色活动引用了世界里不存在的角色: {character_id}"
                )
            if not isinstance(activity, CharacterActivity):
                raise WorldStateError(
                    f"角色 '{character_id}' 的活动必须是 CharacterActivity"
                )
            if (activity.since.tzinfo is None) != (self.clock.tzinfo is None):
                raise WorldStateError(
                    f"角色 '{character_id}' 的活动起始时间与世界时钟时区语义不一致"
                )
            if activity.since > self.clock:
                raise WorldStateError(
                    f"角色 '{character_id}' 的活动起始时间晚于世界时钟"
                )
        for location_id, facts in self.location_state.items():
            if not self.locations.has(location_id):
                raise WorldStateError(
                    f"地点状态引用了未知的 location_id: {location_id}"
                )
            if not isinstance(facts, dict):
                raise WorldStateError(f"地点 '{location_id}' 的状态必须是字典")

    # ── 时间 ────────────────────────────────────────────────────────────
    @property
    def date(self) -> str:
        return self.clock.strftime("%Y-%m-%d")

    @property
    def time(self) -> str:
        """HH:MM 投影，仅供显示/兼容；日期不会因此丢失。"""
        return self.clock.strftime("%H:%M")

    def advance_time(self, minutes: int = 10) -> datetime:
        """推进模拟时间，跨零点时日期一并进位。"""
        if minutes < 0:
            raise WorldStateError("模拟时间不能倒退")
        self.clock = self.clock + timedelta(minutes=minutes)
        return self.clock

    # ── 物理位置 ────────────────────────────────────────────────────────
    def place_character(self, character_id: str, location_id: str) -> None:
        self._require_character_id(character_id)
        if not self.locations.has(location_id):
            raise WorldStateError(f"未知的 location_id: {location_id}")
        self.character_locations[character_id] = location_id

    def remove_character(self, character_id: str) -> None:
        self.character_locations.pop(character_id, None)
        self.character_availability.pop(character_id, None)
        self.character_activities.pop(character_id, None)
        for members in self.channel_members.values():
            members.discard(character_id)

    def location_of(self, character_id: str) -> Optional[str]:
        return self.character_locations.get(character_id)

    def characters_at(
        self, location_id: str, include_contained: bool = False
    ) -> List[str]:
        """在某地点的角色；include_contained=True 时把子地点里的人也算进来。"""
        if not self.locations.has(location_id):
            raise WorldStateError(f"未知的 location_id: {location_id}")
        found = [
            character_id
            for character_id, current in self.character_locations.items()
            if current == location_id
            or (
                include_contained
                and self.locations.contains_location(location_id, current)
            )
        ]
        return sorted(found)

    # ── 线上频道 ────────────────────────────────────────────────────────
    def join_channel(self, character_id: str, channel_id: str) -> None:
        self._require_character_id(character_id)
        if not self.channels.has(channel_id):
            raise WorldStateError(f"未知的 channel_id: {channel_id}")
        self.channel_members.setdefault(channel_id, set()).add(character_id)

    def leave_channel(self, character_id: str, channel_id: str) -> None:
        if channel_id in self.channel_members:
            self.channel_members[channel_id].discard(character_id)

    def is_in_channel(self, character_id: str, channel_id: str) -> bool:
        return character_id in self.channel_members.get(channel_id, set())

    def channels_for(self, character_id: str) -> List[str]:
        return sorted(
            channel_id
            for channel_id, members in self.channel_members.items()
            if character_id in members
        )

    def channel_participants(self, channel_id: str) -> List[str]:
        if not self.channels.has(channel_id):
            raise WorldStateError(f"未知的 channel_id: {channel_id}")
        return sorted(self.channel_members.get(channel_id, set()))

    # ── 可用性 ──────────────────────────────────────────────────────────
    def set_availability(self, character_id: str, availability) -> None:
        self._require_character_id(character_id)
        availability = Availability(availability)
        if availability is Availability.AVAILABLE:
            self.character_availability.pop(character_id, None)
        else:
            self.character_availability[character_id] = availability

    def availability_of(self, character_id: str) -> Availability:
        return self.character_availability.get(character_id, Availability.AVAILABLE)

    # ── 当前活动 ────────────────────────────────────────────────────────
    def set_activity(self, character_id: str, activity) -> CharacterActivity:
        self._require_character_id(character_id)
        if character_id not in self.known_characters():
            raise WorldStateError(f"世界里不存在角色 '{character_id}'")
        try:
            kind = ActivityKind(activity)
        except ValueError:
            raise WorldStateError(f"未知的角色活动: {activity!r}") from None
        record = CharacterActivity(kind=kind, since=self.clock)
        if record.kind is ActivityKind.UNSPECIFIED:
            self.character_activities.pop(character_id, None)
        else:
            self.character_activities[character_id] = record
        return record

    def activity_of(self, character_id: str) -> CharacterActivity:
        self._require_character_id(character_id)
        return self.character_activities.get(
            character_id,
            CharacterActivity(kind=ActivityKind.UNSPECIFIED, since=self.clock),
        )

    # ── 角色 ────────────────────────────────────────────────────────────
    def known_characters(self) -> List[str]:
        """世界当前认识的角色：有物理位置的，或挂在任一频道上的。

        事件提交用它判断"这个角色到底存不存在"，因此这里是唯一的判定口径，
        不去读会话的角色列表 —— 世界不该反过来依赖某个会话选了谁。
        """
        known = set(self.character_locations)
        for members in self.channel_members.values():
            known |= members
        return sorted(known)

    # ── 地点环境状态 ────────────────────────────────────────────────────
    def set_environment(self, location_id: str, facts: Dict) -> None:
        if not self.locations.has(location_id):
            raise WorldStateError(f"未知的 location_id: {location_id}")
        self.location_state.setdefault(location_id, {}).update(deepcopy(facts))

    def environment_of(self, location_id: str) -> Dict:
        return deepcopy(self.location_state.get(location_id, {}))

    # ── 提交事务支持 ────────────────────────────────────────────────────
    #
    # locations/channels 是会话期间的静态结构（WorldState 没有任何方法改它们），
    # 所以快照只覆盖真正可变的那几项，回滚不需要重建整张位置图。
    # 这是一条约束而不只是一个观察：以后如果真要在会话中途改位置图/频道表，
    # 必须先把它们纳入下面这两个方法，否则提交失败时那部分回滚不掉。
    def snapshot_mutable_state(self) -> Dict:
        """取一份可变状态快照，供提交失败时整体回滚。"""
        return {
            "clock": self.clock,
            "character_locations": dict(self.character_locations),
            "channel_members": {
                channel_id: set(members)
                for channel_id, members in self.channel_members.items()
            },
            "character_availability": dict(self.character_availability),
            "character_activities": dict(self.character_activities),
            "location_state": deepcopy(self.location_state),
            "metadata": deepcopy(self.metadata),
        }

    def restore_mutable_state(self, snapshot: Dict) -> None:
        """就地恢复到 snapshot_mutable_state() 的那一刻。"""
        self.clock = snapshot["clock"]
        self.character_locations = dict(snapshot["character_locations"])
        self.channel_members = {
            channel_id: set(members)
            for channel_id, members in snapshot["channel_members"].items()
        }
        self.character_availability = dict(snapshot["character_availability"])
        self.character_activities = dict(snapshot["character_activities"])
        self.location_state = deepcopy(snapshot["location_state"])
        self.metadata = deepcopy(snapshot["metadata"])

    # ── 序列化 ──────────────────────────────────────────────────────────
    def to_dict(self) -> Dict:
        """完整公开形状；返回值全是深拷贝，外部改不动内部状态。"""
        return {
            "clock": self.clock.isoformat(),
            "date": self.date,
            "time": self.time,
            "locations": self.locations.to_dict(),
            "channels": self.channels.to_dict(),
            "character_locations": dict(self.character_locations),
            "channel_members": {
                channel_id: sorted(members)
                for channel_id, members in self.channel_members.items()
            },
            "character_availability": {
                character_id: availability.value
                for character_id, availability in self.character_availability.items()
            },
            "character_activities": {
                character_id: activity.to_dict()
                for character_id, activity in self.character_activities.items()
            },
            "location_state": deepcopy(self.location_state),
            "metadata": deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "WorldState":
        return cls(
            clock=datetime.fromisoformat(payload["clock"]),
            locations=LocationGraph.from_dict(payload.get("locations", {})),
            channels=ChannelRegistry.from_dict(payload.get("channels", {})),
            character_locations=dict(payload.get("character_locations", {})),
            channel_members={
                channel_id: set(members)
                for channel_id, members in payload.get("channel_members", {}).items()
            },
            character_availability=dict(payload.get("character_availability", {})),
            character_activities=dict(payload.get("character_activities", {})),
            location_state=deepcopy(payload.get("location_state", {})),
            metadata=deepcopy(payload.get("metadata", {})),
        )

    @staticmethod
    def _require_character_id(character_id: str) -> None:
        if not isinstance(character_id, str) or not character_id:
            raise WorldStateError("角色必须用非空的稳定 ID 标识")
