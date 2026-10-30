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
# 服务器关掉；EventSource 会带着 Last-Event-ID 自己重连，续传一条不漏。
import asyncio
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


async def _stream(request: Request, session: FeedSession, settings: FeedSettings):
    auth = getattr(request.app.state, "admin_auth", None)
    loop = asyncio.get_running_loop()
    started = last_write = last_check = loop.time()
    yield f"retry: {int(settings.retry_ms)}\n\n"
    while True:
        try:
            messages = await anyio.to_thread.run_sync(session.poll)
        except Exception as e:
            # 头已经发出去了，换不成一个错误状态码。断开就是最诚实的回答：客户端
            # 会带着 Last-Event-ID 重连，位置不会丢。
            print(f"[accord] 事件流中断: {type(e).__name__}: {e}", flush=True)
            return
        now = loop.time()
        if messages:
            yield "".join(message.encode() for message in messages)
            last_write = now
        elif now - last_write >= settings.keepalive_seconds:
            yield ": ping\n\n"
            last_write = now
        if now - started >= settings.lifetime_seconds:
            return
        if now - last_check >= settings.recheck_seconds:
            last_check = now
            if not await anyio.to_thread.run_sync(_still_allowed, auth, request.scope):
                return
        await asyncio.sleep(settings.poll_seconds)


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
    return StreamingResponse(
        _stream(request, session, settings),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
