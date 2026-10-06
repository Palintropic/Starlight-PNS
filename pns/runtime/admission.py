# pns/runtime/admission.py — WORLD-2 的维护入口：地点扩展与居民入住（设计 v3 §3、§4.6、§4.8）
#
# 一笔操作 = 一条权威事件，经**当前拥有这个世界**的 PersistentWorld 与它的提交闸门
# 提交，然后 checkpoint。没有别的写入路径：不读写存档文件，不另起 writer。
#
# 三个动作：
#
#   preflight  只读。对着此刻的世界与冻结内容快照算出提议（地点 / 窗口 / 室友 / 序位）、
#              各项指纹，可行与否、不可行的原因、下一个可行窗口。预检可行不代替执行时
#              在闸门内的重新判断。
#   execute    执行一笔。处理顺序（设计 §4.6）：
#                1. 在闸门内按 operation_id 查世界历史；已提交的同请求只查询或补存，
#                   **不**重判窗口、不重做前身检查、不重算种子；同 id 不同请求拒绝；
#                2. 没提交过，才对着此刻的世界判内容、窗口、室友、前身，构造事件并提交；
#                3. checkpoint，按磁盘证据报告四档结果之一。
#   query      只读。从已提交历史查这一笔与它此刻的耐久档；不补存。
#
# 四档结果：rejected（没有任何改变）/ committed（已提交、还没存盘）/
# visible_not_durable（磁盘上那一版含它、耐久性证实不了）/ durable。
#
# operation_id 绑定的"请求语义"是规范化请求体的指纹，写进事件 envelope；同一个 id
# 只能对应同一个请求。
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Mapping, Optional, Tuple


from pns.models.content_ledger import content_fingerprint
from pns.models.event import AUTHORITY_EVENT_TYPES, Event, EventScope, EventType
from pns.models.session import SessionState
from pns.models.world_state import ActivityKind, Availability
from pns.runtime.event_commit import EventCommitError
from pns.runtime.formal_world import grants_fingerprint, rhythm_fingerprint, rhythm_subject
from pns.runtime.world2_baseline import build_baseline
from pns.runtime.world2_seed import admission_seed
from pns.world.extension import ExtensionError, extended_graph, graph_fingerprint
from pns.world.grants import encode_grants, may_enter
from pns.world.rhythm import encode_rhythm
from pns.world.routing import plan_route

REJECTED = "rejected"
COMMITTED = "committed"
VISIBLE_NOT_DURABLE = "visible_not_durable"
DURABLE = "durable"

EXTENSION = "extension"
ADMISSION = "admission"

_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
# 往后最多看几段找下一个可行窗口（两周的作息足够覆盖平日 / 休息日两张表）。
_WINDOW_SEARCH_SEGMENTS = 14 * 24
# 前身扫描不看这些键下的字符串：自然语言；入住请求里**声明**的室友名单（声明她是
# 别人的室友不是她的记录，设计 §4.8）；以及操作者——那是账户 id，跟角色 id 是两个
# 命名空间，维护者的账户正好叫 airi 也不是"airi 在世界里出现过"。
_NOT_ENTITY_KEYS = frozenset(
    {"text", "content", "summary", "cue", "message", "line", "roommates", "operator"}
)


