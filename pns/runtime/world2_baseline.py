# pns/runtime/world2_baseline.py — WORLD-2 的重放基准（设计 v3 §4.5，复审 F2）
#
# 开局 origin 只记了 resident 名单、作息与授予的指纹，没有地点图、没有定义本身；
# 仅凭它重放不出今天的世界。所以世界上**第一笔** WORLD-2 操作（第一条
# world.locations_extended）在同一个提交闸门里记下一份基准，放在它 payload 的
# `baseline` 字段里：
#
#   * 版本化的地点图、频道表；
#   * 名单，各居民的位置、频道在场、活动、存下的可用性、授予；
#   * 已采用的作息定义（C4 的 canonical codec）与账本此刻的已采用版本、操作序号；
#   * 世界时钟，既有事件前缀的条数与指纹；
#   * 日历指纹与选表规则版本。
#
# 基准只在那一刻由当时的 authority 生成。提交边界拿它跟当时的状态逐字比对（见
# check_baseline_at_commit）；恢复时从它加后缀重放（pns/runtime/world2_replay.py），
# **不能**从恢复出来的快照重新生成一份去替换它。
#
# 后面的扩展事件 `baseline` 一律为 None：一个世界只有一份基准。
from typing import Dict, Mapping, Sequence

from pns.models.content_ledger import content_fingerprint
from pns.models.event import Event
from pns.models.frozen import thaw_json_value
from pns.models.session import SessionState
from pns.runtime.formal_world import rhythm_fingerprint, rhythm_subject
from pns.world.calendar import CALENDAR_RULES_VERSION, calendar_fingerprint
from pns.world.rhythm import DailyRhythm, RhythmError, decode_rhythm, encode_rhythm

BASELINE_SCHEMA = "pns.world2_baseline/1"

_BASELINE_KEYS = frozenset(
    {
        "schema",
        "locations",
        "channels",
        "roster",
        "world",
        "rhythms",
        "ledger",
        "clock",
        "prefix",
        "calendar",
    }
)
# 世界状态里进基准的那几项（WorldState.to_dict 的同名字段）。环境状态（天气）与
# metadata 不在其中：前者不是居民状态，后者装着 origin，origin 自有校验。
WORLD_FIELDS = (
    "character_locations",
    "channel_members",
    "character_availability",
    "character_activities",
    "location_grants",
    "channel_grants",
)


class BaselineError(ValueError):
    """重放基准缺失、形状不对，或与它声称描述的世界不符。"""


def events_fingerprint(events: Sequence[Event]) -> str:
    """一段事件历史的内容指纹（按顺序；序号即位置）。"""
    return content_fingerprint([event.to_dict() for event in events])


def calendar_binding() -> Dict:
    return {"fingerprint": calendar_fingerprint(), "rules_version": CALENDAR_RULES_VERSION}


def build_baseline(state: SessionState, rhythms: Mapping[str, DailyRhythm]) -> Dict:
    """此刻这个世界的重放基准。`rhythms` 是每个已登记作息主体此刻生效的定义。"""
    world = state.world_state
    ledger = state.content
    if world is None or ledger is None:
        raise BaselineError("只有带内容账本的正式世界才有重放基准")
    snapshot = world.to_dict()
    events = state.events.events()
    return {
        "schema": BASELINE_SCHEMA,
        "locations": snapshot["locations"],
        "channels": snapshot["channels"],
        "roster": list(state.characters),
        "world": {name: snapshot[name] for name in WORLD_FIELDS},
        "rhythms": {cid: encode_rhythm(rhythms[cid]) for cid in sorted(rhythms)},
        "ledger": {"next_seq": ledger.next_seq, "adopted": dict(ledger.adopted)},
        "clock": snapshot["clock"],
        "prefix": {"length": len(events), "fingerprint": events_fingerprint(events)},
        "calendar": calendar_binding(),
    }


def decode_baseline_rhythms(baseline: Mapping) -> Dict[str, DailyRhythm]:
    """基准的形状，以及其中的作息定义。不对照任何世界。"""
    if not isinstance(baseline, Mapping) or set(baseline) != _BASELINE_KEYS:
        raise BaselineError("重放基准的字段不对")
    if baseline["schema"] != BASELINE_SCHEMA:
        raise BaselineError(f"不认识的重放基准版本: {baseline['schema']!r}")
    encoded = baseline["rhythms"]
    if not isinstance(encoded, Mapping):
        raise BaselineError("重放基准的 rhythms 必须是 {角色: 作息}")
    try:
        return {
            str(cid): decode_rhythm(payload, character_id=cid)
            for cid, payload in encoded.items()
        }
    except RhythmError as e:
        raise BaselineError(f"重放基准里的作息不成立：{e}") from None


def check_rhythms_against_adopted(
    rhythms: Mapping[str, DailyRhythm], adopted: Mapping[str, str]
) -> None:
    """基准里的作息定义恰好覆盖账本里每个作息主体，而且各自就是已采用的那一版。"""
    subjects = {subject for subject in adopted if subject.startswith("rhythm:")}
    if {rhythm_subject(cid) for cid in rhythms} != subjects:
        raise BaselineError("重放基准里的作息与账本登记的作息主体不一致")
    for cid, rhythm in rhythms.items():
        if rhythm_fingerprint(rhythm) != adopted[rhythm_subject(cid)]:
            raise BaselineError(f"重放基准里 '{cid}' 的作息不是账本已采用的那一版")


def check_baseline_at_commit(state: SessionState, baseline) -> None:
    """第一笔 WORLD-2 操作带的基准，必须逐字就是此刻这个世界的基准。

    作息定义取自基准本身（会话里没有定义，只有指纹），先核它们正是账本已采用的
    那一版，再用它们重算一份基准整体比较。
    """
    baseline = thaw_json_value(baseline)
    rhythms = decode_baseline_rhythms(baseline)
    if state.content is None:
        raise BaselineError("只有带内容账本的正式世界才有重放基准")
    check_rhythms_against_adopted(rhythms, dict(state.content.adopted))
    if baseline != build_baseline(state, rhythms):
        raise BaselineError("重放基准与此刻的世界不符")


__all__ = [
    "BASELINE_SCHEMA",
    "BaselineError",
    "WORLD_FIELDS",
    "build_baseline",
    "calendar_binding",
    "check_baseline_at_commit",
    "check_rhythms_against_adopted",
    "decode_baseline_rhythms",
    "events_fingerprint",
]
