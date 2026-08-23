# pns/models/event_store.py — 客观世界历史
#
# 一个会话只有一份 EventStore，只追加、不改写。它是"发生过什么"的权威记录，
# 与"某个角色知道什么"是两件事：事件不会被复制进角色历史，角色记忆要等
# 后续阶段的 Observation 通道，不从这里直接读。
#
# 排序策略是显式的：事件的模拟时间不允许倒退，时间相同的事件按追加顺序排列。
# 因此追加顺序本身就是确定的世界历史顺序，不需要事后再排序。
from datetime import datetime
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple

from pns.models.event import Event


class EventStoreError(ValueError):
    """对事件历史的非法操作（重复 ID、时间倒流、回滚越界等）。"""


class EventStore:
    """一个会话里唯一一份只追加的已提交事件历史。

    对外只提供读取；带下划线的写入/回滚方法只供 runtime 提交事务使用。
    """

    def __init__(self, events: Iterable[Event] = ()):
        self._events: List[Event] = []
        self._ids: Set[str] = set()
        for event in events:
            self._append(event)

    # ── 追加 ────────────────────────────────────────────────────────────
    def _check_can_append(self, event: Event) -> None:
        """提交前的纯校验：只看能不能追加，不改任何状态。"""
        if not isinstance(event, Event):
            raise EventStoreError("只能向世界历史追加 Event")
        if event.event_id in self._ids:
            raise EventStoreError(f"重复的 event_id: {event.event_id}")
        if self._events and event.occurred_at < self._events[-1].occurred_at:
            raise EventStoreError(
                f"世界历史不能倒流：事件 '{event.event_id}' 的时间 "
                f"{event.occurred_at.isoformat()} 早于上一条 "
                f"{self._events[-1].occurred_at.isoformat()}"
            )

    def _append(self, event: Event) -> int:
        """提交边界内部追加一条已验证事件，返回它的序号。"""
        self._check_can_append(event)
        self._events.append(event)
        self._ids.add(event.event_id)
        return len(self._events) - 1

    def _rollback_to(self, length: int) -> None:
        """中止一次进行中的提交时回退到之前的长度。

        这不是"编辑历史"的接口 —— 只有提交事务在失败路径上才允许调用它，
        已经成功提交的事件不会被任何正常路径删掉。
        """
        if not isinstance(length, int) or isinstance(length, bool):
            raise EventStoreError("回滚长度必须是整数")
        if length < 0 or length > len(self._events):
            raise EventStoreError(f"回滚长度越界: {length}")
        for event in self._events[length:]:
            self._ids.discard(event.event_id)
        del self._events[length:]

    # ── 读取 ────────────────────────────────────────────────────────────
    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[Event]:
        return iter(tuple(self._events))

    def has(self, event_id: str) -> bool:
        return event_id in self._ids

    def events(self) -> Tuple[Event, ...]:
        """按世界历史顺序返回全部事件（元组，外部改不动内部列表）。"""
        return tuple(self._events)

    def get(self, event_id: str) -> Event:
        for event in self._events:
            if event.event_id == event_id:
                return event
        raise EventStoreError(f"未知的 event_id: {event_id}")

    def sequence_of(self, event_id: str) -> int:
        for index, event in enumerate(self._events):
            if event.event_id == event_id:
                return index
        raise EventStoreError(f"未知的 event_id: {event_id}")

    def latest(self) -> Optional[Event]:
        return self._events[-1] if self._events else None

    def since(self, occurred_at: datetime) -> Tuple[Event, ...]:
        """`occurred_at` 那一刻及之后的事件，按世界历史顺序。

        追加时时间不允许倒退（见 _check_can_append），所以从尾部往回扫、遇到
        第一条更早的就停是完备的：不可能有更早的事件排在更晚的后面。代价因此
        只跟窗口内的事件条数有关，而不是整段历史的长度 —— 调用方每次推进都会
        问一次这个问题（"这个角色在当前时段里有没有发生过状态变更"），拿整份
        历史的副本去回答它，会让每一次推进的成本随世界寿命一起涨。
        """
        if not isinstance(occurred_at, datetime):
            raise EventStoreError("since() 需要一个 datetime")
        cut = len(self._events)
        while cut > 0 and self._events[cut - 1].occurred_at >= occurred_at:
            cut -= 1
        return tuple(self._events[cut:])

    def by_type(self, event_type) -> Tuple[Event, ...]:
        return tuple(event for event in self._events if event.type == event_type)

    # ── 序列化 ──────────────────────────────────────────────────────────
    def to_dict(self) -> Dict:
        """安全序列化：每条事件都是新的可变结构，序号即世界历史顺序。"""
        return {
            "events": [
                {"sequence": index, **event.to_dict()}
                for index, event in enumerate(self._events)
            ]
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "EventStore":
        if not isinstance(payload, dict):
            raise EventStoreError("事件历史必须是字典")
        entries = payload.get("events", [])
        if not isinstance(entries, list):
            raise EventStoreError("事件历史的 events 必须是数组")

        restored = []
        for expected, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise EventStoreError(f"事件历史第 {expected} 项必须是字典")
            sequence = entry.get("sequence")
            if (
                not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or sequence != expected
            ):
                raise EventStoreError(
                    f"事件历史 sequence 不连续：第 {expected} 项收到 {sequence!r}"
                )
            restored.append(Event.from_dict(entry))
        return cls(restored)
