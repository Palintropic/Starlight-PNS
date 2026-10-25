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
# 目录链从一个已知耐久的锚点（默认是目标往上两层，即 data/）算起：每次写入
# 都把锚点到目标之间每一层在父目录里的目录项刷一遍，缺的顺手建出来。"目录
# 已经存在"不等于"目录项已经耐久"——上次同步失败后的重试、别人刚建好还没
# 刷的目录，看起来都是"存在"。只刷目标所在目录证明不了这一层本身还在。
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


def _ensure_dir_chain(directory: Path, anchor: Path) -> bool:
    """建出锚点之下缺的各层，并刷每一层的父目录项。返回同步是否都被平台支持；失败抛 OSError。"""
    parts = directory.relative_to(anchor).parts
    supported = True
    for depth in range(1, len(parts) + 1):
        current = anchor.joinpath(*parts[:depth])
        current.mkdir(mode=0o700, exist_ok=True)
        supported = sync_dir(current.parent) and supported
    return supported


def durable_write(path: Path, payload: bytes, *, anchor: Path = None) -> bool:
    """按三档合同写一个 0600 文件。返回目录同步是否被平台支持。

    anchor 是调用方保证已经耐久存在的目录（默认目标往上两层），必须是目标的祖先。
    """
    path = Path(path)
    directory = path.parent
    anchor = directory.parent if anchor is None else Path(anchor)
    if anchor != directory and anchor not in directory.parents:
        raise ValueError("anchor 必须是目标文件的祖先目录")
    try:
        supported = _ensure_dir_chain(directory, anchor)
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
