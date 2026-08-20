# pns/world/channels.py — 最小频道注册表
#
# Nightcord 是线上频道，不是物理地点：角色人在自己房间、同时挂在频道里，
# 这两件事在 WorldState 里互相独立。
from pns.models.channel import Channel, ChannelKind, ChannelRegistry

DEFAULT_CHANNELS = (
    Channel(
        channel_id="nightcord",
        name="Nightcord",
        kind=ChannelKind.MIXED,
        private=True,
        description="Nightcord 语音频道",
    ),
)


def build_default_channel_registry() -> ChannelRegistry:
    """每次返回一份新的默认频道表。"""
    return ChannelRegistry(DEFAULT_CHANNELS)
