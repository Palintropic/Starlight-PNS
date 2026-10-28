# pns/runtime/env_migration.py — `env` 账户落盘与旧单锚点迁移（CFG-1 §4.3、§13 第 4 条）
#
# 新代码在生产上第一次启动时做两件事，每次启动都跑、都幂等：
#
#   1. `env` 账户写进 settings/accounts.json（已存在就不动，created_at 不变）；
#   2. 旧的单锚点 usage/balance_anchor.json 迁移成 `env` 账户的锚点，写进
#      usage/balance_anchors.json，同一次写入里带上"已迁移"标记。
#
# 锚点和标记在**同一个文件、同一次耐久写入**里：不存在"锚点写了、标记没写"或
# "标记写了、锚点没写"的中间状态，所以不会因为标记写早了而漏迁移。中途在第 1、
# 2 步之间退出，下次启动 `env` 已经在（不重建身份），接着做第 2 步。
#
# 已经有 `env` 锚点（操作员后来填的、或上次迁移写过的）就不覆盖。旧锚点文件保留
# 不删：回滚到旧镜像时它还会读那份（余额语义失效，见 §9）。
#
# 只在生产启动时调用：开发环境启动不往仓库的 data/ 里写东西。
import json
from pathlib import Path
from typing import Any, Dict, Optional

from pns.runtime import settings_files
from pns.runtime.provider_settings import ENV_ACCOUNT, Account, ensure_env_account
from pns.runtime.usage_ledger import load_anchor

ANCHORS_FILE = "balance_anchors.json"
ANCHORS_VERSION = 1
MIGRATED_KEY = "migrated_single_anchor"


class AnchorsUnreadable(RuntimeError):
    """balance_anchors.json 存在但读不出来：不猜，拒绝迁移。"""


def read_anchors(usage_root: Path) -> Optional[Dict[str, Any]]:
    path = Path(usage_root) / ANCHORS_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise AnchorsUnreadable("usage/balance_anchors.json 读不出来") from None
    if (
        not isinstance(raw, dict)
        or raw.get("version") != ANCHORS_VERSION
        or not isinstance(raw.get("anchors"), dict)
    ):
        raise AnchorsUnreadable("usage/balance_anchors.json 的形状不认识")
    return raw


def _env_anchor(account: Account, usage_root: Path) -> Optional[Dict[str, Any]]:
    old = load_anchor(usage_root)
    if old is None:
        return None
    return {
        "balance": old.balance_yuan,
        "currency": account.currency,
        "at": old.to_dict()["at"],
        "account_created_at": account.created_at,
    }


def migrate_env_account(settings_dir: Path, usage_root: Path) -> Dict[str, Any]:
    """做完第 1、2 步，返回迁移后的锚点文件内容。写盘失败照三档合同抛出。"""
    account = ensure_env_account(settings_dir)
    current = read_anchors(usage_root)
    if current is not None and current.get(MIGRATED_KEY):
        return current
    payload = current if current is not None else {"version": ANCHORS_VERSION, "anchors": {}}
    anchors = dict(payload["anchors"])
    if ENV_ACCOUNT not in anchors:
        migrated = _env_anchor(account, usage_root)
        if migrated is not None:
            anchors[ENV_ACCOUNT] = migrated
    payload = {**payload, "anchors": anchors, MIGRATED_KEY: True}
    settings_files.durable_write(
        Path(usage_root) / ANCHORS_FILE,
        (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )
    return payload
