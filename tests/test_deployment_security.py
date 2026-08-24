# tests/test_deployment_security.py — DEPLOY-1 的鉴权与部署模式边界。
#
# 盯住的东西按"错了会怎样"排：
#   1. 默认拒绝。公开面是一份**显式**清单；清单之外的每一条路径（含
#      `/ws/run`）没有凭据就进不来，以后新加的路由默认是被保护的。
#   2. 拒绝发生在任何变更之前。被拒绝之后，世界目录逐字节不变、没有世界被
#      打开、配置 revision 没有前进。
#   3. 凭据判定没有"差不多就算了"：无头、错 scheme、错值、空值、重复头、
#      把 token 当 Cookie 用、已登出/已过期的会话，一律拒绝。
#   4. 生产模式 fail-closed：缺管理凭据、凭据太短、还是占位串、没有已构建的
#      Dashboard、没有模型凭据——五种都让 `create_app()` 抛，不是"起来但开着"。
#   5. 密钥不出现在任何一条响应里，公开面和错误路径都算。
#   6. 健康检查没有权威副作用：不建存档根、不开世界、不推进配置 revision。
#   7. 这些绿灯是中间件挣来的：把中间件摘掉，同一批请求必须变成 200。
#
# 运行: python -m unittest tests.test_deployment_security -v
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from pns.interfaces.app import create_app  # noqa: E402
from pns.interfaces.composition import WorldControlPlane  # noqa: E402
from pns.interfaces.paths import DASHBOARD_DIST  # noqa: E402
from pns.interfaces.security import (  # noqa: E402
    ENV_ADMIN_TOKEN,
    ENV_COOKIE_SECURE,
    ENV_MODE,
    ENV_SESSION_TTL,
    LOGIN_MAX_FAILURES,
    MIN_ADMIN_TOKEN_CHARS,
    PLACEHOLDER_TOKENS,
    PUBLIC_PATHS,
    PUBLIC_STATIC_PATHS,
    PUBLIC_STATIC_PREFIXES,
    SESSION_COOKIE,
    AdminAuth,
    DeploymentConfigError,
    DeploymentSettings,
    LoginThrottle,
    SessionStore,
    is_public,
)
from pns.runtime.reload import BOUNDARY  # noqa: E402

SCENE = "nightcord"
CHARACTERS = ["mizuki", "ena"]
# 一把只在测试里存在、形状独一无二的管理凭据。任何一条响应里出现它，都说明
# 服务器侧的凭据从某条路径漏到了浏览器。
ADMIN_TOKEN = "ADMIN-CANARY-0f1e2d3c4b5a69788796a5b4c3d2e1f0"
# 同样形状独一无二的模型凭据。
KEY_CANARY = "MODEL-CANARY-9a8b7c6d5e4f30211203f4e5d6c7b8a9"


class _FakeModelClient:
    """一个绝不该被真的调用的模型客户端。被调用即测试自己走错了路。"""

    def __getattr__(self, name):  # pragma: no cover - 被调用即测试有问题
        raise AssertionError(f"测试不该真的调用模型客户端（.{name}）")


def declared_operations(app):
    """(method, path) 全表，来自 app 自己的 OpenAPI 描述。

    刻意不手写一份清单，也刻意不只看 `app.routes` 的第一层（这一版 FastAPI
    把 include_router 的结果保留成不透明的嵌套节点）。以 app 自己声明的东西
    为准，新加一条路由就会自动进入这条用例的射程。
    """
    for path, operations in app.openapi()["paths"].items():
        concrete = path.replace("{world_id}", "nightcord")
        for method in operations:
            if method.upper() in ("OPTIONS", "HEAD"):
                continue
            yield method.upper(), concrete


def fingerprint(root: Path):
    """一棵目录树的逐字节指纹。用来证明"被拒绝之后什么都没变"。"""
    if not root.exists():
        return None
    entries = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            entries[str(path.relative_to(root))] = path.read_bytes()
        else:
            entries[str(path.relative_to(root)) + "/"] = b""
    return entries


