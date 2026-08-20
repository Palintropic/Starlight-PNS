# pns/runtime/reload.py — 配置重载边界
#
# 后台点一次「重新加载配置」，这里按固定顺序做完五件事：
#
#     1. 拿到重载互斥锁（拿不到就说明已经有人在重载，直接拒绝，不排队）
#     2. 关闭准入闸门 —— 从这一刻起不再接受新 session
#     3. 明确停止所有正在跑的 session
#     4. 从磁盘完整构建并校验一份新的 ContentRegistry
#     5. 成功就原子替换全局引用；失败就原样保留旧的
#
# 无论成功失败，最后都会重新打开闸门恢复服务。失败时活着的仍然是
# last-known-good 那一份，服务继续能开新 session。
#
# 明确不做的事：不监听文件、不滚动更新、不并行多版本、不做分布式同步、
# 不做数据库版本系统，也绝不 importlib.reload 任何模块（代码改动属于
# cold update，必须停服替换重启）。
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from pns.runtime.content_registry import (
    ConfigValidationError,
    ContentRegistry,
    build_content_registry,
)


class SessionAdmissionClosed(Exception):
    """重载正在进行，准入闸门关着，这次不接新 session。"""


class SessionSupervisor:
    """记账：现在有哪些 session 活着，以及还接不接新的。

    只做记账和发停止信号，不负责等待 —— 会话拿的是自己那份 ContentRegistry
    快照，所以就算它还要几秒才走完当前这一轮，也不会读到新配置。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._accepting = True
        self._live: Dict[str, object] = {}

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

    def release(self, session_id: str) -> None:
        with self._lock:
            self._live.pop(session_id, None)

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
    error: Optional[str] = None
    registry: Optional[Dict] = None

    def to_dict(self) -> Dict:
        return {
            "status": self.status,
            "revision": self.revision,
            "finished_at": self.finished_at,
            "stopped_sessions": list(self.stopped_sessions),
            "error": self.error,
            "registry": self.registry,
        }


class ConfigBoundary:
    """持有当前生效的 ContentRegistry，并且是唯一能替换它的地方。"""

    def __init__(self, supervisor: SessionSupervisor) -> None:
        self._supervisor = supervisor
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
            "accepting_sessions": self._supervisor.accepting,
            "live_sessions": self._supervisor.live_session_ids(),
            "registry": active.to_dict() if active is not None else None,
            "last_reload": last.to_dict() if last is not None else None,
        }

    # ── 写 ────────────────────────────────────────────────────────────
    def reload(self, reason: str = "配置重新加载") -> ReloadResult:
        if not self._reload_lock.acquire(blocking=False):
            # 已经有一次重载在跑。第二次不排队、不并发，直接告诉调用方在忙。
            with self._swap_lock:
                revision = self._active.revision if self._active else 0
            return ReloadResult(
                status="busy",
                revision=revision,
                finished_at=_now(),
                error="已有一次配置重载正在进行，请等它结束。",
            )

        try:
            self._supervisor.close_gate()
            stopped = self._supervisor.stop_all(reason)
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
                with self._swap_lock:
                    active = self._active
                    result = ReloadResult(
                        status="failed",
                        revision=active.revision if active is not None else 0,
                        finished_at=_now(),
                        stopped_sessions=stopped,
                        error=detail,
                        registry=active.to_dict() if active is not None else None,
                    )
                    self._last_result = result
                return result

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
            self._reload_lock.release()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# 进程内的单例边界。测试可以自己造独立实例，不必碰这两个。
SUPERVISOR = SessionSupervisor()
BOUNDARY = ConfigBoundary(SUPERVISOR)


def active_registry() -> ContentRegistry:
    return BOUNDARY.active()
