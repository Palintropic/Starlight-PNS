# pns/runtime/persistence/archive.py — 世界存档的信封
#
# 信封回答的问题只有一个：**磁盘上这一坨字节，是不是这个世界某一刻的完整、
# 自洽的权威状态**。
#
# 它不回答（写在这里免得以后被顺手加进来）：怎么写盘（store）、谁拥有这个世界
# （ownership）、什么时候存（lifecycle）。
#
# 四条硬约束：
#
#   1. **身份、版本、修订号、时钟跟状态存在一起。** 一份只有 SessionState 的
#      存档回答不了"这是哪个世界的第几版、停在哪一刻" —— 而这三个问题里任何
#      一个答不上来，恢复就只能靠猜。
#   2. **恢复走既有构造函数。** SessionState.from_dict() 那一整套跨段校验
#      （事件不晚于时钟、排期严格晚于时钟、Agency 记录对得上投递箱和事件、
#      记忆对得上观察）是这份存档能不能用的判据。这里绝不 __dict__ 注水、
#      绝不绕过校验、也绝不"缺段就当空的"。
#   3. **信封和它包着的状态必须互相印证。** 信封说的 session_id 和时钟，必须
#      跟状态里的那一份一模一样。对不上就是两份不同时刻的东西被拼在了一起，
#      这种存档单独看每一段都合法，合起来却是一个不存在过的世界。
#   4. **存档里只有数据。** 调度器、Agency 引擎、记忆编码器、协调器、模型
#      客户端、API Key、锁、回调 —— 一个都不进去。捕获那一刻就检查，而不是
#      等到 json.dumps 抛类型错误：metadata 是个自由字典，谁都能往里塞活对象。
#
# 版本 3 起事件历史分两处放（WORLD-1 存档增长设计 §2）：
#
#   * **分卷**：已经封存的一段段历史，每卷一个 JSONL 文件，封存后不再改动。
#     信封里的清单（segments）是分卷的唯一权威：文件名、序号区间、条数、
#     字节数、sha256。
#   * **活动段**：最后一卷之后的事件，仍在 state.events 里，序号接着清单往下数。
#
# 信封只校验清单与活动段互相接得上；分卷字节的读取归 store，逐卷核对归这里的
# attach_history()。恢复时把分卷与活动段接成完整历史，再走同一个
# SessionState.from_dict()。缺一卷、改一字节、序号断开，都是加载失败。
import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from math import isfinite
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pns.models.session import SessionState
from pns.models.time_events import TimeEventPolicy, legacy_epoch
from pns.runtime.persistence.naming import validate_world_id

# 存档格式版本。改变形状就 +1，并且在这里写清楚旧版怎么升级 —— 不认识的版本
# 一律响亮拒绝，绝不"尽量读读看"。
#
# 版本 3：事件历史分卷（见文件头）。版本 2 就是"没有分卷的存档"，原样可读；
# 它被读回来之后的下一次保存写成版本 3。
WORLD_ARCHIVE_VERSION = 3
READABLE_ARCHIVE_VERSIONS = frozenset({2, 3})
# 版本 1 是 WORLD-1 之前的存档（旧 nightcord / deploy-smoke 世界）。2026-09-26 这些
# 世界已退役：存档保留、不改写、不迁移，本进程明确拒绝加载它们。
RETIRED_ARCHIVE_VERSIONS = frozenset({1})

_ENVELOPE_FIELDS = ("version", "world_id", "session_id", "revision", "clock", "state")

# 封存阈值：活动段里最早的一整段跨满一个模拟日、或攒满这么多条，先到者为准。
SEGMENT_SPAN = timedelta(days=1)
SEGMENT_MAX_EVENTS = 20_000

_SEGMENT_NAME = re.compile(r"events-(\d{6})\.jsonl")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ArchiveError(ValueError):
    """这份存档不能用（形状不对、版本不认识、身份对不上、各段自相矛盾）。"""