class AuthTestCase(unittest.TestCase):
    """每个用例一个独立的存档根、一份独立的部署设定。"""

    production = False

    def setUp(self):
        self.registry = BOUNDARY.active()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "worlds"
        self._env = patch.dict(
            os.environ, {self.registry.models.key_name: KEY_CANARY}
        )
        self._env.start()
        self.settings = DeploymentSettings(
            mode="production" if self.production else "development",
            admin_token=ADMIN_TOKEN,
        )
        self.plane = WorldControlPlane(
            root=self.root, client_factory=lambda *a, **k: _FakeModelClient()
        )
        self.app = create_app(
            self.plane,
            settings=self.settings,
            registry_provider=lambda: self.registry,
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        try:
            self.plane.service.release_all()
        finally:
            self._env.stop()
            self._tmp.cleanup()

    # ── 便捷 ────────────────────────────────────────────────────────────
    @property
    def bearer(self):
        return {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    def login(self):
        response = self.client.post("/api/auth/login", json={"token": ADMIN_TOKEN})
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def create_world(self, world_id="nightcord"):
        return self.client.post(
            "/api/persistent-worlds",
            json={"world_id": world_id, "scene": SCENE, "characters": list(CHARACTERS)},
            headers=self.bearer,
        )


# ── 1. 默认拒绝 ─────────────────────────────────────────────────────────
class DefaultDenyTests(AuthTestCase):
    def test_every_declared_route_is_public_or_protected_deliberately(self):
        """把 app 上的每一条路由都过一遍，不许有"忘了保护"的那一条。"""
        checked = 0
        for method, path in declared_operations(self.app):
            checked += 1
            with self.subTest(operation=f"{method} {path}"):
                response = self.client.request(method, path)
                if is_public(path, method):
                    self.assertNotEqual(
                        response.status_code, 401, f"{method} {path} 不该要凭据"
                    )
                else:
                    self.assertEqual(
                        response.status_code, 401, f"{method} {path} 没有被保护"
                    )
        self.assertGreater(checked, 20, "路由表看起来没被真的遍历到")

    def test_the_api_schema_is_not_public(self):
        """FastAPI 自动挂的那几条也算路由：一台生产控制面不该把接口说明书
        发给一个还没证明自己是谁的人。"""
        for path in ("/openapi.json", "/docs", "/redoc"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_parameterised_world_routes_are_protected(self):
        for method, path in [
            ("GET", "/api/persistent-worlds/nightcord"),
            ("POST", "/api/persistent-worlds/nightcord/restore"),
            ("POST", "/api/persistent-worlds/nightcord/checkpoint"),
            ("POST", "/api/persistent-worlds/nightcord/activity"),
            ("POST", "/api/persistent-worlds/nightcord/close"),
            ("POST", "/api/persistent-worlds/nightcord/autonomy/start"),
            ("POST", "/api/persistent-worlds/nightcord/autonomy/stop"),
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.client.request(method, path).status_code, 401)

    def test_websocket_handshake_is_denied_without_credentials(self):
        """`/ws/run` 会花模型额度，所以它必须跟别的特权入口一样被挡住。"""
        with self.assertRaises(Exception) as caught:
            with self.client.websocket_connect("/ws/run"):
                pass
        self.assertIn("Disconnect", type(caught.exception).__name__)

    def test_websocket_handshake_succeeds_with_a_session_cookie(self):
        """浏览器在 WS 上没法设 Authorization 头，所以 Cookie 这条路必须真的通。"""
        self.login()
        with self.client.websocket_connect("/ws/run") as ws:
            # 握手过了才谈得上协议错误：故意送一段不是 JSON 的东西。
            ws.send_text("not json")
            with self.assertRaises(Exception):
                ws.receive_json()

    def test_a_route_added_after_creation_is_protected_by_default(self):
        """保护靠排除机制，不靠有人记得去加。"""
        @self.app.get("/api/some-future-thing")
        def _future():  # pragma: no cover - 只该被 401 挡住
            return {"ok": True}

        self.assertEqual(self.client.get("/api/some-future-thing").status_code, 401)

    def test_public_static_allowlist_covers_the_built_dashboard(self):
        """dist 里出现清单没覆盖的顶层文件时，这条必须红。"""
        if not DASHBOARD_DIST.exists():
            self.skipTest("Dashboard 未构建")
        for entry in DASHBOARD_DIST.iterdir():
            name = f"/{entry.name}"
            covered = name in PUBLIC_STATIC_PATHS or any(
                (name + "/").startswith(prefix) for prefix in PUBLIC_STATIC_PREFIXES
            )
            self.assertTrue(
                covered,
                f"{name} 在 dist 里但不在公开清单上：要么加进 "
                f"PUBLIC_STATIC_PATHS，要么确认它不该公开",
            )

    def test_traversal_does_not_borrow_static_publicness(self):
        for path in (
            "/assets/../api/persistent-worlds",
            "/assets/../../etc/passwd",
        ):
            with self.subTest(path=path):
                self.assertFalse(is_public(path, "GET"))

    def test_public_paths_are_reachable_without_credentials(self):
        for path in ("/healthz", "/readyz", "/api/auth/session"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)


# ── 2. 拒绝发生在任何变更之前 ───────────────────────────────────────────
class RejectionChangesNothingTests(AuthTestCase):
    """这一组会朝**真的会写仓库源码**的接口打请求：World Editor 写
    `pns/world/*.py`，审核决策写 `review_decisions.jsonl`。

    所以它先把这几份文件的原样存下来，用完逐字节还原并断言没被动过。理由不是
    洁癖：如果哪天守卫真的破了，这组用例本身会把仓库改花——一个在失败时会破坏
    被测环境的测试，跑第二遍就不再可信了。
    """

    WRITABLE_REPO_PATHS = (
        REPO_ROOT / "pns" / "world" / "facts.py",
        REPO_ROOT / "pns" / "world" / "scenes.py",
        REPO_ROOT / "review_decisions.jsonl",
        # World Editor 保存前会留一份覆盖式备份。它也是仓库里的残留物，
        # 一次守卫失效不该在工作树里留下任何东西。
        REPO_ROOT / "pns" / "world" / "facts.py.bak",
        REPO_ROOT / "pns" / "world" / "scenes.py.bak",
    )

    def setUp(self):
        super().setUp()
        self._repo_before = {
            path: (path.read_bytes() if path.exists() else None)
            for path in self.WRITABLE_REPO_PATHS
        }

    def tearDown(self):
        try:
            for path, content in self._repo_before.items():
                after = path.read_bytes() if path.exists() else None
                if after != content:
                    if content is None:
                        path.unlink()
                    else:
                        path.write_bytes(content)
                    self.fail(f"一次被拒绝的请求改动了仓库文件：{path}")
        finally:
            super().tearDown()

    def test_rejected_management_requests_leave_the_disk_byte_identical(self):
        created = self.create_world()
        self.assertEqual(created.status_code, 201, created.text)
        before = fingerprint(self.root)
        revision_before = self.registry.revision

        attempts = [
            ("POST", "/api/persistent-worlds", {"json": {
                "world_id": "second", "scene": SCENE, "characters": CHARACTERS}}),
            ("POST", "/api/persistent-worlds/nightcord/checkpoint", {}),
            ("POST", "/api/persistent-worlds/nightcord/close", {}),
            ("POST", "/api/persistent-worlds/nightcord/restore", {}),
            ("POST", "/api/persistent-worlds/nightcord/autonomy/start", {}),
            ("POST", "/api/persistent-worlds/nightcord/autonomy/stop", {}),
            ("POST", "/api/persistent-worlds/nightcord/activity", {"json": {
                "character_id": "mizuki", "activity": "drawing"}}),
            ("POST", "/api/config/reload", {}),
            ("POST", "/api/config", {"json": {
                "provider_key": "1", "model": "m", "api_key": "k"}}),
            ("POST", "/api/world/facts", {"json": {"facts": {"a": "b"}}}),
            ("POST", "/api/review/decision", {"json": {
                "session_id": "s", "turn": 1, "character": "mizuki",
                "decision": "approve"}}),
        ]
        for method, path, kwargs in attempts:
            with self.subTest(path=path):
                response = self.client.request(method, path, **kwargs)
                self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(fingerprint(self.root), before, "被拒绝的请求改动了磁盘")
        self.assertEqual(BOUNDARY.active().revision, revision_before)
        # 世界仍然是本进程开着的那一个，驱动仍然没被起过。
        status = self.client.get(
            "/api/persistent-worlds/nightcord", headers=self.bearer
        ).json()
        self.assertTrue(status["owned"])
        self.assertIsNone(status["autonomy"])

    def test_a_rejected_request_never_reaches_body_validation(self):
        """没凭据 + 畸形请求体 = 401，不是 422。

        422 会把请求体 schema 讲给一个还没证明自己是谁的人听，而且说明请求
        已经走到了解析这一步。"""
        response = self.client.post(
            "/api/persistent-worlds", json={"nonsense": True}
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"]["category"], "unauthenticated")


# ── 3. 凭据判定 ─────────────────────────────────────────────────────────
class CredentialShapeTests(AuthTestCase):
    def assert_denied(self, **kwargs):
        response = self.client.get("/api/config", **kwargs)
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.headers.get("www-authenticate"), "Bearer")

    def test_missing_header(self):
        self.assert_denied()

    def test_wrong_scheme(self):
        self.assert_denied(headers={"Authorization": f"Basic {ADMIN_TOKEN}"})
        self.assert_denied(headers={"Authorization": ADMIN_TOKEN})
        self.assert_denied(headers={"Authorization": f"bearer{ADMIN_TOKEN}"})

    def test_wrong_and_empty_values(self):
        self.assert_denied(headers={"Authorization": "Bearer "})
        self.assert_denied(headers={"Authorization": "Bearer wrong"})
        self.assert_denied(headers={"Authorization": f"Bearer {ADMIN_TOKEN}x"})
        self.assert_denied(headers={"Authorization": f"Bearer {ADMIN_TOKEN[:-1]}"})

    def test_correct_scheme_is_case_insensitive(self):
        response = self.client.get(
            "/api/config", headers={"Authorization": f"bEaReR {ADMIN_TOKEN}"}
        )
        self.assertEqual(response.status_code, 200)

    def test_duplicate_authorization_headers_are_denied_even_if_one_is_correct(self):
        """两份凭据的请求没有唯一答案，不许挑一个能过的。"""
        auth = AdminAuth(self.settings)
        good = f"Bearer {ADMIN_TOKEN}".encode()
        for headers in (
            [(b"authorization", b"Bearer wrong"), (b"authorization", good)],
            [(b"authorization", good), (b"authorization", b"Bearer wrong")],
            [(b"authorization", good), (b"authorization", good)],
        ):
            with self.subTest(headers=headers):
                self.assertFalse(
                    auth.authenticated({"type": "http", "headers": headers})
                )
                self.assertFalse(
                    auth.allows(
                        {
                            "type": "http",
                            "path": "/api/config",
                            "method": "GET",
                            "headers": headers,
                        }
                    )
                )

    def test_the_raw_token_is_not_a_valid_session_cookie(self):
        """会话 id 是服务端签发的，管理 token 不是会话 id。"""
        self.client.cookies.set(SESSION_COOKIE, ADMIN_TOKEN)
        self.assertEqual(self.client.get("/api/config").status_code, 401)
        self.client.cookies.clear()

    def test_logged_out_session_stops_working(self):
        self.login()
        self.assertEqual(self.client.get("/api/config").status_code, 200)
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 200)
        self.assertEqual(self.client.get("/api/config").status_code, 401)

    def test_session_cookie_attributes(self):
        header = self.login().headers["set-cookie"]
        self.assertIn("HttpOnly", header)
        self.assertIn("SameSite=strict", header)
        self.assertIn("Path=/", header)
        # 明文部署下不加 Secure，否则 Cookie 根本发不出去；开关在
        # PNS_SESSION_COOKIE_SECURE 上，见下一个用例。
        self.assertNotIn("Secure", header)

    def test_secure_cookie_when_configured(self):
        settings = DeploymentSettings(
            mode="development", admin_token=ADMIN_TOKEN, cookie_secure=True
        )
        app = create_app(
            WorldControlPlane(root=self.root / "secure"),
            settings=settings,
            registry_provider=lambda: self.registry,
        )
        with TestClient(app, base_url="https://testserver") as client:
            header = client.post(
                "/api/auth/login", json={"token": ADMIN_TOKEN}
            ).headers["set-cookie"]
        self.assertIn("Secure", header)

    def test_login_throttle_blocks_even_the_correct_token(self):
        for _ in range(LOGIN_MAX_FAILURES):
            self.assertEqual(
                self.client.post("/api/auth/login", json={"token": "wrong"}).status_code,
                401,
            )
        blocked = self.client.post("/api/auth/login", json={"token": ADMIN_TOKEN})
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Retry-After", blocked.headers)
        self.assertEqual(blocked.json()["detail"]["category"], "too_many_attempts")

    def test_login_error_does_not_describe_why(self):
        body = self.client.post("/api/auth/login", json={"token": "wrong"}).json()
        message = body["detail"]["message"]
        for leak in ("长度", "length", str(len(ADMIN_TOKEN)), ADMIN_TOKEN[:8]):
            self.assertNotIn(leak, message)


# ── 4. 生产模式 fail-closed ─────────────────────────────────────────────
class ProductionFailsClosedTests(unittest.TestCase):
    def registry(self, key=KEY_CANARY):
        return SimpleNamespace(
            models=SimpleNamespace(key_name="MIMO_API_KEY", api_key=key),
            revision=1,
        )

    def test_missing_admin_token_refuses_to_construct(self):
        with self.assertRaises(DeploymentConfigError):
            DeploymentSettings(mode="production", admin_token=None)

    def test_short_admin_token_refuses(self):
        with self.assertRaises(DeploymentConfigError):
            DeploymentSettings(
                mode="production", admin_token="a" * (MIN_ADMIN_TOKEN_CHARS - 1)
            )

    def test_placeholder_admin_token_refuses(self):
        for placeholder in PLACEHOLDER_TOKENS:
            padded = placeholder.ljust(MIN_ADMIN_TOKEN_CHARS, "0")
            if padded != placeholder:
                continue  # 补过位的就不是那个占位串了，只测原样够长的
            with self.subTest(placeholder=placeholder):
                with self.assertRaises(DeploymentConfigError):
                    DeploymentSettings(mode="production", admin_token=placeholder)

    def test_whitespace_padded_token_refuses(self):
        with self.assertRaises(DeploymentConfigError):
            DeploymentSettings(mode="production", admin_token=" " + "a" * 40)

    def test_unknown_mode_refuses(self):
        with self.assertRaises(DeploymentConfigError):
            DeploymentSettings(mode="staging", admin_token="a" * 40)

    def test_unbuilt_dashboard_refuses_to_start_in_production(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(DeploymentConfigError):
                create_app(
                    WorldControlPlane(root=Path(tmp) / "worlds"),
                    settings=DeploymentSettings(
                        mode="production", admin_token=ADMIN_TOKEN
                    ),
                    dashboard_dist=Path(tmp) / "no-dist",
                    registry_provider=self.registry,
                )

    def test_missing_model_credential_refuses_to_start_in_production(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            dist.mkdir()
            with self.assertRaises(DeploymentConfigError) as caught:
                create_app(
                    WorldControlPlane(root=Path(tmp) / "worlds"),
                    settings=DeploymentSettings(
                        mode="production", admin_token=ADMIN_TOKEN
                    ),
                    dashboard_dist=dist,
                    registry_provider=lambda: self.registry(key=""),
                )
            self.assertIn("MIMO_API_KEY", str(caught.exception))

    def test_unbuildable_configuration_refuses_to_start_in_production(self):
        def broken():
            raise RuntimeError("scenes.py 里少了一个逗号")

        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            dist.mkdir()
            with self.assertRaises(DeploymentConfigError):
                create_app(
                    WorldControlPlane(root=Path(tmp) / "worlds"),
                    settings=DeploymentSettings(
                        mode="production", admin_token=ADMIN_TOKEN
                    ),
                    dashboard_dist=dist,
                    registry_provider=broken,
                )

    def test_development_stays_open_when_no_token_is_configured(self):
        """既有本地开发行为不变——但那条路径永远到不了生产。"""
        settings = DeploymentSettings(mode="development", admin_token=None)
        self.assertFalse(settings.auth_required)
        self.assertFalse(settings.production)

    def test_from_env_reads_the_documented_names(self):
        env = {
            ENV_MODE: "production",
            ENV_ADMIN_TOKEN: ADMIN_TOKEN,
            ENV_SESSION_TTL: "3600",
            ENV_COOKIE_SECURE: "true",
        }
        settings = DeploymentSettings.from_env(env)
        self.assertTrue(settings.production)
        self.assertTrue(settings.cookie_secure)
        self.assertEqual(settings.session_ttl_seconds, 3600.0)

    def test_from_env_defaults_to_development(self):
        settings = DeploymentSettings.from_env({})
        self.assertFalse(settings.production)
        self.assertFalse(settings.auth_required)

    def test_unreadable_values_fail_loudly_instead_of_falling_back(self):
        """悄悄回落是这类配置最坏的行为：操作者以为自己开了，其实没开。"""
        with self.assertRaises(DeploymentConfigError):
            DeploymentSettings.from_env({ENV_COOKIE_SECURE: "yes-please"})
        with self.assertRaises(DeploymentConfigError):
            DeploymentSettings.from_env({ENV_SESSION_TTL: "一小时"})
        with self.assertRaises(DeploymentConfigError):
            DeploymentSettings.from_env({ENV_SESSION_TTL: "5"})


# ── 5. 密钥不出现在响应里 ───────────────────────────────────────────────
class NoSecretInResponsesTests(AuthTestCase):
    def assert_clean(self, response):
        for canary in (ADMIN_TOKEN, KEY_CANARY):
            self.assertNotIn(canary, response.text)
            for name, value in response.headers.items():
                if name.lower() == "set-cookie":
                    self.assertNotIn(canary, value)
                self.assertNotIn(canary, name)
                self.assertNotIn(canary, value)

    def test_public_surface_carries_no_secret(self):
        for path in ("/healthz", "/readyz", "/api/auth/session"):
            with self.subTest(path=path):
                self.assert_clean(self.client.get(path))

    def test_denied_and_error_responses_carry_no_secret(self):
        self.assert_clean(self.client.get("/api/config"))
        self.assert_clean(self.client.post("/api/auth/login", json={"token": "wrong"}))
        self.assert_clean(
            self.client.post("/api/auth/login", json={"token": ADMIN_TOKEN})
        )
        self.assert_clean(
            self.client.get("/api/persistent-worlds/missing", headers=self.bearer)
        )
        self.assert_clean(
            self.client.post(
                "/api/persistent-worlds",
                json={"world_id": "", "scene": SCENE, "characters": []},
                headers=self.bearer,
            )
        )

    def test_authenticated_surface_carries_no_secret(self):
        self.assertEqual(self.create_world().status_code, 201)
        for path in (
            "/api/config",
            "/api/config/reload",
            "/api/config/providers",
            "/api/persistent-worlds",
            "/api/persistent-worlds/nightcord",
            "/api/review/turns",
        ):
            with self.subTest(path=path):
                self.assert_clean(self.client.get(path, headers=self.bearer))

    def test_session_cookie_is_not_the_admin_token(self):
        header = self.login().headers["set-cookie"]
        self.assertNotIn(ADMIN_TOKEN, header)


# ── 6. 健康检查没有权威副作用 ───────────────────────────────────────────
class HealthHasNoAuthorityTests(AuthTestCase):
    def test_health_checks_touch_nothing(self):
        revision_before = BOUNDARY.active().revision
        self.assertFalse(self.root.exists(), "存档根不该在任何人操作之前出现")
        for _ in range(20):
            self.assertEqual(self.client.get("/healthz").status_code, 200)
            self.assertEqual(self.client.get("/readyz").status_code, 200)
        self.assertFalse(self.root.exists(), "健康检查建了存档根")
        self.assertEqual(BOUNDARY.active().revision, revision_before)
        self.assertEqual(list(self.plane.service.list_worlds()), [])

    def test_health_bodies_say_only_what_they_may(self):
        ready = self.client.get("/readyz").json()
        self.assertEqual(
            set(ready), {"status", "mode", "auth_required", "dashboard"}
        )
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})


# ── 7. 这些绿灯是中间件挣来的 ───────────────────────────────────────────
class MiddlewareIsTheMechanismTests(AuthTestCase):
    def test_without_the_middleware_the_same_requests_succeed(self):
        """把守卫拿掉，上面那批 401 必须变成 200。

        否则"被保护"可能只是别的东西碰巧挡住了，这一整组测试就没有证伪能力。
        """
        naked = FastAPI()
        naked.state.world_control_plane = self.plane
        naked.state.admin_auth = AdminAuth(self.settings)
        from pns.interfaces import config as config_routes
        from pns.interfaces import persistent_worlds as world_routes

        naked.include_router(config_routes.router)
        naked.include_router(world_routes.router)
        with TestClient(naked) as client:
            self.assertEqual(client.get("/api/config").status_code, 200)
            self.assertEqual(client.get("/api/persistent-worlds").status_code, 200)


# ── 会话与节流的行为 ────────────────────────────────────────────────────
class SessionStoreTests(unittest.TestCase):
    def test_sessions_expire(self):
        now = [0.0]
        store = SessionStore(100.0, clock=lambda: now[0])
        sid = store.issue()
        self.assertTrue(store.valid(sid))
        now[0] = 100.0
        self.assertFalse(store.valid(sid))
        self.assertEqual(store.live, 0)

    def test_issuing_beyond_the_cap_drops_the_oldest(self):
        now = [0.0]
        store = SessionStore(1000.0, max_sessions=2, clock=lambda: now[0])
        first = store.issue()
        now[0] += 1
        second = store.issue()
        now[0] += 1
        third = store.issue()
        self.assertFalse(store.valid(first))
        self.assertTrue(store.valid(second))
        self.assertTrue(store.valid(third))

    def test_session_ids_are_unique_and_unguessable(self):
        store = SessionStore(1000.0, max_sessions=1000)
        ids = {store.issue() for _ in range(200)}
        self.assertEqual(len(ids), 200)
        self.assertTrue(all(len(sid) >= 32 for sid in ids))

    def test_revoke_is_immediate(self):
        store = SessionStore(1000.0)
        sid = store.issue()
        store.revoke(sid)
        self.assertFalse(store.valid(sid))

    def test_empty_session_id_is_never_valid(self):
        store = SessionStore(1000.0)
        store.issue()
        for candidate in (None, "", "   "):
            self.assertFalse(store.valid(candidate))


class LoginThrottleTests(unittest.TestCase):
    def test_window_expires(self):
        now = [0.0]
        throttle = LoginThrottle(max_failures=2, window_seconds=10.0, clock=lambda: now[0])
        throttle.record_failure()
        throttle.record_failure()
        self.assertTrue(throttle.blocked())
        now[0] = 11.0
        self.assertFalse(throttle.blocked())

    def test_success_resets(self):
        throttle = LoginThrottle(max_failures=2, window_seconds=10.0)
        throttle.record_failure()
        throttle.reset()
        throttle.record_failure()
        self.assertFalse(throttle.blocked())


if __name__ == "__main__":
    unittest.main()
