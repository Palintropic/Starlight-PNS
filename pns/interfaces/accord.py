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
# 检查由一只看门狗做，不排在写出后面：客户端读得慢、send() 卡住时照样按时掐断。
import os
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


def _require_world(feed: AccordFeed, world_id: str) -> None:
    if not feed.exists(world_id):
        raise _error(404, "archive_not_found", f"世界 '{world_id}' 还没有存档")


def _still_allowed(auth, scope) -> bool:
    if auth is None:
        return True
    principal = auth.resolve(scope)
    return principal is not None and principal.has(SCOPE_READ)


class _Watch:
    """一条流还该不该活着。寿命按此刻的时间算；身份由 `watch()` 每隔几秒重认一次。"""

    def __init__(self, request: Request, settings: FeedSettings) -> None:
        self._auth = getattr(request.app.state, "admin_auth", None)
        self._scope = request.scope
        self._recheck = settings.recheck_seconds
        self._deadline = anyio.current_time() + settings.lifetime_seconds
        self._denied = False

    def open(self) -> bool:
        return not self._denied and anyio.current_time() < self._deadline

    async def watch(self) -> None:
        """流该结束时返回。"""
        while True:
            remaining = self._deadline - anyio.current_time()
            if remaining <= 0:
                return
            await anyio.sleep(min(self._recheck, remaining))
            if not self.open():
                return
            if not await anyio.to_thread.run_sync(_still_allowed, self._auth, self._scope):
                self._denied = True
                return


async def _stream(watch: _Watch, session: FeedSession, settings: FeedSettings):
    last_write = anyio.current_time()
    yield f"retry: {int(settings.retry_ms)}\n\n"
    while watch.open():
        try:
            messages = await anyio.to_thread.run_sync(session.poll)
        except Exception as e:
            # 头已经发出去了，换不成一个错误状态码。断开就是最诚实的回答：客户端
            # 会带着 Last-Event-ID 重连，位置不会丢。
            print(f"[accord] 事件流中断: {type(e).__name__}: {e}", flush=True)
            return
        # 读的这一阵里可能已经到了寿命：那这一批就不写了。
        if not watch.open():
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

    寿命到了、或者主体不再能读，看门狗直接取消整个写出——哪怕这时正卡在一次
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
        self.body_iterator = _stream(watch, self._session, self._settings)
        finished = False
        async with anyio.create_task_group() as group:

            async def guard() -> None:
                await watch.watch()
                group.cancel_scope.cancel()

            group.start_soon(guard)
            await super().stream_response(send)
            finished = True
            group.cancel_scope.cancel()
        if not finished:
            # 被看门狗掐断的：试着把响应正常收尾；还写不出去就算了，连接交给服务器关。
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