class ArchiveCorrupt(ArchiveError):
    """磁盘上的字节根本不是一份存档（截断、乱码、不是 JSON）。"""


def _plain(value: Any, path: str = "state", depth: int = 0) -> Any:
    """校验并复制出一份纯数据。碰到活对象就响亮失败。

    顺带做归一化（元组 → 列表），这样"捕获出来的存档"和"从 JSON 读回来的
    存档"是同一个形状 —— 否则两者比较永远不相等，而这正是存档测试要比的东西。
    """
    if depth > 64:
        raise ArchiveError(f"{path}: 存档嵌套过深，大概率是循环引用")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ArchiveError(f"{path}: {value!r} 不是合法 JSON 数字")
        return value
    if isinstance(value, Mapping):
        plain = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ArchiveError(f"{path}: 字典的键必须是字符串，收到 {key!r}")
            plain[key] = _plain(item, f"{path}.{key}", depth + 1)
        return plain
    if isinstance(value, (list, tuple)):
        return [
            _plain(item, f"{path}[{index}]", depth + 1)
            for index, item in enumerate(value)
        ]
    raise ArchiveError(
        f"{path}: 存档里不能有 {type(value).__name__} —— 存档只装数据，"
        "服务实例、模型客户端、锁和回调都不进去"
    )


def _parse_clock(value, label: str) -> datetime:
    if isinstance(value, datetime):
        clock = value
    elif isinstance(value, str):
        try:
            clock = datetime.fromisoformat(value)
        except ValueError:
            raise ArchiveError(f"无法解析的{label}: {value!r}") from None
    else:
        raise ArchiveError(f"{label}必须是 ISO 时间字符串")
    if clock.tzinfo is not None:
        raise ArchiveError(f"{label}必须是不带时区的模拟时间")
    return clock


def _require_revision(revision) -> int:
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise ArchiveError("revision 必须是整数")
    if revision < 1:
        raise ArchiveError(f"revision 必须从 1 开始递增，收到 {revision}")
    return revision


def _cross_check(world_id: str, session_id: str, clock: datetime, state: Mapping) -> None:
    """信封说的和状态里写的必须是同一件事。"""
    if not isinstance(state, Mapping):
        raise ArchiveError("state 必须是字典")
    if state.get("session_id") != session_id:
        raise ArchiveError(
            f"信封说这是会话 '{session_id}' 的存档，里面的状态却属于 "
            f"'{state.get('session_id')}'"
        )
    world_payload = state.get("world_state") or {}
    if not isinstance(world_payload, Mapping) or "clock" not in world_payload:
        raise ArchiveError(
            f"世界 '{world_id}' 的存档缺少权威 WorldState —— 没有世界状态的"
            "会话不是一个可以被恢复的世界"
        )
    state_clock = _parse_clock(world_payload["clock"], "状态里的世界时钟")
    if state_clock != clock:
        raise ArchiveError(
            f"信封的时钟 {clock.isoformat()} 跟状态里的世界时钟 "
            f"{state_clock.isoformat()} 对不上"
        )


def segment_file_name(index: int) -> str:
    """第 index 卷（从 1 数）的文件名。清单里的名字必须恰好是这个。"""
    return f"events-{index:06d}.jsonl"


