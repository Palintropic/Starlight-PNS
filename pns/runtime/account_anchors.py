# pns/runtime/account_anchors.py — 按账户的手填余额锚点（CFG-1 §4.3、§8.1 manual、§8.2）
#
# 锚点存在 usage/balance_anchors.json（形状由 env_migration 定）：
#
#     {"version": 1, "anchors": {account_id: {balance, currency, at, account_created_at}},
#      "migrated_single_anchor": true}
#
# 锚点有效 ⇔ 账户在目录里、没退役、`account_created_at` 和币种都与账户目录一致。
# 账户 id 不复用，但目录被人手改过、账户被重建过时，旧锚点必须失效，不能接着
# 从一个已经不是它的账户上往下扣。无效的锚点不删：给出原因，等操作员重新填。
#
# 写锚点是读-改-写整份文件：同一进程里串行（模块锁），写盘走三档合同。
import json
import math
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from pns.runtime import settings_files
from pns.runtime.env_migration import ANCHORS_FILE, ANCHORS_VERSION, read_anchors
from pns.runtime.provider_settings import Account, AccountCatalog
from pns.runtime.usage_ledger import AccountView, BalanceAnchor, _parse_at, validate_anchor

# 锚点无效的原因（封闭枚举，界面按它出固定文案）。
PROBLEM_RETIRED = "retired"
PROBLEM_ACCOUNT_RECREATED = "account_recreated"
PROBLEM_CURRENCY_MISMATCH = "currency_mismatch"
PROBLEM_MALFORMED = "malformed"

_WRITE_LOCK = threading.Lock()


class AccountAnchorError(ValueError):
    """锚点请求不合规。消息是给操作者看的固定措辞。"""


@dataclass(frozen=True)
class StoredAnchor:
    balance: float
    currency: str
    at: datetime
    account_created_at: str


def _parse_entry(raw: Any) -> Optional[StoredAnchor]:
    if not isinstance(raw, dict):
        return None
    balance, currency, created = raw.get("balance"), raw.get("currency"), raw.get("account_created_at")
    if (
        type(balance) not in (int, float)
        or not math.isfinite(balance)
        or not isinstance(currency, str)
        or not isinstance(created, str)
    ):
        return None
    try:
        at = _parse_at(raw.get("at"))
    except ValueError:
        return None
    return StoredAnchor(float(balance), currency, at, created)


def problem_of(entry: Optional[StoredAnchor], account: Account) -> Optional[str]:
    """None 表示锚点有效。"""
    if entry is None:
        return PROBLEM_MALFORMED
    if account.retired:
        return PROBLEM_RETIRED
    if entry.account_created_at != account.created_at:
        return PROBLEM_ACCOUNT_RECREATED
    if entry.currency != account.currency:
        return PROBLEM_CURRENCY_MISMATCH
    return None


def account_views(usage_root: Path, catalog: AccountCatalog) -> Dict[str, AccountView]:
    """账户目录里每个账户一项：币种 + 有效锚点，或者锚点为什么不算数。

    锚点文件读不出来照 env_migration 的规矩抛 AnchorsUnreadable，不猜。
    """
    stored = read_anchors(usage_root)
    raw_anchors: Mapping[str, Any] = stored["anchors"] if stored is not None else {}
    views: Dict[str, AccountView] = {}
    for account_id, account in catalog.accounts.items():
        anchor, problem = None, None
        if account_id in raw_anchors:
            entry = _parse_entry(raw_anchors[account_id])
            problem = problem_of(entry, account)
            if problem is None:
                anchor = BalanceAnchor(entry.balance, entry.at)
        views[account_id] = AccountView(
            currency=account.currency, anchor=anchor, anchor_problem=problem,
            retired=account.retired,
        )
    return views


def save_account_anchor(
    usage_root: Path, account: Optional[Account], balance: Any, at: Optional[datetime], now: datetime
) -> Dict[str, Any]:
    """记下某个账户"控制台上看到的此刻余额"（账户原币种）。返回写进去的那一项。"""
    if account is None:
        raise AccountAnchorError("没有这个账户")
    if account.retired:
        raise AccountAnchorError("账户已退役，不能再填余额")
    try:
        anchor = validate_anchor(balance, at, now)
    except ValueError as e:
        raise AccountAnchorError(str(e)) from None
    entry = {
        "balance": anchor.balance_yuan,
        "currency": account.currency,
        "at": anchor.to_dict()["at"],
        "account_created_at": account.created_at,
    }
    with _WRITE_LOCK:
        current = read_anchors(usage_root)
        payload = dict(current) if current is not None else {"version": ANCHORS_VERSION, "anchors": {}}
        payload["anchors"] = {**payload["anchors"], account.account_id: entry}
        settings_files.durable_write(
            Path(usage_root) / ANCHORS_FILE,
            (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        )
    return {"account": account.account_id, **entry}


__all__ = [
    "AccountAnchorError",
    "PROBLEM_ACCOUNT_RECREATED",
    "PROBLEM_CURRENCY_MISMATCH",
    "PROBLEM_MALFORMED",
    "PROBLEM_RETIRED",
    "account_views",
    "problem_of",
    "save_account_anchor",
]
