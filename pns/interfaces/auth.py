# pns/interfaces/auth.py — 操作者登录边界
#
#     GET  /api/auth/session    要不要登录、现在是谁
#     POST /api/auth/login      用户名 + 密码换一张会话 Cookie
#     POST /api/auth/logout     作废当前会话
#     POST /api/auth/password   改自己的密码（**需要已认证**）
#
# 前三条是公开的，加上健康检查和前端静态资源就是这台服务器公开面的**全部**
# （清单在 security.PUBLIC_PATHS 里）。它们必须公开，否则浏览器连"要不要
# 登录"都问不出来。第四条不公开——改密码是一次已认证主体对自己的操作。
#
# 三条边界：
#
#   * **浏览器登录不接受 `PNS_ADMIN_TOKEN`。** 那把 token 是 break-glass /
#     自动化的**非人类**主体，只走 `Authorization: Bearer`。让它同时是"网页
#     登录口令"，等于把一把不会过期、不属于任何人、撤销要重启进程的钥匙发给
#     每一个用浏览器的人——那样账户体系里的停用、改角色、改密码就全都绕得过去。
#   * **失败只有一句话。** 用户名不存在、密码不对、账户被停用，响应完全一样。
#     区别只写进审计。否则登录框就是一台用户名枚举机。
#   * **凭据不留在浏览器里。** 密码只在提交那一刻经过前端 state，换回来的是
#     一张 HttpOnly / SameSite=Strict 的 Cookie，不进 localStorage、不进 URL、
#     因而也不进任何一条访问日志。
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .accounts import (
    MAX_PASSWORD_CHARS,
    AccountStore,
    InvalidPassword,
    canonical_username,
)
from .authz import VIA_SESSION, RequestPrincipal, current_principal
from .security import SESSION_COOKIE, AdminAuth

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    # 上限只是不让一个巨大的请求体走那么远。**下限是 1，不是密码策略的下限**：
    # 一个"密码太短所以 422"的分支会让攻击者不花 Argon2 的时间就区分出结果。
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_CHARS)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_CHARS)
    new_password: str = Field(min_length=1, max_length=MAX_PASSWORD_CHARS)


def _auth(request: Request) -> AdminAuth:
    auth = getattr(request.app.state, "admin_auth", None)
    if auth is None:  # pragma: no cover - create_app 总会装上一个
        raise HTTPException(
            503, {"category": "auth_unavailable", "message": "本进程没有装配鉴权边界"}
        )
    return auth


def _accounts(auth: AdminAuth) -> AccountStore:
    if auth.accounts is None:
        raise HTTPException(
            409,
            {
                "category": "accounts_unavailable",
                "message": "这台服务器没有账户库，登录无从谈起",
            },
        )
    return auth.accounts


def _session_view(auth: AdminAuth, request: Request) -> dict:
    """要不要登录、现在是谁。**不带任何凭据材料。**

    没配凭据也没有账户的开发服务器上，主体是一个显式的 `open-development`
    ——那是实话：那台服务器上谁都能做管理操作。前端据此决定要不要弹登录框，
    也据此决定显示哪些按钮。
    """
    principal = auth.resolve(request.scope)
    return {
        **auth.public_dict(),
        "authenticated": principal is not None,
        "principal": principal.public_dict() if principal is not None else None,
    }


def _throttle_key(username: object) -> str:
    """节流分桶的键。规范化不了的用户名共用一个桶。"""
    try:
        return canonical_username(username)[1]
    except Exception:
        return ""


def _set_session_cookie(response: Response, auth: AdminAuth, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=int(auth.settings.session_ttl_seconds),
        httponly=True,
        samesite="strict",
        secure=auth.settings.cookie_secure,
        path="/",
    )


def _clear_session_cookie(response: Response, auth: AdminAuth) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="strict",
        secure=auth.settings.cookie_secure,
    )


@router.get("/session")
def get_session(request: Request):
    """要不要登录、现在登没登上、登上的是谁。"""
    return _session_view(_auth(request), request)


