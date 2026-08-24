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
# 三条硬约束：
#
#   * **拒绝发生在任何变更之前。** 鉴权是 ASGI 中间件，包在整个应用外面：
#     它在路由匹配、请求体解析、依赖求解之前就决定放不放行。所以一次被拒绝
#     的请求连请求体都没被读过，更不可能改到任何状态。
#   * **浏览器不持有 bearer 密钥。** 操作者把 token 贴进登录框，服务端换给
#     它一个 HttpOnly / SameSite=Strict 的会话 Cookie。密钥不进 JS 包、不进
#     localStorage、不进 URL，因此也不进任何访问日志。
#   * **比较是定时安全的，失败是沉默的。** token 比较走 `compare_digest`，
#     响应里不说"长度不对"或"scheme 不对"这类能拿来做区分的话。
import os
import secrets
import time
from dataclasses import dataclass
from hmac import compare_digest
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from fastapi import HTTPException, Request

# ── 环境变量名 ──────────────────────────────────────────────────────────
ENV_MODE = "PNS_ENV"
ENV_ADMIN_TOKEN = "PNS_ADMIN_TOKEN"
ENV_SESSION_TTL = "PNS_SESSION_TTL_SECONDS"
ENV_COOKIE_SECURE = "PNS_SESSION_COOKIE_SECURE"

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
# 同时有效的会话数上限。会话只由一个人签发，正常永远到不了这个数；它挡的是
# "反复登录把进程内存撑大"这一种玩法。
MAX_LIVE_SESSIONS = 64

# 登录节流：这么多次失败之后，这个窗口内不再受理登录。它跟 token 长度下限是
# 两道独立的闸——前者挡在线暴力，后者让离线猜测没有意义。
LOGIN_MAX_FAILURES = 10
LOGIN_FAILURE_WINDOW_SECONDS = 60.0


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


@dataclass(frozen=True)
class DeploymentSettings:
    """一台服务器的部署身份。构造即校验，构造成功即可用。"""

    mode: str
    admin_token: Optional[str]
    session_ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS
    cookie_secure: bool = False

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

    @property
    def production(self) -> bool:
        return self.mode == PRODUCTION

    @property
    def auth_required(self) -> bool:
        """配了 token 就一定强制。开发模式下配了也强制——它不是开关。"""
        return self.admin_token is not None

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
        return cls(
            mode=mode,
            admin_token=token,
            session_ttl_seconds=ttl,
            cookie_secure=_env_flag(env, ENV_COOKIE_SECURE, False),
        )

    def to_public_dict(self) -> Dict[str, object]:
        """能给浏览器看的那部分。**token 不在里面，也永远不该在里面。**"""
        return {"mode": self.mode, "auth_required": self.auth_required}


