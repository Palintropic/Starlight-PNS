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
# 账本上的每一次操作（记下一版、做一次决定）各占一个**操作序号**，从 0 连续
# 往上数，一个不缺。加载时按序号从开局版本完整重放：每次记录都要针对当时的
# 已采用版本、每次采用都要针对当时的已采用版本，最后恰好走到此刻的版本。
# 顺序只看序号，不看墙钟（墙钟会被往回拨）；删掉任何一条记录都会在序号里
# 留下空洞。
#
# 第三种操作是**入住采用**（WORLD-2）：一位新居民入住时，她的作息作为一项新内容
# 进入账本。它与记录、决定共用同一条操作序号；内容项只能经入住采用新增，开局之后
# 没有别的路。
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
    # 记下这一版时占用的操作序号；决定时再占一个。
    offered_seq: int
    decided_at_wall: Optional[str] = None
    decided_seq: Optional[int] = None

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
        _seq(self.offered_seq, "offered_seq")
        pending = self.status is ConflictStatus.PENDING
        if pending != (self.decided_at_wall is None) or pending != (self.decided_seq is None):
            raise ContentLedgerError("只有待决记录没有决定（时间与序号），已决记录必须都有")
        if self.decided_at_wall is not None:
            _text(self.decided_at_wall, "decided_at_wall")
        if self.decided_seq is not None:
            _seq(self.decided_seq, "decided_seq")
            if self.decided_seq <= self.offered_seq:
                raise ContentLedgerError("决定的序号必须晚于记录的序号")

    @property
    def conflict_id(self) -> str:
        return f"{self.subject}@{self.offered_fingerprint[:16]}#{self.offered_seq}"

    def to_dict(self) -> Dict:
        return {
            "subject": self.subject,
            "adopted_fingerprint": self.adopted_fingerprint,
            "offered_fingerprint": self.offered_fingerprint,
            "registry_revision": self.registry_revision,
            "status": self.status.value,
            "recorded_at_wall": self.recorded_at_wall,
            "offered_seq": self.offered_seq,
            "decided_at_wall": self.decided_at_wall,
            "decided_seq": self.decided_seq,
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
                offered_seq=payload["offered_seq"],
                decided_at_wall=payload["decided_at_wall"],
                decided_seq=payload["decided_seq"],
            )
        except KeyError as e:
            raise ContentLedgerError(f"冲突记录缺字段: {e}") from None


