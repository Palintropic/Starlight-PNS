# pns/runtime/persistence/ownership.py — 一个世界只有一个拥有者
#
# 两个进程同时跑同一个世界，后果不是"存档写乱了"这么轻：它们各自推进同一个
# 世界的时钟、各自处理同一批到期资格、各自往同一条历史里提交，然后轮流用自己
# 那份内存状态覆盖对方的存档。事后没有任何办法判断哪一份是真的。
#
# 所以所有权是**两道**闸，缺一不可：
#
#   1. **进程内注册表**（按锁文件的规范路径记账）。它挡住同一个进程里开两个
#      句柄 —— 包括"两个 FileWorldStore 指着同一个存档根"这种情况，因为身份
#      是那条路径，不是某个 store 实例，也不是一个光秃秃的 world_id。
#   2. **文件锁**（`fcntl.flock`，独占非阻塞）。它挡住别的进程，而且这道闸的
#      判据由内核给：**锁随着持有它的进程一起消失**。所以"上一个拥有者还活着
#      吗"这个问题永远不需要靠 pid 猜 —— 用 pid 猜会在两个方向上都错：pid 被
#      复用时会误判成"活着"（世界从此永远锁死），进程恰好在检查那一瞬间退出
#      时会误判成"死了"（两个拥有者）。
#
# 陈旧拥有者的恢复因此是**自动且可证的**：上一个拥有者崩了，内核已经放开了它
# 的锁，我们能拿到；锁文件里还留着它的记录，于是我们知道自己接管的是一个崩掉
# 的世界，并如实报告（recovered_from）。它干净退出的话，记录会被改成
# "released"，接管时就没有什么需要报告的。任何时候都**不存在**"抢走一个活着的
# 拥有者"这条路径 —— 那需要内核先把锁给我们，而它不会。
#
# 明确不做的事：不跨主机（NFS 上的 flock 不可靠，这里只保证本地文件系统）、
# 不做租约续期线程、不做分布式协调。跨主机是 P12 的非目标。
import json
import os
import socket
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

try:  # pragma: no cover - 平台差异
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


class OwnershipError(RuntimeError):
    """所有权操作失败。"""


class WorldAlreadyOwned(OwnershipError):
    """这个世界已经有拥有者了 —— 本进程里的，或者另一个活着的进程。"""


class OwnershipUnsupported(OwnershipError):
    """当前平台给不出跨进程独占锁，因此这里拒绝假装有。"""


