# pns/runtime/persistence/store.py — 世界存档的存储
#
# 这一层回答两个问题，而且只回答这两个：**存档在哪**、**怎么把它完整地写下去**。
#
# 耐久性契约（就这么多，不多一个字）：
#
#   * 一次保存要么整份生效，要么一点都不生效。写的是同目录的临时文件，
#     flush + fsync 之后 os.replace 原子替换，再 fsync 一次目录。任何一步
#     失败，磁盘上留着的仍然是**上一份完整存档** —— 不存在"写了一半的存档"
#     这种第三态，因为半截内容永远待在临时文件里，而临时文件不叫 world.json。
#   * 失败会如实报告有没有留下需要人处理的残留（临时文件）。清理不掉就说
#     清理不掉，不假装干净。
#   * `os.replace` 让新存档**可见**，`fsync` 目录让那次改名**耐久**。两件事
#     不是一件事：目录同步失败之后，读者读到的已经是新存档，可掉电之后回来
#     的可能还是旧的。所以目录同步失败**不算成功保存** —— 它抛
#     ArchiveNotDurable，而不是被吞掉。平台/文件系统压根不支持目录同步是另一
#     档：那是"这里拿不到更强的保证"，不是"这次同步失败了"，它照常成功，但在
#     SaveResult 里明说 directory_sync_supported=False。
#   * 崩溃恢复读到的是**最后一次成功 replace 的那一份**。残留的临时文件被
#     报告，但绝不会被当成存档读回来。
#
# 事件分卷（存档版本 3）走同一套写法，而且顺序是契约的一部分：
#
#   * 先写分卷：临时文件 → fsync → 改名 → fsync history/ 目录；
#   * 再写 world.json，它的清单里才出现这一卷。
#
# 两步之间崩溃，磁盘上多一个清单外的分卷，world.json 仍是上一版、仍含这些事件；
# 恢复用的是上一版，那个文件只是残留，下次封存同一序号时被覆盖。反过来的顺序会
# 让 world.json 指着一卷掉电后不存在的文件 —— 所以分卷的目录同步失败就是这次
# checkpoint 失败，不降级。清单里已有的分卷永远不会被重写或删除。
#
# 明确不做的事：没有 WAL、没有事件重放、没有多版本历史、没有数据库、没有云。
# "最后一次成功 checkpoint 之后的内存工作会丢"是这一层的**真实**保证边界，
# 不许对外说成别的。
#
# 路径安全：world_id 先过 naming.validate_world_id，然后再用 realpath 判一次
# 目录是不是真的落在存档根之下。两道是刻意的 —— 第一道挡文本，第二道挡软链，
# 它们挡的不是同一种攻击。
import errno
import json
import os
import stat
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from pns.runtime.persistence.archive import (
    ArchiveCorrupt,
    ArchiveError,
    EventSegment,
    WorldArchive,
    describe_segment,
    encode_segment,
)
from pns.runtime.persistence.naming import WorldIdError, validate_world_id
from pns.runtime.persistence.ownership import OwnershipHandle, acquire_world

__all__ = [
    "ArchiveNotDurable",
    "ArchiveNotFound",
    "FileWorldStore",
    "SaveResult",
    "StorageError",
    "WorldIdError",
    "WorldStore",
]


# 目录同步拿不到、但**不代表磁盘出问题**的 errno。它们的共同含义是"这个平台
# 或文件系统不提供这个能力"：一些平台打不开目录句柄（EISDIR），一些文件系统
# 对目录 fd 的 fsync 直接回 EINVAL / ENOTSUP / ENOSYS。EACCES / EPERM 不在
# 这里：它们也可能是一次真实的权限故障，不能被降级成“平台不支持”。
# 名单是白名单而不是黑名单：不认识的 errno 一律当成真失败 —— 在耐久性这件事
# 上，猜错的方向必须是"多报一次问题"，不是"少报一次"。
_UNSUPPORTED_SYNC_ERRNOS = frozenset(
    value
    for value in (
        getattr(errno, name, None)
        for name in ("EINVAL", "ENOSYS", "ENOTSUP", "EOPNOTSUPP", "EISDIR")
    )
    if value is not None
)


