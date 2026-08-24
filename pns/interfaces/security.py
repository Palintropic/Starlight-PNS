# pns/interfaces/security.py — 部署模式与管理面鉴权边界
#
# 这一层回答两个问题，而且只回答这两个：
#
#   1. **这台服务器是不是生产？** 生产模式下必填的安全/配置项缺任何一样，
#      进程就起不来（`DeploymentConfigError`）。不存在"缺了就回落到开发模式"
#      这条路——那正是把一台生产服务器变成公开控制面的那一步。
#   2. **这次请求是谁发的？** 默认拒绝：除了一份**显式**的公开清单，其余
#      每一条路径（含 `/ws/run`）都要求一个已认证主体。以后新加的路由默认
#      是被保护的，不是默认公开的——保护靠排除机制，不靠有人记得去加。
#
# "那个人能做什么"是另一个问题，在 `authz.py` 里。这里只负责把一次请求认成
# 一个 `RequestPrincipal`，然后把它挂到 ASGI scope 上。
#
# 四条硬约束：
#
#   * **拒绝发生在任何变更之前。** 鉴权是 ASGI 中间件，包在整个应用外面：
#     它在路由匹配、请求体解析、依赖求解之前就决定放不放行。所以一次被拒绝
#     的请求连请求体都没被读过，更不可能改到任何状态。
#   * **浏览器不持有 bearer 密钥。** `PNS_ADMIN_TOKEN` 是 break-glass /
#     自动化用的**非人类**主体，只走 `Authorization: Bearer`。浏览器登录走
#     用户名 + 密码（AUTH-1），换回来的是 HttpOnly / SameSite=Strict 的会话
#     Cookie。任何一种凭据都不进 JS 包、不进 localStorage、不进 URL。
#   * **会话跟着账户的权威走。** 每张会话记着签发时的 `security_revision`；
#     停用、改角色、改密码都会推进它，于是所有旧会话在**下一次请求**就失效，
#     而不是"等它自己过期"。
#   * **比较是定时安全的，失败是沉默的。** token 比较走 `compare_digest`，
#     响应里不说"长度不对"或"scheme 不对"这类能拿来做区分的话；登录失败也
#     只有一句话，用户名存不存在从响应里读不出来。
import os
import secrets
import threading
import time
from dataclasses import dataclass
from hmac import compare_digest
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from .accounts import AccountStore, InvalidPassword, _validate_argon2id
from .authz import (
    PRINCIPAL_SCOPE_KEY,
    SAFE_METHODS,
    RequestPrincipal,
    break_glass_principal,
    open_development_principal,
    required_scope,
    session_principal,
)

# ── 环境变量名 ──────────────────────────────────────────────────────────
ENV_MODE = "PNS_ENV"
ENV_ADMIN_TOKEN = "PNS_ADMIN_TOKEN"
ENV_SESSION_TTL = "PNS_SESSION_TTL_SECONDS"
ENV_COOKIE_SECURE = "PNS_SESSION_COOKIE_SECURE"
# AUTH-1：账户库位置与首个管理员的引导。
ENV_ACCOUNTS_DB = "PNS_ACCOUNTS_DB"
ENV_BOOTSTRAP_USERNAME = "PNS_BOOTSTRAP_ADMIN_USERNAME"
ENV_BOOTSTRAP_PASSWORD_HASH = "PNS_BOOTSTRAP_ADMIN_PASSWORD_HASH"
# 反向代理改写了 Host 时，浏览器实际访问的源。见 `same_origin`。
ENV_TRUSTED_ORIGINS = "PNS_TRUSTED_ORIGINS"

PRODUCTION = "production"
DEVELOPMENT = "development"
VALID_MODES = (PRODUCTION, DEVELOPMENT)

SESSION_COOKIE = "pns_session"

# 一个能被暴力猜出来的管理 token 等于没有 token。32 个字符是下限，不是建议值；
# 文档里给的生成方式是 `openssl rand -hex 32`（64 字符）。
MIN_ADMIN_TOKEN_CHARS = 32

# `.env.example` 里的占位串，以及几个人人都会先试一遍的值。生产模式下它们
# 一律不算凭据——一份没被改过的示例配置不该能启动一台生产服务器。
PLACEHOLDER_TOKENS = frozenset(
    {
        "replace-with-openssl-rand-hex-32-output",
        "change-me",
        "changeme",
        "your-admin-token-here",
        "secret",
        "password",
        "admin",
    }
)

