# pns/interfaces/provider_api.py — 面板改模型线路（CFG-1 §5）
#
#     GET    /api/settings/provider           当前线路的公开投影、隔离状态
#     PUT    /api/settings/provider           保存两条线路
#     DELETE /api/settings/provider           恢复默认（删 provider.json，回到 .env 派生）
#     POST   /api/settings/provider/resolve   解除隔离：选定保留哪一份
#
#     GET    /api/settings/accounts           计费账户目录
#     POST   /api/settings/accounts           新建账户
#     POST   /api/settings/accounts/{id}      改显示名 / 倍率（币种、kind 不可改）
#     POST   /api/settings/accounts/{id}/retire  退役（被线路引用时拒绝）
#
# 写操作要 `accounts:manage`（只有管理员有）：换的是整台服务器的模型凭据。
#
# 一次保存的顺序是固定的（§5.3）：进 ProviderSwitchGate → 所有持久世界都已关闭
# （§5.1，判据是服务的已打开句柄集合）→ 构建候选并做有效线路校验 → P7 持锁入口
# 里的文件事务（provider_txn）。锁顺序先闸门后 P7；世界入口只拿闸门。
#
# 对外文字都是固定措辞或线路校验的固定文案，不带请求里的值，也不带 token。
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from pns.runtime import content_registry, provider_txn
from pns.runtime.provider_settings import (
    AccountInUse,
    ModelSettings,
    ProviderSettingsError,
    accounts_in_use,
    build_candidate,
    create_account,
    load_accounts,
    read_current,
    retire_account,
    update_account,
    validate_settings,
)
from pns.runtime.settings_files import NotPublished, PublishedNotDurable
from pns.runtime.provider_switch import ProviderSwitchBusy
from pns.runtime.reload import BOUNDARY, QUARANTINE_MESSAGE, ReloadBusy, SettingsQuarantined

from .accounts import SCOPE_ACCOUNTS
from .authz import principal_of, require_scope
from .composition import WorldControlPlane
from .persistent_worlds import get_control_plane

router = APIRouter(prefix="/api/settings/provider", tags=["settings"])
accounts_router = APIRouter(prefix="/api/settings/accounts", tags=["settings"])
_admin = [Depends(require_scope(SCOPE_ACCOUNTS))]


def _error(status: int, category: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status, {"category": category, "message": message, **extra})


def _view() -> Dict[str, Any]:
    registry = BOUNDARY.active()
    return {
        "settings": registry.models.public_dict(),
        "config_revision": registry.revision,
        "quarantined": BOUNDARY.quarantined,
        "transaction_pending": provider_txn.pending(content_registry.SETTINGS_DIR),
    }


@router.get("")
def get_provider_settings():
    return _view()


def _require_all_closed(plane: WorldControlPlane) -> None:
    open_worlds: List[str] = list(plane.service.open_world_ids())
    if open_worlds:
        raise _error(
            409,
            "world_open",
            "换模型要先关闭所有世界，保存后再恢复；恢复时会照常检查内容采用、补跑离线时间",
            worlds=open_worlds,
        )


def _updated_by(request: Request) -> str:
    principal = principal_of(request)
    return principal.principal_id if principal is not None else "unknown"


def _commit(plane: WorldControlPlane, candidate: Optional[bytes], reason: str) -> Dict[str, Any]:
    """在 P7 持锁入口里发布并切换；把结局翻译成 HTTP。调用方已持闸门。"""
    try:
        outcome = provider_txn.save(BOUNDARY, content_registry.SETTINGS_DIR, candidate, reason=reason)
    except ReloadBusy:
        raise _error(409, "reload_in_progress", "配置正在重新加载，本次保存未写入任何内容，请稍后重试") from None
    except SettingsQuarantined as e:
        raise _error(503, "settings_quarantined", str(e)) from None
    reload = outcome.reload.to_dict() if outcome.reload is not None else None
    if outcome.status == provider_txn.OUTCOME_OK:
        return {**_view(), "reload": reload}
    if outcome.status == provider_txn.OUTCOME_NOT_WRITTEN:
        raise _error(500, "not_written", "设置文件没写成，磁盘与当前配置都没有变化")
    if outcome.status == provider_txn.OUTCOME_ROLLED_BACK:
        if outcome.reload is not None and outcome.reload.status == "failed":
            raise _error(
                400, "rolled_back", "新设置没有生效，已回退到保存前的设置", reload=reload
            )
        raise _error(500, "rolled_back", "新设置的写入无法证实耐久，已回退到保存前的设置")
    raise _error(503, "settings_quarantined", QUARANTINE_MESSAGE)


@router.put("", dependencies=_admin)
def put_provider_settings(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    plane: WorldControlPlane = Depends(get_control_plane),
):
    try:
        with plane.switch_gate.switch():
            _require_all_closed(plane)
            if BOUNDARY.quarantined:
                raise _error(503, "settings_quarantined", QUARANTINE_MESSAGE)
            settings_dir = content_registry.SETTINGS_DIR
            try:
                candidate, _ = build_candidate(
                    payload,
                    read_current(settings_dir),
                    load_accounts(settings_dir),
                    os.environ,
                    updated_by=_updated_by(request),
                    ooc_threshold=BOUNDARY.active().models.ooc_threshold,
                )
            except ProviderSettingsError as e:
                raise _error(400, "invalid_settings", str(e)) from None
            return _commit(plane, candidate, "面板保存模型线路")
    except ProviderSwitchBusy as e:
        raise _error(409, "provider_switch_in_progress", str(e)) from None


