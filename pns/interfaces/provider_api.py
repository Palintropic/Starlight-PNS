# pns/interfaces/provider_api.py — 面板改模型卡片（CFG-1 §5、卡片补丁 §3 / §9）
#
#     GET    /api/settings/provider              卡片（公开投影）、active、展开后的两条线路、隔离状态
#     PUT    /api/settings/provider/active       切换：生成用哪张、判分跟随还是用哪张（可覆盖模型）
#     POST   /api/settings/provider/cards        新建卡片（只保存不启用；可顺手建同 id 的账户）
#     PUT    /api/settings/provider/cards/{id}   编辑卡片（token 留空 = 沿用已存的）
#     DELETE /api/settings/provider/cards/{id}   删除卡片（生成或判分正引用着就拒绝）
#     DELETE /api/settings/provider              删掉全部卡片，回到 .env
#     POST   /api/settings/provider/resolve      解除隔离：选定保留哪一份
#
#     GET    /api/settings/accounts           计费账户目录
#     POST   /api/settings/accounts           新建账户
#     POST   /api/settings/accounts/{id}      改显示名 / 倍率（币种、kind 不可改）
#     POST   /api/settings/accounts/{id}/retire  退役（被生效卡片引用时拒绝）
#
# 写操作要 `accounts:manage`（只有管理员有）：换的是整台服务器的模型凭据。
#
# 每个卡片写操作（新建、编辑、删除、切换，不分生效没生效）的顺序都是固定的：
# 进 ProviderSwitchGate → 所有持久世界都已关闭（判据是服务的已打开句柄集合）→
# 读当前 provider.json（坏了就停，不当成空的接着写）→ 在内存里改 → 校验 →
# P7 持锁入口里的文件事务（provider_txn）。"这张卡片有没有生效"在拿到闸门之后
# 才读。锁顺序先闸门后 P7；世界入口只拿闸门。
#
# 对外文字都是固定措辞或校验的固定文案，不带请求里的值，也不带 token。
import os
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from pns.runtime import content_registry, provider_txn
from pns.runtime.provider_settings import (
    AccountCatalog,
    AccountInUse,
    CardInUse,
    ProviderDocument,
    ProviderSettingsError,
    EMPTY_DOCUMENT,
    accounts_in_use,
    candidate_payload,
    create_account,
    delete_card,
    load_accounts,
    new_account,
    new_card_id,
    put_card,
    read_document,
    retire_account,
    set_active,
    settings_from_document,
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


def _read_for_write(settings_dir) -> tuple:
    """(账户目录, 当前卡片文件)。文件不存在当成空的；坏了就停。"""
    try:
        catalog = load_accounts(settings_dir)
    except ProviderSettingsError as e:
        raise _error(500, "accounts_unreadable", str(e)) from None
    try:
        document = read_document(settings_dir, catalog)
    except ProviderSettingsError as e:
        raise _error(
            409, "provider_file_broken",
            f"当前的模型设置文件读不出来（{e}），不能在它上面接着改；请在服务器上核对后恢复",
        ) from None
    return catalog, (EMPTY_DOCUMENT if document is None else document)


def _check(before: ProviderDocument, after: ProviderDocument, catalog: AccountCatalog) -> None:
    """候选的校验。

    跟加载用同一套解析和展开；改变了生效线路的操作（切换、编辑生效中的卡片、
    删光回到 .env）还要过完整的有效线路校验 —— 包括 env 卡片要有 key。
    """
    env = os.environ
    threshold = BOUNDARY.active().models.ooc_threshold
    try:
        old = settings_from_document(
            None if before.is_default else before, catalog, env, ooc_threshold=threshold
        )
    except ProviderSettingsError:
        old = None
    try:
        new = settings_from_document(
            None if after.is_default else after, catalog, env, ooc_threshold=threshold
        )
        if old is None or (new.generation, new.judge) != (old.generation, old.judge):
            validate_settings(new, catalog)
    except ProviderSettingsError as e:
        raise _error(400, "invalid_settings", str(e)) from None


def _commit(candidate: Optional[bytes], reason: str) -> Dict[str, Any]:
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


def _write(
    plane: WorldControlPlane,
    request: Request,
    change: Callable[[ProviderDocument, AccountCatalog], ProviderDocument],
    reason: str,
) -> Dict[str, Any]:
    """一次卡片写操作：闸门 → 世界全关 → 读 → 改 → 校验 → 事务。"""
    try:
        with plane.switch_gate.switch():
            _require_all_closed(plane)
            if BOUNDARY.quarantined:
                raise _error(503, "settings_quarantined", QUARANTINE_MESSAGE)
            catalog, before = _read_for_write(content_registry.SETTINGS_DIR)
            try:
                after = change(before, catalog)
            except KeyError:
                raise _error(404, "card_not_found", "没有这张卡片") from None
            except CardInUse as e:
                raise _error(409, "card_in_use", str(e)) from None
            except ProviderSettingsError as e:
                raise _error(400, "invalid_settings", str(e)) from None
            _check(before, after, catalog)
            return _commit(candidate_payload(after, updated_by=_updated_by(request)), reason)
    except ProviderSwitchBusy as e:
        raise _error(409, "provider_switch_in_progress", str(e)) from None


@router.put("/active", dependencies=_admin)
def put_active(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    plane: WorldControlPlane = Depends(get_control_plane),
):
    return _write(plane, request, lambda doc, _: set_active(doc, payload), "面板切换模型卡片")


@router.put("/cards/{card_id}", dependencies=_admin)
def put_card_route(
    card_id: str,
    request: Request,
    payload: Dict[str, Any] = Body(...),
    plane: WorldControlPlane = Depends(get_control_plane),
):
    return _write(
        plane, request,
        lambda doc, catalog: put_card(doc, card_id, payload, catalog, create=False),
        "面板编辑模型卡片",
    )


@router.delete("/cards/{card_id}", dependencies=_admin)
def delete_card_route(
    card_id: str, request: Request, plane: WorldControlPlane = Depends(get_control_plane)
):
    return _write(plane, request, lambda doc, _: delete_card(doc, card_id), "面板删除模型卡片")


@router.delete("", dependencies=_admin)
def delete_provider_settings(request: Request, plane: WorldControlPlane = Depends(get_control_plane)):
    """删掉全部卡片、回到 .env。只想换回 .env 而留着卡片，用 PUT /active。"""
    return _write(plane, request, lambda doc, _: EMPTY_DOCUMENT, "面板删除全部模型卡片")


@router.post("/cards", status_code=201, dependencies=_admin)
def post_card(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    plane: WorldControlPlane = Depends(get_control_plane),
):
    """新建一张卡片：只保存、不启用。id 由服务端生成。

    带 `create_account`（{kind?, currency?, multiplier?}）时，在同一次闸门持有里
    先建一个同 id 的账户、再写卡片（审查 F6）：

      * 账户没发布                → 什么都没变（not_written）；
      * 账户已发布、耐久证实不了  → 停下，不写卡片（account_not_durable + 账户 id）；
      * 账户成功、卡片没写成      → card_failed + 账户 id（账户保留，不自动退役）；
      * 卡片写完进了隔离          → 照常回隔离，带上账户 id。

    重试由调用方明确地挂到返回的账户上（不带 create_account），服务端不自动
    复用同名账户。
    """
    if not isinstance(payload, dict):
        raise _error(400, "invalid_settings", "请求体必须是对象")
    body = {k: v for k, v in payload.items() if k != "create_account"}
    account_request = payload.get("create_account")
    if account_request is not None and not isinstance(account_request, dict):
        raise _error(400, "invalid_settings", "create_account 必须是对象")
    settings_dir = content_registry.SETTINGS_DIR
    try:
        with plane.switch_gate.switch():
            _require_all_closed(plane)
            if BOUNDARY.quarantined:
                raise _error(503, "settings_quarantined", QUARANTINE_MESSAGE)
            catalog, before = _read_for_write(settings_dir)
            card_id = new_card_id([*before.cards, *catalog.accounts])
            created = None
            if account_request is not None:
                body["account"] = card_id
                try:
                    account = new_account(catalog, card_id, {
                        "name": body.get("name"),
                        "kind": account_request.get("kind", "other"),
                        "currency": account_request.get("currency", "CNY"),
                        "multiplier": account_request.get("multiplier", "1"),
                    })
                    # 先在假设账户已建的目录上把卡片校验完：卡片本身不合法就一个都不建。
                    trial = AccountCatalog(accounts={**catalog.accounts, card_id: account})
                    _check(before, put_card(before, card_id, body, trial, create=True), trial)
                except ProviderSettingsError as e:
                    raise _error(400, "invalid_settings", str(e)) from None
                try:
                    created = create_account(settings_dir, card_id, account.to_dict()).account_id
                except NotPublished:
                    raise _error(500, "not_written", "账户没建成，什么都没变") from None
                except PublishedNotDurable:
                    raise _error(
                        500, "account_not_durable",
                        "账户已经写入但耐久无法证实，卡片没有写；核对后把卡片挂到这个账户上重试",
                        account=card_id,
                    ) from None
                except ProviderSettingsError as e:
                    raise _error(400, "invalid_settings", str(e)) from None
                catalog = load_accounts(settings_dir)
            try:
                after = put_card(before, card_id, body, catalog, create=True)
            except ProviderSettingsError as e:
                raise _card_failed(created, 400, "invalid_settings", str(e)) from None
            try:
                _check(before, after, catalog)
                result = _commit(candidate_payload(after, updated_by=_updated_by(request)), "面板新建模型卡片")
            except HTTPException as e:
                if created is None:
                    raise
                raise _card_failed(created, e.status_code, e.detail["category"], e.detail["message"]) from None
            return {**result, "card": card_id, "account": created}
    except ProviderSwitchBusy as e:
        raise _error(409, "provider_switch_in_progress", str(e)) from None


def _card_failed(account: Optional[str], status: int, category: str, message: str) -> HTTPException:
    """账户已经建好、卡片没写成：带上账户 id，让调用方明确地挂到它上面重试。"""
    if account is None:
        return _error(status, category, message)
    if category == "settings_quarantined":
        return _error(status, category, message, account=account)
    return _error(
        status, "card_failed", f"账户已经建好，卡片没有写成：{message}",
        account=account, cause=category,
    )


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
