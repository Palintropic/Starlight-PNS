# pns/integrations/accord/feed.py — 把一个持久世界投影成 Accord 的事件词汇
#
# 桌面那边（CI 枢纽、桌宠插件）只认 Accord 的几个词：hello / line / resident /
# clock / world。这一层把世界历史翻成这几个词，自己**不写**世界里的任何东西：
# 每次读取都在 snapshot_boundary 里做，跟 overview 是同一条边界、同一份客观记录。
#
# 透出的字段是 overview 已经透出的东西的子集：居民的位置、活动、在线频道，台词
# 正文，时钟。观察、曝光、记忆、provenance、存档路径一概不碰。
#
# ── 游标 ────────────────────────────────────────────────────────────────
#
# overview 的 seq 是事件在历史里的下标。在一个世界句柄的生命里历史只追加，所以
# "从第 n 条之后接着推"在同一个句柄里成立。跨句柄就不一定了：恢复回到上一次
# checkpoint，之后那段历史没了，新事件会**重新用同一批下标**。而且有些事件 ID
# 是按规则拼出来的（台词是 `<会话>:t<回合>:dialogue`，时间推进是
# `<会话>:clock:<条数>`），恢复之后同一个下标上可能长出一条 ID 相同、正文不同的
# 台词。所以游标既不能只是下标，也不能只是"下标 + 事件 ID"：
#
#     <下标>.<摘要>
#
# 摘要 = 到这一条为止**所有台词**的滚动哈希，再并上这一条事件本身。续传时在当前
# 句柄上重算：对得上，说明客户端见过的每一句台词都还在原位、原样，从下一条接着
# 推；对不上，就是历史被改过，推 `world: restored`，客户端重取快照，不重放。
# 只滚台词，是因为客户端从流里拿到的历史只有台词；居民和时钟是状态，每次连上都
# 随 hello 重给一份。
import bisect
import hashlib
import json
import re
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from pns.models.event import Event, EventType
from pns.models.session import SessionFencedError, TransactionBoundaryError
from pns.runtime.formal_world import formal_world

PROTOCOL = 1
SOURCE_WORLD = "world"
LINE_KINDS = {
    EventType.DIALOGUE_SPOKEN: "dialogue",
    EventType.MESSAGE_SENT: "message",
}

STATE_RUNNING = "running"
STATE_STOPPED = "stopped"
STATE_RESTORED = "restored"

# 一次读取等事务让路的上限。比 overview 短得多：流每一轮都会再来，等不到就下一轮。
READ_TIMEOUT = 1.0
# 时钟最多多久推一条（秒）。世界每分钟推进一次，推得更勤只是噪音。
DEFAULT_CLOCK_INTERVAL = 60.0

_DIGEST_BYTES = 8
_CURSOR_RE = re.compile(r"^(-1|0|[1-9][0-9]{0,15})\.([0-9a-f]{16})$")


class CursorError(ValueError):
    """客户端交来的游标看不懂。"""


@dataclass(frozen=True)
class Cursor:
    """流已经送到了哪儿：最后一条已处理事件的下标，加上那一刻的台词摘要。"""

    seq: int  # -1 = 那时历史还是空的
    digest: str

    def encode(self) -> str:
        return f"{self.seq}.{self.digest}"

    @classmethod
    def parse(cls, text: object) -> "Cursor":
        if not isinstance(text, str):
            raise CursorError("游标必须是字符串")
        match = _CURSOR_RE.match(text.strip())
        if match is None:
            raise CursorError("游标格式不对")
        return cls(int(match.group(1)), match.group(2))


def _hash(*parts: bytes) -> bytes:
    # 每段前面带上长度：拼接方式不同的两组输入不会撞成同一串字节。
    digest = hashlib.blake2b(digest_size=_DIGEST_BYTES)
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.digest()


