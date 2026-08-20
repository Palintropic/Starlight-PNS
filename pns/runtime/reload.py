# pns/runtime/reload.py — 配置重载边界
#
# 后台点一次「重新加载配置」，这里按固定顺序做完五件事：
#
#     1. 拿到重载互斥锁（拿不到就说明已经有人在重载，直接拒绝，不排队）
#     2. 关闭准入闸门 —— 从这一刻起不再接受新 session
#     3. 停止所有正在跑的 session，并**等到它们确认退出**
#     4. 从磁盘完整构建并校验一份新的 ContentRegistry
#     5. 成功就原子替换全局引用；失败就原样保留旧的
#
# 第 3 步的等待是硬要求，不是保险措施：没有它，旧配置的 session 会和新配置的
# session 短暂并存，"整体切换"就成了一句空话。等不到（超过 stop_timeout）就判
# 这次重载失败，绝不切换 —— 宁可继续用旧配置，也不要两份配置同时在跑。
#
# 无论成功失败，最后都会重新打开闸门恢复服务。失败时活着的仍然是
# last-known-good 那一份，服务继续能开新 session。
#
# 明确不做的事：不监听文件、不滚动更新、不并行多版本、不做分布式同步、
# 不做数据库版本系统，也绝不 importlib.reload 任何模块（代码改动属于
# cold update，必须停服替换重启）。
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from pns.runtime.content_registry import (
    ConfigValidationError,
    ContentRegistry,
    build_content_registry,
)

# 等正在跑的 session 退出的上限。一轮对话要打两次模型 API，给得太短会让重载在
# 正常负载下就失败；给得太长会让后台按钮看起来卡死。
DEFAULT_STOP_TIMEOUT = 60.0


class SessionAdmissionClosed(Exception):
    """重载正在进行，准入闸门关着，这次不接新 session。"""


