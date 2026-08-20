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
from typing import Dict, List, Optional, Set

from pns.models.channel import ChannelRegistry
from pns.models.location import LocationGraph


class WorldStateError(ValueError):
    """对世界状态的非法操作（未知地点/频道、非法角色 ID、时间倒流等）。"""


@dataclass
class WorldState:
    """一个会话里唯一一份可变世界状态。"""

    clock: datetime
    locations: LocationGraph = field(default_factory=LocationGraph)
    channels: ChannelRegistry = field(default_factory=ChannelRegistry)
    character_locations: Dict[str, str] = field(default_factory=dict)
    channel_members: Dict[str, Set[str]] = field(default_factory=dict)
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

    # ── 地点环境状态 ────────────────────────────────────────────────────
    def set_environment(self, location_id: str, facts: Dict) -> None:
        if not self.locations.has(location_id):
            raise WorldStateError(f"未知的 location_id: {location_id}")
        self.location_state.setdefault(location_id, {}).update(deepcopy(facts))

    def environment_of(self, location_id: str) -> Dict:
        return deepcopy(self.location_state.get(location_id, {}))

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
            location_state=deepcopy(payload.get("location_state", {})),
            metadata=deepcopy(payload.get("metadata", {})),
        )

    @staticmethod
    def _require_character_id(character_id: str) -> None:
        if not isinstance(character_id, str) or not character_id:
            raise WorldStateError("角色必须用非空的稳定 ID 标识")