@router.delete("", dependencies=_admin)
def delete_provider_settings(plane: WorldControlPlane = Depends(get_control_plane)):
    try:
        with plane.switch_gate.switch():
            _require_all_closed(plane)
            if BOUNDARY.quarantined:
                raise _error(503, "settings_quarantined", QUARANTINE_MESSAGE)
            settings_dir = content_registry.SETTINGS_DIR
            # 恢复默认之后生效的是 env 那一套：它本身无效就不删文件（§6）。
            try:
                fallback = ModelSettings.from_env(
                    os.environ, ooc_threshold=BOUNDARY.active().models.ooc_threshold
                )
                validate_settings(fallback, load_accounts(settings_dir))
            except ProviderSettingsError as e:
                raise _error(400, "env_default_invalid", f"恢复默认后 .env 那一套线路不可用：{e}") from None
            return _commit(plane, None, "面板恢复默认模型线路")
    except ProviderSwitchBusy as e:
        raise _error(409, "provider_switch_in_progress", str(e)) from None


@router.post("/resolve", dependencies=_admin)
def resolve_provider_settings(
    payload: Dict[str, Any] = Body(...),
    plane: WorldControlPlane = Depends(get_control_plane),
):
    keep = payload.get("keep") if isinstance(payload, dict) else None
    if keep not in provider_txn.CHOICES:
        raise _error(422, "invalid_choice", f"keep 只能是 {'、'.join(provider_txn.CHOICES)}")
    try:
        with plane.switch_gate.switch():
            _require_all_closed(plane)
            try:
                outcome = provider_txn.resolve(
                    BOUNDARY, content_registry.SETTINGS_DIR, keep, reason="解除模型设置隔离"
                )
            except ReloadBusy:
                raise _error(409, "reload_in_progress", "配置正在重新加载，请稍后重试") from None
            except provider_txn.NothingToResolve as e:
                raise _error(409, "nothing_to_resolve", str(e)) from None
            except provider_txn.ResolveRefused as e:
                raise _error(409, "resolve_refused", str(e)) from None
    except ProviderSwitchBusy as e:
        raise _error(409, "provider_switch_in_progress", str(e)) from None
    if outcome.status != provider_txn.OUTCOME_OK:
        reload = outcome.reload.to_dict() if outcome.reload is not None else None
        raise _error(503, "settings_quarantined", "所选设置没能完成恢复，仍处于隔离状态", reload=reload)
    return {**_view(), "reload": outcome.reload.to_dict() if outcome.reload is not None else None}


# ── 计费账户 ────────────────────────────────────────────────────────────
# 账户写操作也进闸门（独占）：退役要看"现在哪些账户被线路引用"，不能跟一次
# 保存交错。隔离时一律拒绝——那时磁盘上的线路是哪一份还没定。


def _catalog_view() -> Dict[str, Any]:
    try:
        catalog = load_accounts(content_registry.SETTINGS_DIR)
    except ProviderSettingsError as e:
        raise _error(500, "accounts_unreadable", str(e)) from None
    return {"accounts": catalog.public_dict()}


@accounts_router.get("")
def get_accounts():
    return _catalog_view()


def _write_account(plane: WorldControlPlane, action) -> Dict[str, Any]:
    try:
        with plane.switch_gate.switch():
            if BOUNDARY.quarantined:
                raise _error(503, "settings_quarantined", QUARANTINE_MESSAGE)
            try:
                account = action()
            except KeyError:
                raise _error(404, "account_not_found", "没有这个账户") from None
            except AccountInUse as e:
                raise _error(409, "account_in_use", str(e)) from None
            except ProviderSettingsError as e:
                raise _error(400, "invalid_account", str(e)) from None
            except NotPublished:
                raise _error(500, "not_written", "账户目录没写成，什么都没变") from None
            except PublishedNotDurable:
                raise _error(500, "not_durable", "账户目录已经写入，但耐久性无法证实") from None
    except ProviderSwitchBusy as e:
        raise _error(409, "provider_switch_in_progress", str(e)) from None
    return {"account": account.public_dict(), **_catalog_view()}


@accounts_router.post("", status_code=201, dependencies=_admin)
def post_account(
    payload: Dict[str, Any] = Body(...), plane: WorldControlPlane = Depends(get_control_plane)
):
    return _write_account(
        plane, lambda: create_account(content_registry.SETTINGS_DIR, payload.get("id"), payload)
    )


@accounts_router.post("/{account_id}", dependencies=_admin)
def post_account_update(
    account_id: str,
    payload: Dict[str, Any] = Body(...),
    plane: WorldControlPlane = Depends(get_control_plane),
):
    return _write_account(
        plane, lambda: update_account(content_registry.SETTINGS_DIR, account_id, payload)
    )


@accounts_router.post("/{account_id}/retire", dependencies=_admin)
def post_account_retire(account_id: str, plane: WorldControlPlane = Depends(get_control_plane)):
    def retire():
        settings_dir = content_registry.SETTINGS_DIR
        in_use = accounts_in_use(settings_dir, BOUNDARY.active().models)
        return retire_account(settings_dir, account_id, in_use)

    return _write_account(plane, retire)
