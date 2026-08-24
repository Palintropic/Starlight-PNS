# pns/interfaces/accounts.py — 人类账户与主体（AUTH-1）
#
# 这一层只回答一件事：**这台服务器上有哪些人，他们各自有多大权限。**
# 它不知道 HTTP、不知道 Cookie、不知道世界；反过来，世界也永远不知道它。
#
# 四条边界，每一条都对应一种具体的坏法：
#
#   * **人不是角色。** Dashboard 账户和世界里的角色是两种实体。用户名叫
#     "mizuki" 不会让谁变成 25 时的那个 mizuki——`kind` 是 `human`，而角色
#     根本不在这张表里。以后 Sekai Times 的 service principal 走 `service`，
#     用同一套 principal_id / scopes / 审计词汇，但同样不是人、也不是角色。
#   * **权威是 principal_id，不是用户名。** 用户名可以改、可以复用显示形式；
#     一次授权、一条审计记录指向的永远是那个不变的不透明 ID。
#   * **明文密码在这个模块里活不过一次函数调用。** 只存 Argon2id 哈希，
#     不记日志、不进审计、不进任何返回值。审计里连"尝试用的用户名"都不记
#     ——把密码打进用户名框是每天都在发生的事，记下来就等于把它写进磁盘。
#   * **权威变更是事务性的。** "最后一个管理员"这条不变量在 `BEGIN IMMEDIATE`
#     拿到写锁之后才求值、并在同一个事务里落地。两个并发的"降级最后一个管理员"
#     不可能都通过——它们被 SQLite 的写锁串行化，第二个看到的是第一个的结果。
#
# 存储刻意用 SQLite 而不是 JSON 文件：需要的是"读-改-写"之间没有缝，而不是
# 一次原子替换。journal_mode 保持 DELETE（不开 WAL）——账户库跟着 P12 的
# 数据卷一起备份，WAL 会让"拷走这一个文件"不再等于"拷走这个库"。
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)

# ── 角色与权限 ──────────────────────────────────────────────────────────
#
# 角色是**权限的捆绑**，不是散落在各条路由里的 if。路由问的永远是"有没有这个
# scope"，不是"是不是管理员"——所以新增一个角色不需要回去改每一条路由。
SCOPE_READ = "read"
SCOPE_OPERATE = "operate"
SCOPE_ACCOUNTS = "accounts:manage"

ALL_SCOPES = frozenset({SCOPE_READ, SCOPE_OPERATE, SCOPE_ACCOUNTS})

ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_OBSERVER = "observer"
ROLES: Tuple[str, ...] = (ROLE_ADMIN, ROLE_OPERATOR, ROLE_OBSERVER)

ROLE_SCOPES: Dict[str, frozenset] = {
    ROLE_ADMIN: frozenset({SCOPE_READ, SCOPE_OPERATE, SCOPE_ACCOUNTS}),
    ROLE_OPERATOR: frozenset({SCOPE_READ, SCOPE_OPERATE}),
    ROLE_OBSERVER: frozenset({SCOPE_READ}),
}

KIND_HUMAN = "human"
KIND_SERVICE = "service"
KINDS: Tuple[str, ...] = (KIND_HUMAN, KIND_SERVICE)

# 用户名是**登录标识**，不是密码。它的规则只服务一件事：两个看起来一样的
# 名字不能同时存在。
USERNAME_MIN_CHARS = 3
USERNAME_MAX_CHARS = 64
USERNAME_EXTRA_CHARS = "._-"

# 密码长度下限。Argon2id 让离线猜测昂贵，长度让在线猜测没有意义；两道闸缺一
# 不可，所以这里不接受"短但很复杂"的那种自我安慰。
MIN_PASSWORD_CHARS = 12
MAX_PASSWORD_CHARS = 512

# 这个库的 schema 版本。**读到比自己新的版本就拒绝打开**：一个旧进程按自己
# 的理解去写一个新 schema，比起不起得来危险得多（升级/回滚见部署文档）。
SCHEMA_VERSION = 1

