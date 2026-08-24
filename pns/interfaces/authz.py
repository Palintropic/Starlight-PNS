# pns/interfaces/authz.py — 这次请求**能做什么**
#
# `security.py` 回答"这次请求是谁"，这一层回答"那个人能不能做这件事"。分成两
# 个文件不是洁癖：认证失败和授权失败是两种不同的事故（前者是凭据坏了，后者是
# 权限不够），把它们混在一处，最后总会写出一个"反正都拒绝"的分支，然后某天
# 那个分支放行了不该放行的一半。
#
# 授权靠的是**排除机制，不是记性**：
#
#   * 每一条非安全方法（POST/PUT/PATCH/DELETE）默认要求 `operate`，每一条
#     安全方法默认要求 `read`，每一条 WebSocket 默认要求 `operate`。判据来自
#     方法和路径本身，不来自"这条路由记得挂依赖"。所以明天新加一条路由，
#     observer 依然进不去——除非有人**显式**把它放进自服务清单。
#   * 需要更严的（账户管理）在路由上再挂一层 `require_scope`。加严是显式的，
#     放松也是显式的，两边都没有默认值可以被顺手继承。
#
# `RequestPrincipal` 是这条边界的产物：一次请求确定下来的主体身份。它带着
# 稳定的 `principal_id` 和 `kind`，所以以后 ST-1 往 Sekai Times 发东西时，
# "这条内容是谁授权发的"有一个不依赖用户名、不依赖会话、也不依赖角色名字的
# 答案可写进 provenance。
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, Optional

from fastapi import HTTPException, Request

from .accounts import (
    ALL_SCOPES,
    KIND_SERVICE,
    ROLE_ADMIN,
    SCOPE_OPERATE,
    SCOPE_READ,
    Principal,
)

# 中间件把主体挂在 ASGI scope 的这个键上。刻意用一个自有键而不是 Starlette 的
# `scope["state"]`：后者的存在与形状取决于 lifespan 怎么装配，而这条边界不该
# 依赖一个我们不拥有的约定。
PRINCIPAL_SCOPE_KEY = "pns_principal"

# 一次请求是**怎么**被认下来的。它进 `/api/auth/session`，也进审计——
# "谁做的"和"用什么凭据做的"是两个问题，混在一起会让 break-glass 的每一次
# 使用都看起来像一次普通登录。
VIA_SESSION = "session"
VIA_BEARER = "bearer"
VIA_OPEN_DEVELOPMENT = "open-development"

# break-glass bearer 是一个**不在账户库里**的主体。它有稳定的 ID，所以审计和
# provenance 指得到它；它是 `service` 而不是 `human`，所以它永远不会出现在
# 用户列表里，也永远不能改密码——它根本没有密码，只有一把部署时注入的 token。
BREAK_GLASS_PRINCIPAL_ID = "svc-break-glass"
BREAK_GLASS_USERNAME = "break-glass"

# 没配任何凭据的开发服务器。把它做成一个显式主体而不是"跳过鉴权"，是为了让
# `/api/auth/session` 能如实说出"这台机器上谁都是管理员"这件事。
OPEN_DEVELOPMENT_PRINCIPAL_ID = "svc-open-development"
OPEN_DEVELOPMENT_USERNAME = "open-development"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# 自服务路径：任何**已认证**主体都能调，不论角色。清单里只该有"改自己的
# 东西"那一类——observer 也必须能改自己的密码，否则一个被重置了密码的只读
# 账户就再也换不掉那个由别人设定的密码。
SELF_SERVICE_PATHS = frozenset({"/api/auth/password"})


@dataclass(frozen=True)
class RequestPrincipal:
    """这次请求确定下来的主体。"""

    principal_id: str
    username: str
    kind: str
    role: str
    scopes: FrozenSet[str]
    via: str
    session_id: Optional[str] = None

    def has(self, scope: str) -> bool:
        return scope in self.scopes

    def public_dict(self) -> Dict[str, Any]:
        return {
            "principal_id": self.principal_id,
            "username": self.username,
            "kind": self.kind,
            "role": self.role,
            "scopes": sorted(self.scopes),
            "via": self.via,
        }


