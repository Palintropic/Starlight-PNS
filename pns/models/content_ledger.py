# pns/models/content_ledger.py — 正式世界采用了哪一版内容，以及冲突待决记录
#
# 正式世界（WORLD-1「夜明け前」）持续跟随官方新资料，但新资料只能作为**今后**
# 的内容输入，不能改写已经提交的世界历史（计划 §2 第 4 条、§4.3）。这里记两件事：
#
#   * adopted：这个世界此刻采用的每一项内容的指纹（例如某人的作息表）；
#   * conflicts：内容包里出现了与已采用版本不同的一版时，留下的待决记录。
#
# 门的规则只有一条：**没有被明确采用的新版本不生效。** 识别到差异就记一条
# pending，这一项继续按"未采用新版"对待；采用、驳回、暂缓由项目所有者决定，
# 采用在下一次打开世界时生效。不自动采纳、不回写历史、不改 resident 记忆。
#
# 它是运维记录（Article XIII），不是世界真相，不进任何角色的上下文。不可变值
# 对象，整体替换，随事务回滚、随存档往返。
import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Mapping, Optional, Tuple


class ContentLedgerError(ValueError):
    """内容账本不合法，或一次决定不成立。"""


class ConflictStatus(str, Enum):
    PENDING = "pending"
    ADOPTED = "adopted"
    DECLINED = "declined"
    DEFERRED = "deferred"


