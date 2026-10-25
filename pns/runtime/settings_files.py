# pns/runtime/settings_files.py — 面板设置文件的写盘合同（CFG-1 §4.1）
#
# 临时文件（创建即 0600）→ fsync → rename → 目录 fsync。结果分三档，跟存档
# （pns/runtime/persistence/store.py）的区分一致：
#
#   * NotPublished —— rename 之前失败。临时文件已收拾，目标文件**确实**没变。
#   * PublishedNotDurable —— rename 成功、目录同步失败。新内容已经是目标文件，
#     不能再说"原文件不变"；调用方要按"已经发生"处理（走事务恢复）。
#   * 正常返回 —— 已耐久（或这个平台不提供目录同步，返回值说明）。
#
# 目录不存在时顺手建出来，并把每一层新目录在父目录里的目录项也刷下去；
# 只刷目标所在目录证明不了新目录本身还在。
#
# 删除同样三档：unlink 失败 = 没删；unlink 成功、目录同步失败 = 已删但不耐久。
#
# 异常消息是固定措辞：这些文件里装着 token，任何从文件内容派生的文字都不过边界。
import errno
import os
import tempfile
from pathlib import Path

FILE_MODE = 0o600

_UNSUPPORTED_SYNC_ERRNOS = frozenset(
    value
    for value in (
        getattr(errno, name, None)
        for name in ("EINVAL", "ENOSYS", "ENOTSUP", "EOPNOTSUPP", "EISDIR")
    )
    if value is not None
)


class SettingsWriteError(RuntimeError):
    """写设置文件失败的基类。"""


class NotPublished(SettingsWriteError):
    """rename / unlink 没有发生：目标文件还是之前的样子。"""


class PublishedNotDurable(SettingsWriteError):
    """rename / unlink 已经发生，但目录同步失败：掉电后可能退回之前。"""


def sync_dir(directory: Path) -> bool:
    """把目录项刷下去。返回这个平台支不支持；真失败抛 OSError。"""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError as e:
        if e.errno in _UNSUPPORTED_SYNC_ERRNOS:
            return False
        raise
    try:
        os.fsync(fd)
    except OSError as e:
        if e.errno in _UNSUPPORTED_SYNC_ERRNOS:
            return False
        raise
    finally:
        os.close(fd)
    return True


def _ensure_dir(directory: Path) -> bool:
    """建出缺的各层目录并刷父目录项。返回目录同步是否被平台支持；失败抛 OSError。"""
    missing = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    supported = True
    for created in reversed(missing):
        supported = sync_dir(created.parent) and supported
    return supported


def durable_write(path: Path, payload: bytes) -> bool:
    """按三档合同写一个 0600 文件。返回目录同步是否被平台支持。"""
    path = Path(path)
    directory = path.parent
    try:
        supported = _ensure_dir(directory)
        fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=f".{path.name}.", suffix=".tmp")
    except OSError:
        raise NotPublished(f"{path.name} 的临时文件建不出来") from None
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), FILE_MODE)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise NotPublished(f"{path.name} 没写成，磁盘上还是之前的样子") from None
    try:
        return sync_dir(directory) and supported
    except OSError:
        raise PublishedNotDurable(f"{path.name} 已经写上，但耐久性无法证实") from None


def durable_unlink(path: Path) -> bool:
    """按三档合同删一个文件。本来就不存在算已删（仍做目录同步）。"""
    path = Path(path)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        raise NotPublished(f"{path.name} 没删掉") from None
    try:
        return sync_dir(path.parent)
    except OSError:
        raise PublishedNotDurable(f"{path.name} 已经删掉，但耐久性无法证实") from None