# ── 会话 ────────────────────────────────────────────────────────────────
class SessionStore:
    """进程内的操作者会话表。

    刻意不持久化：重启一次就得重新登录，这是**想要**的行为——会话是这台
    进程的东西，不是这个世界的状态，它没有资格活过一次重启。
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
        self._sessions: Dict[str, float] = {}

    def _sweep(self) -> None:
        now = self._clock()
        for sid in [s for s, exp in self._sessions.items() if exp <= now]:
            del self._sessions[sid]

    def issue(self) -> str:
        self._sweep()
        while len(self._sessions) >= self._max:
            # 满了就先丢最早到期的那个。丢掉的会话立刻失效，不是"还能再用一会"。
            oldest = min(self._sessions, key=self._sessions.get)
            del self._sessions[oldest]
        sid = secrets.token_urlsafe(32)
        self._sessions[sid] = self._clock() + self._ttl
        return sid

    def valid(self, sid: Optional[str]) -> bool:
        if not sid:
            return False
        expires = self._sessions.get(sid)
        if expires is None:
            return False
        if expires <= self._clock():
            del self._sessions[sid]
            return False
        return True

    def revoke(self, sid: Optional[str]) -> None:
        if sid:
            self._sessions.pop(sid, None)

    def revoke_all(self) -> None:
        self._sessions.clear()

    @property
    def live(self) -> int:
        self._sweep()
        return len(self._sessions)


class LoginThrottle:
    """登录失败节流。窗口内失败够多次就整体拒绝受理。

    刻意不按来源 IP 分桶：这是一个单操作者的管理面，反向代理之后的来源地址
    也未必可信。按来源分桶只会给攻击者一个绕过维度。
    """

    def __init__(
        self,
        *,
        max_failures: int = LOGIN_MAX_FAILURES,
        window_seconds: float = LOGIN_FAILURE_WINDOW_SECONDS,
        clock=time.monotonic,
    ) -> None:
        self._max = int(max_failures)
        self._window = float(window_seconds)
        self._clock = clock
        self._failures: List[float] = []

    def _prune(self) -> None:
        cutoff = self._clock() - self._window
        self._failures = [t for t in self._failures if t > cutoff]

    def blocked(self) -> bool:
        self._prune()
        return len(self._failures) >= self._max

    def record_failure(self) -> None:
        self._prune()
        self._failures.append(self._clock())

    def reset(self) -> None:
        self._failures.clear()

    def retry_after(self) -> int:
        self._prune()
        if not self._failures:
            return 0
        return max(1, int(self._window - (self._clock() - self._failures[0])) + 1)


# ── 公开面 ──────────────────────────────────────────────────────────────
#
# 这份清单就是"公开"的**全部**定义。改它是一次显式决定，而不是新加一条路由
# 的副作用。有测试盯着：`dashboard/dist` 里出现清单没覆盖的顶层文件时，测试
# 会红。
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


# ── 鉴权 ────────────────────────────────────────────────────────────────
def _headers(scope: Mapping) -> Sequence[Tuple[bytes, bytes]]:
    return scope.get("headers") or ()


def _header_values(scope: Mapping, name: bytes) -> List[bytes]:
    return [value for key, value in _headers(scope) if key.lower() == name]


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
    """一次请求算不算已认证，只由这里回答。"""

    def __init__(
        self,
        settings: DeploymentSettings,
        *,
        sessions: Optional[SessionStore] = None,
        throttle: Optional[LoginThrottle] = None,
    ) -> None:
        self.settings = settings
        self.sessions = (
            sessions
            if sessions is not None
            else SessionStore(settings.session_ttl_seconds)
        )
        self.throttle = throttle if throttle is not None else LoginThrottle()

    @property
    def required(self) -> bool:
        return self.settings.auth_required

    def verify_token(self, candidate: object) -> bool:
        """token 对不对。定时安全比较；没配 token 时永远是 False。"""
        expected = self.settings.admin_token
        if expected is None or not isinstance(candidate, str) or not candidate:
            return False
        return compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))

    def session_id(self, scope: Mapping) -> Optional[str]:
        cookies = _parse_cookies(_header_values(scope, b"cookie"))
        return cookies.get(SESSION_COOKIE)

    def authenticated(self, scope: Mapping) -> bool:
        """这次请求带着一个有效凭据吗。

        顺序是刻意的：**只要出现了 Authorization 头，就由它决定**。带着一个
        错的 bearer 却因为浏览器里还有一张有效 Cookie 而被放行，会让"这次调用
        用的是哪个凭据"变成一个说不清的问题。重复的 Authorization 头一律拒绝
        ——两份凭据的请求没有唯一答案，不许挑一个能过的。
        """
        auth_headers = _header_values(scope, b"authorization")
        if auth_headers:
            if len(auth_headers) != 1:
                return False
            try:
                raw = auth_headers[0].decode("latin-1")
            except UnicodeDecodeError:  # pragma: no cover - latin-1 不会失败
                return False
            scheme, sep, value = raw.partition(" ")
            if not sep or scheme.lower() != "bearer":
                return False
            return self.verify_token(value.strip())
        return self.sessions.valid(self.session_id(scope))

    def allows(self, scope: Mapping) -> bool:
        kind = scope.get("type", "http")
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if is_public(path, method, kind):
            return True
        if not self.required:
            # 开发模式且没配 token：保持既有本地行为。生产模式永远到不了这里
            # ——没有 token 的生产进程根本起不来。
            return True
        return self.authenticated(scope)


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


class AdminAuthMiddleware:
    """默认拒绝的 ASGI 中间件。

    刻意是 ASGI 而不是 `BaseHTTPMiddleware`，也刻意不是路由依赖：
    它要同时管住 WebSocket（`/ws/run` 会花模型额度），而且要在路由匹配和
    请求体解析**之前**就把请求挡下来。被拒绝的请求，请求体一个字节都没被读过。
    """

    def __init__(self, app, auth: AdminAuth) -> None:
        self.app = app
        self.auth = auth

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if self.auth.allows(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # 握手阶段直接关掉。ASGI 服务器会把"accept 之前的 close"翻译成
            # 一次失败的握手，浏览器那边不会拿到一条已建立的连接。
            await send({"type": "websocket.close", "code": 1008})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                    (b"content-length", str(len(_DENIED_BODY)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _DENIED_BODY})


__all__ = [
    "AdminAuth",
    "AdminAuthMiddleware",
    "DEVELOPMENT",
    "DeploymentConfigError",
    "DeploymentSettings",
    "ENV_ADMIN_TOKEN",
    "ENV_COOKIE_SECURE",
    "ENV_MODE",
    "ENV_SESSION_TTL",
    "LoginThrottle",
    "MIN_ADMIN_TOKEN_CHARS",
    "PLACEHOLDER_TOKENS",
    "PRODUCTION",
    "PUBLIC_PATHS",
    "PUBLIC_STATIC_PATHS",
    "PUBLIC_STATIC_PREFIXES",
    "refuse_in_production",
    "SESSION_COOKIE",
    "SessionStore",
    "DeploymentSettings",
    "is_public",
]