@dataclass(frozen=True)
class OwnerRecord:
    """锁文件里那条给人看的记录。它是**诊断**，不是判据。

    判据永远是文件锁本身。这条记录的用处是回答"刚才是谁"，比如拒绝一次抢占
    时报出对方的 pid，或者接管一个崩掉的世界时说清楚接管的是谁。
    """

    world_id: str
    pid: int
    host: str
    acquired_at: str
    renewed_at: str
    state: str = "held"  # held / released

    def to_dict(self) -> Dict:
        return {
            "world_id": self.world_id,
            "pid": self.pid,
            "host": self.host,
            "acquired_at": self.acquired_at,
            "renewed_at": self.renewed_at,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, payload) -> "OwnerRecord":
        if not isinstance(payload, dict):
            raise OwnershipError("所有权记录必须是字典")
        try:
            return cls(
                world_id=str(payload["world_id"]),
                pid=int(payload["pid"]),
                host=str(payload["host"]),
                acquired_at=str(payload["acquired_at"]),
                renewed_at=str(payload.get("renewed_at", payload["acquired_at"])),
                state=str(payload.get("state", "held")),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise OwnershipError(f"所有权记录读不出来: {e}") from e


# 本进程持有的世界，按锁文件的规范路径记账。刻意只有一个空字典和一把锁 ——
# 导入这个模块不碰磁盘、不建目录、不拿任何锁（有子进程测试盯着）。
_REGISTRY_LOCK = threading.Lock()
_OWNED: Dict[str, Optional[OwnerRecord]] = {}


def owned_world_paths() -> Tuple[str, ...]:
    """本进程此刻持有的世界锁路径。给状态面和测试用。"""
    with _REGISTRY_LOCK:
        return tuple(sorted(_OWNED))


class OwnershipHandle:
    """持有中的所有权。释放之前，这个世界属于本进程的这一个句柄。"""

    def __init__(
        self,
        *,
        world_id: str,
        path: Path,
        fd: int,
        owner: OwnerRecord,
        recovered_from: Optional[OwnerRecord],
        key: str,
    ) -> None:
        self._world_id = world_id
        self._path = path
        self._fd = fd
        self._owner = owner
        self._recovered_from = recovered_from
        self._key = key
        self._held = True
        self._lock = threading.Lock()

    @property
    def world_id(self) -> str:
        return self._world_id

    @property
    def path(self) -> Path:
        return self._path

    @property
    def owner(self) -> OwnerRecord:
        return self._owner

    @property
    def recovered_from(self) -> Optional[OwnerRecord]:
        """上一个拥有者的记录，仅当它是**崩掉**的时候。

        干净释放过的世界这里是 None：没有什么需要恢复，也没有什么需要报告。
        """
        return self._recovered_from

    @property
    def held(self) -> bool:
        return self._held

    def verify(self) -> None:
        """确认这份所有权此刻**仍然**成立。不成立就响亮失败。

        锁是挂在一个 inode 上的，不是挂在路径上的。所以有人把锁文件删掉、
        或者用一个新文件替换掉它之后，会出现这样一幕：我们手上的 fd 还锁着
        一个已经没人能找到的 inode，而下一个进程在**新的**文件上一拿就拿到 ——
        两个进程同时"拥有"同一个世界，各自往同一份存档上写。

        这一步防不住那次删除（谁都防不住），但它把"两个写手静静地互相覆盖"
        变成"第二个写手一落笔就响亮失败"。每次写存档之前都问一遍。
        """
        with self._lock:
            if not self._held:
                raise OwnershipError(
                    f"世界 '{self._world_id}' 的所有权已经释放了"
                )
            try:
                mine = os.fstat(self._fd)
                theirs = os.stat(str(self._path))
            except OSError as e:
                raise OwnershipError(
                    f"世界 '{self._world_id}' 的所有权锁不见了（{self._path}）——"
                    "别的进程现在可以拿到这个世界，本进程不能再写它"
                ) from e
            if (mine.st_dev, mine.st_ino) != (theirs.st_dev, theirs.st_ino):
                raise OwnershipError(
                    f"世界 '{self._world_id}' 的所有权锁已经被换成了另一个文件"
                    f"（{self._path}）—— 本进程手上这把锁不再代表这个世界"
                )

    def renew(self) -> OwnerRecord:
        """刷新记录里的时间戳。锁本身不需要续期 —— 它跟进程同生死。"""
        with self._lock:
            if not self._held:
                raise OwnershipError(
                    f"世界 '{self._world_id}' 的所有权已经释放了，不能续期"
                )
            self._owner = OwnerRecord(
                world_id=self._owner.world_id,
                pid=self._owner.pid,
                host=self._owner.host,
                acquired_at=self._owner.acquired_at,
                renewed_at=datetime.now().isoformat(),
            )
            _write_record(self._fd, self._owner)
            return self._owner

    def release(self) -> None:
        """归还所有权。幂等。

        顺序是刻意的：先把记录改成 released（这样下一个拥有者知道上一个是
        干净走的），再解锁、关 fd、从注册表里销号。写记录失败也照样解锁 ——
        锁必须还回去，最坏的后果只是下一个拥有者以为上一个是崩的。
        """
        with self._lock:
            if not self._held:
                return
            self._held = False
            try:
                _write_record(
                    self._fd,
                    OwnerRecord(
                        world_id=self._owner.world_id,
                        pid=self._owner.pid,
                        host=self._owner.host,
                        acquired_at=self._owner.acquired_at,
                        renewed_at=datetime.now().isoformat(),
                        state="released",
                    ),
                )
            except OSError:
                pass
            finally:
                try:
                    if fcntl is not None:
                        fcntl.flock(self._fd, fcntl.LOCK_UN)
                finally:
                    try:
                        os.close(self._fd)
                    except OSError:
                        pass
                    with _REGISTRY_LOCK:
                        _OWNED.pop(self._key, None)

    def to_dict(self) -> Dict:
        return {
            "world_id": self._world_id,
            "path": str(self._path),
            "held": self._held,
            "owner": self._owner.to_dict(),
            "recovered_from": (
                self._recovered_from.to_dict()
                if self._recovered_from is not None
                else None
            ),
        }


def acquire_world(lock_path, world_id: str) -> OwnershipHandle:
    """拿下一个世界的独占所有权。拿不到就抛 WorldAlreadyOwned。

    进程内注册表**先**登记再去拿文件锁：两个线程同时进来时，输的那个在第一道
    闸就被挡住，不会两个都跑去 open 同一个文件。文件锁失败时把登记撤掉。
    """
    path = Path(lock_path)
    key = os.path.realpath(path)
    with _REGISTRY_LOCK:
        if key in _OWNED:
            existing = _OWNED[key]
            raise WorldAlreadyOwned(
                f"世界 '{world_id}' 已经被本进程持有"
                + (f"（{existing.acquired_at} 起）" if existing is not None else "")
                + " —— 同一个世界不能在一个进程里开两次"
            )
        _OWNED[key] = None
    try:
        handle = _acquire_file_lock(path, world_id, key)
    except BaseException:
        with _REGISTRY_LOCK:
            _OWNED.pop(key, None)
        raise
    with _REGISTRY_LOCK:
        _OWNED[key] = handle.owner
    return handle


def _acquire_file_lock(path: Path, world_id: str, key: str) -> OwnershipHandle:
    if fcntl is None:  # pragma: no cover - Windows
        raise OwnershipUnsupported(
            "当前平台没有 fcntl.flock，给不出跨进程独占所有权。P12 拒绝在"
            "只有进程内检查的情况下假装一个世界是独占的。"
        )
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            previous = _read_record(fd)
            raise WorldAlreadyOwned(
                f"世界 '{world_id}' 正被另一个活着的拥有者持有"
                + (
                    f"（pid {previous.pid} @ {previous.host}，"
                    f"{previous.acquired_at} 起）"
                    if previous is not None
                    else ""
                )
                + "。锁是内核给的，所以这不是猜测；要接管必须先让那个进程退出。"
            ) from e
        # 锁到手了。文件里如果还留着一条 held 记录，说明上一个拥有者是崩掉的
        # （干净退出会把它改成 released）—— 内核已经替我们证明了它不在了。
        previous = _read_record(fd)
        recovered = previous if previous is not None and previous.state == "held" else None
        now = datetime.now().isoformat()
        owner = OwnerRecord(
            world_id=world_id,
            pid=os.getpid(),
            host=socket.gethostname(),
            acquired_at=now,
            renewed_at=now,
        )
        _write_record(fd, owner)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return OwnershipHandle(
        world_id=world_id,
        path=path,
        fd=fd,
        owner=owner,
        recovered_from=recovered,
        key=key,
    )


def _read_record(fd: int) -> Optional[OwnerRecord]:
    """读锁文件里那条记录。读不出来就当没有 —— 它只是诊断信息。"""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        blob = os.read(fd, 64 * 1024)
    except OSError:
        return None
    if not blob.strip():
        return None
    try:
        return OwnerRecord.from_dict(json.loads(blob.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, OwnershipError):
        return None


def _write_record(fd: int, record: OwnerRecord) -> None:
    blob = json.dumps(record.to_dict(), ensure_ascii=False).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, blob)
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - 少数文件系统不支持
        pass