DEFAULT_SESSION_TTL_SECONDS = 12 * 3600
MAX_SESSION_TTL_SECONDS = 7 * 24 * 3600
# 同时有效的会话数上限。多用户之后它比以前更容易被摸到，但它挡的仍然是
# "反复登录把进程内存撑大"这一种玩法，不是正常使用。
MAX_LIVE_SESSIONS = 256

# 登录节流。**按账户分桶**，外加一个全局桶：
#   * 分桶让"对某个账户的猜测"有独立的预算，一次成功登录只清掉**自己**那个
#     桶——否则攻击者只要能让任意一个账户登录成功，就把全场的失败史抹掉了。
#   * 全局桶挡住"每个用户名试 9 次"这种横扫。它比分桶宽得多，正常使用碰不到。
LOGIN_MAX_FAILURES = 10
LOGIN_GLOBAL_MAX_FAILURES = 50
LOGIN_FAILURE_WINDOW_SECONDS = 60.0
# 同时跟踪多少个账户桶。超了就丢最老的——节流不该变成一条内存增长路径。
LOGIN_MAX_TRACKED_KEYS = 512


class DeploymentConfigError(RuntimeError):
    """这台服务器的部署配置不成立，不能启动。

    它**只**在启动路径上抛：生产模式缺必填项时，正确的结果是进程起不来，
    而不是起来之后带着一个开着的管理面。
    """


# ── 部署设定 ────────────────────────────────────────────────────────────
def _env_flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    """读一个布尔配置。**看不懂就响亮失败，绝不悄悄回落。**

    悄悄回落在这里尤其坏：把 `PNS_SESSION_COOKIE_SECURE=yes` 读成 false，
    操作者以为自己开了 Secure，而 Cookie 照样能走明文。
    """
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise DeploymentConfigError(
        f"环境变量 {name} 不是合法布尔值：{raw!r}（用 true/false）"
    )


def _split_origin(value: str) -> Optional[Tuple[str, str]]:
    """把一个源拆成 (scheme, authority)，authority 里的默认端口去掉。"""
    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.hostname:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    try:
        port = parts.port
    except ValueError:  # 端口不是数字
        return None
    default = {"http": 80, "https": 443}.get(scheme)
    authority = host if port is None or port == default else f"{host}:{port}"
    return scheme, authority


def _normalize_origin(value: str) -> Optional[str]:
    """把一个源规范成 `scheme://host[:port]`，默认端口去掉。"""
    split = _split_origin(value)
    return None if split is None else f"{split[0]}://{split[1]}"