def _seq(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContentLedgerError(f"{label} 必须是非负整数")
    return value


@dataclass(frozen=True)
class ContentAdmission:
    """一项内容随新居民入住进入账本（WORLD-2）：开局之后新增内容项的唯一来源。

    `operation_id` 指向世界历史里那条 world.resident_admitted；恢复时两边逐条互验。
    """

    subject: str
    fingerprint: str
    seq: int
    operation_id: str

    def __post_init__(self) -> None:
        _text(self.subject, "subject")
        _fingerprint(self.fingerprint, "fingerprint")
        _seq(self.seq, "seq")
        _text(self.operation_id, "operation_id")

    def to_dict(self) -> Dict:
        return {
            "subject": self.subject,
            "fingerprint": self.fingerprint,
            "seq": self.seq,
            "operation_id": self.operation_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> "ContentAdmission":
        if not isinstance(payload, Mapping) or set(payload) != {
            "subject",
            "fingerprint",
            "seq",
            "operation_id",
        }:
            raise ContentLedgerError("入住采用记录的字段不对")
        return cls(**dict(payload))


@dataclass(frozen=True)
class ContentLedger:
    adopted: Tuple[Tuple[str, str], ...]
    conflicts: Tuple[ContentConflict, ...] = ()
    # 下一次操作要用的序号 = 已经发生过的操作数。
    next_seq: int = 0
    # 入住采用（WORLD-2），按序号排列。
    admissions: Tuple[ContentAdmission, ...] = ()

    def __post_init__(self) -> None:
        adopted = tuple(sorted((str(k), str(v)) for k, v in dict(self.adopted).items()))
        if len(adopted) != len(tuple(self.adopted)):
            raise ContentLedgerError("同一项内容只能有一个已采用版本")
        for subject, fingerprint in adopted:
            _text(subject, "subject")
            _fingerprint(fingerprint, f"{subject} 的指纹")
        object.__setattr__(self, "adopted", adopted)
        _seq(self.next_seq, "next_seq")
        current = dict(adopted)
        used = []
        admitted = set()
        previous_admission = -1
        for admission in self.admissions:
            if not isinstance(admission, ContentAdmission):
                raise ContentLedgerError("admissions 里只能放 ContentAdmission")
            if admission.subject not in current:
                raise ContentLedgerError(
                    f"入住采用记录的内容项 '{admission.subject}' 不在已采用版本里"
                )
            if admission.subject in admitted:
                raise ContentLedgerError(f"内容项 '{admission.subject}' 入住采用了两次")
            if admission.seq <= previous_admission:
                raise ContentLedgerError("入住采用记录必须按序号排列")
            admitted.add(admission.subject)
            previous_admission = admission.seq
            used.append(admission.seq)
        previous_offer = -1
        for conflict in self.conflicts:
            if not isinstance(conflict, ContentConflict):
                raise ContentLedgerError("conflicts 里只能放 ContentConflict")
            if conflict.subject not in current:
                raise ContentLedgerError(
                    f"冲突记录引用了这个世界没有采用过的内容 '{conflict.subject}'"
                )
            if conflict.offered_seq <= previous_offer:
                raise ContentLedgerError("冲突记录必须按记录序号排列")
            previous_offer = conflict.offered_seq
            used.append(conflict.offered_seq)
            if conflict.decided_seq is not None:
                used.append(conflict.decided_seq)
        # 序号从 0 连续到 next_seq - 1，每个恰好用一次：删掉、改回待决、凭空多出
        # 的任何一条操作都会在这里露出空洞或重号。
        if sorted(used) != list(range(self.next_seq)):
            raise ContentLedgerError(
                "内容账本的操作序号不连续（有记录被删、被改回待决或被凭空加入）"
            )
        # 不用开局版本也能查的一条：每项内容若被采用过（含入住采用），此刻的版本
        # 就是按序号最后一次采用的那一版。完整的重放见 check_genesis。
        latest: Dict[str, Tuple[int, str]] = {
            admission.subject: (admission.seq, admission.fingerprint)
            for admission in self.admissions
        }
        for conflict in self.conflicts:
            if conflict.status is ConflictStatus.ADOPTED:
                seen = latest.get(conflict.subject)
                if seen is None or conflict.decided_seq > seen[0]:
                    latest[conflict.subject] = (conflict.decided_seq, conflict.offered_fingerprint)
        for subject, (_, fingerprint) in latest.items():
            if current[subject] != fingerprint:
                raise ContentLedgerError(
                    f"'{subject}' 的已采用版本与最后一次采用记录不一致"
                )
        object.__setattr__(self, "conflicts", tuple(self.conflicts))
        object.__setattr__(self, "admissions", tuple(self.admissions))

    # ── 查询 ────────────────────────────────────────────────────────────
    def adopted_fingerprint(self, subject: str) -> Optional[str]:
        return dict(self.adopted).get(subject)

    def pending(self) -> Tuple[ContentConflict, ...]:
        return tuple(c for c in self.conflicts if c.status is ConflictStatus.PENDING)

    def accepts(self, subject: str, fingerprint: str) -> bool:
        """这一版内容此刻能不能生效：只有已采用的那一版能。"""
        return self.adopted_fingerprint(subject) == fingerprint

    def adoption_seq(self, subject: str) -> int:
        """此刻的已采用版本是哪一次采用定下的（那次决定的序号）。开局以来没换过是 -1。

        它划出"此刻这一个采用周期"：序号在它之后的记录，针对的都是此刻的已采用
        版本。已采用版本可以绕回一个用过的指纹（A → B → A），所以只看指纹分不出
        周期，要看序号。
        """
        return max(
            (
                c.decided_seq
                for c in self.conflicts
                if c.subject == subject and c.status is ConflictStatus.ADOPTED
            ),
            default=-1,
        )

    def current_offer(self, subject: str, fingerprint: str) -> Optional[ContentConflict]:
        """此刻这一个采用周期里，提议 `fingerprint` 的那条记录（至多一条）。"""
        start = self.adoption_seq(subject)
        adopted = self.adopted_fingerprint(subject)
        for conflict in self.conflicts:
            if (
                conflict.subject == subject
                and conflict.offered_seq > start
                and conflict.adopted_fingerprint == adopted
                and conflict.offered_fingerprint == fingerprint
            ):
                return conflict
        return None

    # ── 转换 ────────────────────────────────────────────────────────────
    def offered(
        self, subject: str, fingerprint: str, *, registry_revision: int, wall: str
    ) -> "ContentLedger":
        """内容包里此刻是这一版。与已采用版本不同、又没在此刻这个采用周期里记过，就记一条 pending。

        "记过"按（这一项，这一版，此刻的采用周期）判断：已采用版本变了之后，同一版
        再出现就是一个新提议，要重新记，否则它永远没有能被决定的记录。已采用版本
        绕回一个用过的指纹（A → B → A）也是一个新的周期，旧周期里的记录不算数。
        """
        adopted = self.adopted_fingerprint(subject)
        if adopted is None:
            raise ContentLedgerError(f"'{subject}' 不是这个世界采用过的内容")
        if adopted == fingerprint:
            return self
        if self.current_offer(subject, fingerprint) is not None:
            return self  # 此刻这个周期里已经记过（无论决定了没有），不重复记
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
                    offered_seq=self.next_seq,
                ),
            ),
            self.next_seq + 1,
            self.admissions,
        )

    def decided(self, conflict_id: str, status, *, wall: str) -> "ContentLedger":
        """项目所有者对一条待决记录的决定。采用会替换已采用版本。

        驳回、暂缓对任何待决记录都成立；采用要求它针对的正是此刻的已采用版本
        （过期的待决可以驳回；那一版若仍在内容里，下次打开会针对新版本重新记一条）。
        """
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
            if (
                status is ConflictStatus.ADOPTED
                and conflict.adopted_fingerprint != self.adopted_fingerprint(conflict.subject)
            ):
                raise ContentLedgerError(
                    f"冲突 '{conflict_id}' 针对的已采用版本已经变了，不能再采用；"
                    "可以驳回它，那一版若仍在内容里，下次打开会针对新版本重新记一条"
                )
            conflicts[index] = ContentConflict(
                subject=conflict.subject,
                adopted_fingerprint=conflict.adopted_fingerprint,
                offered_fingerprint=conflict.offered_fingerprint,
                registry_revision=conflict.registry_revision,
                status=status,
                recorded_at_wall=conflict.recorded_at_wall,
                offered_seq=conflict.offered_seq,
                decided_at_wall=wall,
                decided_seq=self.next_seq,
            )
            adopted = dict(self.adopted)
            if status is ConflictStatus.ADOPTED:
                adopted[conflict.subject] = conflict.offered_fingerprint
            return ContentLedger(
                tuple(adopted.items()), tuple(conflicts), self.next_seq + 1, self.admissions
            )
        raise ContentLedgerError(f"没有这条冲突记录: {conflict_id}")

    def admitted(self, subject: str, fingerprint: str, *, operation_id: str) -> "ContentLedger":
        """一位新居民入住：她的这一项内容以这一版进入账本，占一个操作序号。"""
        if self.adopted_fingerprint(subject) is not None:
            raise ContentLedgerError(f"'{subject}' 已经在账本里，不能再入住采用")
        admission = ContentAdmission(subject, fingerprint, self.next_seq, operation_id)
        return ContentLedger(
            self.adopted + ((subject, fingerprint),),
            self.conflicts,
            self.next_seq + 1,
            self.admissions + (admission,),
        )

    # ── 校验：已采用版本从哪来 ──────────────────────────────────────────
    def check_successor_of(self, previous: "ContentLedger") -> None:
        """self 是不是 previous 的一次合法后继（一个事务里的变化）。

        旧记录只能原样保留，或由待决变成已决；新操作的序号都在 previous 之后。
        "每次记录 / 采用都针对当时的版本、最后走到此刻的版本"由 check_genesis 的
        完整重放负责，调用方两者都要调。
        """
        before_all, after_all = previous.conflicts, self.conflicts
        if len(after_all) < len(before_all):
            raise ContentLedgerError("冲突记录只能追加或被决定，不能删")
        if self.next_seq < previous.next_seq:
            raise ContentLedgerError("操作序号不能倒退")
        before_admissions = previous.admissions
        if self.admissions[: len(before_admissions)] != before_admissions:
            raise ContentLedgerError("入住采用记录只能追加，不能删改")
        new_admissions = self.admissions[len(before_admissions):]
        for admission in new_admissions:
            if admission.seq < previous.next_seq:
                raise ContentLedgerError("新的入住采用用了一个已经用过的序号")
        # 内容项只能经入住采用新增，不能减少。
        if set(dict(self.adopted)) != set(dict(previous.adopted)) | {
            admission.subject for admission in new_admissions
        }:
            raise ContentLedgerError("内容账本不能增减内容项（新增只能经入住采用）")
        for before, after in zip(before_all, after_all):
            if before == after:
                continue
            if before.status is not ConflictStatus.PENDING or after.status is ConflictStatus.PENDING:
                raise ContentLedgerError("冲突记录只能追加或被决定，不能删改")
            if _identity(before) != _identity(after):
                raise ContentLedgerError(f"冲突 '{before.conflict_id}' 在决定时被改了内容")
            if after.decided_seq < previous.next_seq:
                raise ContentLedgerError("决定用了一个已经用过的序号")
        for after in after_all[len(before_all):]:
            if after.offered_seq < previous.next_seq:
                raise ContentLedgerError("新记录用了一个已经用过的序号")

    def check_genesis(self, genesis: Mapping[str, str]) -> None:
        """从开局版本出发，按操作序号重放全部记录、决定与入住采用，必须恰好走到此刻的版本。

        每条记录针对的必须是记录那一刻的已采用版本；每次采用针对的必须是采用那一
        刻的已采用版本；入住采用的内容项在那一刻之前不存在。只有序号决定先后，墙钟
        时间不参与。
        """
        if set(dict(self.adopted)) != set(genesis) | {a.subject for a in self.admissions}:
            raise ContentLedgerError("内容账本的内容项与开局来源加入住采用不一致")
        if dict(self._replay(genesis, self.next_seq)) != dict(self.adopted):
            raise ContentLedgerError("已采用版本不是从开局版本经采用记录走到的")

    def adopted_before(self, genesis: Mapping[str, str], seq: int) -> Dict[str, str]:
        """从开局版本重放到序号 `seq` 之前（不含）时的已采用版本。WORLD-2 基准拿它核对。"""
        _seq(seq, "seq")
        if seq > self.next_seq:
            raise ContentLedgerError("序号超出了账本已经发生过的操作")
        return self._replay(genesis, seq)

    def _replay(self, genesis: Mapping[str, str], until: int) -> Dict[str, str]:
        current = dict(genesis)
        operations = []
        for conflict in self.conflicts:
            operations.append((conflict.offered_seq, "offer", conflict))
            if conflict.decided_seq is not None:
                operations.append((conflict.decided_seq, "decide", conflict))
        for admission in self.admissions:
            operations.append((admission.seq, "admit", admission))
        for seq, kind, record in sorted(operations, key=lambda op: op[0]):
            if seq >= until:
                break
            if kind == "admit":
                if record.subject in current:
                    raise ContentLedgerError(
                        f"入住采用的内容项 '{record.subject}' 在那一刻已经存在"
                    )
                current[record.subject] = record.fingerprint
                continue
            conflict = record
            if conflict.subject not in current:
                raise ContentLedgerError(
                    f"冲突 '{conflict.conflict_id}' 针对的内容项在那一刻还不存在"
                )
            base = current[conflict.subject]
            if kind == "offer":
                if conflict.adopted_fingerprint != base:
                    raise ContentLedgerError(
                        f"冲突 '{conflict.conflict_id}' 记录时针对的不是当时的已采用版本"
                    )
            elif conflict.status is ConflictStatus.ADOPTED:
                if conflict.adopted_fingerprint != base:
                    raise ContentLedgerError(
                        f"冲突 '{conflict.conflict_id}' 被采用时针对的不是当时的已采用版本"
                    )
                current[conflict.subject] = conflict.offered_fingerprint
        return current

    # ── 序列化 ──────────────────────────────────────────────────────────
    def to_dict(self) -> Dict:
        payload = {
            "adopted": dict(self.adopted),
            "conflicts": [c.to_dict() for c in self.conflicts],
            "next_seq": self.next_seq,
        }
        # 没入住过的世界存档形状不变（WORLD-2 之前的代码照样读得懂）。
        if self.admissions:
            payload["admissions"] = [a.to_dict() for a in self.admissions]
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping) -> "ContentLedger":
        if not isinstance(payload, Mapping):
            raise ContentLedgerError("内容账本必须是字典")
        adopted = payload.get("adopted")
        conflicts = payload.get("conflicts")
        if not isinstance(adopted, Mapping) or not isinstance(conflicts, list):
            raise ContentLedgerError("内容账本必须有 adopted（字典）与 conflicts（列表）")
        if "next_seq" not in payload:
            raise ContentLedgerError("内容账本缺少 next_seq（操作序号）")
        admissions = payload.get("admissions", [])
        if not isinstance(admissions, list) or ("admissions" in payload and not admissions):
            # 空列表不写进存档（to_dict）；出现了就必须是非空数组。
            raise ContentLedgerError("内容账本的 admissions 必须是非空数组")
        return cls(
            tuple(adopted.items()),
            tuple(ContentConflict.from_dict(item) for item in conflicts),
            payload["next_seq"],
            tuple(ContentAdmission.from_dict(item) for item in admissions),
        )


def _identity(conflict: ContentConflict) -> Tuple:
    return (
        conflict.subject,
        conflict.adopted_fingerprint,
        conflict.offered_fingerprint,
        conflict.registry_revision,
        conflict.recorded_at_wall,
        conflict.offered_seq,
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
    "ContentAdmission",
    "ContentConflict",
    "ContentLedger",
    "ContentLedgerError",
    "content_fingerprint",
    "genesis_from_origin",
]
