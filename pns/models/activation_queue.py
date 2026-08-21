# pns/models/activation_queue.py — 待触发激活的队列
#
# 队列只管一件事：**按确定的顺序保存还没触发的激活**。它不认识时钟，不会
# 自己触发任何东西，也不知道什么叫"世界" —— 那些是 PersistentScheduler 的事。
# 这条分工跟 Event / EventStore 那一对完全一样：形状校验在这里，与世界相关的
# 校验（不能排到过去、角色得真的存在）在运行时那一层。
#
# 顺序是显式的，而不是"碰巧按插入顺序"：
#
#     排序键 = (due_at, sequence)
#
# sequence 是登记顺序，队列在 _append 时分配，之后跟着这条激活一辈子（周期
# 激活重排也保留原来的号）。因此同一时刻到期的两条激活，谁先登记谁先来 ——
# 这个结果不依赖 dict 的迭代顺序，也不依赖 sort 的稳定性，序列化一圈回来
# 仍然一模一样。
#
# 对外只提供读取；带下划线的写入/快照方法只供运行时的调度事务使用。
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from pns.models.activation import ActivationError, ScheduledActivation


class ActivationQueueError(ValueError):
    """对激活队列的非法操作（重复 ID、未知 ID、损坏的持久化顺序等）。"""