@dataclass(frozen=True)
class DeploymentSettings:
    """一台服务器的部署身份。构造即校验，构造成功即可用。"""

    mode: str
    admin_token: Optional[str]
    session_ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS
    cookie_secure: bool = False
    accounts_db: Optional[str] = None
    bootstrap_admin_username: Optional[str] = None
    bootstrap_admin_password_hash: Optional[str] = None
    trusted_origins: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in VALID_MODES:
            raise DeploymentConfigError(
                f"{ENV_MODE} 必须是 {'/'.join(VALID_MODES)} 之一，实际是 {self.mode!r}"
            )
        token = self.admin_token
        if token is not None:
            if token != token.strip():
                raise DeploymentConfigError(
                    f"{ENV_ADMIN_TOKEN} 首尾不能有空白字符（多半是复制粘贴带进来的）"
                )
            if len(token) < MIN_ADMIN_TOKEN_CHARS:
                raise DeploymentConfigError(
                    f"{ENV_ADMIN_TOKEN} 至少要 {MIN_ADMIN_TOKEN_CHARS} 个字符，"
                    f"实际 {len(token)} 个；用 `openssl rand -hex 32` 生成"
                )
            if token.lower() in PLACEHOLDER_TOKENS:
                raise DeploymentConfigError(
                    f"{ENV_ADMIN_TOKEN} 还是示例占位串，必须换成真正的随机值"
                )
        elif self.production:
            raise DeploymentConfigError(
                f"生产模式必须提供 {ENV_ADMIN_TOKEN}；没有它，创建/恢复/推进/"
                f"停止/关闭/重载/改活动这些操作对任何能连到端口的人都是开放的"
            )
        if not 60 <= float(self.session_ttl_seconds) <= MAX_SESSION_TTL_SECONDS:
            raise DeploymentConfigError(
                f"{ENV_SESSION_TTL} 必须落在 60–{MAX_SESSION_TTL_SECONDS} 秒，"
                f"实际是 {self.session_ttl_seconds}"
            )
        # 半份 bootstrap 配置是**最坏**的一种：它什么都不做，而操作者以为自己
        # 建好了第一个管理员，直到发现登不进去。当场失败。
        username = self.bootstrap_admin_username
        password_hash = self.bootstrap_admin_password_hash
        if bool(username) != bool(password_hash):
            raise DeploymentConfigError(
                f"{ENV_BOOTSTRAP_USERNAME} 与 {ENV_BOOTSTRAP_PASSWORD_HASH} "
                f"必须一起给：只给一个不会创建任何账户"
            )
        if password_hash:
            # 哈希形状在**启动时**就验，而不是留到第一次登录失败时才发现。
            try:
                _validate_argon2id(password_hash)
            except InvalidPassword as e:
                raise DeploymentConfigError(
                    f"{ENV_BOOTSTRAP_PASSWORD_HASH} 不是 Argon2id 哈希"
                    f"（收到的是 {len(password_hash)} 个字符）。两种常见原因："
                    f"(1) 填的是明文密码——用 "
                    f"`python scripts/accounts.py hash-password` 生成哈希；"
                    f"(2) 值在 .env 里没加单引号——Docker Compose 会把 `$` 后面"
                    f"那段当成变量吃掉，正确写法是 "
                    f"{ENV_BOOTSTRAP_PASSWORD_HASH}='$argon2id$...'"
                ) from e
        for origin in self.trusted_origins:
            if _normalize_origin(origin) is None:
                raise DeploymentConfigError(
                    f"{ENV_TRUSTED_ORIGINS} 里的 {origin!r} 不是一个合法的源"
                    f"（形如 https://pns.example.lan）"
                )

    @property
    def production(self) -> bool:
        return self.mode == PRODUCTION

    @property
    def auth_required(self) -> bool:
        """配了 token 就一定强制。开发模式下配了也强制——它不是开关。"""
        return self.admin_token is not None

    @property
    def normalized_trusted_origins(self) -> Tuple[str, ...]:
        return tuple(
            origin
            for origin in (_normalize_origin(o) for o in self.trusted_origins)
            if origin is not None
        )

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "DeploymentSettings":
        env = os.environ if env is None else env
        raw_mode = (env.get(ENV_MODE) or "").strip()
        mode = raw_mode or DEVELOPMENT
        raw_token = env.get(ENV_ADMIN_TOKEN)
        token = raw_token if raw_token else None
        raw_ttl = (env.get(ENV_SESSION_TTL) or "").strip()
        if raw_ttl:
            try:
                ttl = float(raw_ttl)
            except (TypeError, ValueError) as e:
                raise DeploymentConfigError(
                    f"环境变量 {ENV_SESSION_TTL} 不是合法数值：{raw_ttl!r}"
                ) from e
        else:
            ttl = DEFAULT_SESSION_TTL_SECONDS
        origins = tuple(
            part.strip()
            for part in (env.get(ENV_TRUSTED_ORIGINS) or "").split(",")
            if part.strip()
        )
        return cls(
            mode=mode,
            admin_token=token,
            session_ttl_seconds=ttl,
            cookie_secure=_env_flag(env, ENV_COOKIE_SECURE, False),
            accounts_db=(env.get(ENV_ACCOUNTS_DB) or "").strip() or None,
            bootstrap_admin_username=(env.get(ENV_BOOTSTRAP_USERNAME) or "").strip()
            or None,
            bootstrap_admin_password_hash=(
                env.get(ENV_BOOTSTRAP_PASSWORD_HASH) or ""
            ).strip()
            or None,
            trusted_origins=origins,
        )

    def to_public_dict(self) -> Dict[str, object]:
        """能给浏览器看的那部分。**token 和 bootstrap 哈希不在里面。**"""
        return {"mode": self.mode, "auth_required": self.auth_required}


# ── 会话 ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SessionRecord:
    """一张会话记着的东西。

    `security_revision` 是这张会话的**撤销判据**：账户被停用、改角色或改密码
    时那个数会前进，于是这张会话在下一次请求就对不上号。它不是缓存——每次
    请求都拿它去跟账户库里的当前值比，所以撤销不需要等 TTL，也不需要一次
    成功的"通知"。
    """

    session_id: str
    principal_id: str
    security_revision: int
    expires_at: float