def content_fingerprint(payload) -> str:
    """一项内容的指纹：规范化 JSON 的 sha256。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(value, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContentLedgerError(f"{label} 必须是非空字符串")
    return value


def _fingerprint(value, label: str) -> str:
    value = _text(value, label)
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ContentLedgerError(f"{label} 必须是 sha256 十六进制串")
    return value


@dataclass(frozen=True)
class ContentConflict:
    """一项内容出现了与已采用版本不同的一版。"""

    subject: str
    adopted_fingerprint: str
    offered_fingerprint: str
    registry_revision: int
    status: ConflictStatus
    recorded_at_wall: str
    decided_at_wall: Optional[str] = None

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        _text(self.subject, "subject")
        _fingerprint(self.adopted_fingerprint, "adopted_fingerprint")
        _fingerprint(self.offered_fingerprint, "offered_fingerprint")
        if self.adopted_fingerprint == self.offered_fingerprint:
            raise ContentLedgerError("相同的指纹不构成冲突")
        revision = self.registry_revision
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ContentLedgerError("registry_revision 必须是非负整数")
        try:
            set_(self, "status", ConflictStatus(self.status))
        except ValueError:
            raise ContentLedgerError(f"未知的冲突状态: {self.status!r}") from None
        _text(self.recorded_at_wall, "recorded_at_wall")
        if (self.status is ConflictStatus.PENDING) != (self.decided_at_wall is None):
            raise ContentLedgerError("只有待决记录没有决定时间，已决记录必须有")
        if self.decided_at_wall is not None:
            _text(self.decided_at_wall, "decided_at_wall")

    @property
    def conflict_id(self) -> str:
        return f"{self.subject}@{self.offered_fingerprint[:16]}"

    def to_dict(self) -> Dict:
        return {
            "subject": self.subject,
            "adopted_fingerprint": self.adopted_fingerprint,
            "offered_fingerprint": self.offered_fingerprint,
            "registry_revision": self.registry_revision,
            "status": self.status.value,
            "recorded_at_wall": self.recorded_at_wall,
            "decided_at_wall": self.decided_at_wall,
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> "ContentConflict":
        if not isinstance(payload, Mapping):
            raise ContentLedgerError("冲突记录必须是字典")
        try:
            return cls(
                subject=payload["subject"],
                adopted_fingerprint=payload["adopted_fingerprint"],
                offered_fingerprint=payload["offered_fingerprint"],
                registry_revision=payload["registry_revision"],
                status=payload["status"],
                recorded_at_wall=payload["recorded_at_wall"],
                decided_at_wall=payload["decided_at_wall"],
            )
        except KeyError as e:
            raise ContentLedgerError(f"冲突记录缺字段: {e}") from None


@dataclass(frozen=True)
class ContentLedger:
    adopted: Tuple[Tuple[str, str], ...]
    conflicts: Tuple[ContentConflict, ...] = ()

    def __post_init__(self) -> None:
        adopted = tuple(sorted((str(k), str(v)) for k, v in dict(self.adopted).items()))
        if len(adopted) != len(tuple(self.adopted)):
            raise ContentLedgerError("同一项内容只能有一个已采用版本")
        for subject, fingerprint in adopted:
            _text(subject, "subject")
            _fingerprint(fingerprint, f"{subject} 的指纹")
        object.__setattr__(self, "adopted", adopted)
        seen = set()
        for conflict in self.conflicts:
            if not isinstance(conflict, ContentConflict):
                raise ContentLedgerError("conflicts 里只能放 ContentConflict")
            if conflict.conflict_id in seen:
                raise ContentLedgerError(f"重复的冲突记录: {conflict.conflict_id}")
            seen.add(conflict.conflict_id)
        # 采用是唯一能让已采用版本变化的途径：一项内容若有过 adopted 记录，
        # 它的已采用版本必须恰好是最后一次采用的那一版。
        current = dict(adopted)
        latest_adoption: Dict[str, str] = {}
        for conflict in self.conflicts:
            if conflict.subject not in current:
                raise ContentLedgerError(
                    f"冲突记录引用了这个世界没有采用过的内容 '{conflict.subject}'"
                )
            if conflict.status is ConflictStatus.ADOPTED:
                latest_adoption[conflict.subject] = conflict.offered_fingerprint
        for subject, fingerprint in latest_adoption.items():
            if current[subject] != fingerprint:
                raise ContentLedgerError(
                    f"'{subject}' 的已采用版本与最后一次采用记录不一致"
                )
        object.__setattr__(self, "conflicts", tuple(self.conflicts))

    # ── 查询 ────────────────────────────────────────────────────────────
    def adopted_fingerprint(self, subject: str) -> Optional[str]:
        return dict(self.adopted).get(subject)

    def pending(self) -> Tuple[ContentConflict, ...]:
        return tuple(c for c in self.conflicts if c.status is ConflictStatus.PENDING)

    def accepts(self, subject: str, fingerprint: str) -> bool:
        """这一版内容此刻能不能生效：只有已采用的那一版能。"""
        return self.adopted_fingerprint(subject) == fingerprint

    # ── 转换 ────────────────────────────────────────────────────────────
    def offered(
        self, subject: str, fingerprint: str, *, registry_revision: int, wall: str
    ) -> "ContentLedger":
        """内容包里此刻是这一版。与已采用版本不同、又没记过，就记一条 pending。"""
        adopted = self.adopted_fingerprint(subject)
        if adopted is None:
            raise ContentLedgerError(f"'{subject}' 不是这个世界采用过的内容")
        if adopted == fingerprint:
            return self
        if any(
            c.subject == subject and c.offered_fingerprint == fingerprint
            for c in self.conflicts
        ):
            return self  # 这一版已经记过（无论决定了没有），不重复记
        return ContentLedger(
            self.adopted,
            self.conflicts
            + (
                ContentConflict(
                    subject=subject,
                    adopted_fingerprint=adopted,
                    offered_fingerprint=fingerprint,
                    registry_revision=registry_revision,
                    status=ConflictStatus.PENDING,
                    recorded_at_wall=wall,
                ),
            ),
        )

    def decided(self, conflict_id: str, status, *, wall: str) -> "ContentLedger":
        """项目所有者对一条待决记录的决定。采用会替换已采用版本。"""
        try:
            status = ConflictStatus(status)
        except ValueError:
            raise ContentLedgerError(f"未知的决定: {status!r}") from None
        if status is ConflictStatus.PENDING:
            raise ContentLedgerError("决定不能是 pending")
        conflicts = list(self.conflicts)
        for index, conflict in enumerate(conflicts):
            if conflict.conflict_id != conflict_id:
                continue
            if conflict.status is not ConflictStatus.PENDING:
                raise ContentLedgerError(f"冲突 '{conflict_id}' 已经决定过了")
            if conflict.adopted_fingerprint != self.adopted_fingerprint(conflict.subject):
                raise ContentLedgerError(
                    f"冲突 '{conflict_id}' 针对的已采用版本已经变了，先处理更新的那条"
                )
            conflicts[index] = ContentConflict(
                subject=conflict.subject,
                adopted_fingerprint=conflict.adopted_fingerprint,
                offered_fingerprint=conflict.offered_fingerprint,
                registry_revision=conflict.registry_revision,
                status=status,
                recorded_at_wall=conflict.recorded_at_wall,
                decided_at_wall=wall,
            )
            adopted = dict(self.adopted)
            if status is ConflictStatus.ADOPTED:
                adopted[conflict.subject] = conflict.offered_fingerprint
            return ContentLedger(tuple(adopted.items()), tuple(conflicts))
        raise ContentLedgerError(f"没有这条冲突记录: {conflict_id}")

    # ── 校验：已采用版本从哪来 ──────────────────────────────────────────
    def check_successor_of(self, previous: "ContentLedger") -> None:
        """self 是不是 previous 的一次合法后继（一个事务里的变化）。

        合法的变化只有三种：追加待决记录；把待决记录决定掉；因为某条待决被决定
        为采用，把那一项的已采用版本换成它提议的那一版。已采用版本的其它任何
        变化 —— 直接换指纹、加减内容项 —— 都不成立。
        """
        before_all, after_all = previous.conflicts, self.conflicts
        if len(after_all) < len(before_all):
            raise ContentLedgerError("冲突记录只能追加或被决定，不能删")
        current = dict(previous.adopted)
        if set(dict(self.adopted)) != set(current):
            raise ContentLedgerError("内容账本不能增减内容项")
        for before, after in zip(before_all, after_all):
            if before == after:
                continue
            if before.status is not ConflictStatus.PENDING or after.status is ConflictStatus.PENDING:
                raise ContentLedgerError("冲突记录只能追加或被决定，不能删改")
            if _identity(before) != _identity(after):
                raise ContentLedgerError(f"冲突 '{before.conflict_id}' 在决定时被改了内容")
            if after.status is ConflictStatus.ADOPTED:
                if current[after.subject] != after.adopted_fingerprint:
                    raise ContentLedgerError(
                        f"冲突 '{after.conflict_id}' 针对的已采用版本已经变了"
                    )
                current[after.subject] = after.offered_fingerprint
        for after in after_all[len(before_all):]:
            if after.status is not ConflictStatus.PENDING:
                raise ContentLedgerError("新追加的冲突记录只能是待决")
            if after.adopted_fingerprint != current[after.subject]:
                raise ContentLedgerError(
                    f"新冲突 '{after.conflict_id}' 记的已采用版本不是此刻的那一版"
                )
        if dict(self.adopted) != current:
            raise ContentLedgerError("已采用版本只能由一次采用决定改变")

    def check_genesis(self, genesis: Mapping[str, str]) -> None:
        """从开局时采用的那一版出发，沿采用记录重放，必须恰好得到此刻的已采用版本。

        每一项内容的采用记录连成一条链：第一条针对开局版本，之后每条针对上一条
        提议的版本。链接不上、有用不上的采用记录、终点不是此刻的版本，都说明
        已采用版本不是靠采用走到这里的。
        """
        adopted = dict(self.adopted)
        if set(adopted) != set(genesis):
            raise ContentLedgerError("内容账本的内容项与开局来源不一致")
        for subject, start in genesis.items():
            links = [
                c
                for c in self.conflicts
                if c.subject == subject and c.status is ConflictStatus.ADOPTED
            ]
            head = start
            while links:
                candidates = [c for c in links if c.adopted_fingerprint == head]
                if not candidates:
                    raise ContentLedgerError(
                        f"'{subject}' 的采用记录接不上开局版本（有采用记录不在链上）"
                    )
                step = min(candidates, key=lambda c: c.decided_at_wall or "")
                links.remove(step)
                head = step.offered_fingerprint
            if head != adopted[subject]:
                raise ContentLedgerError(
                    f"'{subject}' 的已采用版本不是从开局版本经采用记录走到的"
                )

    # ── 序列化 ──────────────────────────────────────────────────────────
    def to_dict(self) -> Dict:
        return {
            "adopted": dict(self.adopted),
            "conflicts": [c.to_dict() for c in self.conflicts],
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> "ContentLedger":
        if not isinstance(payload, Mapping):
            raise ContentLedgerError("内容账本必须是字典")
        adopted = payload.get("adopted")
        conflicts = payload.get("conflicts")
        if not isinstance(adopted, Mapping) or not isinstance(conflicts, list):
            raise ContentLedgerError("内容账本必须有 adopted（字典）与 conflicts（列表）")
        return cls(
            tuple(adopted.items()),
            tuple(ContentConflict.from_dict(item) for item in conflicts),
        )


def _identity(conflict: ContentConflict) -> Tuple:
    return (
        conflict.subject,
        conflict.adopted_fingerprint,
        conflict.offered_fingerprint,
        conflict.registry_revision,
        conflict.recorded_at_wall,
    )


def genesis_from_origin(origin) -> Dict[str, str]:
    """正式世界开局来源里记下的作息指纹 → 内容账本的开局版本。

    内容项的命名与 pns.runtime.formal_world.rhythm_subject 一致（rhythm:<角色>）。
    """
    content = origin.get("content") if isinstance(origin, Mapping) else None
    rhythms = content.get("rhythms") if isinstance(content, Mapping) else None
    if not isinstance(rhythms, Mapping):
        raise ContentLedgerError("有内容账本的世界必须带开局来源里的作息指纹")
    return {f"rhythm:{cid}": _fingerprint(fp, f"开局来源里 {cid} 的作息指纹")
            for cid, fp in rhythms.items()}


__all__ = [
    "ConflictStatus",
    "ContentConflict",
    "ContentLedger",
    "ContentLedgerError",
    "content_fingerprint",
    "genesis_from_origin",
]
