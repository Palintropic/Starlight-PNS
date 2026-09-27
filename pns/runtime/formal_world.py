# pns/runtime/formal_world.py — 正式世界的开局（WORLD-1「夜明け前」）
#
# 1.x 的世界从一个遗留场景投影出来：场景写好几点、谁在哪、在做什么。正式世界
# 不这样开局。它的身份和开局规则写在这里，初始状态**全部**由已校验的内容推出：
#
#   * 时钟：启动当天（按世界时区）的开局时刻，例如 Asia/Tokyo 19:00；
#   * 每个 resident：授予来自角色内容；位置、活动、频道在场取自作息表在开局
#     那一刻所在的那一段——开局就与作息一致，不需要第一步去"纠正"它；
#   * 来源：世界身份、时区、开局时刻、创建时刻（UTC）、内容版本与指纹、操作者，
#     写进 WorldState 的 origin（与 1.x 的 legacy_scene 来源同一个位置）。它是
#     运维来源，运行时不读它做判断，也不进任何角色的上下文；
#   * 内容账本：记下开局采用的每人作息的指纹，之后内容包里的新版本要经项目
#     所有者明确采用才生效（见 pns/models/content_ledger.py）。
#
# 开局不写任何世界事件，也不给角色写开场白：19:00 之前的事没有发生过，不能
# 伪造成 resident 的经历（计划 §4.1 第 4 条、§5.2）。
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Dict, Mapping, Optional, Tuple

from pns.models.content_ledger import ContentLedger, content_fingerprint
from pns.models.session import SessionState
from pns.models.world_state import WorldState, WorldStateError
from pns.world.grants import install_grants


class FormalWorldError(ValueError):
    """正式世界开不了局：内容不齐，或开局安排没有授予支撑。"""


@dataclass(frozen=True)
class FormalWorldSpec:
    world_id: str
    display_name: str
    timezone_name: str
    # 世界时区相对 UTC 的偏移。日本没有夏令时，固定偏移就是 Asia/Tokyo 的全部
    # 规则；不依赖系统时区数据库（精简镜像里常常没有）。
    utc_offset: timedelta
    start: time
    residents: Tuple[str, ...]

    def tz(self) -> timezone:
        return timezone(self.utc_offset, self.timezone_name)

    def launch_clock(self, wall: datetime) -> datetime:
        """wall 那一刻所在的"启动当天"的开局时刻（模拟时间，naive）。"""
        if wall.utcoffset() is None:
            raise FormalWorldError("开局的现实时刻必须带时区")
        local = wall.astimezone(self.tz())
        return datetime.combine(local.date(), self.start)


YOAKE_MAE = FormalWorldSpec(
    world_id="yoake-mae",
    display_name="夜明け前",
    timezone_name="Asia/Tokyo",
    utc_offset=timedelta(hours=9),
    start=time(19, 0),
    residents=("mizuki", "ena"),
)

FORMAL_WORLDS: Mapping[str, FormalWorldSpec] = {YOAKE_MAE.world_id: YOAKE_MAE}


def formal_world(world_id: str) -> Optional[FormalWorldSpec]:
    return FORMAL_WORLDS.get(world_id)


def rhythm_subject(character_id: str) -> str:
    """内容账本里"某人的作息表"这一项的名字。"""
    return f"rhythm:{character_id}"


def rhythm_fingerprint(rhythm) -> str:
    return content_fingerprint(rhythm.to_dict())


# 内容里没有这一项时的指纹。
ABSENT = content_fingerprint(None)


def _grants_fingerprint(grants) -> str:
    return content_fingerprint(
        {
            "locations": sorted(list(pair) for pair in grants.locations),
            "channels": sorted(grants.channels),
        }
    )