class SessionStore:
    """进程内的操作者会话表。

    刻意不持久化：重启一次就得重新登录，这是**想要**的行为——会话是这台
    进程的东西，不是这个世界的状态，它没有资格活过一次重启。

    整张表由一把锁保护。这不是过度防御：认证发生在 ASGI 中间件（事件循环
    线程）里，而签发和撤销发生在同步路由（线程池里的**别的**线程）里。
    "先算出最早到期的那个再删掉它"这种读-改-写在两个线程之间会撞出 KeyError，
    而那会变成一次合法请求上的 500。单操作者时代碰不到，多用户之后碰得到。
    """

    def __init__(
        self,
        ttl_seconds: float,
        *,
        max_sessions: int = MAX_LIVE_SESSIONS,
        clock=time.monotonic,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._max = int(max_sessions)
        self._clock = clock
        self._sessions: Dict[str, SessionRecord] = {}
        self._lock = threading.Lock()

    def _sweep_locked(self) -> None:
        now = self._clock()
        for sid in [
            sid for sid, record in self._sessions.items() if record.expires_at <= now
        ]:
            del self._sessions[sid]

    def issue(self, principal_id: str, security_revision: int) -> str:
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._sweep_locked()
            while len(self._sessions) >= self._max:
                # 满了就先丢最早到期的那个。丢掉的会话立刻失效，不是"还能再
                # 用一会"。
                oldest = min(
                    self._sessions, key=lambda s: self._sessions[s].expires_at
                )
                del self._sessions[oldest]
            self._sessions[sid] = SessionRecord(
                session_id=sid,
                principal_id=str(principal_id),
                security_revision=int(security_revision),
                expires_at=self._clock() + self._ttl,
            )
        return sid

    def get(self, sid: Optional[str]) -> Optional[SessionRecord]:
        if not sid:
            return None
        with self._lock:
            record = self._sessions.get(sid)
            if record is None:
                return None
            if record.expires_at <= self._clock():
                self._sessions.pop(sid, None)
                return None
            return record

    def valid(self, sid: Optional[str]) -> bool:
        return self.get(sid) is not None

    def revoke(self, sid: Optional[str]) -> None:
        if sid:
            with self._lock:
                self._sessions.pop(sid, None)

    def revoke_principal(self, principal_id: str) -> int:
        """作废某个主体的**全部**会话，返回作废了几张。

        它是撤销的"立刻生效"那一半；另一半是每次请求都比对
        `security_revision`。两条都要有：前者管住本进程里那些还没发下一次请求
        的会话，后者管住任何绕过了这次调用的路径（别的进程、竞态里刚签发的
        那一张）。只有前者的话，撤销就变成了一次**通知**，而通知会漏。
        """
        with self._lock:
            doomed = [
                sid
                for sid, record in self._sessions.items()
                if record.principal_id == principal_id
            ]
            for sid in doomed:
                del self._sessions[sid]
        return len(doomed)

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    @property
    def live(self) -> int:
        with self._lock:
            self._sweep_locked()
            return len(self._sessions)


class LoginThrottle:
    """登录失败节流。按账户分桶，另有一个全局桶。

    刻意不按来源 IP 分桶：反向代理之后的来源地址未必可信，按来源分桶只会给
    攻击者一个绕过维度。代价写在部署文档里：能碰到这个端口的人可以让登录框
    卡一会儿；`Authorization: Bearer` 的自动化路径不受它影响。
    """

    def __init__(
        self,
        *,
        max_failures: int = LOGIN_MAX_FAILURES,
        global_max_failures: int = LOGIN_GLOBAL_MAX_FAILURES,
        window_seconds: float = LOGIN_FAILURE_WINDOW_SECONDS,
        max_tracked_keys: int = LOGIN_MAX_TRACKED_KEYS,
        clock=time.monotonic,
    ) -> None:
        self._max = int(max_failures)
        self._global_max = int(global_max_failures)
        self._window = float(window_seconds)
        self._max_keys = int(max_tracked_keys)
        self._clock = clock
        self._buckets: Dict[str, List[float]] = {}
        self._global: List[float] = []
        # 同一个理由：登录路由跑在线程池里，多个并发登录会同时改这两张表。
        self._lock = threading.Lock()

    def _prune(self) -> None:
        cutoff = self._clock() - self._window
        self._global = [t for t in self._global if t > cutoff]
        for key in list(self._buckets):
            kept = [t for t in self._buckets[key] if t > cutoff]
            if kept:
                self._buckets[key] = kept
            else:
                del self._buckets[key]

    @staticmethod
    def _key(key: object) -> str:
        return key if isinstance(key, str) else ""

    def blocked(self, key: object = "") -> bool:
        with self._lock:
            self._prune()
            if len(self._global) >= self._global_max:
                return True
            return len(self._buckets.get(self._key(key), ())) >= self._max

    def record_failure(self, key: object = "") -> None:
        with self._lock:
            self._prune()
            name = self._key(key)
            self._global.append(self._clock())
            bucket = self._buckets.setdefault(name, [])
            bucket.append(self._clock())
            while len(self._buckets) > self._max_keys:
                oldest = min(self._buckets, key=lambda k: self._buckets[k][-1])
                if oldest == name:  # pragma: no cover - 刚写过的桶不会是最老的
                    break
                del self._buckets[oldest]

    def record_success(self, key: object = "") -> None:
        """只清掉**这个账户**的失败史。全局桶不清。

        清全局桶等于给攻击者一条免费的解锁指令：他只要有任意一个能登录成功的
        账户（甚至只是等到别人正常登录一次），就把全场的失败史抹掉了。
        """
        with self._lock:
            self._buckets.pop(self._key(key), None)

    def reset(self) -> None:
        """全清。只在测试和显式管理动作里用。"""
        with self._lock:
            self._buckets.clear()
            self._global.clear()

    def retry_after(self, key: object = "") -> int:
        with self._lock:
            self._prune()
            oldest: List[float] = []
            if len(self._global) >= self._global_max:
                oldest.append(self._global[0])
            bucket = self._buckets.get(self._key(key), [])
            if len(bucket) >= self._max:
                oldest.append(bucket[0])
            if not oldest:
                return 0
            return max(1, int(self._window - (self._clock() - min(oldest))) + 1)


# ── 公开面 ──────────────────────────────────────────────────────────────
#
# 这份清单就是"公开"的**全部**定义。改它是一次显式决定，而不是新加一条路由
# 的副作用。有测试盯着：`dashboard/dist` 里出现清单没覆盖的顶层文件时，测试
# 会红。
#
# `/api/auth/password`（改自己的密码）**不在**这里：它要求已认证。
PUBLIC_PATHS = frozenset(
    {
        "/healthz",
        "/readyz",
        "/api/auth/session",
        "/api/auth/login",
        "/api/auth/logout",
    }
)

# 前端外壳与静态资源。只对 GET/HEAD 公开，且里面**不许**有任何服务器侧秘密
# ——这一点由"密钥不进构建"那条边界保证，不由这份清单保证。
PUBLIC_STATIC_PATHS = frozenset({"/", "/index.html", "/favicon.svg", "/icons.svg"})
PUBLIC_STATIC_PREFIXES = ("/assets/",)

_SAFE_METHODS = frozenset({"GET", "HEAD"})


def is_public(path: str, method: str, kind: str = "http") -> bool:
    """这条路径是不是公开面。**默认返回 False。**

    路径里出现 `..` 段一律不算公开：一条 `/assets/../api/...` 既不该被当成
    静态资源，也不该借静态资源的公开性混过去。
    """
    if kind != "http":
        return False
    if not path.startswith("/") or ".." in path.split("/"):
        return False
    if path in PUBLIC_PATHS:
        return True
    if method.upper() not in _SAFE_METHODS:
        return False
    if path in PUBLIC_STATIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_STATIC_PREFIXES)


