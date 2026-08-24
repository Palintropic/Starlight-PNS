# pns/interfaces/auth.py — 操作者登录边界
#
#     GET  /api/auth/session
#     POST /api/auth/login
#     POST /api/auth/logout
#
# 这三条是**公开**的，而且只有这三条加上健康检查和前端静态资源是公开的
# （清单在 security.PUBLIC_PATHS 里）。它们必须公开，否则浏览器连"要不要
# 登录"都问不出来。
#
# 浏览器**永远拿不到** token：操作者把它贴进登录框，服务端换给浏览器一个
# HttpOnly / SameSite=Strict 的会话 Cookie。所以密钥不进 JS 包、不进
# localStorage、不进 URL，因而也不进任何一条访问日志。
#
# SameSite=Strict 是这里的 CSRF 机制：跨站发起的请求根本带不上这张 Cookie，
# 所以别的站点诱导浏览器去 POST 一次「关闭世界」是打不通的。
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .security import SESSION_COOKIE, AdminAuth

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    # 上限只是不让一个巨大的请求体走那么远；真正的判据是定时安全比较。
    token: str = Field(min_length=1, max_length=512)


def _auth(request: Request) -> AdminAuth:
    auth = getattr(request.app.state, "admin_auth", None)
    if auth is None:  # pragma: no cover - create_app 总会装上一个
        raise HTTPException(
            503, {"category": "auth_unavailable", "message": "本进程没有装配鉴权边界"}
        )
    return auth


def _session_view(auth: AdminAuth, request: Request) -> dict:
    """没配 token 的开发服务器上 `authenticated` 恒为 true —— 那是实话：
    那台服务器上谁都能做管理操作。前端据此决定要不要弹登录框。"""
    return {
        **auth.settings.to_public_dict(),
        "authenticated": auth.authenticated(request.scope) if auth.required else True,
    }


@router.get("/session")
def get_session(request: Request):
    """要不要登录、现在登没登上。**不带任何凭据材料。**"""
    return _session_view(_auth(request), request)


@router.post("/login")
def post_login(payload: LoginRequest, response: Response, request: Request):
    """用管理 token 换一张会话 Cookie。"""
    auth = _auth(request)
    if not auth.required:
        raise HTTPException(
            409,
            {
                "category": "auth_not_configured",
                "message": "这台服务器没有配置管理凭据，登录无从谈起",
            },
        )
    if auth.throttle.blocked():
        retry = auth.throttle.retry_after()
        raise HTTPException(
            429,
            {
                "category": "too_many_attempts",
                "message": f"登录失败次数过多，请 {retry} 秒后再试",
            },
            headers={"Retry-After": str(retry)},
        )
    if not auth.verify_token(payload.token):
        auth.throttle.record_failure()
        # 只说"不对"。不说是长度不对还是内容不对——那种区分是白送的猜测线索。
        raise HTTPException(
            401, {"category": "invalid_credential", "message": "管理凭据不正确"}
        )
    auth.throttle.reset()
    session_id = auth.sessions.issue()
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=int(auth.settings.session_ttl_seconds),
        httponly=True,
        samesite="strict",
        secure=auth.settings.cookie_secure,
        path="/",
    )
    return {**auth.settings.to_public_dict(), "authenticated": True}


@router.post("/logout")
def post_logout(response: Response, request: Request):
    """作废当前会话。公开是刻意的：登出不该需要先证明自己登着。"""
    auth = _auth(request)
    session_id: Optional[str] = auth.session_id(request.scope)
    auth.sessions.revoke(session_id)
    response.delete_cookie(
        SESSION_COOKIE, path="/", httponly=True, samesite="strict",
        secure=auth.settings.cookie_secure,
    )
    return {**auth.settings.to_public_dict(), "authenticated": False}
