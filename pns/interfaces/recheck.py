# pns/interfaces/recheck.py — 长连接的身份复核
#
# 中间件只在连接建立时认一次身份。连接活得比会话久（握手以后等权限过期、撤权、
# 降档），所以开着的连接要自己定期重认。Accord 的流和 /ws/run 共用这里的名额池。
import threading
from typing import Callable, Mapping, Optional

import anyio

# 整个进程里同时在查账户库的复核最多几次（见 RecheckSlots）。
MAX_RECHECKS_IN_FLIGHT = 8

# 复核名额满了时隔多久再试。
_RETRY_STEP = 0.01


class RecheckSlots:
    """流复核的在途名额，整个进程共用。

    复核超时以后流不再等它，但线程还在账户库那儿排着；名额要等那个线程真正
    退出才还。所以账户库卡住的时候，卡在里面的复核线程最多这么多条，不会随着
    一条条超时的流越积越多。名额是在线程里拿的：拿不到就立刻返回，由调用方
    稍后再试，不会有一个名额拿到了、线程却没跑起来、永远还不回去。
    """

    BUSY = object()

    def __init__(self, limit: int = MAX_RECHECKS_IN_FLIGHT) -> None:
        self._lock = threading.Lock()
        self._free = limit

    def run(self, fn, *args):
        with self._lock:
            if self._free <= 0:
                return self.BUSY
            self._free -= 1
        try:
            return fn(*args)
        finally:
            with self._lock:
                self._free += 1


def _has_scope(auth, scope: Mapping, needed: Optional[str]) -> bool:
    # 查不了就是认不出来：账户库出错时不能放行一次要花模型额度的调用。
    try:
        principal = auth.resolve(scope)
    except Exception:
        return False
    if principal is None:
        return False
    return needed is None or principal.has(needed)


class IdentityGate:
    """一条连接的"这个主体现在还能做这件事吗"。

    结果缓存 `recheck_seconds`，从查询**开始**算起。查询超过 `timeout_seconds`
    按拒绝处理，超时不刷新缓存。一旦拒绝就一直拒绝：这条连接的授权只会失去，
    不会因为一次恢复的查询又长回来。
    """

    def __init__(
        self,
        auth,
        scope: Mapping,
        needed: Optional[str],
        slots: RecheckSlots,
        *,
        recheck_seconds: float = 5.0,
        timeout_seconds: float = 2.0,
        clock: Callable[[], float] = anyio.current_time,
    ) -> None:
        self._auth = auth
        self._scope = scope
        self._needed = needed
        self._slots = slots
        self._recheck = recheck_seconds
        self._timeout = timeout_seconds
        self._clock = clock
        self._verified_until: Optional[float] = None
        self.denied = False

    async def __call__(self) -> bool:
        if self.denied:
            return False
        started = self._clock()
        if self._verified_until is not None and started < self._verified_until:
            return True
        allowed = False
        with anyio.move_on_after(self._timeout):
            while True:
                # 超时就不等那次查询了：线程自己跑完，结果丢掉，名额到那时才还。
                result = await anyio.to_thread.run_sync(
                    self._slots.run, _has_scope, self._auth, self._scope, self._needed,
                    abandon_on_cancel=True,
                )
                if result is not RecheckSlots.BUSY:
                    allowed = result
                    break
                await anyio.sleep(_RETRY_STEP)
        if not allowed:
            self.denied = True
            return False
        self._verified_until = started + self._recheck
        return True


__all__ = ["IdentityGate", "MAX_RECHECKS_IN_FLIGHT", "RecheckSlots"]