# 登录失败的类别。它们只进审计，**不进响应**——响应里"未知用户"和"密码不对"
# 必须是同一句话，否则登录框就成了一台用户名枚举机。
LOGIN_UNKNOWN = "unknown_user"
LOGIN_BAD_PASSWORD = "bad_password"
LOGIN_DISABLED = "disabled"
LOGIN_MALFORMED = "malformed_request"
LOGIN_SUCCESS = "success"


class AccountError(RuntimeError):
    """账户操作不成立。`category` 是给 API 用的稳定类别。"""

    category = "account_error"


class AccountConflict(AccountError):
    category = "account_conflict"


class LastAdminError(AccountError):
    category = "last_admin"


class InvalidUsername(AccountError):
    category = "invalid_username"


class InvalidPassword(AccountError):
    category = "invalid_password"


class AccountStoreError(AccountError):
    """这个账户库本身打不开或者不该被这个版本打开。"""

    category = "account_store_unavailable"


@dataclass(frozen=True)
class Principal:
    """一个持久主体。**没有密码字段，也永远不会有。**"""

    principal_id: str
    username: str
    kind: str
    role: str
    enabled: bool
    security_revision: int
    created_at: str = ""
    updated_at: str = ""

    @property
    def scopes(self) -> frozenset:
        return ROLE_SCOPES[self.role]

    def public_dict(self) -> Dict[str, Any]:
        """能给浏览器看的那一份。哈希、security_revision 都不在里面：
        前者是凭据材料，后者是服务端的会话判据，浏览器拿它没有用处。"""
        return {
            "principal_id": self.principal_id,
            "username": self.username,
            "kind": self.kind,
            "role": self.role,
            "scopes": sorted(self.scopes),
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class LoginOutcome:
    """一次登录尝试的结果。

    `reason` 是给**审计**的类别，不是给响应的。调用方必须把所有失败翻译成
    同一句话——这个类型把"我知道失败原因"和"我要把它说出去"分开，正是为了
    让后者成为一次显式的错误决定，而不是顺手。
    """

    principal: Optional[Principal]
    reason: str

    @property
    def ok(self) -> bool:
        return self.principal is not None


def canonical_username(value: object) -> Tuple[str, str]:
    """把一个用户名规范化成 (显示形式, 唯一键)。

    唯一键是**唯一**的判重规则：NFKC 折叠掉全角/兼容形式，casefold 折叠掉
    大小写。字符集限制在 ASCII 是这条规则的一部分而不是额外的洁癖——
    西里尔字母 "а" 是 alnum、casefold 之后仍然是它自己，于是 "аdmin" 会成为
    一个和 "admin" 并存、但在任何界面上都看不出区别的账户。
    """
    if not isinstance(value, str):
        raise InvalidUsername("用户名必须是字符串")
    display = unicodedata.normalize("NFKC", value).strip()
    if not USERNAME_MIN_CHARS <= len(display) <= USERNAME_MAX_CHARS:
        raise InvalidUsername(
            f"用户名长度必须是 {USERNAME_MIN_CHARS}–{USERNAME_MAX_CHARS} 个字符"
        )
    if not display.isascii():
        raise InvalidUsername("用户名只能用 ASCII 字母、数字和 . _ -")
    if not all(ch.isalnum() or ch in USERNAME_EXTRA_CHARS for ch in display):
        raise InvalidUsername("用户名只能包含字母、数字和 . _ -")
    if not display[0].isalnum():
        raise InvalidUsername("用户名必须以字母或数字开头")
    return display, display.casefold()


def _validate_argon2id(encoded: object) -> str:
    """这串东西是不是一个 Argon2id 哈希。**不接受"能解析就行"。**

    只检查能解析的话，一个 `$argon2i$...` 会被收下——它是合法的 Argon2 编码，
    但不是这个板子承诺存的那一种。bootstrap 是唯一一条由操作者手写哈希进来的
    路径，正是最该严的地方。
    """
    if not isinstance(encoded, str) or not encoded.startswith("$argon2id$"):
        raise InvalidPassword("bootstrap 密码必须是 Argon2id 哈希（$argon2id$ 开头）")
    try:
        extract_parameters(encoded)
    except Exception as exc:  # argon2 用 InvalidHashError，但别的解析错也算
        raise InvalidPassword("bootstrap 密码不是一个可解析的 Argon2id 哈希") from exc
    return encoded


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS principals (
    principal_id      TEXT PRIMARY KEY,
    username          TEXT NOT NULL,
    canonical_username TEXT NOT NULL UNIQUE,
    kind              TEXT NOT NULL CHECK(kind IN ('human','service')),
    role              TEXT NOT NULL CHECK(role IN ('admin','operator','observer')),
    enabled           INTEGER NOT NULL CHECK(enabled IN (0,1)),
    password_hash     TEXT,
    security_revision INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
-- 审计里的 actor 刻意**没有**外键约束：break-glass bearer 是一个不在这张表里
-- 的主体，而"谁做的"这件事不该因为他不是一条账户记录就记不下来。
CREATE TABLE IF NOT EXISTS auth_audit (
    sequence           INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at        TEXT NOT NULL,
    actor_principal_id TEXT,
    target_principal_id TEXT,
    action             TEXT NOT NULL,
    result             TEXT NOT NULL,
    detail_json        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_audit_time ON auth_audit(occurred_at);
"""


class AccountStore:
    """一个小而事务化的身份库。

    进程内用一把可重入锁串行化这条共享连接；跨进程靠 `BEGIN IMMEDIATE` 的写锁。
    两者缺一不可：前者让同一个进程里的线程池不会撞在一条 sqlite3 连接上，后者
    让"最后一个管理员"这类不变量在**任何**写者面前都只被求值一次。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        hasher: Optional[PasswordHasher] = None,
        timeout: float = 10.0,
    ) -> None:
        self.path = str(path)
        self._memory = self.path == ":memory:"
        if not self._memory:
            target = Path(self.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            # **先用 0600 把文件创出来，再连**。反过来的话，从 sqlite3 建出
            # 文件到 chmod 生效之间有一个窗口，而那个窗口里已经可以写进哈希了。
            # SQLite 会把回滚日志的权限对齐到主库文件，所以这一次 chmod 也管住
            # 了 `-journal`。
            if not target.exists():
                fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.close(fd)
            else:
                target.chmod(0o600)
        self._db = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None, timeout=timeout
        )
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._hasher = hasher or PasswordHasher()
        # 未知用户也要付一次 Argon2 的钱，否则"这个用户名存不存在"可以用秒表
        # 问出来。这份影子哈希在构造时算一次——放到第一次未知用户登录时再算，
        # 那一次的耗时形状恰好又不一样。
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(24))
        try:
            self._initialize()
        except Exception:
            self._db.close()
            raise

    # ── 生命周期 ────────────────────────────────────────────────────────
    def _initialize(self) -> None:
        with self._lock:
            # PRAGMA 要在任何事务之外执行才生效；executescript 会先提交，所以
            # 这两条刻意单独走。
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.executescript(_SCHEMA)
            row = self._db.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO schema_meta(key,value) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),),
                )
                return
            try:
                found = int(row["value"])
            except (TypeError, ValueError) as exc:
                raise AccountStoreError(
                    f"账户库 {self.path} 的 schema 版本读不出来，拒绝按猜测打开"
                ) from exc
            if found > SCHEMA_VERSION:
                raise AccountStoreError(
                    f"账户库 {self.path} 的 schema 版本是 {found}，"
                    f"本进程只认到 {SCHEMA_VERSION}；请回滚代码或按部署文档升级"
                )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _principal(row: sqlite3.Row) -> Principal:
        return Principal(
            principal_id=row["principal_id"],
            username=row["username"],
            kind=row["kind"],
            role=row["role"],
            enabled=bool(row["enabled"]),
            security_revision=int(row["security_revision"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ── 密码 ────────────────────────────────────────────────────────────
    def hash_password(self, password: object) -> str:
        """校验密码策略并返回 Argon2id 哈希。**明文到此为止。**

        刻意不 strip、不 NFKC：那是在悄悄改掉一个秘密，然后让用户在别的客户端
        上登不进来。
        """
        if not isinstance(password, str):
            raise InvalidPassword("密码必须是字符串")
        if not MIN_PASSWORD_CHARS <= len(password) <= MAX_PASSWORD_CHARS:
            raise InvalidPassword(
                f"密码长度必须是 {MIN_PASSWORD_CHARS}–{MAX_PASSWORD_CHARS} 个字符"
            )
        if not password.strip():
            raise InvalidPassword("密码不能全是空白字符")
        return self._hasher.hash(password)

    def _verify(self, encoded: Optional[str], password: object) -> bool:
        """定时形状尽量一致的口令校验。

        无论用户存不存在、有没有哈希，都真的跑一次 Argon2。返回值只有真/假，
        `VerifyMismatchError` 之外的解析错也是假——但**别的**异常照旧抛出去：
        一次说不清的失败该变成 500，而不是一次静悄悄的"密码不对"。
        """
        candidate = encoded if isinstance(encoded, str) and encoded else self._dummy_hash
        material = password if isinstance(password, str) else ""
        try:
            return bool(self._hasher.verify(candidate, material))
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    # ── 审计 ────────────────────────────────────────────────────────────
    def _audit(
        self,
        actor: Optional[str],
        target: Optional[str],
        action: str,
        result: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """写一条审计。**detail 由调用方逐字段构造，永远不放原始异常或凭据。**"""
        self._db.execute(
            "INSERT INTO auth_audit"
            "(occurred_at,actor_principal_id,target_principal_id,action,result,detail_json)"
            " VALUES(?,?,?,?,?,?)",
            (
                self._now(),
                actor,
                target,
                action,
                result,
                json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
            ),
        )

    def record_event(
        self,
        action: str,
        result: str,
        *,
        actor: Optional[str] = None,
        target: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """给接口层用的审计入口（登出、跨主体事件等）。"""
        with self._lock:
            self._audit(actor, target, action, result, detail)

    def audit_records(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM auth_audit ORDER BY sequence DESC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            raw = record.pop("detail_json", "{}")
            try:
                record["detail"] = json.loads(raw)
            except (TypeError, ValueError):  # pragma: no cover - 只有我们写它
                record["detail"] = {}
            records.append(record)
        return records

    # ── 读 ──────────────────────────────────────────────────────────────
    def get(self, principal_id: str) -> Principal:
        principal = self.find(principal_id)
        if principal is None:
            raise KeyError(principal_id)
        return principal

    def find(self, principal_id: object) -> Optional[Principal]:
        if not isinstance(principal_id, str) or not principal_id:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM principals WHERE principal_id=?", (principal_id,)
            ).fetchone()
        return self._principal(row) if row is not None else None

    def find_by_username(self, username: object) -> Optional[Principal]:
        """按用户名找账户。**只给管理工具用，不是登录路径。**

        登录不走这里：那条路必须无论用户名存不存在都付一样的代价（见
        `authenticate`），而这个函数在用户名不合法时立刻返回。
        """
        try:
            _, canonical = canonical_username(username)
        except InvalidUsername:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM principals WHERE canonical_username=?", (canonical,)
            ).fetchone()
        return self._principal(row) if row is not None else None

    def list_humans(self) -> List[Principal]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM principals WHERE kind='human' ORDER BY canonical_username"
            ).fetchall()
        return [self._principal(row) for row in rows]

    def count(self) -> int:
        with self._lock:
            return int(
                self._db.execute("SELECT count(*) FROM principals").fetchone()[0]
            )

    def enabled_admin_count(self) -> int:
        with self._lock:
            return self._enabled_admins()

    def has_enabled_admin(self) -> bool:
        return self.enabled_admin_count() > 0

    def _enabled_admins(self) -> int:
        return int(
            self._db.execute(
                "SELECT count(*) FROM principals"
                " WHERE kind='human' AND role='admin' AND enabled=1"
            ).fetchone()[0]
        )

    # ── 写 ──────────────────────────────────────────────────────────────
    def create_human(
        self,
        username: str,
        password: str,
        role: str,
        *,
        actor: Optional[str] = None,
    ) -> Principal:
        display, canonical = canonical_username(username)
        if role not in ROLES:
            raise AccountError(f"未知角色：{role!r}")
        password_hash = self.hash_password(password)
        principal_id = str(uuid.uuid4())
        now = self._now()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "INSERT INTO principals VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        principal_id, display, canonical, KIND_HUMAN, role, 1,
                        password_hash, 1, now, now,
                    ),
                )
                self._audit(
                    actor, principal_id, "account.create", "success", {"role": role}
                )
                self._db.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self._db.execute("ROLLBACK")
                raise AccountConflict("这个用户名已经被占用了") from exc
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return self.get(principal_id)

    def bootstrap_admin(
        self, username: str, password_hash: str
    ) -> Optional[Principal]:
        """在**空库**上创建第一个管理员。已有任何主体时什么都不做，返回 None。

        幂等性靠的不是"我记得已经做过"，而是"存在性检查和插入在同一个写事务
        里"：两个并发启动的进程不可能都插进去，第二个看到的是第一个的结果。
        """
        display, canonical = canonical_username(username)
        encoded = _validate_argon2id(password_hash)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if self._db.execute("SELECT 1 FROM principals LIMIT 1").fetchone():
                    self._db.execute("COMMIT")
                    return None
                principal_id = str(uuid.uuid4())
                now = self._now()
                self._db.execute(
                    "INSERT INTO principals VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        principal_id, display, canonical, KIND_HUMAN, ROLE_ADMIN, 1,
                        encoded, 1, now, now,
                    ),
                )
                self._audit(None, principal_id, "account.bootstrap", "success")
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return self.get(principal_id)

    def set_authority(
        self,
        target: str,
        *,
        role: Optional[str] = None,
        enabled: Optional[bool] = None,
        actor: Optional[str] = None,
    ) -> Principal:
        """改角色/启用状态。**总是推进 security_revision。**

        即使这次改动在数值上等于没改，也推进：会话是按 revision 判活的，而
        "这次改动没实际变化所以不必踢掉会话"是一个需要额外论证的优化，
        它换来的是一整类"以为已经撤销了"的漏网。
        """
        if role is not None and role not in ROLES:
            raise AccountError(f"未知角色：{role!r}")
        if role is None and enabled is None:
            raise AccountError("没有要修改的权威字段")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT * FROM principals WHERE principal_id=?", (target,)
                ).fetchone()
                if row is None:
                    raise KeyError(target)
                old = self._principal(row)
                if old.kind != KIND_HUMAN:
                    raise AccountError("这个主体不是人类账户")
                new_role = old.role if role is None else role
                new_enabled = old.enabled if enabled is None else bool(enabled)
                losing_admin = old.role == ROLE_ADMIN and old.enabled and (
                    new_role != ROLE_ADMIN or not new_enabled
                )
                if losing_admin and self._enabled_admins() <= 1:
                    raise LastAdminError(
                        "这是最后一个可用的管理员，不能停用或降级"
                        "——那会让这台服务器再也没人能管账户"
                    )
                self._db.execute(
                    "UPDATE principals SET role=?,enabled=?,"
                    "security_revision=security_revision+1,updated_at=?"
                    " WHERE principal_id=?",
                    (new_role, int(new_enabled), self._now(), target),
                )
                self._audit(
                    actor, target,
                    "account.role" if role is not None else "account.enabled",
                    "success",
                    {"role": new_role, "enabled": new_enabled},
                )
                self._db.execute("COMMIT")
            except LastAdminError:
                # 审计必须写在**回滚之后**：写在事务里的话，它会跟着那次被拒绝
                # 的改动一起消失，于是"有人试图移除最后一个管理员"这条信号就
                # 只存在于一次 409 响应里，没人回头看得到。
                self._db.execute("ROLLBACK")
                self._audit(
                    actor, target, "account.authority", "failure",
                    {"reason": "last_admin"},
                )
                raise
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return self.get(target)

    def set_password(
        self,
        target: str,
        password: str,
        *,
        actor: Optional[str] = None,
        action: str = "account.password_reset",
    ) -> Principal:
        """重置某个账户的密码。同样推进 security_revision（旧会话立刻作废）。"""
        password_hash = self.hash_password(password)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                changed = self._db.execute(
                    "UPDATE principals SET password_hash=?,"
                    "security_revision=security_revision+1,updated_at=?"
                    " WHERE principal_id=? AND kind='human'",
                    (password_hash, self._now(), target),
                ).rowcount
                if not changed:
                    raise KeyError(target)
                self._audit(actor, target, action, "success")
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return self.get(target)

    def change_own_password(
        self, target: str, current_password: object, new_password: object
    ) -> Principal:
        """本人改密码。先证明自己知道旧密码，再落新的。

        旧密码不对时**也**要写一条审计：一串失败的改密尝试是这台服务器上最像
        "有人拿着一张偷来的 Cookie"的信号。
        """
        principal = self.find(target)
        if principal is None or principal.kind != KIND_HUMAN:
            raise KeyError(target)
        with self._lock:
            row = self._db.execute(
                "SELECT password_hash FROM principals WHERE principal_id=?", (target,)
            ).fetchone()
        if not self._verify(row["password_hash"] if row else None, current_password):
            self.record_event(
                "auth.password_change", "failure",
                actor=target, target=target, detail={"reason": LOGIN_BAD_PASSWORD},
            )
            raise InvalidPassword("当前密码不正确")
        return self.set_password(
            target, new_password, actor=target, action="auth.password_change"
        )

    # ── 登录 ────────────────────────────────────────────────────────────
    def authenticate(self, username: object, password: object) -> LoginOutcome:
        """用户名 + 密码。**每一条路径都跑一次 Argon2。**

        包括用户名根本不合法的那一条：一个"格式不对所以立刻返回"的分支，会让
        "这个用户名存不存在"变成一次可以用秒表读出来的问答。
        """
        try:
            _, canonical = canonical_username(username)
            malformed = False
        except InvalidUsername:
            canonical, malformed = "", True
        with self._lock:
            row = (
                None
                if malformed
                else self._db.execute(
                    "SELECT * FROM principals WHERE canonical_username=? AND kind='human'",
                    (canonical,),
                ).fetchone()
            )
        valid = self._verify(row["password_hash"] if row is not None else None, password)
        principal = self._principal(row) if row is not None else None

        if malformed:
            reason = LOGIN_MALFORMED
        elif principal is None:
            reason = LOGIN_UNKNOWN
        elif not valid:
            reason = LOGIN_BAD_PASSWORD
        elif not principal.enabled:
            reason = LOGIN_DISABLED
        else:
            reason = LOGIN_SUCCESS

        granted = principal if reason == LOGIN_SUCCESS else None
        # 审计里**不写尝试用的用户名**：把密码打进用户名框每天都在发生，记下来
        # 就等于把一个明文密码写进了磁盘。存在的账户记 target，不存在的什么都
        # 不记——"有人试了一个不存在的名字"这件事本身由 result 和 reason 说清。
        self.record_event(
            "auth.login",
            "success" if granted else "failure",
            actor=granted.principal_id if granted else None,
            target=principal.principal_id if principal else None,
            detail={"reason": reason},
        )
        return LoginOutcome(principal=granted, reason=reason)


__all__ = [
    "ALL_SCOPES",
    "AccountConflict",
    "AccountError",
    "AccountStore",
    "AccountStoreError",
    "InvalidPassword",
    "InvalidUsername",
    "KINDS",
    "KIND_HUMAN",
    "KIND_SERVICE",
    "LastAdminError",
    "LoginOutcome",
    "LOGIN_BAD_PASSWORD",
    "LOGIN_DISABLED",
    "LOGIN_MALFORMED",
    "LOGIN_SUCCESS",
    "LOGIN_UNKNOWN",
    "MAX_PASSWORD_CHARS",
    "MIN_PASSWORD_CHARS",
    "Principal",
    "ROLES",
    "ROLE_ADMIN",
    "ROLE_OBSERVER",
    "ROLE_OPERATOR",
    "ROLE_SCOPES",
    "SCHEMA_VERSION",
    "SCOPE_ACCOUNTS",
    "SCOPE_OPERATE",
    "SCOPE_READ",
    "canonical_username",
]
