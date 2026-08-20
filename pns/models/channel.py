# pns/models/channel.py — 线上频道领域模型
#
# 频道是通信空间，不是物理地点：Nightcord 是频道，角色可以人在自己房间、
# 同时挂在频道里。所以它跟 pns/models/location.py 完全分开，两边没有互相引用。
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, Iterator, List


class ChannelKind(str, Enum):
    VOICE = "voice"
    TEXT = "text"
    MIXED = "mixed"


@dataclass(frozen=True)
class Channel:
    """一个线上通信频道。"""

    channel_id: str
    name: str
    kind: ChannelKind = ChannelKind.MIXED
    private: bool = True
    description: str = ""

    @property
    def display(self) -> str:
        return self.description or self.name

    def to_dict(self) -> Dict:
        return {
            "channel_id": self.channel_id,
            "name": self.name,
            "kind": self.kind.value,
            "private": self.private,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "Channel":
        return cls(
            channel_id=payload["channel_id"],
            name=payload["name"],
            kind=ChannelKind(payload.get("kind", ChannelKind.MIXED.value)),
            private=bool(payload.get("private", True)),
            description=payload.get("description", ""),
        )


class ChannelRegistryError(ValueError):
    """频道表不自洽（重复 ID、引用不存在的频道等）。"""


class ChannelRegistry:
    """已知频道的静态注册表；谁在频道里属于 WorldState，不在这里。"""

    def __init__(self, channels: Iterable[Channel] = ()):
        self._channels: Dict[str, Channel] = {}
        for channel in channels:
            self.add(channel)

    def add(self, channel: Channel) -> None:
        if not channel.channel_id:
            raise ChannelRegistryError("channel_id 不能为空")
        if channel.channel_id in self._channels:
            raise ChannelRegistryError(f"重复的 channel_id: {channel.channel_id}")
        self._channels[channel.channel_id] = channel

    def __contains__(self, channel_id: object) -> bool:
        return channel_id in self._channels

    def __iter__(self) -> Iterator[Channel]:
        return iter(self._channels.values())

    def __len__(self) -> int:
        return len(self._channels)

    def has(self, channel_id: str) -> bool:
        return channel_id in self._channels

    def get(self, channel_id: str) -> Channel:
        try:
            return self._channels[channel_id]
        except KeyError:
            raise ChannelRegistryError(f"未知的 channel_id: {channel_id}") from None

    def ids(self) -> List[str]:
        return list(self._channels)

    def to_dict(self) -> Dict:
        return {cid: channel.to_dict() for cid, channel in self._channels.items()}

    @classmethod
    def from_dict(cls, payload: Dict) -> "ChannelRegistry":
        return cls(Channel.from_dict(entry) for entry in deepcopy(payload).values())
