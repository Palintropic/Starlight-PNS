# pns/interfaces/accounts_api.py — 账户管理面
#
#     GET  /api/accounts                        列出人类账户
#     POST /api/accounts                        新建账户
#     GET  /api/accounts/audit                  安全审计记录
#     POST /api/accounts/{id}/role              改角色
#     POST /api/accounts/{id}/enabled           停用 / 启用
#     POST /api/accounts/{id}/password          重置密码
#
# 整个路由器挂在 `require_scope("accounts:manage")` 上，而不是逐条路由挂：
# 这个路由器以后新增的每一条路由都自动继承那道门。**operator 有 `operate`，
# 所以中间件那一层的默认档放得进来——账户管理必须自己再加一层，不能靠默认。**
#
# 两条落在这一层而不是存储层的规则：
#
#   * **权威一变，目标的会话立刻作废。** 存储层推进 `security_revision`
#     （于是任何一张旧会话在下一次请求就对不上号），这一层再显式清一遍进程内
#     的会话表（于是"立刻"不依赖于下一次请求什么时候来）。两条都要有。
#   * **"最后一个管理员"由存储层在写事务里裁决**，不在这里先查后写：那种写法
#     在两个并发请求下会双双通过，然后这台服务器就再也没有管理员了。
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .accounts import (
    MAX_PASSWORD_CHARS,
    ROLES,
    AccountConflict,
    AccountError,
    AccountStore,
    LastAdminError,
    Principal,
    SCOPE_ACCOUNTS,
)
from .authz import RequestPrincipal, current_principal, require_scope
from .security import AdminAuth

router = APIRouter(
    prefix="/api/accounts",
    tags=["accounts"],
    dependencies=[Depends(require_scope(SCOPE_ACCOUNTS))],
)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_CHARS)
    role: str = Field(min_length=1, max_length=32)


class RoleRequest(BaseModel):
    role: str = Field(min_length=1, max_length=32)


class EnabledRequest(BaseModel):
    enabled: bool


class PasswordResetRequest(BaseModel):
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_CHARS)


def _auth(request: Request) -> AdminAuth:
    auth = getattr(request.app.state, "admin_auth", None)
    if auth is None:  # pragma: no cover - create_app 总会装上一个
        raise HTTPException(
            503, {"category": "auth_unavailable", "message": "本进程没有装配鉴权边界"}
        )
    return auth


def _store(request: Request) -> AccountStore:
    auth = _auth(request)
    if auth.accounts is None:
        raise HTTPException(
            409,
            {
                "category": "accounts_unavailable",
                "message": "这台服务器没有账户库；账户管理无从谈起",
            },
        )
    return auth.accounts


def _fail(exc: Exception) -> HTTPException:
    """把存储层的失败翻译成稳定的 API 类别。**不把原始异常正文透出去。**"""
    if isinstance(exc, KeyError):
        return HTTPException(
            404, {"category": "account_not_found", "message": "没有这个账户"}
        )
    if isinstance(exc, AccountConflict):
        return HTTPException(409, {"category": exc.category, "message": str(exc)})
    if isinstance(exc, LastAdminError):
        return HTTPException(409, {"category": exc.category, "message": str(exc)})
    if isinstance(exc, AccountError):
        return HTTPException(400, {"category": exc.category, "message": str(exc)})
    raise exc  # pragma: no cover - 未知失败照旧变成 500，不在这里被吞掉


def _view(principal: Principal, revoked: Optional[int] = None) -> Dict[str, Any]:
    payload = principal.public_dict()
    if revoked is not None:
        payload["revoked_sessions"] = revoked
    return payload


@router.get("")
def list_accounts(request: Request) -> Dict[str, Any]:
    return {"users": [_view(p) for p in _store(request).list_humans()]}


@router.get("/audit")
def list_audit(
    request: Request, limit: int = Query(default=200, ge=1, le=1000)
) -> Dict[str, Any]:
    """安全审计。**记录里没有凭据、没有哈希、没有原始异常。**

    principal_id 顺便翻成用户名，纯粹是为了界面好读；权威仍然是那个 ID
    （break-glass 这类不在账户库里的主体翻不出名字，如实留 null）。
    """
    store = _store(request)
    names = {p.principal_id: p.username for p in store.list_humans()}
    records: List[Dict[str, Any]] = []
    for record in store.audit_records(limit):
        records.append(
            {
                **record,
                "actor_username": names.get(record.get("actor_principal_id")),
                "target_username": names.get(record.get("target_principal_id")),
            }
        )
    return {"records": records}


@router.post("", status_code=201)
def create_account(
    payload: CreateUserRequest,
    request: Request,
    actor: RequestPrincipal = Depends(current_principal),
) -> Dict[str, Any]:
    if payload.role not in ROLES:
        raise HTTPException(
            400,
            {
                "category": "invalid_role",
                "message": f"角色必须是 {'/'.join(ROLES)} 之一",
            },
        )
    try:
        created = _store(request).create_human(
            payload.username, payload.password, payload.role,
            actor=actor.principal_id,
        )
    except (AccountError, KeyError) as exc:
        raise _fail(exc) from exc
    return _view(created)


@router.post("/{principal_id}/role")
def set_account_role(
    principal_id: str,
    payload: RoleRequest,
    request: Request,
    actor: RequestPrincipal = Depends(current_principal),
) -> Dict[str, Any]:
    if payload.role not in ROLES:
        raise HTTPException(
            400,
            {
                "category": "invalid_role",
                "message": f"角色必须是 {'/'.join(ROLES)} 之一",
            },
        )
    auth = _auth(request)
    try:
        updated = _store(request).set_authority(
            principal_id, role=payload.role, actor=actor.principal_id
        )
    except (AccountError, KeyError) as exc:
        raise _fail(exc) from exc
    return _view(updated, auth.sessions.revoke_principal(principal_id))


@router.post("/{principal_id}/enabled")
def set_account_enabled(
    principal_id: str,
    payload: EnabledRequest,
    request: Request,
    actor: RequestPrincipal = Depends(current_principal),
) -> Dict[str, Any]:
    auth = _auth(request)
    try:
        updated = _store(request).set_authority(
            principal_id, enabled=payload.enabled, actor=actor.principal_id
        )
    except (AccountError, KeyError) as exc:
        raise _fail(exc) from exc
    return _view(updated, auth.sessions.revoke_principal(principal_id))


@router.post("/{principal_id}/password")
def reset_account_password(
    principal_id: str,
    payload: PasswordResetRequest,
    request: Request,
    actor: RequestPrincipal = Depends(current_principal),
) -> Dict[str, Any]:
    """管理员重置某个账户的密码。**不需要旧密码，所以一定要踢掉旧会话。**"""
    auth = _auth(request)
    try:
        updated = _store(request).set_password(
            principal_id, payload.password, actor=actor.principal_id
        )
    except (AccountError, KeyError) as exc:
        raise _fail(exc) from exc
    return _view(updated, auth.sessions.revoke_principal(principal_id))
