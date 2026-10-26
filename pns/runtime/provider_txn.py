# pns/runtime/provider_txn.py — 模型设置保存的文件事务（CFG-1 §4.4、§5.3、§5.4、§13 第 2、3 条）
#
# 一次 PUT / DELETE 在 provider_transaction()（P7 锁、准入关着）里按这个顺序做：
#
#   1. 恢复材料：旧文件原样存成 provider.json.prev（旧文件不存在就删掉残留的
#      .prev），再写 provider.txn 记下 op 和新旧两份内容的摘要。两者都耐久之后，
#      回读 .prev 核对摘要——不误用上一次遗留的 .prev。
#   2. 发布候选：PUT 写 provider.json，DELETE 删它。
#   3. 构建并切换快照（停旧会话、等退出）。
#   4. 成功：先删 .txn 再删 .prev。失败：把旧内容写回 / 删回，耐久之后再删 .txn
#      和 .prev——只有走完这一步才对外说"已回退"。
#
# 任何一步之间进程退出，只要 provider.json 已经不是旧内容，.txn 就还在（第 4 步
# 成功分支删掉 .txn 之前快照已经切到了新内容）。生产启动发现 .txn 拒绝起来，
# 不靠 schema 判断"这份合法的配置是不是事务做完了"。
#
# 收不了尾（恢复材料清不掉、旧内容写不回、切换之后清理失败、切换时抛了异常而
# 快照是否已换说不清）就进隔离：快照已经切到新内容之后，只把磁盘恢复成旧的
# 不能叫"已回退"，证明不了磁盘与内存一致就继续隔离。解除隔离走 resolve()。
#
# 调用方先持有 ProviderSwitchGate（锁顺序：闸门 → P7），本模块只管 P7 那一层。
# 这些文件里装着 token：这里抛出和返回的文字都是固定措辞。
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pns.runtime import settings_files
from pns.runtime.provider_settings import PROVIDER_FILE
from pns.runtime.reload import ConfigBoundary, ProviderTransaction, ReloadResult
from pns.runtime.settings_files import NotPublished, PublishedNotDurable, SettingsWriteError

TXN_FILE = "provider.txn"
PREV_FILE = PROVIDER_FILE + ".prev"
ABSENT = "absent"

OP_PUT = "put"
OP_DELETE = "delete"
OP_RESOLVE = "resolve"

OUTCOME_OK = "ok"
OUTCOME_NOT_WRITTEN = "not_written"  # 候选没发布：磁盘、快照都是之前的
OUTCOME_ROLLED_BACK = "rolled_back"  # 发布了、没生效：旧内容已耐久恢复
OUTCOME_QUARANTINED = "quarantined"

KEEP_CURRENT = "current"
KEEP_PREVIOUS = "previous"
KEEP_ABSENT = "absent"
CHOICES = (KEEP_CURRENT, KEEP_PREVIOUS, KEEP_ABSENT)

STARTUP_REFUSAL = (
    "data/settings/provider.txn 存在：上一次模型设置保存没有收尾。请在服务器上核对 "
    "provider.json 与 provider.json.prev，决定保留哪一份（或恢复为不存在），然后用 "
    "scripts/provider_resolve.py 完成恢复；不要直接删除 provider.txn。"
)


class NothingToResolve(RuntimeError):
    """没有遗留事务、也不在隔离状态。"""


class ResolveRefused(RuntimeError):
    """所选的那一份无法核实（.prev 缺失或摘要对不上），什么都没改。"""


@dataclass(frozen=True)
class SaveOutcome:
    status: str
    reload: Optional[ReloadResult] = None


def digest(payload: Optional[bytes]) -> str:
    return ABSENT if payload is None else "sha256:" + hashlib.sha256(payload).hexdigest()


def pending(settings_dir: Path) -> bool:
    """有没有遗留的事务记录。"""
    return (Path(settings_dir) / TXN_FILE).exists()