class StorageError(RuntimeError):
    """存储层拒绝或没能完成这次操作。

    `residue` 是这次失败之后留在磁盘上、需要人处理的临时文件。空元组的意思是
    "现场已经收拾干净了"，而不是"没检查"。
    """

    def __init__(self, message: str, *, residue: Tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.residue = tuple(residue)


class ArchiveNotFound(StorageError):
    """这个世界还没有任何一份完整存档。"""


class ArchiveNotDurable(StorageError):
    """新存档已经在位、读得回来，但它的耐久性没能证实。

    只有一种情况会走到这里：内容写好了、fsync 过了、`os.replace` 也成功了 ——
    所以**磁盘上确实已经是这一版**，任何读者现在读到的都是它 —— 但那次改名
    所在的目录同步失败了。掉电之后回来的可能还是上一版。

    它跟 StorageError 的其余用法方向相反，所以必须单独一档：那些是"这次保存
    没发生"，这一档是"发生了，但保证不到"。调用方的账要按**已经发生**记
    （修订号得往前走，不然下一次会拿同一个号写不同的内容），而对外的话要按
    **保证不到**说（不许宣布干净、不许宣布耐久）。
    """

    def __init__(self, message: str, *, revision: int, path: str) -> None:
        super().__init__(message)
        self.revision = revision
        self.path = path


@dataclass(frozen=True)
class SaveResult:
    """一次成功保存的结果。

    `directory_synced` 为 False 只有一种合法原因：这个平台/文件系统给不了目录
    同步（`directory_sync_supported` 同时为 False）。真正的同步失败不会走到
    这里 —— 那是 ArchiveNotDurable。
    """

    world_id: str
    path: str
    revision: int
    bytes_written: int
    residue: Tuple[str, ...] = ()
    directory_synced: bool = True
    directory_sync_supported: bool = True


class WorldStore(ABC):
    """世界存档的存储接口。

    实现只需要保证上面那份耐久性契约；具体是文件系统、以后是别的什么，
    生命周期层不关心，也不许关心。
    """

    @abstractmethod
    def archive_path(self, world_id: str) -> Path:
        """这个世界的存档应该在的位置（不保证存在）。"""

    @abstractmethod
    def list_worlds(self) -> Tuple[str, ...]:
        """已知的世界 ID。只算有完整存档的。"""

    @abstractmethod
    def exists(self, world_id: str) -> bool:
        """这个世界有没有一份完整存档。"""

    @abstractmethod
    def load(self, world_id: str, *, history: bool = True) -> WorldArchive:
        """读回最后一次成功保存的那一份。

        `history=False` 只读信封与活动段，分卷只核对在不在、大小对不对 ——
        给状态面用；这样的存档恢复不出世界（restore_state 会拒绝）。
        """

    @abstractmethod
    def seal(self, archive: WorldArchive) -> WorldArchive:
        """把活动段里已满的整段写成新分卷，返回清单已更新的信封。

        返回的信封还没写下去：调用方接着 save() 它，清单才算生效。
        """

    @abstractmethod
    def save(self, archive: WorldArchive) -> SaveResult:
        """原子地写下一份完整存档。"""

    @abstractmethod
    def residue(
        self, world_id: str, segments: Optional[Sequence[EventSegment]] = None
    ) -> Tuple[str, ...]:
        """留在磁盘上的、不完整的临时文件。

        给了清单时，history/ 里清单之外的文件也算残留。
        """

    @abstractmethod
    def archive_bytes(self, world_id: str) -> Optional[int]:
        """world.json 此刻的字节数；还没有存档时是 None。分卷的大小看清单。"""

    @abstractmethod
    def acquire(self, world_id: str) -> OwnershipHandle:
        """拿下这个世界的独占所有权。"""


class FileWorldStore(WorldStore):
    """把世界存档放在一个配置好的存档根之下的文件系统实现。

    布局刻意平坦、可读、可以用 `ls` 看懂：

        <root>/<world_id>/world.json          最后一次成功保存的完整存档
        <root>/<world_id>/OWNER.lock          所有权（flock + 一条给人看的记录）
        <root>/<world_id>/world.json.*.tmp    写到一半的残留，永远不会被读回来
        <root>/<world_id>/history/events-000001.jsonl
                                              已封存的事件分卷（清单在 world.json 里）

    构造这个对象**不碰磁盘**：根目录在第一次真正要写的时候才建。
    """

    ARCHIVE_NAME = "world.json"
    LOCK_NAME = "OWNER.lock"
    TMP_SUFFIX = ".tmp"
    HISTORY_DIR = "history"

    def __init__(self, root) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        return f"FileWorldStore({str(self._root)!r})"

    # ── 路径 ────────────────────────────────────────────────────────────
    def _world_dir(self, world_id: str) -> Path:
        """存档根之下、属于这个世界的目录。逃出去就拒绝。

        两道判断：先是文本（validate_world_id），再是 realpath。第二道挡的是
        软链 —— `<root>/<world>` 被做成指向别处的软链时，文本检查一个字都
        看不出问题，而写下去就写到存档根外面了。
        """
        name = validate_world_id(world_id)
        directory = self._root / name
        real_root = Path(os.path.realpath(self._root))
        real_dir = Path(os.path.realpath(directory))
        if real_dir.parent != real_root or real_dir.name != name:
            raise StorageError(
                f"世界 '{name}' 的目录解析到了存档根之外（{real_dir}）——"
                "拒绝在存档根以外读写任何东西"
            )
        return directory

    def archive_path(self, world_id: str) -> Path:
        return self._world_dir(world_id) / self.ARCHIVE_NAME

    def _archive_file(self, world_id: str) -> Path:
        path = self.archive_path(world_id)
        if path.is_symlink():
            raise StorageError(
                f"世界 '{world_id}' 的存档是一个软链（{path}）——"
                "存档必须是存档根之下的普通文件"
            )
        return path

    def lock_path(self, world_id: str) -> Path:
        return self._world_dir(world_id) / self.LOCK_NAME

    def history_dir(self, world_id: str) -> Path:
        """分卷目录。它必须是世界目录里的一个真目录，软链一律拒绝。"""
        directory = self._world_dir(world_id) / self.HISTORY_DIR
        if directory.is_symlink():
            raise StorageError(
                f"世界 '{world_id}' 的分卷目录是一个软链（{directory}）——"
                "分卷必须是存档根之下的普通文件"
            )
        return directory

    def _segment_file(self, world_id: str, name: str) -> Path:
        path = self.history_dir(world_id) / name
        if path.is_symlink():
            raise StorageError(
                f"世界 '{world_id}' 的分卷 {name} 是一个软链 —— 分卷必须是普通文件"
            )
        return path

    # ── 读 ──────────────────────────────────────────────────────────────
    def list_worlds(self) -> Tuple[str, ...]:
        if not self._root.is_dir():
            return ()
        found = []
        for child in sorted(self._root.iterdir()):
            if not child.is_dir() or child.is_symlink():
                continue
            try:
                validate_world_id(child.name)
            except WorldIdError:
                continue
            if (child / self.ARCHIVE_NAME).is_file():
                found.append(child.name)
        return tuple(found)

    def exists(self, world_id: str) -> bool:
        return self._archive_file(world_id).is_file()

    def load(self, world_id: str, *, history: bool = True) -> WorldArchive:
        name = validate_world_id(world_id)
        path = self._archive_file(name)
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            raise ArchiveNotFound(f"世界 '{name}' 还没有任何一份完整存档") from None
        except OSError as e:
            raise StorageError(f"世界 '{name}' 的存档读不出来: {e}") from e
        try:
            payload = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ArchiveCorrupt(
                f"世界 '{name}' 的存档不是完整的 JSON（截断或损坏）: {e}"
            ) from e
        archive = WorldArchive.from_dict(payload)
        if archive.world_id != name:
            raise ArchiveError(
                f"这份存档自称属于世界 '{archive.world_id}'，却躺在 '{name}' 的"
                "位置上 —— 身份对不上的存档不许恢复"
            )
        if not archive.segments:
            return archive
        if not history:
            for segment in archive.segments:
                self._stat_segment(name, segment)
            return archive
        blobs: List[bytes] = []
        for segment in archive.segments:
            self._stat_segment(name, segment)
            path = self._segment_file(name, segment.file)
            try:
                blobs.append(path.read_bytes())
            except OSError as e:
                raise StorageError(
                    f"世界 '{name}' 的分卷 {segment.file} 读不出来: {e}"
                ) from e
        return archive.attach_history(blobs)

    def _stat_segment(self, world_id: str, segment: EventSegment) -> None:
        """便宜的一道：清单上的这一卷在不在、是不是普通文件、大小对不对。"""
        path = self._segment_file(world_id, segment.file)
        try:
            info = path.stat()
        except FileNotFoundError:
            raise ArchiveCorrupt(
                f"世界 '{world_id}' 的分卷 {segment.file} 不见了（清单里有，磁盘上没有）"
            ) from None
        except OSError as e:
            raise StorageError(
                f"世界 '{world_id}' 的分卷 {segment.file} 读不出来: {e}"
            ) from e
        if not stat.S_ISREG(info.st_mode):
            raise ArchiveCorrupt(
                f"世界 '{world_id}' 的分卷 {segment.file} 不是普通文件"
            )
        if info.st_size != segment.bytes:
            raise ArchiveCorrupt(
                f"世界 '{world_id}' 的分卷 {segment.file} 有 {info.st_size} 字节，"
                f"清单记的是 {segment.bytes}（截断或被改过）"
            )

    def archive_bytes(self, world_id: str) -> Optional[int]:
        try:
            return self._archive_file(world_id).stat().st_size
        except FileNotFoundError:
            return None
        except OSError as e:
            raise StorageError(f"世界 '{world_id}' 的存档读不出来: {e}") from e

    def residue(
        self, world_id: str, segments: Optional[Sequence[EventSegment]] = None
    ) -> Tuple[str, ...]:
        directory = self._world_dir(world_id)
        if not directory.is_dir():
            return ()
        found = [
            str(child)
            for child in directory.iterdir()
            if child.name.endswith(self.TMP_SUFFIX)
        ]
        history = self.history_dir(world_id)
        if history.is_dir():
            known = (
                {segment.file for segment in segments} if segments is not None else None
            )
            for child in history.iterdir():
                if child.name.endswith(self.TMP_SUFFIX):
                    found.append(str(child))
                elif known is not None and child.name not in known:
                    # 清单外的分卷：一次没写成 world.json 的封存留下的，
                    # 或者是别人放进来的。它不参与加载。
                    found.append(str(child))
        return tuple(sorted(found))

    # ── 写 ──────────────────────────────────────────────────────────────
    def save(self, archive: WorldArchive) -> SaveResult:
        """同目录临时文件 → flush → fsync → os.replace → fsync 目录。

        失败时上一份完整存档原封不动，并且报告有没有清理不掉的残留。
        """
        if not isinstance(archive, WorldArchive):
            raise StorageError("只能保存 WorldArchive")
        directory = self._world_dir(archive.world_id)
        target = self._archive_file(archive.world_id)
        try:
            payload = json.dumps(
                archive.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError) as e:
            raise StorageError(f"存档序列化失败: {e}") from e

        self._ensure_dir(directory, archive.world_id)
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(directory),
                prefix=self.ARCHIVE_NAME + ".",
                suffix=self.TMP_SUFFIX,
            )
        except OSError as e:
            raise StorageError(
                f"世界 '{archive.world_id}' 的临时存档文件建不出来: {e}"
            ) from e
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException as e:
            residue = self._discard(tmp_name)
            raise StorageError(
                f"世界 '{archive.world_id}' 的存档保存失败，磁盘上还是保存之前"
                f"的样子（上一份完整存档，或者本来就还没有）: "
                f"{type(e).__name__}: {e}"
                + (f"（残留待处理: {', '.join(residue)}）" if residue else ""),
                residue=residue,
            ) from e
        # 走到这里新存档已经可见了。剩下的只有"那次改名耐不耐得住掉电"，
        # 而它有可能失败 —— 失败就必须说出来，见 ArchiveNotDurable。这之后抛出
        # 的**任何**错误都只能是这一档：新版已经在盘上，调用方必须按"已经发生"
        # 记账，否则内存会落后磁盘一版，下一次拿同一个修订号写不同的内容。
        try:
            supported = self._sync_dir(directory, archive)
        except ArchiveNotDurable:
            raise
        except Exception as e:
            raise ArchiveNotDurable(
                f"世界 '{archive.world_id}' 的第 {archive.revision} 版已经写在磁盘上，"
                f"但之后的目录同步出了意外错误，耐久性无法证实: {type(e).__name__}: {e}",
                revision=archive.revision,
                path=str(target),
            ) from e
        return SaveResult(
            world_id=archive.world_id,
            path=str(target),
            revision=archive.revision,
            bytes_written=len(payload),
            directory_synced=supported,
            directory_sync_supported=supported,
        )

    def seal(self, archive: WorldArchive) -> WorldArchive:
        """把已满的整段写成新分卷（每卷：临时文件 → fsync → 改名），最后同步一次
        history/ 目录。任何一步失败都抛 StorageError，world.json 一个字节不动。
        """
        if not isinstance(archive, WorldArchive):
            raise StorageError("只能封存 WorldArchive")
        try:
            chunks = archive.seal_plan()
        except (ArchiveError, KeyError, TypeError, ValueError) as e:
            raise StorageError(f"世界 '{archive.world_id}' 的活动段切不开: {e}") from e
        if not chunks:
            return archive
        world_dir = self._world_dir(archive.world_id)
        history = self.history_dir(archive.world_id)
        self._ensure_dir(world_dir, archive.world_id)
        created = not history.is_dir()
        self._ensure_dir(history, archive.world_id)
        new_segments: List[EventSegment] = []
        index = len(archive.segments)
        for chunk in chunks:
            index += 1
            try:
                blob = encode_segment(chunk)
            except (TypeError, ValueError) as e:
                raise StorageError(f"分卷序列化失败: {e}") from e
            segment = describe_segment(index, chunk, blob)
            self._write_segment(archive.world_id, history, segment, blob)
            new_segments.append(segment)
        # 分卷的改名必须先耐久，world.json 才能指着它们。平台说"不支持"时照常
        # 往下走，但这份信封要如实带着"分卷目录没同步过"，不许被报成已同步。
        synced = self._sync_history(history, archive.world_id)
        if created:
            synced = self._sync_history(world_dir, archive.world_id) and synced
        return replace(archive.sealed(new_segments), history_synced=synced)

    def _write_segment(
        self, world_id: str, history: Path, segment: EventSegment, blob: bytes
    ) -> None:
        target = self._segment_file(world_id, segment.file)
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(history), prefix=segment.file + ".", suffix=self.TMP_SUFFIX
            )
        except OSError as e:
            raise StorageError(
                f"世界 '{world_id}' 的分卷临时文件建不出来: {e}"
            ) from e
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            # 同序号的清单外残留（上一次没写成的封存）在这里被覆盖。
            os.replace(tmp_name, target)
        except BaseException as e:
            residue = self._discard(tmp_name)
            raise StorageError(
                f"世界 '{world_id}' 的分卷 {segment.file} 写入失败，world.json 未动: "
                f"{type(e).__name__}: {e}"
                + (f"（残留待处理: {', '.join(residue)}）" if residue else ""),
                residue=residue,
            ) from e

    @classmethod
    def _sync_history(cls, directory: Path, world_id: str) -> bool:
        """分卷所在目录的同步，返回"这里支不支持"。真失败就是这次封存失败。"""
        try:
            dir_fd = os.open(str(directory), os.O_RDONLY)
        except OSError as e:
            if cls._is_unsupported(e):
                return False
            raise StorageError(
                f"世界 '{world_id}' 的分卷目录打不开，新分卷的耐久性无法证实: {e}"
            ) from e
        supported = True
        try:
            os.fsync(dir_fd)
        except OSError as e:
            if not cls._is_unsupported(e):
                raise StorageError(
                    f"世界 '{world_id}' 的分卷目录同步失败，新分卷的耐久性无法证实: {e}"
                ) from e
            supported = False
        finally:
            try:
                os.close(dir_fd)
            except OSError as e:
                raise StorageError(
                    f"世界 '{world_id}' 关闭分卷目录时出错，新分卷的耐久性无法证实: {e}"
                ) from e
        return supported

    def acquire(self, world_id: str) -> OwnershipHandle:
        name = validate_world_id(world_id)
        directory = self._world_dir(name)
        self._ensure_dir(directory, name)
        return acquire_world(directory / self.LOCK_NAME, name)

    # ── 收尾 ────────────────────────────────────────────────────────────
    @staticmethod
    def _ensure_dir(directory: Path, world_id: str) -> None:
        """建出世界目录。建不出来就翻译成 StorageError。

        文件系统会以各种方式说不：这个位置上已经放着一个普通文件、根是只读的、
        磁盘满了。调用方只该 catch 这一层的错误，不该被迫去 catch 十种 OSError ——
        那种"漏出去的原始异常"最后总是变成上层某处一个不该有的 except Exception。
        """
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise StorageError(
                f"世界 '{world_id}' 的存档目录建不出来（{directory}）: {e}"
            ) from e

    @staticmethod
    def _discard(tmp_name: str) -> Tuple[str, ...]:
        """收拾临时文件。收拾不掉就把它报上去，不假装干净。"""
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            return ()
        except OSError:
            return (tmp_name,)
        return ()

    @classmethod
    def _sync_dir(cls, directory: Path, archive: WorldArchive) -> bool:
        """把改名所在的目录项刷下去。返回"这个平台支不支持"。

        两种失败必须分开，因为它们对调用方意味着完全相反的事：

        * **不支持**（见 _UNSUPPORTED_SYNC_ERRNOS）—— 这个平台或文件系统压根
          不提供目录同步。那是"这里拿不到更强的保证"，不是"这次同步失败了"。
          返回 False，保存照常算成功，SaveResult 里写明。
        * **真的失败**（EIO、ENOSPC、EROFS……）—— 磁盘在出问题。新存档已经可见，
          但那次改名可能扛不住掉电。抛 ArchiveNotDurable，绝不吞掉：吞掉就等于
          在一块正在坏的盘上宣布"存好了"。
        """
        try:
            dir_fd = os.open(str(directory), os.O_RDONLY)
        except OSError as e:
            if cls._is_unsupported(e):
                # Windows 上根本打不开目录句柄，属于"这里就没有这个能力"。
                return False
            raise ArchiveNotDurable(
                f"世界 '{archive.world_id}' 的第 {archive.revision} 版已经写在"
                f"磁盘上、读得回来，但存档目录打不开、那次改名的耐久性无法证实"
                f"（掉电之后可能回到上一版）: {e}",
                revision=archive.revision,
                path=str(directory / cls.ARCHIVE_NAME),
            ) from e
        try:
            os.fsync(dir_fd)
        except OSError as e:
            if cls._is_unsupported(e):
                return False
            raise ArchiveNotDurable(
                f"世界 '{archive.world_id}' 的第 {archive.revision} 版已经写在"
                f"磁盘上、读得回来，但存档目录同步失败、那次改名的耐久性无法"
                f"证实（掉电之后可能回到上一版）: {e}",
                revision=archive.revision,
                path=str(directory / cls.ARCHIVE_NAME),
            ) from e
        finally:
            try:
                os.close(dir_fd)
            except OSError as e:
                # 关闭目录句柄报错（EIO）同样说明这次同步不可信。
                raise ArchiveNotDurable(
                    f"世界 '{archive.world_id}' 的第 {archive.revision} 版已经写在"
                    f"磁盘上，但关闭存档目录时出错，耐久性无法证实: {e}",
                    revision=archive.revision,
                    path=str(directory / cls.ARCHIVE_NAME),
                ) from e
        return True

    @staticmethod
    def _is_unsupported(error: OSError) -> bool:
        """这个 OSError 是"这里没有这个能力"，还是"这次操作失败了"。"""
        return error.errno in _UNSUPPORTED_SYNC_ERRNOS
