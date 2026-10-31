# pns/interfaces/accord.py — Accord feed 的 HTTP 边界（只读，默认关）
#
#     GET /api/accord/worlds
#     GET /api/accord/worlds/{world_id}/hello
#     GET /api/accord/worlds/{world_id}/events     text/event-stream
#
# 只在 `PNS_ACCORD_FEED` 打开时挂上（见 app.py）；关着时这几条路由根本不存在，
# 进程跟没有这个模块一样。
#
# 全是 GET，所以默认拒绝的守卫按方法给它们 `read`：observer 能用，没登录 401，
# 不进公开清单。刻意不开 WebSocket，也不开 MCP——那两样在这个应用里都要 `operate`。
#
# 一条流活着的时候，主体每隔几秒重新认一次：账户被停用、权威变了、会话被撤销，
# 流当场结束，而不是靠连上那一刻的身份一直推下去。每条流还有寿命上限，到点由
# 服务器关掉；EventSource 会带着 Last-Event-ID 自己重连，续传一条不漏。这两样
# 检查由看门狗做，不排在写出后面，也互不牵连：客户端读得慢、send() 卡住，或者
# 账户库被锁住、重认迟迟回不来，都照样按时掐断。
import os
import threading
from dataclasses import dataclass
from typing import Mapping, Optional

import anyio
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from pns.integrations.accord.feed import AccordFeed, Cursor, CursorError, FeedSession

from .accounts import SCOPE_READ
from .composition import WorldControlPlane
from .persistent_worlds import _error, _translated, get_control_plane
from .security import _env_flag

ENV_ACCORD_FEED = "PNS_ACCORD_FEED"

# 游标是 `<下标>.<16 位十六进制>`，再长的不可能是我们发的。
MAX_CURSOR_CHARS = 64

# 正在重认身份时，写的那一边隔多久看一眼认完没有；复核名额满了时隔多久再试。
_READY_STEP = 0.01

# 整个进程里同时在查账户库的流复核最多几次（见 RecheckSlots）。
MAX_RECHECKS_IN_FLIGHT = 8


def feed_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """开关看不懂就起不来，跟其它布尔配置一样（见 security._env_flag）。"""
    return _env_flag(os.environ if env is None else env, ENV_ACCORD_FEED, False)


@dataclass(frozen=True)
class FeedSettings:
    """一条事件流的节拍（秒）。"""

    poll_seconds: float = 1.0
    keepalive_seconds: float = 20.0
    clock_seconds: float = 60.0
    recheck_seconds: float = 5.0
    recheck_timeout_seconds: float = 2.0
    lifetime_seconds: float = 1800.0
    retry_ms: int = 3000


router = APIRouter(prefix="/api/accord", tags=["accord"])


def get_feed(request: Request) -> AccordFeed:
    feed = getattr(request.app.state, "accord_feed", None)
    if feed is None:  # pragma: no cover - 路由挂上时 create_app 总会装一个
        raise _error(503, "accord_feed_unavailable", "本进程没有装配 Accord feed")
    return feed


def get_feed_settings(request: Request) -> FeedSettings:
    return getattr(request.app.state, "accord_feed_settings", None) or FeedSettings()


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


def get_recheck_slots(request: Request) -> RecheckSlots:
    slots = getattr(request.app.state, "accord_rechecks", None)
    if slots is None:  # pragma: no cover - 路由挂上时 create_app 总会装一个
        slots = request.app.state.accord_rechecks = RecheckSlots()
    return slots


def _require_world(feed: AccordFeed, world_id: str) -> None:
    if not feed.exists(world_id):
        raise _error(404, "archive_not_found", f"世界 '{world_id}' 还没有存档")


def _still_allowed(auth, scope) -> bool:
    if auth is None:
        return True
    principal = auth.resolve(scope)
    return principal is not None and principal.has(SCOPE_READ)


class _Watch:
    """一条流还能不能写。

    寿命按此刻的时间算，到点就停。身份每 `recheck_seconds` 重认一次：上一次认过
    已经超过这个间隔、新的一次还没认完，就先不写；一次重认超过
    `recheck_timeout_seconds` 还没回来，按认不出来处理。所以撤权之后流最多再写
    `recheck_seconds` 那么久，账户库被锁住也一样。

    第一次写之前也要先认一次：守卫放行和流真正开始之间可能排了很久的队，那一刻
    的放行不能拿来顶这一段。
    """

    def __init__(self, request: Request, settings: FeedSettings) -> None:
        self._auth = getattr(request.app.state, "admin_auth", None)
        self._scope = request.scope
        self._slots = get_recheck_slots(request)
        self._recheck = settings.recheck_seconds
        self._recheck_timeout = settings.recheck_timeout_seconds
        now = anyio.current_time()
        self._deadline = now + settings.lifetime_seconds
        self._verified_until = now
        self._denied = False

    def alive(self) -> bool:
        return not self._denied and anyio.current_time() < self._deadline

    async def ready(self) -> bool:
        """等到可以写为止；流该结束了就是 False。"""
        while self.alive():
            if anyio.current_time() < self._verified_until:
                return True
            await anyio.sleep(_READY_STEP)
        return False

    async def expire(self) -> None:
        await anyio.sleep_until(self._deadline)

    async def recheck(self) -> None:
        """主体不再能读、或者认不出来时返回。"""
        while True:
            await anyio.sleep_until(self._verified_until)
            started = anyio.current_time()
            allowed = False
            with anyio.move_on_after(self._recheck_timeout):
                while True:
                    # 超时就不等那次查询了：线程自己跑完，结果丢掉，名额到那时才还。
                    result = await anyio.to_thread.run_sync(
                        self._slots.run, _still_allowed, self._auth, self._scope,
                        abandon_on_cancel=True,
                    )
                    if result is not RecheckSlots.BUSY:
                        allowed = result
                        break
                    await anyio.sleep(_READY_STEP)
            if not allowed:
                self._denied = True
                return
            self._verified_until = started + self._recheck