# ── 同源 ────────────────────────────────────────────────────────────────
def _headers(scope: Mapping) -> Sequence[Tuple[bytes, bytes]]:
    return scope.get("headers") or ()


def _header_values(scope: Mapping, name: bytes) -> List[bytes]:
    return [value for key, value in _headers(scope) if key.lower() == name]


def _single_header(scope: Mapping, name: bytes) -> Optional[str]:
    values = _header_values(scope, name)
    if len(values) != 1:
        return None
    try:
        return values[0].decode("latin-1")
    except UnicodeDecodeError:  # pragma: no cover - latin-1 不会失败
        return None


def same_origin(scope: Mapping, trusted: Sequence[str] = ()) -> bool:
    """这次请求是不是从本站发起的。**没有 Origin 头时算是。**

    这是 CSRF 的判据。`SameSite=Strict` 已经让跨站请求带不上会话 Cookie，
    这一条是**第二把锁**——它不依赖浏览器的 SameSite 实现，也管得住
    WebSocket 握手（`SameSite` 对 WS 的覆盖历史上并不一致）。
    非浏览器客户端（curl、运维脚本）不发 Origin，所以它们不受影响。

    比较的是 **authority（host[:port]），不比 scheme**。这一条是刻意的，
    理由很具体：反向代理终结 TLS 之后，浏览器发来的是 `https://pns.lan`，
    而应用进程在回环上看到的 `scope["scheme"]` 是 `http`。按完整源比较的话，
    每一台"nginx 终结 TLS + 转发到回环"的正常部署上，所有写操作都会 403。
    换来的风险是窄的：能在 `http://<同一个 host>` 上放东西的攻击者，已经站在
    这个局域网的中间人位置上了。**不做**的是去信 `X-Forwarded-Proto` —— 那把
    判据交给了一个任何人都能伪造的请求头。

    host 的来源是 `Host` 头。反向代理如果把它改写成了上游地址（`proxy_pass`
    的默认行为），浏览器发来的 Origin 一样对不上——那种部署要么在代理上写
    `proxy_set_header Host $host;`，要么把浏览器访问的源写进
    `PNS_TRUSTED_ORIGINS`。两条路都在部署文档里；**不做**的是"对不上就放行"。
    """
    origins = _header_values(scope, b"origin")
    if not origins:
        # 没有 Origin：非浏览器客户端（curl、运维脚本）。
        return True
    if len(origins) != 1:
        # 两个 Origin 头没有唯一答案，不许挑一个能过的——跟重复的
        # Authorization 头同一条纪律。浏览器只会发一个。
        return False
    raw_origin = _single_header(scope, b"origin")
    if raw_origin is None or raw_origin.lower() == "null":
        # `null` 是沙箱化的跨源上下文：既不是同源，也不在白名单里。
        return False
    split = _split_origin(raw_origin)
    if split is None:
        return False
    if f"{split[0]}://{split[1]}" in trusted:
        return True
    host = _single_header(scope, b"host")
    if not host:
        return False
    expected = _split_origin(f"//{host}" if "//" not in host else host)
    if expected is None:
        # `Host` 里只有 authority，没有 scheme —— 借一个 scheme 才拆得动。
        expected = _split_origin(f"http://{host}")
    return expected is not None and split[1] == expected[1]


