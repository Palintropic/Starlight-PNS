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
#   * 崩溃恢复读到的是**最后一次成功 replace 的那一份**。残留的临时文件被
#     报告，但绝不会被当成存档读回来。
#
# 明确不做的事：没有 WAL、没有事件重放、没有多版本历史、没有数据库、没有云。
# "最后一次成功 checkpoint 之后的内存工作会丢"是这一层的**真实**保证边界，
# 不许对外说成别的。
#
# 路径安全：world_id 先过 naming.validate_world_id，然后再用 realpath 判一次
# 目录是不是真的落在存档根之下。两道是刻意的 —— 第一道挡文本，第二道挡软链，
# 它们挡的不是同一种攻击。
import json
import os
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from pns.runtime.persistence.archive import ArchiveCorrupt, ArchiveError, WorldArchive
from pns.runtime.persistence.naming import WorldIdError, validate_world_id
from pns.runtime.persistence.ownership import OwnershipHandle, acquire_world

__all__ = [
    "ArchiveNotFound",
    "FileWorldStore",
    "SaveResult",
    "StorageError",
    "WorldIdError",
    "WorldStore",
]


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


@dataclass(frozen=True)
class SaveResult:
    """一次成功保存的结果。"""

    world_id: str
    path: str
    revision: int
    bytes_written: int
    residue: Tuple[str, ...] = ()


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
    def load(self, world_id: str) -> WorldArchive:
        """读回最后一次成功保存的那一份。"""

    @abstractmethod
    def save(self, archive: WorldArchive) -> SaveResult:
        """原子地写下一份完整存档。"""

    @abstractmethod
    def residue(self, world_id: str) -> Tuple[str, ...]:
        """留在磁盘上的、不完整的临时文件。"""

    @abstractmethod
    def acquire(self, world_id: str) -> OwnershipHandle:
        """拿下这个世界的独占所有权。"""


class FileWorldStore(WorldStore):
    """把世界存档放在一个配置好的存档根之下的文件系统实现。

    布局刻意平坦、可读、可以用 `ls` 看懂：

        <root>/<world_id>/world.json          最后一次成功保存的完整存档
        <root>/<world_id>/OWNER.lock          所有权（flock + 一条给人看的记录）
        <root>/<world_id>/world.json.*.tmp    写到一半的残留，永远不会被读回来

    构造这个对象**不碰磁盘**：根目录在第一次真正要写的时候才建。
    """

    ARCHIVE_NAME = "world.json"
    LOCK_NAME = "OWNER.lock"
    TMP_SUFFIX = ".tmp"

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

    def load(self, world_id: str) -> WorldArchive:
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
        return archive

    def residue(self, world_id: str) -> Tuple[str, ...]:
        directory = self._world_dir(world_id)
        if not directory.is_dir():
            return ()
        return tuple(
            sorted(
                str(child)
                for child in directory.iterdir()
                if child.name.endswith(self.TMP_SUFFIX)
            )
        )

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
        self._sync_dir(directory)
        return SaveResult(
            world_id=archive.world_id,
            path=str(target),
            revision=archive.revision,
            bytes_written=len(payload),
        )

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

    @staticmethod
    def _sync_dir(directory: Path) -> None:
        """把目录项也刷下去。不支持就算了 —— 它是加固，不是原子性的前提。"""
        try:
            dir_fd = os.open(str(directory), os.O_RDONLY)
        except OSError:  # pragma: no cover - 平台差异
            return
        try:
            os.fsync(dir_fd)
        except OSError:  # pragma: no cover - 平台差异
            pass
        finally:
            os.close(dir_fd)
