# tests/accounts_support.py — AUTH-1 测试共用的账户材料
#
# 生产的 Argon2id 参数是 64 MiB × 3 轮，一次校验大约 60 毫秒。这套测试要登录
# 几百次，所以测试里用**便宜参数**的 hasher。这不是把安全性调低了：参数写在
# 编码后的哈希串里，生产进程用的仍然是 `PasswordHasher()` 的默认值，而
# `tests/test_deployment_security.py` 里有一条用例专门盯住"默认参数没被改过"。
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from argon2 import PasswordHasher  # noqa: E402

from pns.interfaces.accounts import AccountStore  # noqa: E402

CHEAP_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)

ADMIN_USERNAME = "test-admin"
ADMIN_PASSWORD = "pns-test-password-1"
OTHER_PASSWORD = "pns-test-password-2"

# 用便宜参数预先算好的哈希，字面量写死：子进程测试因此不用现算一遍，而且它
# 恰好也是操作者要贴进 `.env` 的那个形状。
ADMIN_PASSWORD_HASH = (
    "$argon2id$v=19$m=8,t=1,p=1$4jZyXL7fhLrDs3+lcpyprQ"
    "$nrlYqj++2/fcqKqPGKx5+PgaTZB8OPJ5rTc12jiJ4YY"
)
OTHER_PASSWORD_HASH = (
    "$argon2id$v=19$m=8,t=1,p=1$0ud8LvXMLByyw/gm0u29Rg"
    "$fhGv+Q3rmjG8TZZtqa27pmqwbpHVyuX7Lt0HFR1vz3k"
)


def cheap_store(path) -> AccountStore:
    """一个用便宜 Argon2 参数的账户库。"""
    return AccountStore(path, hasher=CHEAP_HASHER)


def bootstrap_env(db_path) -> dict:
    """让一个生产进程能起来的最小账户配置。

    变量名从部署层现取而不是写死。这个 import 放在函数里：拿一个便宜的账户库
    不该顺带把部署层拖进来——存储层本来就不认识部署配置。
    """
    from pns.interfaces.security import (
        ENV_ACCOUNTS_DB,
        ENV_BOOTSTRAP_PASSWORD_HASH,
        ENV_BOOTSTRAP_USERNAME,
    )

    return {
        ENV_ACCOUNTS_DB: str(db_path),
        ENV_BOOTSTRAP_USERNAME: ADMIN_USERNAME,
        ENV_BOOTSTRAP_PASSWORD_HASH: ADMIN_PASSWORD_HASH,
    }