# ── 鉴权 ────────────────────────────────────────────────────────────────
def _parse_cookies(raw: Iterable[bytes]) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    for chunk in raw:
        for part in chunk.decode("latin-1").split(";"):
            name, sep, value = part.partition("=")
            if not sep:
                continue
            cookies[name.strip()] = value.strip()
    return cookies


class AdminAuth:
    """一次请求算不算已认证、认成了谁，只由这里回答。"""

    def __init__(
        self,
        settings: DeploymentSettings,
        *,
        sessions: Optional[SessionStore] = None,
        throttle: Optional[LoginThrottle] = None,
        accounts: Optional[AccountStore] = None,
    ) -> None:
        self.settings = settings
        self.sessions = (
            sessions
            if sessions is not None
            else SessionStore(settings.session_ttl_seconds)
        )
        self.throttle = throttle if throttle is not None else LoginThrottle()
        self.accounts = accounts
        # "这台服务器要不要凭据"在**启动时**定下来，不是每次请求现算。
        # 现算的话，一台开着的开发服务器会在有人建出第一个账户的那一刻突然
        # 开始要求登录——一个随请求变化的安全姿态，比两种姿态里的任何一种都糟。
        self._accounts_required = accounts is not None and accounts.count() > 0

    @property
    def required(self) -> bool:
        return self.settings.auth_required or self._accounts_required

    @property
    def trusted_origins(self) -> Tuple[str, ...]:
        return self.settings.normalized_trusted_origins

    def public_dict(self) -> Dict[str, object]:
        """能给浏览器看的那部分。

        `auth_required` 报的是**实情**，不是配置里那一项：一台没配 token 但
        库里有账户的服务器照样要登录，说它 `auth_required: false` 就是在骗
        前端——前端会据此不弹登录框，然后所有请求撞 401。
        """
        return {**self.settings.to_public_dict(), "auth_required": self.required}

    def verify_token(self, candidate: object) -> bool:
        """token 对不对。定时安全比较；没配 token 时永远是 False。"""
        expected = self.settings.admin_token
        if expected is None or not isinstance(candidate, str) or not candidate:
            return False
        return compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))

    def session_id(self, scope: Mapping) -> Optional[str]:
        cookies = _parse_cookies(_header_values(scope, b"cookie"))
        return cookies.get(SESSION_COOKIE)

    def resolve(self, scope: Mapping) -> Optional[RequestPrincipal]:
        """这次请求是谁。认不出来就是 None。

        顺序是刻意的：**只要出现了 Authorization 头，就由它决定**。带着一个
        错的 bearer 却因为浏览器里还有一张有效 Cookie 而被放行，会让"这次调用
        用的是哪个凭据"变成一个说不清的问题。重复的 Authorization 头一律拒绝
        ——两份凭据的请求没有唯一答案，不许挑一个能过的。
        """
        if not self.required:
            # 开发模式且既没配 token 也没有账户：保持既有本地行为。生产模式
            # 永远到不了这里——没有 token、没有管理员的生产进程根本起不来。
            return open_development_principal()

        auth_headers = _header_values(scope, b"authorization")
        if auth_headers:
            if len(auth_headers) != 1:
                return None
            try:
                raw = auth_headers[0].decode("latin-1")
            except UnicodeDecodeError:  # pragma: no cover - latin-1 不会失败
                return None
            scheme, sep, value = raw.partition(" ")
            if not sep or scheme.lower() != "bearer":
                return None
            return break_glass_principal() if self.verify_token(value.strip()) else None

        sid = self.session_id(scope)
        record = self.sessions.get(sid)
        if record is None:
            return None
        if self.accounts is None:
            # 有会话却没有账户库：这张会话不可能是本进程签发的。
            self.sessions.revoke(sid)
            return None
        account = self.accounts.find(record.principal_id)
        if (
            account is None
            or not account.enabled
            or account.security_revision != record.security_revision
        ):
            # 账户没了、被停用了、或者权威变过了——这张会话当场作废，
            # 而不是等它自己过期。
            self.sessions.revoke(sid)
            return None
        return session_principal(account, record.session_id)

    def authenticated(self, scope: Mapping) -> bool:
        return self.resolve(scope) is not None

    def allows(self, scope: Mapping) -> bool:
        """只回答"进不进得来"。**能做什么由 authz.required_scope 决定。**"""
        kind = scope.get("type", "http")
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if is_public(path, method, kind):
            return True
        return self.resolve(scope) is not None


