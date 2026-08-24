#!/usr/bin/env python3
# scripts/accounts.py — 账户库的离线管理入口（AUTH-1）
#
# 它存在的理由只有一个：**第一个管理员必须能在没有任何管理员的情况下被造出来。**
# 网页那条路要求先登录，所以它解不开这个死结；而给登录接口开一个"没有账户时
# 谁都能建管理员"的后门，等于给每一台还没配好的服务器留一个公开的提权入口。
#
# 用法（在仓库根或容器里跑）：
#
#     python scripts/accounts.py hash-password
#     python scripts/accounts.py create-admin --username mizuki
#     python scripts/accounts.py list
#     python scripts/accounts.py set-role --username ena --role operator
#     python scripts/accounts.py disable --username ena
#     python scripts/accounts.py reset-password --username ena
#     python scripts/accounts.py audit --limit 50
#
# 生产（Docker）上用一次性容器跑，**不要** exec 进正在跑的那个：
#
#     docker compose run --rm --entrypoint "" app python scripts/accounts.py list
#
# 两件它刻意不做的事：
#
#   * **不打印密码，也不回显。** 输入走 getpass，两次确认；`hash-password`
#     只把哈希写到 stdout，明文一个字节都不落地。
#   * **不碰正在跑的进程的会话表。** 会话在服务器进程的内存里，这个脚本改不到
#     它。但改权威会推进 `security_revision`，而服务器每次请求都拿它去比对，
#     所以停用/改角色/重置密码**在下一次请求就生效**——不需要重启服务。
import argparse
import getpass
import os
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
for _p in (ROOT_DIR, ROOT_DIR / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - 生产镜像里没有 python-dotenv 也能跑
    pass

from pns.interfaces.accounts import (  # noqa: E402
    ROLES,
    AccountError,
    AccountStore,
)
from pns.interfaces.paths import ACCOUNTS_DB_FILE  # noqa: E402
from pns.interfaces.security import ENV_ACCOUNTS_DB  # noqa: E402


def db_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    configured = (os.environ.get(ENV_ACCOUNTS_DB) or "").strip()
    return Path(configured) if configured else ACCOUNTS_DB_FILE


def read_password(prompt: str = "密码", *, confirm: bool = True) -> str:
    """读一个密码。**不回显，两次确认。**

    非交互输入（管道）走 stdin 一行——CI 和自动化需要它，而 getpass 在没有
    tty 时会直接失败。
    """
    if not sys.stdin.isatty():
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            raise SystemExit("从 stdin 没读到密码")
        return password
    first = getpass.getpass(f"{prompt}：")
    if not confirm:
        return first
    if first != getpass.getpass(f"{prompt}（再输一次）："):
        raise SystemExit("两次输入不一致")
    return first


def open_store(args) -> AccountStore:
    path = db_path(args.db)
    if args.command != "create-admin" and args.command != "create" and not path.exists():
        raise SystemExit(f"账户库不存在：{path}（先跑一次 create-admin）")
    try:
        return AccountStore(path)
    except (AccountError, sqlite3.Error, OSError) as e:
        raise SystemExit(f"账户库打不开：{e}")


def print_principals(store: AccountStore) -> None:
    rows = store.list_humans()
    if not rows:
        print("（没有账户）")
        return
    width = max(len(p.username) for p in rows)
    print(f"{'用户名'.ljust(width)}  {'角色':<9} {'状态':<6} principal_id")
    for p in rows:
        print(
            f"{p.username.ljust(width)}  {p.role:<9} "
            f"{'启用' if p.enabled else '已停用':<6} {p.principal_id}"
        )


def cmd_hash_password(args) -> int:
    """只算哈希，不碰任何库。产出直接贴进 .env 的 PNS_BOOTSTRAP_ADMIN_PASSWORD_HASH。"""
    from argon2 import PasswordHasher

    from pns.interfaces.accounts import MAX_PASSWORD_CHARS, MIN_PASSWORD_CHARS

    password = read_password()
    if not MIN_PASSWORD_CHARS <= len(password) <= MAX_PASSWORD_CHARS:
        raise SystemExit(
            f"密码长度必须是 {MIN_PASSWORD_CHARS}–{MAX_PASSWORD_CHARS} 个字符"
        )
    print(PasswordHasher().hash(password))
    return 0


def cmd_create(args) -> int:
    store = open_store(args)
    try:
        role = getattr(args, "role", "admin")
        if store.find_by_username(args.username) is not None:
            raise SystemExit(f"用户名已存在：{args.username}")
        password = read_password()
        created = store.create_human(args.username, password, role, actor=None)
    except AccountError as e:
        raise SystemExit(str(e))
    print(f"已创建 {created.username}（{created.role}）：{created.principal_id}")
    store.close()
    return 0


def cmd_list(args) -> int:
    store = open_store(args)
    print_principals(store)
    store.close()
    return 0


def _target(store: AccountStore, username: str):
    principal = store.find_by_username(username)
    if principal is None:
        raise SystemExit(f"没有这个账户：{username}")
    return principal


def cmd_set_role(args) -> int:
    store = open_store(args)
    target = _target(store, args.username)
    try:
        updated = store.set_authority(target.principal_id, role=args.role, actor=None)
    except AccountError as e:
        raise SystemExit(str(e))
    print(f"{updated.username} 现在是 {updated.role}；该账户的所有会话已失效")
    store.close()
    return 0


def cmd_enabled(args) -> int:
    store = open_store(args)
    target = _target(store, args.username)
    enabled = args.command == "enable"
    try:
        updated = store.set_authority(
            target.principal_id, enabled=enabled, actor=None
        )
    except AccountError as e:
        raise SystemExit(str(e))
    print(
        f"{updated.username} 已{'启用' if updated.enabled else '停用'}；"
        f"该账户的所有会话已失效"
    )
    store.close()
    return 0


def cmd_reset_password(args) -> int:
    store = open_store(args)
    target = _target(store, args.username)
    password = read_password("新密码")
    try:
        store.set_password(target.principal_id, password, actor=None)
    except AccountError as e:
        raise SystemExit(str(e))
    print(f"{target.username} 的密码已重置；该账户的所有会话已失效")
    store.close()
    return 0


def cmd_audit(args) -> int:
    store = open_store(args)
    names = {p.principal_id: p.username for p in store.list_humans()}
    for record in reversed(store.audit_records(args.limit)):
        actor = names.get(record["actor_principal_id"]) or record["actor_principal_id"]
        target = (
            names.get(record["target_principal_id"]) or record["target_principal_id"]
        )
        detail = record["detail"]
        extra = f" {detail}" if detail else ""
        print(
            f"{record['occurred_at']}  {record['action']:<24} {record['result']:<8}"
            f" actor={actor} target={target}{extra}"
        )
    store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Starlight-PNS 账户管理")
    parser.add_argument(
        "--db", default=None, help=f"账户库路径（默认 {ENV_ACCOUNTS_DB} 或 data/accounts.sqlite3）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("hash-password", help="只生成 Argon2id 哈希，不写任何库")

    create_admin = sub.add_parser("create-admin", help="创建一个管理员账户")
    create_admin.add_argument("--username", required=True)

    create = sub.add_parser("create", help="创建一个账户")
    create.add_argument("--username", required=True)
    create.add_argument("--role", choices=ROLES, default="observer")

    sub.add_parser("list", help="列出账户")

    set_role = sub.add_parser("set-role", help="改角色")
    set_role.add_argument("--username", required=True)
    set_role.add_argument("--role", choices=ROLES, required=True)

    for name, help_text in (("disable", "停用账户"), ("enable", "启用账户")):
        action = sub.add_parser(name, help=help_text)
        action.add_argument("--username", required=True)

    reset = sub.add_parser("reset-password", help="重置某个账户的密码")
    reset.add_argument("--username", required=True)

    audit = sub.add_parser("audit", help="打印安全审计记录")
    audit.add_argument("--limit", type=int, default=50)
    return parser


HANDLERS = {
    "hash-password": cmd_hash_password,
    "create-admin": cmd_create,
    "create": cmd_create,
    "list": cmd_list,
    "set-role": cmd_set_role,
    "disable": cmd_enabled,
    "enable": cmd_enabled,
    "reset-password": cmd_reset_password,
    "audit": cmd_audit,
}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return HANDLERS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