def formal_session_state(
    spec: FormalWorldSpec,
    registry,
    *,
    session_id: str,
    wall: datetime,
    operator: Optional[str] = None,
) -> SessionState:
    """按正式世界的规则造一份**新的**初始 SessionState（还没绑任何服务）。"""
    clock = spec.launch_clock(wall)
    rhythms: Dict[str, object] = {}
    grants: Dict[str, object] = {}
    for character_id in spec.residents:
        if not registry.has_character(character_id):
            raise FormalWorldError(f"resident '{character_id}' 不在当前角色包里")
        content = registry.character(character_id)
        if content.system_prompt is None:
            raise FormalWorldError(f"resident '{character_id}' 还没有可用的提示词")
        if content.rhythm is None:
            raise FormalWorldError(f"resident '{character_id}' 没有作息表，开不了局")
        if content.grants is None:
            raise FormalWorldError(f"resident '{character_id}' 没有声明授予，开不了局")
        rhythms[character_id] = content.rhythm
        grants[character_id] = content.grants

    origin = {
        "kind": "formal_bootstrap",
        "world_id": spec.world_id,
        "display_name": spec.display_name,
        "timezone": spec.timezone_name,
        "utc_offset_minutes": int(spec.utc_offset.total_seconds() // 60),
        "launch_day": clock.date().isoformat(),
        "start": clock.isoformat(),
        "created_at_utc": wall.astimezone(timezone.utc).isoformat(),
        "operator": operator,
        "content": {
            "pack": registry.pack_name,
            "revision": registry.revision,
            "rhythms": {cid: rhythm_fingerprint(r) for cid, r in rhythms.items()},
            "grants": {cid: _grants_fingerprint(g) for cid, g in grants.items()},
        },
        "residents": list(spec.residents),
    }
    world = WorldState(
        clock=clock,
        locations=registry.new_location_graph(),
        channels=registry.new_channel_registry(),
        metadata={"origin": origin},
    )
    for character_id in spec.residents:
        install_grants(world, grants[character_id])
    try:
        for character_id in spec.residents:
            segment = rhythms[character_id].segment_at(clock)
            if segment.location_id is None:
                raise FormalWorldError(
                    f"resident '{character_id}' 在开局时刻所在的那一段没写地点"
                )
            world.place_character(character_id, segment.location_id)
            world.set_activity(character_id, segment.activity)
            if segment.channel_id is not None:
                world.join_channel(character_id, segment.channel_id)
    except WorldStateError as e:
        raise FormalWorldError(f"开局安排没有授予支撑：{e}") from e

    state = SessionState(
        session_id=session_id,
        scene=f"formal:{spec.world_id}",
        characters=list(spec.residents),
    )
    state.attach_world_state(world)
    with state.atomic_commit():
        state.set_content(
            ContentLedger(
                tuple(
                    (rhythm_subject(cid), rhythm_fingerprint(r))
                    for cid, r in rhythms.items()
                )
            )
        )
    return state


def gated_rhythms(state: SessionState, rhythms: Mapping, *, registry_revision: int, wall: str):
    """这个世界此刻能生效的作息表；新版本记一条待决，不生效。

    调用方持有事务（恢复路径的组装阶段）。返回过滤之后的作息表字典。没有内容
    账本的会话（1.x 场景世界）不设门，原样返回。
    """
    ledger = state.content
    if ledger is None:
        return dict(rhythms)
    accepted = {}
    residents = {
        subject[len("rhythm:"):]
        for subject, _ in ledger.adopted
        if subject.startswith("rhythm:")
    }
    for character_id in sorted(residents):
        subject = rhythm_subject(character_id)
        rhythm = rhythms.get(character_id)
        # 作息表从内容里消失了也是"新的一版"：同样要明确采用才生效，不能因为
        # 少了一个键就悄悄让这个人不再受作息驱动。
        fingerprint = rhythm_fingerprint(rhythm) if rhythm is not None else ABSENT
        ledger = ledger.offered(
            subject, fingerprint, registry_revision=registry_revision, wall=wall
        )
        if rhythm is not None and ledger.accepts(subject, fingerprint):
            accepted[character_id] = rhythm
    if ledger is not state.content:
        state.set_content(ledger)
    return accepted


__all__ = [
    "FORMAL_WORLDS",
    "YOAKE_MAE",
    "FormalWorldError",
    "FormalWorldSpec",
    "formal_session_state",
    "formal_world",
    "gated_rhythms",
    "rhythm_fingerprint",
    "rhythm_subject",
]