# ── 生产不可变边界 ──────────────────────────────────────────────────────
def refuse_in_production(request: Request) -> None:
    """写回仓库源码的接口在生产模式下一律拒绝。

    理由不是"生产要严一点"，而是那种写入**在生产里没有意义且会骗人**：
    World Editor 写的是镜像层里的 `pns/world/*.py`，`POST /api/config` 写的是
    镜像层里的 `.env`。它们在下一次 `docker compose up --build` 之后就没了，
    而且容器里的 `.env` 还会盖住 Compose 注入的那份配置——于是操作者看到的是
    "改好了"，实际得到的是一次会在下次重建时静静回退的改动。

    与其让一次写入变成一个定时的谎，不如当场说清楚：生产的内容与配置从仓库和
    环境注入来，改法是改仓库/改环境再重建（见 docs/DEPLOY_UBUNTU_DOCKER.md）。
    这跟鉴权是两件事——这些接口首先要通过鉴权，然后才会撞到这一条。

    它是一个路由级依赖，所以它在**请求体校验之前**跑：一份畸形请求体在生产
    上拿到的也是 409，而不是一句把 schema 讲出去的 422。

    判据优先取 app 自己那份部署设定；**取不到就回环境变量**，取不出来就当成
    生产。理由是方向性的：一个不是由 `create_app()` 装配起来的 app 上没有这份
    设定，而"说不清是不是生产"跟"确定不是生产"不是一回事——前者不该换来一次
    放行。生产镜像把 PNS_ENV 固化成 production，所以这条回退在真实部署里总能
    答出正确答案。
    """
    deployment = getattr(request.app.state, "deployment", None)
    if deployment is None:
        try:
            deployment = DeploymentSettings.from_env()
        except DeploymentConfigError:
            deployment = None
            production = True
        else:
            production = deployment.production
    else:
        production = deployment.production
    if production:
        raise HTTPException(
            409,
            {
                "category": "immutable_deployment",
                "message": (
                    "生产部署不接受从浏览器写回仓库源码或 .env："
                    "那份写入活不过下一次容器重建。请改仓库/改环境后重新部署。"
                ),
            },
        )


_DENIED_BODY = (
    b'{"detail":{"category":"unauthenticated",'
    b'"message":"\\u9700\\u8981\\u7ba1\\u7406\\u51ed\\u636e"}}'
)

