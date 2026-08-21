# pns/models/activation_outbox.py — 已到期激活的持久化投递箱
#
# 为什么必须有这个东西：到期资格如果只活在一次调用的返回值里，那它就是易失的。
# 一条一次性激活到期的那一刻已经从队列里摘掉了，如果下游还没来得及处理，进程
# 就中断/重启，这次到期就永远消失了 —— 队列里没有它，世界历史里也只有一条
# "时间推进了 N 分钟"，没有任何东西能让它重新到期。
#
# 所以到期记录跟时钟、事件、队列一起进同一个事务，落在这里，并且在被**明确
# 确认**之前一直可读。确认是幂等的：同一条记录确认两次，第二次什么都不做。
#
# 确认过的记录不删除，只打标记。这样"这条我处理过了"是可判断的事实，而不是
# "它不在了，所以大概处理过了"；重复投递和默默丢失都能被区分出来。
#
# 对外只提供读取；带下划线的写入/快照方法只供运行时的调度事务使用。
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple

from pns.models.activation import ActivationDue, ActivationError


class ActivationOutboxError(ValueError):
    """对投递箱的非法操作（重复记录、时间倒流、未知 due_id、存档损坏等）。"""


class ActivationOutbox:
    """一个会话里唯一一份已到期激活记录。"""

    def __init__(
        self,
        records: Iterable[ActivationDue] = (),
        acknowledged: Iterable[str] = (),
    ):
        self._records: List[ActivationDue] = []
        self._index: Dict[str, int] = {}
        self._acknowledged: Set[str] = set()
        for record in records:
            self._append(record)
        for due_id in acknowledged:
            if due_id not in self._index:
                raise ActivationOutboxError(f"确认了不存在的 due_id: {due_id}")
            self._acknowledged.add(due_id)

    # ── 写入（只给调度事务用） ──────────────────────────────────────────
    def _check_can_append(self, record: ActivationDue) -> None:
        """落箱前的纯校验：只看能不能落，不改任何状态。"""
        if not isinstance(record, ActivationDue):
            raise ActivationOutboxError("只能向投递箱追加 ActivationDue")
        if record.due_id in self._index:
            raise ActivationOutboxError(f"重复的 due_id: {record.due_id}")
        if self._records and record.fired_at < self._records[-1].fired_at:
            # 触发时刻只会随时钟前进。倒流意味着有人在拼接两个不同时刻的状态。
            raise ActivationOutboxError(
                f"到期记录 '{record.due_id}' 的触发时间 "
                f"{record.fired_at.isoformat()} 早于上一条 "
                f"{self._records[-1].fired_at.isoformat()}"
            )

    def _append(self, record: ActivationDue) -> int:
        self._check_can_append(record)
        self._records.append(record)
        self._index[record.due_id] = len(self._records) - 1
        return len(self._records) - 1

    def _acknowledge(self, due_id: str) -> bool:
        """确认一条到期记录已被消费；已经确认过就返回 False。"""
        if not isinstance(due_id, str) or not due_id:
            raise ActivationOutboxError("due_id 必须是非空字符串")
        if due_id not in self._index:
            # 未知 ID 不是"重复确认"，而是消费方拿着一条不存在的记录 ——
            # 这种混淆必须响亮，不能当成幂等的一部分吞掉。
            raise ActivationOutboxError(f"未知的 due_id: {due_id}")
        if due_id in self._acknowledged:
            return False
        self._acknowledged.add(due_id)
        return True

    def _snapshot(self) -> Dict:
        """取一份快照，供事务失败时整体回滚（含确认状态）。"""
        return {
            "records": list(self._records),
            "index": dict(self._index),
            "acknowledged": set(self._acknowledged),
        }

    def _restore(self, snapshot: Dict) -> None:
        self._records = list(snapshot["records"])
        self._index = dict(snapshot["index"])
        self._acknowledged = set(snapshot["acknowledged"])

    # ── 读取 ────────────────────────────────────────────────────────────
    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[ActivationDue]:
        return iter(tuple(self._records))

    def has(self, due_id: str) -> bool:
        return due_id in self._index

    def get(self, due_id: str) -> ActivationDue:
        index = self._index.get(due_id)
        if index is None:
            raise ActivationOutboxError(f"未知的 due_id: {due_id}")
        return self._records[index]

    def records(self) -> Tuple[ActivationDue, ...]:
        """全部到期记录，按落箱顺序（也就是触发顺序）。"""
        return tuple(self._records)

    def pending(self) -> Tuple[ActivationDue, ...]:
        """还没被确认的到期记录 —— 恢复之后要接着处理的就是这些。"""
        return tuple(
            record for record in self._records if record.due_id not in self._acknowledged
        )

    def acknowledged_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self._acknowledged))

    def is_acknowledged(self, due_id: str) -> bool:
        if due_id not in self._index:
            raise ActivationOutboxError(f"未知的 due_id: {due_id}")
        return due_id in self._acknowledged

    def for_activation(self, activation_id: str) -> Tuple[ActivationDue, ...]:
        return tuple(
            record for record in self._records if record.activation_id == activation_id
        )

    def latest(self) -> Optional[ActivationDue]:
        return self._records[-1] if self._records else None

    # ── 序列化 ──────────────────────────────────────────────────────────
    def to_dict(self) -> Dict:
        # 位置键叫 outbox_sequence 而不是 sequence：到期记录自己带的 sequence 是
        # 它在**队列**里的登记号（同刻到期的排序依据），跟它在投递箱里的第几条
        # 是两回事。共用一个键名会让其中一个悄悄覆盖另一个。
        return {
            "records": [
                {
                    "outbox_sequence": index,
                    "acknowledged": record.due_id in self._acknowledged,
                    **record.to_dict(),
                }
                for index, record in enumerate(self._records)
            ]
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "ActivationOutbox":
        """从持久化形状恢复；序号不连续、记录损坏、时间倒流一律拒绝。"""
        if not isinstance(payload, dict):
            raise ActivationOutboxError("投递箱必须是字典")
        entries = payload.get("records", [])
        if not isinstance(entries, list):
            raise ActivationOutboxError("投递箱的 records 必须是数组")

        outbox = cls()
        for expected, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ActivationOutboxError(f"投递箱第 {expected} 项必须是字典")
            sequence = entry.get("outbox_sequence")
            if (
                not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or sequence != expected
            ):
                raise ActivationOutboxError(
                    f"投递箱 outbox_sequence 不连续：第 {expected} 项收到 {sequence!r}"
                )
            try:
                record = ActivationDue.from_dict(entry)
            except ActivationError as e:
                raise ActivationOutboxError(f"投递箱第 {expected} 项不合法: {e}") from e
            outbox._append(record)
            acknowledged = entry.get("acknowledged", False)
            if not isinstance(acknowledged, bool):
                raise ActivationOutboxError(
                    f"投递箱第 {expected} 项的 acknowledged 必须是布尔值"
                )
            if acknowledged:
                outbox._acknowledged.add(record.due_id)
        return outbox