@router.post("/login")
def post_login(payload: LoginRequest, response: Response, request: Request):
    """用户名 + 密码换一张会话 Cookie。"""
    auth = _auth(request)
    if not auth.required:
        raise HTTPException(
            409,
            {
                "category": "auth_not_configured",
                "message": "这台服务器没有配置任何账户，登录无从谈起",
            },
        )
    store = _accounts(auth)
    key = _throttle_key(payload.username)
    if auth.throttle.blocked(key):
        retry = auth.throttle.retry_after(key)
        raise HTTPException(
            429,
            {
                "category": "too_many_attempts",
                "message": f"登录失败次数过多，请 {retry} 秒后再试",
            },
            headers={"Retry-After": str(retry)},
        )
    outcome = store.authenticate(payload.username, payload.password)
    if not outcome.ok:
        auth.throttle.record_failure(key)
        # 只说"不对"。不说是用户名不存在、密码不对还是账户被停用——那种区分
        # 是白送的枚举线索。失败的**类别**写进了审计，管理员看得到。
        raise HTTPException(
            401, {"category": "invalid_credential", "message": "用户名或密码不正确"}
        )
    # 只清掉这个账户的失败史，全局桶不清（见 LoginThrottle.record_success）。
    auth.throttle.record_success(key)
    account = outcome.principal
    session_id = auth.sessions.issue(account.principal_id, account.security_revision)
    _set_session_cookie(response, auth, session_id)
    return _session_view(auth, request) | {
        "authenticated": True,
        "principal": {
            **account.public_dict(),
            "via": VIA_SESSION,
        },
    }


@router.post("/logout")
def post_logout(response: Response, request: Request):
    """作废当前会话。公开是刻意的：登出不该需要先证明自己登着。"""
    auth = _auth(request)
    principal: Optional[RequestPrincipal] = auth.resolve(request.scope)
    session_id: Optional[str] = auth.session_id(request.scope)
    auth.sessions.revoke(session_id)
    if (
        auth.accounts is not None
        and principal is not None
        and principal.via == VIA_SESSION
    ):
        auth.accounts.record_event(
            "auth.logout",
            "success",
            actor=principal.principal_id,
            target=principal.principal_id,
        )
    _clear_session_cookie(response, auth)
    return {
        **auth.public_dict(),
        "authenticated": False,
        "principal": None,
    }


@router.post("/password")
def post_password(
    payload: PasswordChangeRequest,
    response: Response,
    request: Request,
):
    """改**自己**的密码。成功之后当前会话也一起作废。

    连自己这张一起踢掉是刻意的：改密码的常见理由是"我怀疑它泄露了"，而那种
    时候"除了我手上这张之外的会话都失效"根本不够——泄露的可能正是这一张。
    所以这里不签发新会话，前端会退回登录框。
    """
    auth = _auth(request)
    principal = current_principal(request)
    store = _accounts(auth)
    if principal.via != VIA_SESSION:
        raise HTTPException(
            409,
            {
                "category": "not_an_account",
                "message": (
                    "当前主体不是一个账户（break-glass token 或开放的开发服务器），"
                    "没有可改的密码"
                ),
            },
        )
    if payload.new_password == payload.current_password:
        raise HTTPException(
            400,
            {"category": "invalid_password", "message": "新密码不能和当前密码相同"},
        )
    try:
        store.change_own_password(
            principal.principal_id, payload.current_password, payload.new_password
        )
    except InvalidPassword as e:
        raise HTTPException(400, {"category": e.category, "message": str(e)}) from e
    except KeyError as e:  # pragma: no cover - 会话刚刚才验过这个主体
        raise HTTPException(
            401, {"category": "unauthenticated", "message": "需要管理凭据"}
        ) from e
    # security_revision 已经前进，所以旧会话在下一次请求就对不上号；这里再
    # 显式清一遍，让"立刻失效"不依赖于下一次请求什么时候来。
    auth.sessions.revoke_principal(principal.principal_id)
    _clear_session_cookie(response, auth)
    return {
        **auth.public_dict(),
        "authenticated": False,
        "principal": None,
        "password_changed": True,
    }