_FORBIDDEN_BODY = (
    b'{"detail":{"category":"forbidden",'
    b'"message":"\\u5f53\\u524d\\u8d26\\u6237\\u6ca1\\u6709\\u6267\\u884c'
    b'\\u8fd9\\u6b21\\u64cd\\u4f5c\\u7684\\u6743\\u9650"}}'
)

_CROSS_ORIGIN_BODY = (
    b'{"detail":{"category":"cross_origin",'
    b'"message":"\\u8de8\\u6e90\\u5199\\u8bf7\\u6c42\\u88ab\\u62d2\\u7edd"}}'
)


class AdminAuthMiddleware:
    """默认拒绝的 ASGI 中间件。

    刻意是 ASGI 而不是 `BaseHTTPMiddleware`，也刻意不是路由依赖：
    它要同时管住 WebSocket（`/ws/run` 会花模型额度），而且要在路由匹配和
    请求体解析**之前**就把请求挡下来。被拒绝的请求，请求体一个字节都没被读过。

    它做四件事，顺序不能换：
      1. 公开面直接放行（健康检查、登录、静态外壳）；
      2. 跨源的写请求**在认证之前**就挡掉——一次 CSRF 不该有机会证明自己是谁；
      3. 认证：认不出主体就 401；
      4. 授权：按方法/路径算出这次至少要什么 scope，不够就 403。
         第 4 步的判据来自方法而不是路由表，所以明天新加的那条 POST 也默认
         要 `operate`——observer 进不去，不靠有人记得挂依赖。
    """

    def __init__(self, app, auth: AdminAuth) -> None:
        self.app = app
        self.auth = auth

    async def _deny(self, scope, send, status: int, body: bytes) -> None:
        if scope["type"] == "websocket":
            # 握手阶段直接关掉。ASGI 服务器会把"accept 之前的 close"翻译成
            # 一次失败的握手，浏览器那边不会拿到一条已建立的连接。
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ]
        if status == 401:
            headers.insert(1, (b"www-authenticate", b"Bearer"))
        await send(
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        kind = scope["type"]
        path = scope.get("path", "")
        method = scope.get("method", "GET")

        # 跨源检查在认证之前，而且**公开面也算**：一次跨站发起的 POST 不该
        # 因为它打的恰好是 /api/auth/login 就被放过去。安全方法不查——它们
        # 不改状态，而 SameSite=Strict 已经让跨站的 GET 带不上会话。
        unsafe = kind == "websocket" or method.upper() not in SAFE_METHODS
        if unsafe and not same_origin(scope, self.auth.trusted_origins):
            await self._deny(scope, send, 403, _CROSS_ORIGIN_BODY)
            return

        if is_public(path, method, kind):
            await self.app(scope, receive, send)
            return

        principal = self.auth.resolve(scope)
        if principal is None:
            await self._deny(scope, send, 401, _DENIED_BODY)
            return

        needed = required_scope(path, method, kind)
        if needed is not None and not principal.has(needed):
            await self._deny(scope, send, 403, _FORBIDDEN_BODY)
            return

        # scope 是同一个 dict 一路传进去的，所以路由依赖读得到它。
        scope[PRINCIPAL_SCOPE_KEY] = principal
        await self.app(scope, receive, send)


__all__ = [
    "AdminAuth",
    "AdminAuthMiddleware",
    "DEVELOPMENT",
    "DeploymentConfigError",
    "DeploymentSettings",
    "ENV_ACCOUNTS_DB",
    "ENV_ADMIN_TOKEN",
    "ENV_BOOTSTRAP_PASSWORD_HASH",
    "ENV_BOOTSTRAP_USERNAME",
    "ENV_COOKIE_SECURE",
    "ENV_MODE",
    "ENV_SESSION_TTL",
    "ENV_TRUSTED_ORIGINS",
    "LOGIN_GLOBAL_MAX_FAILURES",
    "LOGIN_MAX_FAILURES",
    "LoginThrottle",
    "MIN_ADMIN_TOKEN_CHARS",
    "PLACEHOLDER_TOKENS",
    "PRODUCTION",
    "PUBLIC_PATHS",
    "PUBLIC_STATIC_PATHS",
    "PUBLIC_STATIC_PREFIXES",
    "SESSION_COOKIE",
    "SessionRecord",
    "SessionStore",
    "is_public",
    "refuse_in_production",
    "same_origin",
]