class AdmissionRejected(ValueError):
    """这一笔不成立：什么都没改。`code` 给脚本判断用，消息给人看。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _AlreadyCommitted(Exception):
    def __init__(self, event: Event, sequence: int) -> None:
        super().__init__(event.event_id)
        self.event = event
        self.sequence = sequence


# ── 请求 ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ExtensionRequest:
    operation_id: str
    graph_before: str
    graph_after: str

    kind = EXTENSION

    def canonical(self) -> Dict:
        return {
            "kind": EXTENSION,
            "operation_id": self.operation_id,
            "graph_before": self.graph_before,
            "graph_after": self.graph_after,
        }


@dataclass(frozen=True)
class AdmissionRequest:
    operation_id: str
    character_id: str
    location_id: str
    roommates: Tuple[str, ...]
    ordinal: int
    window_start: str
    window_end: str
    rhythm_fingerprint: str
    grants_fingerprint: str

    kind = ADMISSION

    def canonical(self) -> Dict:
        return {
            "kind": ADMISSION,
            "operation_id": self.operation_id,
            "character_id": self.character_id,
            "location_id": self.location_id,
            "roommates": sorted(self.roommates),
            "ordinal": self.ordinal,
            "window": {"start": self.window_start, "end": self.window_end},
            "fingerprints": {"rhythm": self.rhythm_fingerprint, "grants": self.grants_fingerprint},
        }


def _text(body: Mapping, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise AdmissionRejected("bad_request", f"{key} 必须是非空字符串")
    return value


def _exact_keys(body, keys, label) -> None:
    if not isinstance(body, Mapping) or set(body) != set(keys):
        raise AdmissionRejected(
            "bad_request", f"{label} 的字段必须正好是 {'、'.join(sorted(keys))}"
        )


def parse_operation(body) -> "ExtensionRequest | AdmissionRequest":
    """执行请求体 → 类型化请求。字段严格，不认识的一律拒绝。"""
    if not isinstance(body, Mapping):
        raise AdmissionRejected("bad_request", "请求体必须是对象")
    kind = body.get("kind")
    if kind == EXTENSION:
        _exact_keys(body, {"kind", "operation_id", "graph_before", "graph_after"}, "扩展请求")
        request = ExtensionRequest(
            _operation_id(body), _text(body, "graph_before"), _text(body, "graph_after")
        )
    elif kind == ADMISSION:
        _exact_keys(
            body,
            {"kind", "operation_id", "character_id", "location_id", "roommates", "ordinal", "window", "fingerprints"},
            "入住请求",
        )
        roommates = body["roommates"]
        if (
            not isinstance(roommates, (list, tuple))
            or not all(isinstance(r, str) and r for r in roommates)
            or len(set(roommates)) != len(roommates)
        ):
            raise AdmissionRejected("bad_request", "roommates 必须是不重复的角色 id 列表")
        ordinal = body["ordinal"]
        if isinstance(ordinal, bool) or not isinstance(ordinal, int):
            raise AdmissionRejected("bad_request", "ordinal 必须是整数")
        window = body["window"]
        _exact_keys(window, {"start", "end"}, "window")
        fingerprints = body["fingerprints"]
        _exact_keys(fingerprints, {"rhythm", "grants"}, "fingerprints")
        request = AdmissionRequest(
            operation_id=_operation_id(body),
            character_id=_text(body, "character_id"),
            location_id=_text(body, "location_id"),
            roommates=tuple(roommates),
            ordinal=ordinal,
            window_start=_text(window, "start"),
            window_end=_text(window, "end"),
            rhythm_fingerprint=_text(fingerprints, "rhythm"),
            grants_fingerprint=_text(fingerprints, "grants"),
        )
    else:
        raise AdmissionRejected("bad_request", "kind 必须是 extension 或 admission")
    return request


def _operation_id(body: Mapping) -> str:
    value = body.get("operation_id")
    if not isinstance(value, str) or not _OPERATION_ID.match(value):
        raise AdmissionRejected("bad_request", "operation_id 必须是 1–64 位字母数字与 ._:-")
    return value


def request_digest(request) -> str:
    return content_fingerprint(request.canonical())


# ── 窗口 ────────────────────────────────────────────────────────────────
def sleep_window(rhythm, grants, graph, clock: datetime) -> Optional[Tuple[datetime, datetime, object]]:
    """`clock` 所在那一段若是睡眠段：返回 (段开始, 出发时刻, 段)；否则 None。

    出发时刻 = 下一段开始减去从这里走过去的路程（与作息导演同一套寻路与授予），
    下一段在同一地点就是下一段开始。跨午夜、跨平日 / 休息日两张表都走
    DailyRhythm.occurrence_at / next_after，与导演、恢复同一套日期逻辑。
    """
    segment, start = rhythm.occurrence_at(clock)
    if not _sleeping(segment):
        return None
    following, next_start = rhythm.next_after(start)
    # 同一处接着睡的下一段（例如表在零点切开的那一段）不是出发：窗口一直延到真正
    # 要换状态的那一段。
    for _ in range(_WINDOW_SEARCH_SEGMENTS):
        if not (_sleeping(following) and following.location_id == segment.location_id):
            break
        following, next_start = rhythm.next_after(next_start)
    departure = next_start
    if following.location_id is not None and following.location_id != segment.location_id:
        route = plan_route(
            graph,
            lambda location_id: graph.has(location_id)
            and may_enter(grants, graph.get(location_id)),
            segment.location_id,
            following.location_id,
        )
        if route is not None:
            departure = next_start - timedelta(minutes=route.total_minutes)
    return start, departure, segment


def _sleeping(segment) -> bool:
    return (
        segment.activity is ActivityKind.RESTING
        and segment.location_id is not None
        and segment.channel_id is None
    )


def next_sleep_window(rhythm, grants, graph, clock: datetime) -> Optional[Tuple[datetime, datetime]]:
    """此刻或之后第一个还没结束的睡眠窗口（只按作息算；房里有没有人醒着要到时再判）。"""
    _, start = rhythm.occurrence_at(clock)
    for _ in range(_WINDOW_SEARCH_SEGMENTS):
        window = sleep_window(rhythm, grants, graph, start)
        if window is not None and window[1] > clock and window[0] < window[1]:
            return window[0], window[1]
        _, start = rhythm.next_after(start)
    return None


# ── 前身（D7） ──────────────────────────────────────────────────────────
def predecessor_records(state: SessionState, character_id: str) -> List[str]:
    """世界里任何持久域对这个 id 的实体引用（键或值精确等于它）。空 = 首次解析。

    整份会话状态走一遍：世界历史、名单、观察、曝光、记忆、Agency、排期 / 投递箱、
    授予、origin、账本。自然语言字段与入住请求声明的室友名单不算（见 _NOT_ENTITY_KEYS）。
    账本里的内容项名字是 rhythm:<id>，单独查。
    """
    found: List[str] = []

    def walk(value, path: str) -> None:
        if len(found) >= 20:
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                here = f"{path}.{key}"
                if key == character_id:
                    found.append(here)
                if key in _NOT_ENTITY_KEYS:
                    continue
                walk(item, here)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")
        elif value == character_id:
            found.append(path)

    walk(state.to_dict(), "state")
    if state.content is not None and state.content.adopted_fingerprint(
        rhythm_subject(character_id)
    ) is not None:
        found.append(f"content.adopted.{rhythm_subject(character_id)}")
    return found


def find_operation(state: SessionState, operation_id: str) -> Optional[Tuple[Event, int]]:
    """这个 operation_id 对应的那一条已提交 WORLD-2 事件。多于一条就是存档坏了，不挑一条凑合。"""
    found = [
        (event, sequence)
        for sequence, event in enumerate(state.events.events())
        if event.type in AUTHORITY_EVENT_TYPES and event.payload["operation_id"] == operation_id
    ]
    if len(found) > 1:
        raise AdmissionRejected(
            "operation_id_ambiguous",
            f"世界历史里有 {len(found)} 笔 WORLD-2 操作用了 operation_id '{operation_id}'",
        )
    return found[0] if found else None


# ── 服务 ────────────────────────────────────────────────────────────────
class WorldAdmission:
    """一个已打开世界的 WORLD-2 维护入口。每次调用都对着那一刻的世界。"""

    def __init__(self, world, *, cadence, operator: str) -> None:
        if world.content is None:
            raise AdmissionRejected(
                "no_content", "这个世界打开时没有交内容快照，不能做 WORLD-2 操作"
            )
        self._world = world
        self._cadence = cadence
        self._operator = operator

    @property
    def _state(self) -> SessionState:
        return self._world.state

    @property
    def _content(self):
        return self._world.content

    # ── 预检 ────────────────────────────────────────────────────────────
    def preflight(self, body) -> Dict:
        if not isinstance(body, Mapping):
            raise AdmissionRejected("bad_request", "请求体必须是对象")
        kind = body.get("kind")
        if kind == EXTENSION:
            _exact_keys(body, {"kind"}, "扩展预检")
            return self._preflight_extension()
        if kind == ADMISSION:
            _exact_keys(body, {"kind", "character_id", "roommates", "ordinal"}, "入住预检")
            return self._preflight_admission(body)
        raise AdmissionRejected("bad_request", "kind 必须是 extension 或 admission")

    def _preflight_extension(self) -> Dict:
        world = self._state.world_state
        report = {"kind": EXTENSION, "feasible": False, "reasons": [], "proposal": None}
        try:
            locations, appended, after = self._extension_definition(world.locations)
        except AdmissionRejected as e:
            report["reasons"].append({"code": e.code, "message": str(e)})
            return report
        report["feasible"] = True
        report["proposal"] = {
            "locations": [entry["location_id"] for entry in locations],
            "append_connections": appended,
            "first_operation": find_first_authority(self._state) is None,
        }
        report["fingerprints"] = {
            "graph_before": graph_fingerprint(world.locations),
            "graph_after": graph_fingerprint(after),
        }
        return report

    def _preflight_admission(self, body: Mapping) -> Dict:
        character_id = _text(body, "character_id")
        state = self._state
        world = state.world_state
        report = {"kind": ADMISSION, "feasible": False, "reasons": [], "proposal": None}
        try:
            rhythm, grants = self._newcomer_content(character_id)
        except AdmissionRejected as e:
            report["reasons"].append({"code": e.code, "message": str(e)})
            return report
        window = sleep_window(rhythm, grants, world.locations, world.clock)
        upcoming = next_sleep_window(rhythm, grants, world.locations, world.clock)
        report["next_window"] = (
            {"start": upcoming[0].isoformat(), "end": upcoming[1].isoformat()} if upcoming else None
        )
        report["fingerprints"] = {
            "rhythm": rhythm_fingerprint(rhythm),
            "grants": grants_fingerprint(grants),
        }
        roommates = body.get("roommates")
        ordinal = body.get("ordinal")
        location_id = window[2].location_id if window is not None else None
        report["proposal"] = {
            "character_id": character_id,
            "location_id": location_id,
            "roommates": list(roommates) if isinstance(roommates, (list, tuple)) else roommates,
            "ordinal": ordinal,
            "window": (
                {"start": window[0].isoformat(), "end": window[1].isoformat()}
                if window is not None
                else None
            ),
        }
        try:
            request = parse_operation(
                {
                    "kind": ADMISSION,
                    "operation_id": "preflight",
                    "character_id": character_id,
                    "location_id": location_id or "-",
                    "roommates": roommates,
                    "ordinal": ordinal,
                    "window": report["proposal"]["window"] or {"start": "-", "end": "-"},
                    "fingerprints": report["fingerprints"],
                }
            )
            self._check_admission(state, request, rhythm, grants)
        except AdmissionRejected as e:
            report["reasons"].append({"code": e.code, "message": str(e)})
            return report
        report["feasible"] = True
        return report

    # ── 执行 ────────────────────────────────────────────────────────────
    def execute(self, body) -> Dict:
        request = parse_operation(body)
        runtime = self._world.runtime
        digest = request_digest(request)
        content_rhythms = dict(self._content.rhythms())
        try:
            runtime._commit_world2(
                lambda state: self._build(state, request, digest),
                content_rhythms=content_rhythms,
                registry_revision=self._world.content_revision,
            )
        except _AlreadyCommitted as found:
            return self._retry(request, digest, found.event, found.sequence)
        except AdmissionRejected:
            raise
        except (EventCommitError, ValueError, RuntimeError) as e:
            # 闸门、门、名单核对、提交边界拒绝：事务已回滚，什么都没改。
            raise AdmissionRejected("refused", str(e)) from e
        found = find_operation(self._state, request.operation_id)
        event, sequence = found
        self._save(request.operation_id)
        return self._report(event, sequence, retry=False)

    def _retry(self, request, digest: str, event: Event, sequence: int) -> Dict:
        if event.payload["envelope"].get("request_digest") != digest:
            raise AdmissionRejected(
                "operation_id_conflict",
                f"operation_id '{request.operation_id}' 已经用于另一个请求",
            )
        if self._tier(sequence) != DURABLE:
            # 补存：同 id 同请求的重试可以写盘，直到磁盘证据说耐久为止。
            self._save(request.operation_id)
        return self._report(event, sequence, retry=True)

    def _save(self, operation_id: str) -> None:
        try:
            self._world.checkpoint(f"world2:{operation_id}")
        except Exception:
            # 结果按磁盘证据报告（_tier）：没写上就是 committed，写上了但证实不了耐久
            # 就是 visible_not_durable。错误本身留在世界的 status 里。
            pass

    # ── 查询 ────────────────────────────────────────────────────────────
    def query(self, operation_id: str) -> Dict:
        if not isinstance(operation_id, str) or not _OPERATION_ID.match(operation_id):
            raise AdmissionRejected("bad_request", "operation_id 不合法")
        found = find_operation(self._state, operation_id)
        if found is None:
            return {"operation_id": operation_id, "outcome": None, "found": False}
        return self._report(*found, retry=False)

    def _tier(self, sequence: int) -> str:
        count, durable, synced = self._world.saved_durability()
        if count is None or sequence >= count:
            return COMMITTED
        if durable and synced:
            return DURABLE
        return VISIBLE_NOT_DURABLE

    def _report(self, event: Event, sequence: int, *, retry: bool) -> Dict:
        payload = event.payload
        return {
            "operation_id": payload["operation_id"],
            "kind": EXTENSION if event.type is EventType.WORLD_LOCATIONS_EXTENDED else ADMISSION,
            "found": True,
            "outcome": self._tier(sequence),
            "event_id": event.event_id,
            "sequence": sequence,
            "revision": self._world.revision,
            "character_id": payload.get("character_id"),
            "retry": retry,
        }

    # ── 闸门之内 ────────────────────────────────────────────────────────
    def _build(self, state: SessionState, request, digest: str) -> Event:
        """在协调器闸门之内、事务之前调用：先查已提交，再对着此刻的世界判与构造。"""
        found = find_operation(state, request.operation_id)
        if found is not None:
            raise _AlreadyCommitted(*found)
        envelope = {
            "world_id": self._world.world_id,
            "operator": self._operator,
            "content_revision": self._world.content_revision,
            "request_digest": digest,
        }
        world = state.world_state
        if request.kind == EXTENSION:
            locations, appended, after = self._extension_definition(world.locations)
            fingerprints = (graph_fingerprint(world.locations), graph_fingerprint(after))
            if fingerprints != (request.graph_before, request.graph_after):
                raise AdmissionRejected(
                    "stale_preflight", "地点图或冷图与预检时不同，重新预检"
                )
            baseline = None
            if find_first_authority(state) is None:
                rhythms = self._content.rhythms()
                baseline = build_baseline(state, {cid: rhythms[cid] for cid in state.characters})
            payload = {
                "operation_id": request.operation_id,
                "envelope": envelope,
                "locations": locations,
                "append_connections": appended,
                "graph_before": fingerprints[0],
                "graph_after": fingerprints[1],
                "baseline": baseline,
            }
            kind = EventType.WORLD_LOCATIONS_EXTENDED
        else:
            rhythm, grants = self._newcomer_content(request.character_id)
            segment = self._check_admission(state, request, rhythm, grants)
            payload = {
                "operation_id": request.operation_id,
                "envelope": envelope,
                "character_id": request.character_id,
                "location_id": segment.location_id,
                "activity": segment.activity.value,
                "channel_id": segment.channel_id,
                "roommates": sorted(request.roommates),
                "rhythm": encode_rhythm(rhythm),
                "grants": encode_grants(grants),
                "fingerprints": {
                    "rhythm": rhythm_fingerprint(rhythm),
                    "grants": grants_fingerprint(grants),
                },
                "seed": admission_seed(
                    request.character_id,
                    ordinal=request.ordinal,
                    admitted_at=world.clock,
                    first_delay_minutes=self._cadence.first_delay_minutes,
                    stagger_minutes=self._cadence.stagger_minutes,
                    interval_minutes=self._cadence.interval_minutes,
                    cue=self._cadence.cue,
                ),
            }
            kind = EventType.WORLD_RESIDENT_ADMITTED
        return Event(
            event_id=f"world2:{request.operation_id}",
            type=kind,
            occurred_at=world.clock,
            scope=EventScope.PUBLIC,
            payload=payload,
        )

    def _newcomer_content(self, character_id: str):
        content = self._content
        if not content.has_character(character_id):
            raise AdmissionRejected("no_content", f"内容快照里没有 '{character_id}'")
        rhythm = content.rhythm(character_id)
        grants = content.grants(character_id)
        if rhythm is None or grants is None:
            raise AdmissionRejected("no_content", f"'{character_id}' 没有作息或授予")
        return rhythm, grants

    def _check_admission(self, state: SessionState, request, rhythm, grants):
        """入住在此刻成立吗（设计 §3.2、§4.8）。成立就交出她此刻所在的那一段。"""
        world = state.world_state
        cid = request.character_id
        if request.rhythm_fingerprint != rhythm_fingerprint(rhythm) or (
            request.grants_fingerprint != grants_fingerprint(grants)
        ):
            raise AdmissionRejected("stale_preflight", "内容快照与预检时不同，重新预检")
        if cid in request.roommates:
            raise AdmissionRejected("bad_request", "室友名单不能包含她自己")
        records = predecessor_records(state, cid)
        if records:
            raise AdmissionRejected(
                "predecessor",
                f"世界里已有 '{cid}' 的记录（{records[0]} 等 {len(records)} 处），promotion 未支持",
            )
        window = sleep_window(rhythm, grants, world.locations, world.clock)
        if window is None:
            raise AdmissionRejected("not_asleep", f"'{cid}' 此刻不在作息的睡眠段")
        start, departure, segment = window
        if segment.location_id != request.location_id:
            raise AdmissionRejected(
                "not_lodging", "请求的地点不是她此刻作息指定的住宿地点"
            )
        if world.clock >= departure:
            raise AdmissionRejected("past_departure", "已经到了下一段的出发时刻")
        if (start.isoformat(), departure.isoformat()) != (request.window_start, request.window_end):
            raise AdmissionRejected("stale_preflight", "入住窗口与预检时不同，重新预检")
        rhythms = self._content.rhythms()
        for roommate in request.roommates:
            theirs = rhythms.get(roommate)
            if theirs is None or theirs.segment_at(world.clock).location_id != request.location_id:
                raise AdmissionRejected(
                    "bad_roommate", f"声明的室友 '{roommate}' 此刻按作息不住在这里"
                )
        for occupant in world.characters_at(request.location_id):
            if occupant not in request.roommates:
                raise AdmissionRejected("occupied", f"房里有非声明室友 '{occupant}'")
            if world.availability_of(occupant) is not Availability.ASLEEP:
                raise AdmissionRejected("awake", f"房里的室友 '{occupant}' 醒着")
        for resident in state.characters:
            if world.location_of(resident) is None:
                raise AdmissionRejected("unknown_occupancy", f"居民 '{resident}' 的位置未知")
        return segment

    def _extension_definition(self, graph):
        """冷图里比世界图多出来的节点，以及旧节点末尾追加的、连向它们的连接。"""
        cold = self._content.new_location_graph()
        new_ids = [lid for lid in cold.ids() if not graph.has(lid)]
        if not new_ids:
            raise AdmissionRejected("nothing_to_extend", "冷图里没有这个世界还没有的地点")
        appended: Dict[str, List[Dict]] = {}
        for location in graph:
            if not cold.has(location.location_id):
                raise AdmissionRejected("cold_graph_conflict", "冷图缺少世界里已有的地点")
            mine = [c.to_dict() for c in location.connections]
            theirs = [c.to_dict() for c in cold.get(location.location_id).connections]
            if theirs[: len(mine)] != mine or {
                **location.to_dict(),
                "connections": [],
            } != {**cold.get(location.location_id).to_dict(), "connections": []}:
                raise AdmissionRejected(
                    "cold_graph_conflict", f"冷图改动了已有地点 '{location.location_id}'"
                )
            if len(theirs) > len(mine):
                appended[location.location_id] = theirs[len(mine):]
        locations = [cold.get(lid).to_dict() for lid in new_ids]
        try:
            after = extended_graph(graph, locations, appended)
        except ExtensionError as e:
            raise AdmissionRejected("bad_extension", str(e)) from None
        return locations, appended, after


def find_first_authority(state: SessionState) -> Optional[Event]:
    for event in state.events.events():
        if event.type in AUTHORITY_EVENT_TYPES:
            return event
    return None


__all__ = [
    "ADMISSION",
    "COMMITTED",
    "DURABLE",
    "EXTENSION",
    "REJECTED",
    "VISIBLE_NOT_DURABLE",
    "AdmissionRejected",
    "WorldAdmission",
    "find_operation",
    "next_sleep_window",
    "parse_operation",
    "predecessor_records",
    "request_digest",
    "sleep_window",
]