def _canonical(event: Event) -> bytes:
    return json.dumps(
        event.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


_EMPTY_CURSOR = Cursor(-1, _hash(b"").hex())


class _LineChain:
    """一个世界句柄上"到第 n 条为止的全部台词"的滚动摘要。

    跟句柄的历史一起只追加。句柄换了（恢复、重开）就是另一条链，旧的随旧句柄
    一起被回收。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._scanned = 0
        self._seqs: List[int] = []
        self._rolling: List[bytes] = []

    def extend(self, events: Sequence[Event]) -> None:
        with self._lock:
            # 几条订阅各自在快照边界里读，扩展的先后不一定跟读的先后一致：晚到的
            # 那份可能比已经扫过的短。同一个句柄的历史只追加，短的那份是长的前缀，
            # 没有新东西，跳过就是了。
            for seq in range(self._scanned, len(events)):
                event = events[seq]
                if event.type in LINE_KINDS:
                    previous = self._rolling[-1] if self._rolling else b""
                    self._rolling.append(_hash(previous, _canonical(event)))
                    self._seqs.append(seq)
            self._scanned = max(self._scanned, len(events))

    def cursor_at(self, events: Sequence[Event], seq: int) -> Cursor:
        """下标 seq 处的游标。调用方保证已经 extend 到至少 seq + 1 条。"""
        if seq < 0:
            return _EMPTY_CURSOR
        with self._lock:
            index = bisect.bisect_right(self._seqs, seq)
            lines = self._rolling[index - 1] if index else b""
        return Cursor(seq, _hash(lines, _canonical(events[seq])).hex())


@dataclass(frozen=True)
class Message:
    """一条 SSE 消息。`id` 是送完这一条之后流所在的位置；没有就不写 id 行。"""

    event: str
    data: Dict
    id: Optional[str] = None

    def encode(self) -> str:
        # JSON 会把正文里的换行转义掉，所以 data 永远只占一行。
        out = []
        if self.id is not None:
            out.append(f"id: {self.id}")
        out.append(f"event: {self.event}")
        out.append(
            "data: " + json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        )
        return "\n".join(out) + "\n\n"


@dataclass(frozen=True)
class _Frame:
    """在一次快照边界里读到的世界。"""

    clock: str
    length: int
    events: Optional[Tuple[Event, ...]]
    places: Dict[str, str]
    channels: Dict[str, str]
    residents: Dict[str, Dict]


class AccordFeed:
    """一个进程里 Accord feed 的唯一入口。只读。

    台词链按世界句柄缓存（弱引用）：同一个世界的多条订阅共用一条，句柄被关掉、
    回收之后它也跟着没了。
    """

    def __init__(self, plane) -> None:
        self._plane = plane
        self._chains: "weakref.WeakKeyDictionary[object, _LineChain]" = (
            weakref.WeakKeyDictionary()
        )
        self._chains_lock = threading.Lock()

    # ── 世界 ────────────────────────────────────────────────────────────
    def opened(self, world_id: str):
        """本进程里开着、而且还在接受写入的那个句柄；没有就是 None。"""
        world = self._plane.service.opened(world_id)
        if world is None or world.closed or not world.runtime.running:
            return None
        return world

    def exists(self, world_id: str) -> bool:
        if self._plane.service.opened(world_id) is not None:
            return True
        return self._plane.store.exists(world_id)

    def list_worlds(self) -> List[Dict]:
        worlds = []
        for status in self._plane.list_worlds():
            world_id = status["world_id"]
            worlds.append(
                {
                    "id": world_id,
                    "name": world_name(world_id),
                    "state": STATE_RUNNING if self.opened(world_id) else STATE_STOPPED,
                }
            )
        return worlds

    def hello(self, world_id: str) -> Dict:
        """快照。开着的世界给居民和游标；没开着的只给状态和存档里的时钟。"""
        world = self.opened(world_id)
        if world is None:
            return self._stopped_hello(world_id)
        frame = self._read(world, with_events=True)
        head = self._chain(world).cursor_at(frame.events, frame.length - 1)
        return self._hello(world_id, frame, head)

    def subscribe(
        self,
        world_id: str,
        after: Optional[Cursor] = None,
        *,
        clock_interval: float = DEFAULT_CLOCK_INTERVAL,
        now: Callable[[], float] = time.monotonic,
    ) -> "FeedSession":
        return FeedSession(self, world_id, after, clock_interval=clock_interval, now=now)

    # ── 内部 ────────────────────────────────────────────────────────────
    def _chain(self, world) -> _LineChain:
        with self._chains_lock:
            chain = self._chains.get(world)
            if chain is None:
                chain = self._chains[world] = _LineChain()
            return chain

    def _read(self, world, *, with_events: bool) -> _Frame:
        with world.state.snapshot_boundary(timeout=READ_TIMEOUT) as state:
            ws = state.world_state
            places = {loc.location_id: loc.name for loc in ws.locations}
            channels = {ch.channel_id: ch.name for ch in ws.channels}
            residents = {}
            for character_id in ws.known_characters():
                location_id = ws.location_of(character_id)
                residents[character_id] = {
                    "id": character_id,
                    "place": (
                        places.get(location_id, location_id)
                        if location_id is not None
                        else None
                    ),
                    "activity": ws.activity_of(character_id).kind.value,
                    "online": [
                        channels.get(channel_id, channel_id)
                        for channel_id in ws.channels_for(character_id)
                    ],
                }
            length = len(state.events)
            events = state.events.events() if with_events else None
            clock = ws.clock.isoformat()
        # 哈希在边界外面算：事件是不可变的，没必要让世界的提交等这一段。
        if events is not None:
            self._chain(world).extend(events)
        return _Frame(clock, length, events, places, channels, residents)

    def _names(self, character_ids) -> Dict[str, str]:
        registry = self._plane.registry()
        names = {}
        for character_id in character_ids:
            try:
                meta = registry.character_metadata(character_id)
            except ValueError:
                # 内容包里已经没有这个角色：照实给 ID，不猜名字（同 overview）。
                meta = {}
            names[character_id] = str(meta.get("name") or character_id)
        return names

    def _hello(self, world_id: str, frame: _Frame, head: Cursor) -> Dict:
        names = self._names(frame.residents)
        return {
            "node": _node(world_id),
            "clock": frame.clock,
            "state": STATE_RUNNING,
            "residents": [
                {**resident, "name": names[character_id]}
                for character_id, resident in frame.residents.items()
            ],
            "cursor": head.encode(),
            "at": frame.clock,
        }

    def _stopped_hello(self, world_id: str) -> Dict:
        clock = self._plane.status(world_id).get("clock")
        return {
            "node": _node(world_id),
            "clock": clock,
            "state": STATE_STOPPED,
            "residents": [],
            "cursor": None,
            "at": clock,
        }


def world_name(world_id: str) -> str:
    spec = formal_world(world_id)
    return spec.display_name if spec is not None else world_id


def _node(world_id: str) -> Dict:
    return {
        "id": f"pns:{world_id}",
        "name": world_name(world_id),
        "protocol": PROTOCOL,
        "kind": "world",
    }


def _line(event: Event, frame: _Frame) -> Optional[Dict]:
    kind = LINE_KINDS.get(event.type)
    if kind is None:
        return None
    text = event.to_dict()["payload"].get("text")
    if not isinstance(text, str) or not text:
        return None
    data = {
        "text": text,
        "speaker": event.actor_id,
        "source": SOURCE_WORLD,
        "kind": kind,
        "at": event.occurred_at.isoformat(),
    }
    if kind == "message":
        channel_id = event.channel_id
        data["channel"] = frame.channels.get(channel_id, channel_id) if channel_id else None
    else:
        location_id = event.location_id
        data["place"] = frame.places.get(location_id, location_id) if location_id else None
    return data


class FeedSession:
    """一条订阅从头到尾的状态：送到哪了、上次送出的居民和时钟是什么样。

    纯同步，一次 `poll()` 读一次世界、返回这一轮该推的消息。HTTP 那一层只负责
    隔一会儿叫它一次、把消息写出去。
    """

    def __init__(
        self,
        feed: AccordFeed,
        world_id: str,
        after: Optional[Cursor],
        *,
        clock_interval: float,
        now: Callable[[], float],
    ) -> None:
        self._feed = feed
        self._world_id = world_id
        self._now = now
        self._clock_interval = clock_interval
        # 已经送到的位置。None = 这条流还没有任何位置可言（开局时世界没开着，
        # 客户端也没给游标）。
        self._cursor: Optional[Cursor] = after
        self._handle = None
        self._state: Optional[str] = None
        self._residents: Dict[str, Dict] = {}
        self._clock: Optional[str] = None
        self._clock_sent_at: Optional[float] = None
        self._greeted = False

    @property
    def cursor(self) -> Optional[Cursor]:
        return self._cursor

    def poll(self) -> List[Message]:
        try:
            return self._poll()
        except TransactionBoundaryError:
            # 有一次提交一直占着世界：这一轮什么都不推，下一轮再读。
            return []
        except SessionFencedError:
            # 读到一半世界被关上了。下一轮会看到它不在了，在那时推 stopped。
            return []

    # ── 内部 ────────────────────────────────────────────────────────────
    def _id(self) -> Optional[str]:
        return self._cursor.encode() if self._cursor is not None else None

    def _poll(self) -> List[Message]:
        world = self._feed.opened(self._world_id)
        if not self._greeted:
            return self._greet(world)
        if world is None:
            self._handle = None
            if self._state == STATE_STOPPED:
                return []
            return [self._world_state(STATE_STOPPED, self._clock)]
        if world is not self._handle:
            return self._rejoin(world)
        frame = self._feed._read(world, with_events=self._behind(world))
        return self._advance(world, frame)

    def _behind(self, world) -> bool:
        if self._cursor is None:
            return True
        return len(world.state.events) > self._cursor.seq + 1

    def _matches(self, world, frame: _Frame, cursor: Cursor) -> bool:
        if cursor.seq >= frame.length:
            return False
        chain = self._feed._chain(world)
        return chain.cursor_at(frame.events, cursor.seq) == cursor

    def _greet(self, world) -> List[Message]:
        self._greeted = True
        if world is None:
            # 客户端交来的游标原样留着：世界开起来以后照它判断能不能续上。
            hello = self._feed._stopped_hello(self._world_id)
            self._state = STATE_STOPPED
            self._clock = hello["clock"]
            self._clock_sent_at = self._now()
            return [Message("hello", hello, self._id())]
        frame = self._feed._read(world, with_events=True)
        head = self._feed._chain(world).cursor_at(frame.events, frame.length - 1)
        messages = []
        resume = self._cursor
        if resume is not None and not self._matches(world, frame, resume):
            # 交来的游标在这条历史上对不上：它见过的台词有的已经不在了。
            messages.append(Message("world", {"state": STATE_RESTORED, "at": frame.clock}, head.encode()))
            resume = None
        self._handle = world
        self._state = STATE_RUNNING
        hello = self._feed._hello(self._world_id, frame, head)
        self._remember_snapshot(frame)
        if resume is None:
            self._cursor = head
            messages.append(Message("hello", hello, head.encode()))
            return messages
        # 续传：hello 的 id 还是客户端自己的位置，断在补发途中也不会跳过任何一句。
        messages.append(Message("hello", hello, resume.encode()))
        messages.extend(self._deliver(world, frame, resume))
        return messages

    def _rejoin(self, world) -> List[Message]:
        """跟着的句柄换了：世界被关掉又打开，或者开局时它根本没开着。"""
        frame = self._feed._read(world, with_events=True)
        chain = self._feed._chain(world)
        head = chain.cursor_at(frame.events, frame.length - 1)
        self._handle = world
        if self._cursor is not None and self._matches(world, frame, self._cursor):
            messages = []
            if self._state != STATE_RUNNING:
                self._state = STATE_RUNNING
                messages.append(self._world_state(STATE_RUNNING, frame.clock))
            messages.extend(self._advance(world, frame))
            return messages
        word = STATE_RESTORED if self._cursor is not None else STATE_RUNNING
        self._state = STATE_RUNNING
        self._cursor = head
        self._remember_snapshot(frame)
        return [
            Message("world", {"state": word, "at": frame.clock}, head.encode()),
            Message("hello", self._feed._hello(self._world_id, frame, head), head.encode()),
        ]

    def _advance(self, world, frame: _Frame) -> List[Message]:
        messages: List[Message] = []
        if frame.events is not None:
            messages.extend(self._deliver(world, frame, self._cursor))
        messages.extend(self._resident_changes(frame))
        clock = self._clock_change(frame)
        if clock is not None:
            messages.append(clock)
        return messages

    def _deliver(self, world, frame: _Frame, after: Optional[Cursor]) -> List[Message]:
        """`after` 之后到当前末尾的台词，按历史顺序。送完以后位置推到末尾。"""
        chain = self._feed._chain(world)
        start = after.seq + 1 if after is not None else 0
        messages = []
        for seq in range(start, frame.length):
            event = frame.events[seq]
            data = _line(event, frame)
            if data is None:
                continue
            messages.append(Message("line", data, chain.cursor_at(frame.events, seq).encode()))
        self._cursor = chain.cursor_at(frame.events, frame.length - 1)
        return messages

    def _resident_changes(self, frame: _Frame) -> List[Message]:
        changed = [
            character_id
            for character_id, resident in frame.residents.items()
            if self._residents.get(character_id) != resident
        ]
        if not changed:
            return []
        names = self._feed._names(changed)
        messages = []
        for character_id in changed:
            resident = frame.residents[character_id]
            self._residents[character_id] = resident
            messages.append(
                Message(
                    "resident",
                    {**resident, "name": names[character_id], "at": frame.clock},
                    self._id(),
                )
            )
        return messages

    def _clock_change(self, frame: _Frame) -> Optional[Message]:
        if frame.clock == self._clock:
            return None
        now = self._now()
        if self._clock_sent_at is not None and now - self._clock_sent_at < self._clock_interval:
            return None
        self._clock = frame.clock
        self._clock_sent_at = now
        return Message("clock", {"clock": frame.clock, "at": frame.clock}, self._id())

    def _world_state(self, state: str, clock: Optional[str]) -> Message:
        self._state = state
        return Message("world", {"state": state, "at": clock}, self._id())

    def _remember_snapshot(self, frame: _Frame) -> None:
        self._residents = dict(frame.residents)
        self._clock = frame.clock
        self._clock_sent_at = self._now()