class SessionSupervisor:
    """记账：现在有哪些 session 活着，以及还接不接新的。

    除了记账和发停止信号，还负责回答"现在一个都不剩了吗" —— 重载靠它确认旧
    session 真的退干净了，才允许切换配置。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._accepting = True
        self._live: Dict[str, object] = {}
        # 没有活 session 时置位。重载在这上面等，所以它必须严格跟 _live 同步维护。
        self._idle = threading.Event()
        self._idle.set()

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting

    def live_session_ids(self) -> List[str]:
        with self._lock:
            return sorted(self._live)

    def admit(self, session_id: str, session) -> None:
        """登记一个新 session。闸门关着就抛 SessionAdmissionClosed。

        这是准入的**唯一**权威判断点：调用方可以先看 accepting 做快速拒绝，
        但真正决定的是这里，所以「检查完到登记之间闸门关了」这个竞态不存在。
        """
        with self._lock:
            if not self._accepting:
                raise SessionAdmissionClosed(
                    "配置正在重新加载，暂时不能开始新会话，请稍后重试。"
                )
            self._live[session_id] = session
            self._idle.clear()

    def release(self, session_id: str) -> None:
        with self._lock:
            self._live.pop(session_id, None)
            if not self._live:
                self._idle.set()

    def wait_until_idle(self, timeout: float) -> bool:
        """等到一个活 session 都不剩。超时返回 False。

        调用方必须先关闸门：闸门开着的话新 session 会不停进来，这里可能永远
        等不到 idle。
        """
        return self._idle.wait(timeout)

    def close_gate(self) -> None:
        with self._lock:
            self._accepting = False

    def open_gate(self) -> None:
        with self._lock:
            self._accepting = True

    def stop_all(self, reason: str) -> Tuple[str, ...]:
        """给所有活着的 session 发停止信号，返回被停掉的 session id。

        信号是同步打上的；会话在下一个轮次边界观察到并收尾。选轮次边界而不是
        当场掐断，是为了不破坏 P5 建立的提交边界 —— 一轮要么完整提交，要么
        根本没发生，不会留下半条事件。
        """
        with self._lock:
            targets = list(self._live.items())
        stopped = []
        for session_id, session in targets:
            request_stop = getattr(session, "request_stop", None)
            if request_stop is not None:
                request_stop(reason)
                stopped.append(session_id)
        return tuple(sorted(stopped))


@dataclass(frozen=True)
class ReloadResult:
    """一次重载尝试的结果。直接就是 /api/config/reload 的响应体。"""

    status: str  # "ok" | "failed" | "busy"
    revision: int
    finished_at: str
    stopped_sessions: Tuple[str, ...] = ()
    # 收到停止信号但在超时之内没有退出的 session。非空 ⇒ 这次重载必然是 failed。
    pending_sessions: Tuple[str, ...] = ()
    error: Optional[str] = None
    registry: Optional[Dict] = None

    def to_dict(self) -> Dict:
        return {
            "status": self.status,
            "revision": self.revision,
            "finished_at": self.finished_at,
            "stopped_sessions": list(self.stopped_sessions),
            "pending_sessions": list(self.pending_sessions),
            "error": self.error,
            "registry": self.registry,
        }


class ConfigBoundary:
    """持有当前生效的 ContentRegistry，并且是唯一能替换它的地方。"""

    def __init__(
        self,
        supervisor: SessionSupervisor,
        *,
        stop_timeout: float = DEFAULT_STOP_TIMEOUT,
    ) -> None:
        self._supervisor = supervisor
        self._stop_timeout = stop_timeout
        self._reload_lock = threading.Lock()  # 重载互斥：同一时刻只有一次重载
        self._swap_lock = threading.RLock()   # 保护 _active 的读写与首次构建
        self._active: Optional[ContentRegistry] = None
        self._revision = 0
        self._last_result: Optional[ReloadResult] = None

    # ── 读 ────────────────────────────────────────────────────────────
    @property
    def supervisor(self) -> SessionSupervisor:
        return self._supervisor

    def active(self) -> ContentRegistry:
        """当前生效的配置。首次访问时惰性构建（构建失败就抛，服务本来也跑不了）。"""
        with self._swap_lock:
            if self._active is None:
                self._revision += 1
                self._active = build_content_registry(revision=self._revision)
            return self._active

    def active_or_none(self) -> Optional[ContentRegistry]:
        with self._swap_lock:
            return self._active

    def status(self) -> Dict:
        with self._swap_lock:
            active = self._active
            last = self._last_result
        return {
            "reloading": self._reload_lock.locked(),
            "stop_timeout": self._stop_timeout,
            "accepting_sessions": self._supervisor.accepting,
            "live_sessions": self._supervisor.live_session_ids(),
            "registry": active.to_dict() if active is not None else None,
            "last_reload": last.to_dict() if last is not None else None,
        }

    # ── 写 ────────────────────────────────────────────────────────────
    def _fail(
        self,
        error: str,
        *,
        stopped: Tuple[str, ...] = (),
        pending: Tuple[str, ...] = (),
    ) -> "ReloadResult":
        """记录一次失败的重载。旧快照原封不动，revision 也不前进。"""
        with self._swap_lock:
            active = self._active
            result = ReloadResult(
                status="failed",
                revision=active.revision if active is not None else 0,
                finished_at=_now(),
                stopped_sessions=stopped,
                pending_sessions=pending,
                error=error,
                registry=active.to_dict() if active is not None else None,
            )
            self._last_result = result
        return result

    def _busy(self, note: str = "") -> ReloadResult:
        with self._swap_lock:
            revision = self._active.revision if self._active else 0
        return ReloadResult(
            status="busy",
            revision=revision,
            finished_at=_now(),
            error="已有一次配置重载正在进行，请等它结束。" + note,
        )

    def reload(self, reason: str = "配置重新加载") -> ReloadResult:
        if not self._reload_lock.acquire(blocking=False):
            # 已经有一次重载在跑。第二次不排队、不并发，直接告诉调用方在忙。
            return self._busy()
        try:
            return self._reload_locked(reason)
        finally:
            self._reload_lock.release()

    def write_and_reload(
        self,
        paths: Iterable[Path],
        write: Callable[[], None],
        *,
        reason: str,
    ) -> ReloadResult:
        """把「改配置文件」变成一次事务：新配置没生效，磁盘上就不留新内容。

        没有这层的话，World Editor 存一份过不了校验的配置会留下一个坏文件：运行中
        的进程靠 last-known-good 撑着看起来没事，但只要重启一次，服务就再也起不来了。
        所以顺序是 —— 记住旧内容 → 写候选 → 重载 → 成功保留、失败原子恢复。

        整段过程持有重载互斥锁。这不只是防两次保存打架：如果保存和重载能交错，
        另一次重载可能刚好读到本次的候选内容并把它切换上去，而这边却以为自己
        已经回滚了。拿不到锁就直接返回 busy，**一个字节都不写**。
        """
        if not self._reload_lock.acquire(blocking=False):
            return self._busy("（本次保存未写入任何内容）")
        try:
            paths = list(paths)
            originals: Dict[Path, Optional[bytes]] = {
                path: (path.read_bytes() if path.exists() else None) for path in paths
            }
            try:
                write()
            except BaseException:
                _restore(originals)
                raise

            try:
                result = self._reload_locked(reason)
            except BaseException:
                _restore(originals)
                raise

            if result.status != "ok":
                # 没生效就等于没发生：磁盘退回事务开始前的样子。
                _restore(originals)
            return result
        finally:
            self._reload_lock.release()

    def _reload_locked(self, reason: str) -> ReloadResult:
        """重载流程本体。调用方必须已经持有 _reload_lock。"""
        try:
            self._supervisor.close_gate()
            stopped = self._supervisor.stop_all(reason)

            # 等旧 session 真的退出。等不到就整件事作废：不构建、不切换。
            # 这是"新旧配置绝不并存"这条要求唯一的落实点。
            if not self._supervisor.wait_until_idle(self._stop_timeout):
                pending = tuple(self._supervisor.live_session_ids())
                return self._fail(
                    f"{len(pending)} 个会话在 {self._stop_timeout:g} 秒内没有停止"
                    f"（{'、'.join(pending)}），配置未切换，仍在使用上一份可用配置。",
                    stopped=stopped,
                    pending=pending,
                )

            try:
                with self._swap_lock:
                    next_revision = self._revision + 1
                # 构建在锁外进行：它是整个流程里最慢的一步，而且必须在完全
                # 成功之后才允许影响任何人。构建期间读 active() 的人拿到的
                # 依然是旧快照。
                candidate = build_content_registry(revision=next_revision)
            except Exception as e:
                # 校验失败和非预期异常一视同仁：这次重载不算数，绝不半途替换。
                detail = (
                    str(e)
                    if isinstance(e, ConfigValidationError)
                    else f"{type(e).__name__}: {e}"
                )
                return self._fail(detail, stopped=stopped)

            # 原子替换：只有这一行会改全局引用，且在锁内一次赋值完成。
            with self._swap_lock:
                self._revision = next_revision
                self._active = candidate
                result = ReloadResult(
                    status="ok",
                    revision=candidate.revision,
                    finished_at=_now(),
                    stopped_sessions=stopped,
                    registry=candidate.to_dict(),
                )
                self._last_result = result
            return result
        finally:
            # 成功也好失败也好，服务都必须恢复到能开新 session 的状态。
            self._supervisor.open_gate()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── 写盘 + 重载的文件级事务 ──────────────────────────────────────────────


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """同目录临时文件 + os.replace，保证读者只会看到旧内容或新内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _restore(originals: Dict[Path, Optional[bytes]]) -> None:
    for path, payload in originals.items():
        if payload is None:
            # 事务开始时这个文件还不存在，回滚就是让它继续不存在。
            path.unlink(missing_ok=True)
        else:
            _atomic_write_bytes(path, payload)


def write_and_reload(
    boundary: "ConfigBoundary",
    paths: Iterable[Path],
    write: Callable[[], None],
    *,
    reason: str,
) -> ReloadResult:
    """ConfigBoundary.write_and_reload 的函数式写法，方便路由层直接调用。"""
    return boundary.write_and_reload(paths, write, reason=reason)


# 进程内的单例边界。测试可以自己造独立实例，不必碰这两个。
SUPERVISOR = SessionSupervisor()
BOUNDARY = ConfigBoundary(SUPERVISOR)


def active_registry() -> ContentRegistry:
    return BOUNDARY.active()