def _read(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _publish(path: Path, payload: Optional[bytes]) -> None:
    if payload is None:
        settings_files.durable_unlink(path)
    else:
        settings_files.durable_write(path, payload)


class _Files:
    def __init__(self, settings_dir: Path) -> None:
        settings_dir = Path(settings_dir)
        self.target = settings_dir / PROVIDER_FILE
        self.prev = settings_dir / PREV_FILE
        self.txn = settings_dir / TXN_FILE

    def clear_record(self) -> None:
        """先删 .txn 再删 .prev。删 .txn 失败（含耐久证实不了）抛 SettingsWriteError。

        .txn 没了事务就算结束：之后 .prev 删不掉也不要紧——它只在摘要与 .txn
        一致时才会被用，下一次保存也会先覆盖它。
        """
        settings_files.durable_unlink(self.txn)
        try:
            settings_files.durable_unlink(self.prev)
        except SettingsWriteError:
            pass


def save(
    boundary: ConfigBoundary,
    settings_dir: Path,
    candidate: Optional[bytes],
    *,
    reason: str,
) -> SaveOutcome:
    """发布一份候选（None = 恢复默认，即删除 provider.json）并切换。

    候选应当已经通过有效线路校验；这里的构建切换是第二道关。ReloadBusy /
    SettingsQuarantined 由 provider_transaction() 原样抛出，此时什么都没写。
    """
    with boundary.provider_transaction() as tx:
        return _save(tx, boundary, _Files(settings_dir), candidate, reason)


def _save(
    tx: ProviderTransaction,
    boundary: ConfigBoundary,
    files: _Files,
    candidate: Optional[bytes],
    reason: str,
) -> SaveOutcome:
    if files.txn.exists():
        # 遗留事务不该出现在这里（启动会拦、失败会隔离）；绝不在上面再叠一层。
        tx.quarantine()
        return SaveOutcome(OUTCOME_QUARANTINED)
    previous = _read(files.target)

    # 1. 恢复材料。
    record = json.dumps(
        {
            "op": OP_DELETE if candidate is None else OP_PUT,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "previous": digest(previous),
            "candidate": digest(candidate),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        _publish(files.prev, previous)
        settings_files.durable_write(files.txn, record)
        material_ok = digest(_read(files.prev)) == digest(previous)
    except (SettingsWriteError, OSError):
        material_ok = False
    if not material_ok:
        return _abandon(tx, files)

    # 2. 发布候选。
    try:
        _publish(files.target, candidate)
    except NotPublished:
        return _abandon(tx, files)
    except PublishedNotDurable:
        # 已经是目标文件了，只是耐久证实不了：按"已经发生"处理，恢复旧内容。
        return _roll_back(tx, files, previous, None)

    # 3. 构建并切换。
    before = boundary.active_or_none()
    try:
        result = tx.swap(reason)
    except Exception:
        if boundary.active_or_none() is not before:
            # 快照已经换了却没拿到结果：说不清磁盘该是哪一份。
            tx.quarantine()
            return SaveOutcome(OUTCOME_QUARANTINED)
        return _roll_back(tx, files, previous, None)
    if result.status != "ok":
        return _roll_back(tx, files, previous, result)

    # 4. 收尾。快照已经是新内容：清理失败只能隔离，不能把磁盘退回旧的。
    try:
        files.clear_record()
    except SettingsWriteError:
        tx.quarantine()
        return SaveOutcome(OUTCOME_QUARANTINED, result)
    return SaveOutcome(OUTCOME_OK, result)


def _abandon(tx: ProviderTransaction, files: _Files) -> SaveOutcome:
    """候选还没发布：清掉恢复材料就等于什么都没发生。清不掉就隔离。"""
    try:
        files.clear_record()
    except SettingsWriteError:
        tx.quarantine()
        return SaveOutcome(OUTCOME_QUARANTINED)
    return SaveOutcome(OUTCOME_NOT_WRITTEN)


def _roll_back(
    tx: ProviderTransaction,
    files: _Files,
    previous: Optional[bytes],
    result: Optional[ReloadResult],
) -> SaveOutcome:
    """候选发布了、快照还是旧的：把旧内容耐久地放回去，再清恢复材料。"""
    try:
        _publish(files.target, previous)
        files.clear_record()
    except SettingsWriteError:
        tx.quarantine()
        return SaveOutcome(OUTCOME_QUARANTINED, result)
    return SaveOutcome(OUTCOME_ROLLED_BACK, result)


def resolve(boundary: ConfigBoundary, settings_dir: Path, keep: str, *, reason: str) -> SaveOutcome:
    """解除隔离（CFG-1 §5.4、§13 第 3 条）：选定磁盘上该留哪一份，让内存跟上，再清理。

    keep：current = 留下现在的 provider.json；previous = 用 .prev 恢复（摘要必须
    与 .txn 记录的旧内容一致）；absent = 恢复为不存在（env 派生）。

    按锁顺序进门（调用方持闸门），在准入关着时写所选内容、构建切换（含排空旧
    会话）、耐久清理，全部成功才解除隔离；任何一步失败都保持隔离。
    """
    if keep not in CHOICES:
        raise ValueError(f"keep 只能是 {'、'.join(CHOICES)}")
    files = _Files(settings_dir)
    with boundary.provider_transaction(allow_quarantined=True) as tx:
        if not files.txn.exists() and not boundary.quarantined:
            raise NothingToResolve("没有遗留的模型设置事务，也不在隔离状态")
        if keep == KEEP_PREVIOUS:
            chosen = _verified_previous(files)
        tx.quarantine()  # 从这里起任何一步失败，出门时都保持隔离
        try:
            if keep == KEEP_PREVIOUS:
                settings_files.durable_write(files.target, chosen)
            elif keep == KEEP_ABSENT:
                settings_files.durable_unlink(files.target)
        except SettingsWriteError:
            return SaveOutcome(OUTCOME_QUARANTINED)
        try:
            result = tx.swap(reason)
        except Exception:
            return SaveOutcome(OUTCOME_QUARANTINED)
        if result.status != "ok":
            return SaveOutcome(OUTCOME_QUARANTINED, result)
        try:
            files.clear_record()
        except SettingsWriteError:
            return SaveOutcome(OUTCOME_QUARANTINED, result)
        tx.clear_quarantine()
        return SaveOutcome(OUTCOME_OK, result)


def _verified_previous(files: _Files) -> bytes:
    try:
        record = json.loads(files.txn.read_bytes())
        expected = record["previous"]
    except (OSError, ValueError, TypeError, KeyError):
        raise ResolveRefused("provider.txn 读不出旧内容的摘要，不能用 .prev 恢复") from None
    if not isinstance(expected, str) or expected == ABSENT:
        raise ResolveRefused("事务开始前没有 provider.json：请选择恢复为不存在") from None
    payload = _read(files.prev)
    if payload is None or digest(payload) != expected:
        raise ResolveRefused("provider.json.prev 缺失或与事务记录不一致，不能用它恢复") from None
    return payload


__all__ = [
    "CHOICES",
    "KEEP_ABSENT",
    "KEEP_CURRENT",
    "KEEP_PREVIOUS",
    "NothingToResolve",
    "OUTCOME_NOT_WRITTEN",
    "OUTCOME_OK",
    "OUTCOME_QUARANTINED",
    "OUTCOME_ROLLED_BACK",
    "PREV_FILE",
    "ResolveRefused",
    "STARTUP_REFUSAL",
    "SaveOutcome",
    "TXN_FILE",
    "digest",
    "pending",
    "resolve",
    "save",
]