async def _stream(watch: _Watch, session: FeedSession, settings: FeedSettings):
    if not await watch.ready():
        return
    last_write = anyio.current_time()
    yield f"retry: {int(settings.retry_ms)}\n\n"
    while await watch.ready():
        try:
            messages = await anyio.to_thread.run_sync(session.poll)
        except Exception as e:
            # 头已经发出去了，换不成一个错误状态码。断开就是最诚实的回答：客户端
            # 会带着 Last-Event-ID 重连，位置不会丢。
            print(f"[accord] 事件流中断: {type(e).__name__}: {e}", flush=True)
            return
        # 读的这一阵里可能到了寿命、被撤了权，或者该重认身份了。看门狗的取消要等
        # 这次读完、到下一个等待点才生效，写出要是不等待，这一批就漏出去了。
        if not await watch.ready():
            return
        now = anyio.current_time()
        if messages:
            yield "".join(message.encode() for message in messages)
            last_write = now
        elif now - last_write >= settings.keepalive_seconds:
            yield ": ping\n\n"
            last_write = now
        await anyio.sleep(settings.poll_seconds)


class _FeedResponse(StreamingResponse):
    """SSE 响应，外面套着看门狗。

    寿命到了、或者主体不再能读，看门狗直接取消整个写出，哪怕这时正卡在一次
    `send()` 上（客户端读得慢，传输层背压）。检查要是排在写出后面，慢客户端就能
    让撤了权、过了寿命的连接继续收到之后才有的台词。
    """

    def __init__(self, request: Request, session: FeedSession, settings: FeedSettings):
        super().__init__(
            iter(()),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
        self._request = request
        self._session = session
        self._settings = settings

    async def stream_response(self, send) -> None:
        # 寿命从真正开始写的那一刻算，所以看门狗在这里才装上。
        watch = _Watch(self._request, self._settings)
        body = _stream(watch, self._session, self._settings)
        started = False
        broken: Optional[OSError] = None
        async with anyio.create_task_group() as group:

            async def stop_after(waiter) -> None:
                await waiter()
                group.cancel_scope.cancel()

            group.start_soon(stop_after, watch.expire)
            group.start_soon(stop_after, watch.recheck)
            try:
                # 响应头也在看门狗底下发：这一次 send() 一样可能卡在背压上。
                await send(
                    {"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers}
                )
                started = True
                async for chunk in body:
                    await send(
                        {"type": "http.response.body", "body": chunk.encode(self.charset), "more_body": True}
                    )
            except OSError as e:
                broken = e
            group.cancel_scope.cancel()
        await body.aclose()
        if broken is not None:
            # 客户端已经走了。原样抛给 Starlette，它认得这一种，别让任务组包成异常组。
            raise broken
        if not started:
            # 响应头都没发完就被掐断了：没有响应可收尾，连接交给服务器关。
            return
        # 收尾只在这一处，不管是写完了还是被看门狗掐断的。还写不出去就算了，连接交给服务器关。
        with anyio.move_on_after(1):
            await send({"type": "http.response.body", "body": b"", "more_body": False})


@router.get("/worlds")
def list_accord_worlds(
    request: Request, plane: WorldControlPlane = Depends(get_control_plane)
):
    """这台服务器上的世界：`[{id, name, state}]`，给桌面那边做下拉选择。"""
    feed = get_feed(request)
    with _translated(plane, "list"):
        return feed.list_worlds()


@router.get("/worlds/{world_id}/hello")
def accord_hello(
    world_id: str,
    request: Request,
    plane: WorldControlPlane = Depends(get_control_plane),
):
    """快照：节点信息、时钟、居民此刻在哪做什么、当前游标。没开着的世界是 stopped。"""
    feed = get_feed(request)
    with _translated(plane, "accord_hello", world_id):
        _require_world(feed, world_id)
        return feed.hello(world_id)


@router.get("/worlds/{world_id}/events")
def accord_events(
    world_id: str,
    request: Request,
    after: Optional[str] = Query(None, max_length=MAX_CURSOR_CHARS),
    last_event_id: Optional[str] = Header(
        None, alias="Last-Event-ID", max_length=MAX_CURSOR_CHARS
    ),
    plane: WorldControlPlane = Depends(get_control_plane),
):
    """SSE 事件流。先推一条 hello；带 `Last-Event-ID`（优先）或 `?after=` 时从那之后续传。"""
    feed = get_feed(request)
    settings = get_feed_settings(request)
    raw = last_event_id or after
    with _translated(plane, "accord_events", world_id):
        _require_world(feed, world_id)
        try:
            cursor = Cursor.parse(raw) if raw else None
        except CursorError as e:
            raise _error(400, "invalid_cursor", e)
    session = feed.subscribe(world_id, cursor, clock_interval=settings.clock_seconds)
    return _FeedResponse(request, session, settings)