def break_glass_principal() -> RequestPrincipal:
    return RequestPrincipal(
        principal_id=BREAK_GLASS_PRINCIPAL_ID,
        username=BREAK_GLASS_USERNAME,
        kind=KIND_SERVICE,
        role=ROLE_ADMIN,
        scopes=ALL_SCOPES,
        via=VIA_BEARER,
    )


def open_development_principal() -> RequestPrincipal:
    return RequestPrincipal(
        principal_id=OPEN_DEVELOPMENT_PRINCIPAL_ID,
        username=OPEN_DEVELOPMENT_USERNAME,
        kind=KIND_SERVICE,
        role=ROLE_ADMIN,
        scopes=ALL_SCOPES,
        via=VIA_OPEN_DEVELOPMENT,
    )


def session_principal(account: Principal, session_id: str) -> RequestPrincipal:
    return RequestPrincipal(
        principal_id=account.principal_id,
        username=account.username,
        kind=account.kind,
        role=account.role,
        scopes=account.scopes,
        via=VIA_SESSION,
        session_id=session_id,
    )


def required_scope(path: str, method: str, kind: str = "http") -> Optional[str]:
    """这条路径这个方法**至少**要什么权限。`None` = 已认证即可。

    判据只有方法和路径，没有路由表——所以它对"还不存在的路由"也给得出答案，
    而那正是这条边界要挡的东西。
    """
    if kind == "websocket":
        # 这个应用里的 WebSocket 只有 `/ws/run`，它会花模型额度。一条会花钱的
        # 连接不该因为"它不是 POST"就落进只读那一档。
        return SCOPE_OPERATE
    if path in SELF_SERVICE_PATHS:
        return None
    if (method or "GET").upper() in SAFE_METHODS:
        return SCOPE_READ
    return SCOPE_OPERATE


def principal_of(request: Request) -> Optional[RequestPrincipal]:
    """这次请求的主体。没有中间件的裸 app 上是 None。"""
    principal = request.scope.get(PRINCIPAL_SCOPE_KEY)
    return principal if isinstance(principal, RequestPrincipal) else None


def _forbidden(scope: str) -> HTTPException:
    return HTTPException(
        403,
        {
            "category": "forbidden",
            "message": f"当前账户没有执行这次操作的权限（需要 {scope}）",
        },
    )


def _unauthenticated() -> HTTPException:
    return HTTPException(
        401, {"category": "unauthenticated", "message": "需要管理凭据"}
    )


def current_principal(request: Request) -> RequestPrincipal:
    """路由依赖：拿到当前主体，拿不到就 401。"""
    principal = principal_of(request)
    if principal is None:
        raise _unauthenticated()
    return principal


def require_scope(scope: str) -> Callable[[Request], RequestPrincipal]:
    """路由/路由器依赖：在默认档之上**加严**。

    挂在 `APIRouter(dependencies=[...])` 上而不是逐条路由上，是为了让这个
    路由器以后新增的每一条路由自动继承这道门。
    """

    def dependency(request: Request) -> RequestPrincipal:
        principal = current_principal(request)
        if not principal.has(scope):
            raise _forbidden(scope)
        return principal

    return dependency


__all__ = [
    "BREAK_GLASS_PRINCIPAL_ID",
    "BREAK_GLASS_USERNAME",
    "OPEN_DEVELOPMENT_PRINCIPAL_ID",
    "OPEN_DEVELOPMENT_USERNAME",
    "PRINCIPAL_SCOPE_KEY",
    "RequestPrincipal",
    "SAFE_METHODS",
    "SELF_SERVICE_PATHS",
    "VIA_BEARER",
    "VIA_OPEN_DEVELOPMENT",
    "VIA_SESSION",
    "break_glass_principal",
    "current_principal",
    "open_development_principal",
    "principal_of",
    "require_scope",
    "required_scope",
    "session_principal",
]