def _require_count(value, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ArchiveError(f"{label} 必须是 ≥ {minimum} 的整数，收到 {value!r}")
    return value


@dataclass(frozen=True)
class EventSegment:
    """清单里的一卷：一段已经封存、不再改动的事件历史。"""

    file: str
    first_sequence: int
    last_sequence: int
    count: int
    bytes: int
    sha256: str
    first_at: str
    last_at: str

    def to_dict(self) -> Dict:
        return {
            "file": self.file,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "count": self.count,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "first_at": self.first_at,
            "last_at": self.last_at,
        }

    @classmethod
    def from_dict(cls, payload, position: int) -> "EventSegment":
        """position 是它在清单里的下标（从 0 数），文件名必须与之对应。"""
        label = f"分卷清单第 {position + 1} 项"
        if not isinstance(payload, Mapping):
            raise ArchiveError(f"{label}必须是字典")
        missing = [
            key
            for key in (
                "file",
                "first_sequence",
                "last_sequence",
                "count",
                "bytes",
                "sha256",
                "first_at",
                "last_at",
            )
            if key not in payload
        ]
        if missing:
            raise ArchiveError(f"{label}缺少字段: {', '.join(missing)}")
        name = payload["file"]
        if name != segment_file_name(position + 1):
            raise ArchiveError(
                f"{label}的文件名应当是 {segment_file_name(position + 1)}，"
                f"收到 {name!r} —— 清单外的名字一律不认"
            )
        first = _require_count(payload["first_sequence"], f"{label}的 first_sequence")
        last = _require_count(payload["last_sequence"], f"{label}的 last_sequence")
        count = _require_count(payload["count"], f"{label}的 count", minimum=1)
        size = _require_count(payload["bytes"], f"{label}的 bytes", minimum=1)
        if last - first + 1 != count:
            raise ArchiveError(
                f"{label}（{name}）的序号区间 {first}..{last} 跟条数 {count} 对不上"
            )
        digest = payload["sha256"]
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ArchiveError(f"{label}（{name}）的 sha256 不是 64 位小写十六进制")
        first_at = _parse_clock(payload["first_at"], f"{label}的 first_at")
        last_at = _parse_clock(payload["last_at"], f"{label}的 last_at")
        if last_at < first_at:
            raise ArchiveError(f"{label}（{name}）的时间区间倒流")
        return cls(
            file=name,
            first_sequence=first,
            last_sequence=last,
            count=count,
            bytes=size,
            sha256=digest,
            first_at=first_at.isoformat(),
            last_at=last_at.isoformat(),
        )


def encode_segment(entries: Sequence[Mapping]) -> bytes:
    """一卷的磁盘字节：每行一条事件（带全历史序号），键排序，UTF-8。"""
    return b"".join(
        json.dumps(
            entry, ensure_ascii=False, sort_keys=True, allow_nan=False
        ).encode("utf-8")
        + b"\n"
        for entry in entries
    )


def describe_segment(index: int, entries: Sequence[Mapping], blob: bytes) -> EventSegment:
    """给刚写好的一卷生成清单项。entries 必须是 blob 的来源。"""
    return EventSegment(
        file=segment_file_name(index),
        first_sequence=entries[0]["sequence"],
        last_sequence=entries[-1]["sequence"],
        count=len(entries),
        bytes=len(blob),
        sha256=hashlib.sha256(blob).hexdigest(),
        first_at=entries[0]["occurred_at"],
        last_at=entries[-1]["occurred_at"],
    )


def decode_segment(segment: EventSegment, blob: bytes) -> List[Dict]:
    """核对一卷的字节与清单，返回它的事件。任何一处对不上都是 ArchiveCorrupt。

    核对顺序是从便宜到贵：字节数、哈希、再逐行解析。哈希对得上之后的解析失败
    只可能是写入方自己写坏了 —— 同样不许跳过。
    """
    name = segment.file
    if len(blob) != segment.bytes:
        raise ArchiveCorrupt(
            f"分卷 {name} 有 {len(blob)} 字节，清单记的是 {segment.bytes}（截断或被改过）"
        )
    if hashlib.sha256(blob).hexdigest() != segment.sha256:
        raise ArchiveCorrupt(f"分卷 {name} 的 sha256 跟清单对不上（内容被改过）")
    try:
        lines = blob.decode("utf-8").split("\n")
    except UnicodeDecodeError as e:
        raise ArchiveCorrupt(f"分卷 {name} 不是 UTF-8: {e}") from e
    if lines[-1] != "":
        raise ArchiveCorrupt(f"分卷 {name} 最后一行没有换行（截断）")
    entries: List[Dict] = []
    for offset, line in enumerate(lines[:-1]):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as e:
            raise ArchiveCorrupt(f"分卷 {name} 第 {offset + 1} 行不是 JSON: {e}") from e
        if not isinstance(entry, dict):
            raise ArchiveCorrupt(f"分卷 {name} 第 {offset + 1} 行不是一条事件")
        expected = segment.first_sequence + offset
        sequence = entry.get("sequence")
        if isinstance(sequence, bool) or sequence != expected:
            raise ArchiveCorrupt(
                f"分卷 {name} 第 {offset + 1} 行的序号应当是 {expected}，收到 {sequence!r}"
            )
        entries.append(_plain(entry, f"{name}[{offset}]"))
    if len(entries) != segment.count:
        raise ArchiveCorrupt(
            f"分卷 {name} 有 {len(entries)} 条事件，清单记的是 {segment.count}"
        )
    if (
        entries[0].get("occurred_at") != segment.first_at
        or entries[-1].get("occurred_at") != segment.last_at
    ):
        raise ArchiveCorrupt(f"分卷 {name} 的首尾时刻跟清单对不上")
    return entries


def _parse_segments(raw) -> Tuple[EventSegment, ...]:
    if not isinstance(raw, list):
        raise ArchiveError("segments 必须是数组")
    segments = tuple(EventSegment.from_dict(item, i) for i, item in enumerate(raw))
    _check_manifest(segments)
    return segments


def _check_manifest(segments: Sequence[EventSegment]) -> None:
    """清单上各卷首尾相接：第一卷从 0 开始，每卷接着上一卷，时间不倒流。"""
    expected = 0
    previous_at: Optional[str] = None
    for index, segment in enumerate(segments):
        if segment.file != segment_file_name(index + 1):
            raise ArchiveError(f"分卷清单第 {index + 1} 项的文件名不对: {segment.file}")
        if segment.first_sequence != expected:
            raise ArchiveError(
                f"分卷 {segment.file} 应当从序号 {expected} 开始，清单记的是 "
                f"{segment.first_sequence}（序号断开）"
            )
        if previous_at is not None and datetime.fromisoformat(
            segment.first_at
        ) < datetime.fromisoformat(previous_at):
            raise ArchiveError(f"分卷 {segment.file} 的时间早于上一卷的末尾")
        expected = segment.last_sequence + 1
        previous_at = segment.last_at


def _sealed_total(segments: Sequence[EventSegment]) -> int:
    return segments[-1].last_sequence + 1 if segments else 0


def _active_entries(state: Mapping) -> List:
    events = state.get("events")
    if not isinstance(events, Mapping):
        raise ArchiveError("state.events 必须是字典")
    entries = events.get("events", [])
    if not isinstance(entries, list):
        raise ArchiveError("state.events.events 必须是数组")
    return entries


def _check_active(segments: Sequence[EventSegment], state: Mapping) -> None:
    """活动段必须紧接着最后一卷：第一条的序号 = 已封存条数，时间不早于最后一卷的末尾。"""
    entries = _active_entries(state)
    if not entries:
        return
    first = entries[0]
    if not isinstance(first, Mapping):
        raise ArchiveError("活动段第一条事件必须是字典")
    total = _sealed_total(segments)
    if first.get("sequence") != total or isinstance(first.get("sequence"), bool):
        raise ArchiveError(
            f"活动段应当从序号 {total} 开始（接在已封存的 {len(segments)} 卷之后），"
            f"收到 {first.get('sequence')!r}"
        )
    if segments:
        first_at = _parse_clock(first.get("occurred_at"), "活动段第一条事件的时刻")
        if first_at < datetime.fromisoformat(segments[-1].last_at):
            raise ArchiveError("活动段第一条事件早于最后一卷的末尾（时间倒流）")


def _legacy_time_events(payload: Dict, clock: datetime) -> None:
    """版本 2 没记时间事件链的起点：按旧规矩补一个（见 time_events.legacy_epoch）。"""
    steps = []
    for entry in payload.get("events", {}).get("events", []):
        if isinstance(entry, Mapping) and entry.get("type") == "world.time_advanced":
            try:
                steps.append((datetime.fromisoformat(entry["occurred_at"]), 0))
            except (KeyError, TypeError, ValueError):
                raise ArchiveError("版本 2 存档里有读不懂时刻的时间事件") from None
    payload["time_events"] = TimeEventPolicy(legacy_epoch(steps, clock)).to_dict()


@dataclass(frozen=True)
class WorldArchive:
    """一个世界某一刻的完整存档，连同它的身份与版本。"""

    world_id: str
    session_id: str
    revision: int
    clock: datetime
    saved_at: str
    # state.events 里只有活动段（版本 3）；版本 2 与没封存过的世界里就是全部。
    state: Dict = field(default_factory=dict)
    version: int = WORLD_ARCHIVE_VERSION
    # 分卷清单：已经封存、不再改动的历史。
    segments: Tuple[EventSegment, ...] = ()
    # 分卷里的事件，由 attach_history() 在核对之后挂上。它不进 world.json，
    # 也不参与比较（清单上的 sha256 已经代表了它）。None 表示还没加载。
    sealed_events: Optional[Tuple[Dict, ...]] = field(
        default=None, compare=False, repr=False
    )

    # ── 捕获 ────────────────────────────────────────────────────────────
    @classmethod
    def capture(
        cls,
        world_id: str,
        state: SessionState,
        *,
        revision: int,
        saved_at: Optional[str] = None,
    ) -> "WorldArchive":
        """从一份活的权威状态捕获存档。

        调用方必须自己保证捕获的那一刻状态是自洽的（生命周期层是在 P11 的
        提交边界里取的快照）—— 这里只负责把它变成一份完整、纯数据的存档。
        """
        if not isinstance(state, SessionState):
            raise ArchiveError("只能从 SessionState 捕获世界存档")
        if state.world_state is None:
            raise ArchiveError("没有权威 WorldState 的会话不是一个世界")
        return cls.from_state_payload(
            world_id, state.to_dict(), revision=revision, saved_at=saved_at
        )

    @classmethod
    def from_state_payload(
        cls,
        world_id: str,
        payload: Mapping,
        *,
        revision: int,
        saved_at: Optional[str] = None,
        segments: Sequence[EventSegment] = (),
    ) -> "WorldArchive":
        """从**已经取好的**状态快照建信封。

        生命周期层用这条路：快照必须在提交边界之内取，序列化和写盘可以在边界
        之外做。分开这两步，一次写盘就不会把停机和提交一起堵住。

        `segments` 是磁盘上已经封存的分卷清单；这时 payload 里的事件只能是
        清单之后的活动段（SessionState.to_dict(events_from=...)）。
        """
        segments = tuple(segments)
        for item in segments:
            if not isinstance(item, EventSegment):
                raise ArchiveError("segments 里只能是 EventSegment")
        _check_manifest(segments)
        world_id = validate_world_id(world_id)
        revision = _require_revision(revision)
        state = _plain(payload)
        session_id = state.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ArchiveError("状态里缺少 session_id")
        world_payload = state.get("world_state") or {}
        if not isinstance(world_payload, Mapping) or "clock" not in world_payload:
            raise ArchiveError("没有权威 WorldState 的会话不是一个世界")
        clock = _parse_clock(world_payload["clock"], "世界时钟")
        _cross_check(world_id, session_id, clock, state)
        _check_active(segments, state)
        return cls(
            world_id=world_id,
            session_id=session_id,
            revision=revision,
            clock=clock,
            saved_at=saved_at if saved_at is not None else datetime.now().isoformat(),
            state=state,
            version=WORLD_ARCHIVE_VERSION,
            segments=segments,
            # 活的状态里本来就有全部历史；有分卷时这份快照只带活动段，
            # 分卷在磁盘上，不在这份信封里。
            sealed_events=None if segments else (),
        )

    # ── 序列化 ──────────────────────────────────────────────────────────
    def to_dict(self) -> Dict:
        """完整公开形状；返回值是全新的可变结构，改它影响不到这份存档。"""
        return {
            "version": self.version,
            "world_id": self.world_id,
            "session_id": self.session_id,
            "revision": self.revision,
            "clock": self.clock.isoformat(),
            "saved_at": self.saved_at,
            "state": deepcopy(self.state),
            "segments": [segment.to_dict() for segment in self.segments],
        }

    @classmethod
    def from_dict(cls, payload) -> "WorldArchive":
        """从磁盘形状恢复信封。**只校验信封**，状态的校验在 restore_state()。"""
        if not isinstance(payload, Mapping):
            raise ArchiveError("世界存档必须是字典")
        for required in _ENVELOPE_FIELDS:
            if required not in payload:
                raise ArchiveError(f"世界存档缺少必填字段: {required}")

        version = payload["version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise ArchiveError("version 必须是整数")
        if version in RETIRED_ARCHIVE_VERSIONS:
            raise ArchiveError(
                f"世界存档格式版本 {version} 属于已退役的世界（WORLD-1 之前）。"
                "存档保留在磁盘上未被改动；本进程不再加载它"
            )
        if version not in READABLE_ARCHIVE_VERSIONS:
            raise ArchiveError(
                f"不支持的世界存档格式版本 {version}（本进程只认 "
                f"{sorted(READABLE_ARCHIVE_VERSIONS)}）。升级存档是一次明确的人为决定，"
                "不是恢复路径可以替人做的猜测。"
            )
        if version == 2:
            # 版本 2 没有分卷：带着清单的"版本 2"是两种格式拼出来的东西。
            if "segments" in payload:
                raise ArchiveError("版本 2 的存档不该有分卷清单")
            segments: Tuple[EventSegment, ...] = ()
        else:
            if "segments" not in payload:
                raise ArchiveError("世界存档缺少必填字段: segments")
            segments = _parse_segments(payload["segments"])

        world_id = validate_world_id(payload["world_id"])
        session_id = payload["session_id"]
        if not isinstance(session_id, str) or not session_id:
            raise ArchiveError("session_id 必须是非空字符串")
        revision = _require_revision(payload["revision"])
        clock = _parse_clock(payload["clock"], "存档时钟")
        saved_at = payload.get("saved_at")
        if saved_at is not None and not isinstance(saved_at, str):
            raise ArchiveError("saved_at 必须是 ISO 时间字符串")
        state = _plain(payload["state"])
        _cross_check(world_id, session_id, clock, state)
        _check_active(segments, state)
        return cls(
            world_id=world_id,
            session_id=session_id,
            revision=revision,
            clock=clock,
            saved_at=saved_at if saved_at is not None else "",
            state=state,
            version=version,
            segments=segments,
            sealed_events=None if segments else (),
        )

    # ── 分卷 ────────────────────────────────────────────────────────────
    @property
    def sealed_count(self) -> int:
        """已经封存进分卷的事件条数（= 活动段第一条的序号）。"""
        return _sealed_total(self.segments)

    @property
    def active_count(self) -> int:
        return len(_active_entries(self.state))

    @property
    def sealed_bytes(self) -> int:
        return sum(segment.bytes for segment in self.segments)

    def attach_history(self, blobs: Sequence[bytes]) -> "WorldArchive":
        """挂上逐卷核对过的分卷内容。blobs 与清单一一对应，顺序相同。"""
        if len(blobs) != len(self.segments):
            raise ArchiveCorrupt(
                f"清单上有 {len(self.segments)} 卷，读到的是 {len(blobs)} 卷"
            )
        sealed: List[Dict] = []
        for segment, blob in zip(self.segments, blobs):
            sealed.extend(decode_segment(segment, blob))
        return replace(self, sealed_events=tuple(sealed))

    def seal_plan(
        self,
        *,
        span: Optional[timedelta] = None,
        max_events: Optional[int] = None,
    ) -> Tuple[Tuple[Dict, ...], ...]:
        """活动段里现在该封存的若干整段，从最早的开始。

        一段 = 从活动段开头起，时间早于"第一条 + span"、且不超过 max_events 条的
        最长前缀；只有它**后面还有事件**时才算满（否则这一段还在长）。满了就切下
        来接着算下一段。结果只由活动段决定：同一份活动段永远切出同样的几段，
        所以一次没写成的封存，下次原样重来。
        """
        span = SEGMENT_SPAN if span is None else span
        max_events = SEGMENT_MAX_EVENTS if max_events is None else max_events
        if max_events < 1:
            raise ArchiveError("max_events 必须 ≥ 1")
        entries = _active_entries(self.state)
        chunks = []
        start = 0
        while start < len(entries):
            limit = datetime.fromisoformat(entries[start]["occurred_at"]) + span
            end = start
            while (
                end < len(entries)
                and end - start < max_events
                and datetime.fromisoformat(entries[end]["occurred_at"]) < limit
            ):
                end += 1
            if end >= len(entries):
                break  # 这一段后面没有事件了：还没满
            chunks.append(tuple(entries[start:end]))
            start = end
        return tuple(chunks)

    def sealed(self, new_segments: Sequence[EventSegment]) -> "WorldArchive":
        """封存之后的信封：清单加上新卷，活动段去掉它们包含的事件。

        不改 sealed_events —— 一份刚封存过的存档本来就不需要把分卷读回来；
        它只会被写下去。
        """
        segments = self.segments + tuple(new_segments)
        _check_manifest(segments)
        moved = sum(segment.count for segment in new_segments)
        entries = _active_entries(self.state)
        if moved > len(entries):
            raise ArchiveError("封存的条数超过了活动段")
        state = dict(self.state)
        state["events"] = {**self.state["events"], "events": entries[moved:]}
        _check_active(segments, state)
        return replace(
            self,
            state=state,
            segments=segments,
            version=WORLD_ARCHIVE_VERSION,
            sealed_events=None,
        )

    # ── 恢复 ────────────────────────────────────────────────────────────
    def restore_state(self) -> SessionState:
        """恢复出一份**冷**的权威状态：数据齐全，一个服务都没绑。

        绑定调度器 / Agency / 记忆 / 自主运行时是下一步，而且必须由调用方交出
        冷适配器来做（见 lifecycle.py）。分开这两步是刻意的：存档里没有、也
        不该有任何能变成一个活服务的东西。
        """
        if self.sealed_events is None:
            raise ArchiveError(
                f"世界 '{self.world_id}' 的 {len(self.segments)} 卷历史还没加载，"
                "只有活动段的存档恢复不出一个完整世界"
            )
        if len(self.sealed_events) != self.sealed_count:
            raise ArchiveError("加载的分卷条数跟清单对不上")
        payload = deepcopy(self.state)
        if self.sealed_events:
            payload["events"] = {
                **payload["events"],
                "events": deepcopy(list(self.sealed_events))
                + list(payload["events"].get("events", [])),
            }
        if self.version == 2 and payload.get("time_events") is None:
            _legacy_time_events(payload, self.clock)
        try:
            state = SessionState.from_dict(payload)
        except ArchiveError:
            raise
        except (ValueError, TypeError, KeyError, IndexError, RuntimeError) as e:
            # 敌对存档能让任何一层的构造函数抛任何一种异常。全部翻译成同一句
            # "这份存档不能用"，但**保留原文和原异常**：既不吞掉，也不让调用方
            # 去猜要 catch 哪十种错误。
            raise ArchiveError(
                f"世界 '{self.world_id}' 的存档没通过校验: {type(e).__name__}: {e}"
            ) from e
        if state.session_id != self.session_id:
            raise ArchiveError("恢复出来的会话身份跟信封对不上")
        if state.world_state is None or state.world_state.clock != self.clock:
            raise ArchiveError("恢复出来的世界时钟跟信封对不上")
        return state