class ActivationQueue:
    """一个调度器里唯一一份待触发激活队列。"""

    def __init__(self, activations: Iterable[ScheduledActivation] = ()):
        self._entries: Dict[str, Tuple[int, ScheduledActivation]] = {}
        self._next_sequence = 0
        for activation in activations:
            self._append(activation)

    # ── 写入（只给调度事务用） ──────────────────────────────────────────
    def _check_can_append(self, activation: ScheduledActivation) -> None:
        """入队前的纯校验：只看能不能入队，不改任何状态。"""
        if not isinstance(activation, ScheduledActivation):
            raise ActivationQueueError("只能向队列追加 ScheduledActivation")
        if activation.activation_id in self._entries:
            raise ActivationQueueError(
                f"重复的 activation_id: {activation.activation_id}"
            )

    def _append(self, activation: ScheduledActivation) -> int:
        """登记一条已校验的激活，返回分配给它的 sequence。"""
        self._check_can_append(activation)
        sequence = self._next_sequence
        self._entries[activation.activation_id] = (sequence, activation)
        self._next_sequence += 1
        return sequence

    def _adopt(self, sequence: int, activation: ScheduledActivation) -> None:
        """按给定 sequence 恢复一条激活（只给 from_dict 用）。"""
        self._check_can_append(activation)
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise ActivationQueueError("sequence 必须是整数")
        if sequence < 0:
            raise ActivationQueueError("sequence 不能是负数")
        if any(existing == sequence for existing, _ in self._entries.values()):
            raise ActivationQueueError(f"重复的 sequence: {sequence}")
        self._entries[activation.activation_id] = (sequence, activation)
        self._next_sequence = max(self._next_sequence, sequence + 1)

    def _remove(self, activation_id: str) -> ScheduledActivation:
        """摘掉一条激活并返回它；不存在就抛。"""
        entry = self._entries.pop(activation_id, None)
        if entry is None:
            raise ActivationQueueError(f"未知的 activation_id: {activation_id}")
        return entry[1]

    def _reschedule(self, activation: ScheduledActivation) -> int:
        """把一条周期激活换成它的下一次触发，保留原来的 sequence。

        保留 sequence 是刻意的：它表示"这条激活是第几个被登记的"，重排一次
        不该让它在同刻到期的排序里插队到后来者前面或后面去。
        """
        entry = self._entries.get(activation.activation_id)
        if entry is None:
            raise ActivationQueueError(
                f"未知的 activation_id: {activation.activation_id}"
            )
        sequence = entry[0]
        self._entries[activation.activation_id] = (sequence, activation)
        return sequence

    def _snapshot(self) -> Dict:
        """取一份队列快照，供调度事务失败时整体回滚。"""
        return {
            "entries": dict(self._entries),
            "next_sequence": self._next_sequence,
        }

    def _restore(self, snapshot: Dict) -> None:
        """就地恢复到 _snapshot() 的那一刻。"""
        self._entries = dict(snapshot["entries"])
        self._next_sequence = snapshot["next_sequence"]

    # ── 读取 ────────────────────────────────────────────────────────────
    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[ScheduledActivation]:
        return iter(self.pending())

    def has(self, activation_id: str) -> bool:
        return activation_id in self._entries

    def get(self, activation_id: str) -> ScheduledActivation:
        entry = self._entries.get(activation_id)
        if entry is None:
            raise ActivationQueueError(f"未知的 activation_id: {activation_id}")
        return entry[1]

    def sequence_of(self, activation_id: str) -> int:
        entry = self._entries.get(activation_id)
        if entry is None:
            raise ActivationQueueError(f"未知的 activation_id: {activation_id}")
        return entry[0]

    def entries(self) -> Tuple[Tuple[int, ScheduledActivation], ...]:
        """按队列顺序返回 (sequence, 激活) 对。"""
        return tuple(
            sorted(self._entries.values(), key=lambda item: (item[1].due_at, item[0]))
        )

    def pending(self) -> Tuple[ScheduledActivation, ...]:
        """按队列顺序返回全部待触发激活。"""
        return tuple(activation for _, activation in self.entries())

    def next_due(self) -> Optional[ScheduledActivation]:
        entries = self.entries()
        return entries[0][1] if entries else None

    def due_at_or_before(self, when) -> Tuple[Tuple[int, ScheduledActivation], ...]:
        """到 when 为止会到期的激活，按队列顺序。纯读取，不改任何状态。"""
        return tuple(
            (sequence, activation)
            for sequence, activation in self.entries()
            if activation.due_at <= when
        )

    def for_character(self, character_id: str) -> Tuple[ScheduledActivation, ...]:
        return tuple(
            activation
            for activation in self.pending()
            if activation.character_id == character_id
        )

    # ── 序列化 ──────────────────────────────────────────────────────────
    def to_dict(self) -> Dict:
        """安全序列化：按队列顺序写出，每条都带上它的 sequence。"""
        return {
            "next_sequence": self._next_sequence,
            "activations": [
                {"sequence": sequence, **activation.to_dict()}
                for sequence, activation in self.entries()
            ],
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "ActivationQueue":
        """从持久化形状恢复，顺序不对/ID 重复/sequence 损坏一律拒绝。

        这里刻意重算一次顺序并跟文件里的顺序比对：如果只是照单全收，一份被
        手改过顺序的存档会安静地变成一个"排序键说 A 在前、实际先触发 B"的
        队列，而那种错误在触发之前完全看不出来。
        """
        if not isinstance(payload, dict):
            raise ActivationQueueError("激活队列必须是字典")
        entries = payload.get("activations", [])
        if not isinstance(entries, list):
            raise ActivationQueueError("激活队列的 activations 必须是数组")

        queue = cls()
        restored: List[Tuple[int, ScheduledActivation]] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ActivationQueueError(f"激活队列第 {index} 项必须是字典")
            if "sequence" not in entry:
                raise ActivationQueueError(f"激活队列第 {index} 项缺少 sequence")
            try:
                activation = ScheduledActivation.from_dict(entry)
            except ActivationError as e:
                raise ActivationQueueError(f"激活队列第 {index} 项不合法: {e}") from e
            queue._adopt(entry["sequence"], activation)
            restored.append((entry["sequence"], activation))

        if restored != list(queue.entries()):
            raise ActivationQueueError(
                "激活队列的持久化顺序与 (due_at, sequence) 排序不一致"
            )

        next_sequence = payload.get("next_sequence", queue._next_sequence)
        if isinstance(next_sequence, bool) or not isinstance(next_sequence, int):
            raise ActivationQueueError("next_sequence 必须是整数")
        if next_sequence < queue._next_sequence:
            # 比已有的号还小，等于下一次登记就会撞上一个已经用掉的 sequence。
            raise ActivationQueueError(
                f"next_sequence {next_sequence} 小于队列里已经用掉的号"
            )
        queue._next_sequence = next_sequence
        return queue
