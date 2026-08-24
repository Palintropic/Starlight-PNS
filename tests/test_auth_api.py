# tests/test_auth_api.py — AUTH-1 的接口边界
#
# 盯住的东西按"错了会怎样"排：
#
#   1. **失败无法区分。** 用户名不存在、密码不对、账户被停用，响应必须一模
#      一样。任何一处区别都会把登录框变成一台用户名枚举机。
#   2. **写权限默认拒绝。** 判据是方法和路径，不是"这条路由记得挂依赖"。
#      所以明天新加的那条 POST，observer 也进不去。
#   3. **撤销是即时的，而且不依赖通知。** 停用/改角色/改密码之后，目标手上
#      那张 Cookie 在**下一次请求**就失效——判据是账户库里的 security_revision，
#      不是有人记得去清进程内的会话表。清表只是让"立刻"不必等下一次请求。
#   4. **最后一个管理员挪不走。** 并发的两次降级不可能都通过。
#   5. **break-glass 与人类账户互不相干。** bearer 不受账户停用影响，也不出现
#      在用户列表里、不能改密码；反过来，它也不能从登录框进来。
#   6. **跨源的写请求在认证之前就被挡掉。** SameSite 是第一把锁，这是第二把。
#   7. **生产 fail-closed。** 没有启用着的管理员就起不来，不是"起来但登不进"。
#   8. **这些绿灯是机制挣来的。** 把中间件摘掉，同一批 403 必须变成 200。
#
# 运行: python -m unittest tests.test_auth_api -v
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts"), str(REPO_ROOT / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from pns.interfaces.app import create_app  # noqa: E402
from pns.interfaces.authz import (  # noqa: E402
    BREAK_GLASS_PRINCIPAL_ID,
    OPEN_DEVELOPMENT_PRINCIPAL_ID,
    SELF_SERVICE_PATHS,
    required_scope,
)
from pns.interfaces.composition import WorldControlPlane  # noqa: E402
from pns.interfaces.security import (  # noqa: E402
    ENV_ACCOUNTS_DB,
    ENV_BOOTSTRAP_PASSWORD_HASH,
    ENV_BOOTSTRAP_USERNAME,
    ENV_TRUSTED_ORIGINS,
    DeploymentConfigError,
    DeploymentSettings,
)
from pns.runtime.reload import BOUNDARY  # noqa: E402

from accounts_support import (  # noqa: E402
    ADMIN_PASSWORD,
    ADMIN_PASSWORD_HASH,
    ADMIN_USERNAME,
    OTHER_PASSWORD,
    cheap_store,
)

ADMIN_TOKEN = "AUTH1-ADMIN-CANARY-a1b2c3d4e5f60718293a4b5c6d7e8f90"
KEY_CANARY = "AUTH1-MODEL-CANARY-0f1e2d3c4b5a69788796a5b4c3d2e1f0"

# 一次登录用得到的密码。它是**明文**，所以任何一条响应或磁盘内容里出现它
# 都是一次泄露。
OPERATOR_PASSWORD = "operator-password-01"
OBSERVER_PASSWORD = "observer-password-01"


class _FakeModelClient:
    def __getattr__(self, name):  # pragma: no cover - 被调用即测试有问题
        raise AssertionError(f"测试不该真的调用模型客户端（.{name}）")


def declared_operations(app):
    """(method, path) 全表，来自 app 自己的 OpenAPI 描述。

    刻意不手写清单：新加一条路由就自动进入这些用例的射程。
    """
    for path, operations in app.openapi()["paths"].items():
        concrete = path.replace("{world_id}", "nightcord").replace(
            "{principal_id}", "p-unknown"
        )
        for method in operations:
            if method.upper() in ("OPTIONS", "HEAD"):
                continue
            yield method.upper(), concrete


class ApiTestCase(unittest.TestCase):
    """一份独立的账户库、存档根和部署设定；三个角色各一个账户。"""

    def setUp(self):
        self.registry = BOUNDARY.active()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "worlds"
        self._env = patch.dict(os.environ, {self.registry.models.key_name: KEY_CANARY})
        self._env.start()
        self.addCleanup(self._env.stop)

        self.accounts = cheap_store(self.tmp / "accounts.sqlite3")
        self.addCleanup(self.accounts.close)
        self.admin = self.accounts.create_human(ADMIN_USERNAME, ADMIN_PASSWORD, "admin")
        self.operator = self.accounts.create_human(
            "operator-one", OPERATOR_PASSWORD, "operator"
        )
        self.observer = self.accounts.create_human(
            "observer-one", OBSERVER_PASSWORD, "observer"
        )

        self.settings = DeploymentSettings(
            mode="development", admin_token=ADMIN_TOKEN
        )
        self.plane = WorldControlPlane(
            root=self.root, client_factory=lambda *a, **k: _FakeModelClient()
        )
        self.addCleanup(self.plane.service.release_all)
        self.app = create_app(
            self.plane,
            settings=self.settings,
            account_store=self.accounts,
            registry_provider=lambda: self.registry,
        )
        self.client = TestClient(self.app)

    # ── 便捷 ────────────────────────────────────────────────────────────
    @property
    def bearer(self):
        return {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    def session_for(self, username, password) -> TestClient:
        """一个已登录的独立客户端。每个角色一份 Cookie 罐。"""
        client = TestClient(self.app)
        response = client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def as_admin(self):
        return self.session_for(ADMIN_USERNAME, ADMIN_PASSWORD)

    def as_operator(self):
        return self.session_for("operator-one", OPERATOR_PASSWORD)

    def as_observer(self):
        return self.session_for("observer-one", OBSERVER_PASSWORD)


# ── 1. 会话与登录 ───────────────────────────────────────────────────────
class SessionViewTests(ApiTestCase):
    def test_the_session_view_says_who_without_any_credential_material(self):
        anonymous = self.client.get("/api/auth/session").json()
        self.assertFalse(anonymous["authenticated"])
        self.assertIsNone(anonymous["principal"])

        client = self.as_admin()
        view = client.get("/api/auth/session").json()
        self.assertTrue(view["authenticated"])
        principal = view["principal"]
        self.assertEqual(principal["principal_id"], self.admin.principal_id)
        self.assertEqual(principal["role"], "admin")
        self.assertEqual(principal["via"], "session")
        self.assertEqual(
            sorted(principal["scopes"]), ["accounts:manage", "operate", "read"]
        )
        for leak in ("password_hash", "security_revision", "argon2"):
            self.assertNotIn(leak, str(view))

    def test_every_login_failure_looks_exactly_the_same(self):
        """未知用户、密码不对、账户被停用——三条路径的响应必须逐字节一致。"""
        disabled = self.accounts.create_human("disabled-one", OTHER_PASSWORD, "observer")
        self.accounts.set_authority(disabled.principal_id, enabled=False)

        bodies = set()
        statuses = set()
        for username, password in (
            ("nobody-here", ADMIN_PASSWORD),
            (ADMIN_USERNAME, "wrong-password-here"),
            ("disabled-one", OTHER_PASSWORD),
        ):
            with self.subTest(username=username):
                # 每次用一个干净的客户端：节流是按账户分桶的，别让上一条的失败
                # 把这一条变成 429。
                client = TestClient(self.app)
                response = client.post(
                    "/api/auth/login",
                    json={"username": username, "password": password},
                )
                self.assertEqual(response.status_code, 401)
                self.assertNotIn("set-cookie", {k.lower() for k in response.headers})
                bodies.add(response.text)
                statuses.add(response.status_code)
        self.assertEqual(len(bodies), 1, f"失败之间有区别：{bodies}")
        self.assertEqual(statuses, {401})

    def test_the_session_cookie_is_not_any_credential(self):
        response = self.client.post(
            "/api/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        header = response.headers["set-cookie"]
        self.assertNotIn(ADMIN_PASSWORD, header)
        self.assertNotIn(ADMIN_TOKEN, header)
        self.assertNotIn("$argon2", header)
        self.assertIn("HttpOnly", header)

    def test_logging_out_kills_the_session_and_leaves_an_audit_trail(self):
        client = self.as_admin()
        self.assertEqual(client.get("/api/config").status_code, 200)
        self.assertEqual(client.post("/api/auth/logout").status_code, 200)
        self.assertEqual(client.get("/api/config").status_code, 401)
        actions = [r["action"] for r in self.accounts.audit_records()]
        self.assertIn("auth.logout", actions)
        self.assertIn("auth.login", actions)


# ── 2. 授权默认拒绝 ─────────────────────────────────────────────────────
class ScopeBoundaryTests(ApiTestCase):
    def test_an_observer_cannot_mutate_through_any_declared_route(self):
        """判据来自方法，不是路由表 —— 所以这条用例覆盖的是**全表**。"""
        observer = self.as_observer()
        checked = 0
        for method, path in declared_operations(self.app):
            if method in ("GET",) or path in SELF_SERVICE_PATHS:
                continue
            if path.startswith("/api/auth/"):
                continue  # 登录/登出是公开面，不是变更
            checked += 1
            with self.subTest(operation=f"{method} {path}"):
                response = observer.request(method, path)
                self.assertEqual(
                    response.status_code, 403, f"{method} {path} 放行了只读账户"
                )
                self.assertEqual(response.json()["detail"]["category"], "forbidden")
        self.assertGreater(checked, 8, "路由表看起来没被真的遍历到")

    def test_an_operator_cannot_touch_account_administration(self):
        operator = self.as_operator()
        for method, path in (
            ("GET", "/api/accounts"),
            ("GET", "/api/accounts/audit"),
            ("POST", "/api/accounts"),
            ("POST", f"/api/accounts/{self.observer.principal_id}/role"),
            ("POST", f"/api/accounts/{self.observer.principal_id}/enabled"),
            ("POST", f"/api/accounts/{self.observer.principal_id}/password"),
        ):
            with self.subTest(operation=f"{method} {path}"):
                self.assertEqual(operator.request(method, path).status_code, 403)
        # 但 operator 该能做的事一件都没少。
        self.assertEqual(operator.get("/api/persistent-worlds").status_code, 200)
        self.assertEqual(operator.post("/api/config/reload").status_code, 200)

    def test_an_admin_can_administer(self):
        admin = self.as_admin()
        self.assertEqual(admin.get("/api/accounts").status_code, 200)
        created = admin.post(
            "/api/accounts",
            json={"username": "kanade", "password": OTHER_PASSWORD, "role": "observer"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["role"], "observer")
        self.assertNotIn("password", created.text)

    def test_observers_can_still_change_their_own_password(self):
        """自服务清单是显式的：一个被重置了密码的只读账户必须换得掉那个
        由别人设定的密码。"""
        observer = self.as_observer()
        response = observer.post(
            "/api/auth/password",
            json={
                "current_password": OBSERVER_PASSWORD,
                "new_password": "observer-password-02",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["authenticated"])

    def test_a_route_added_later_is_still_write_protected(self):
        """保护靠机制，不靠有人记得挂依赖。

        这条用例自己起一个没有前端产物的 app：`create_app` 在有 dist 时会把
        `/` 整个挂成静态兜底，那个 mount 会先于事后追加的路由匹配上，于是
        operator 那一半会拿到 405 而不是 200 —— 那样这条用例就只证明了一半。
        """
        app = create_app(
            WorldControlPlane(root=self.tmp / "future"),
            settings=self.settings,
            account_store=self.accounts,
            registry_provider=lambda: self.registry,
            dashboard_dist=self.tmp / "no-dist",
        )

        @app.post("/api/some-future-write")
        def _future():
            return {"ok": True}

        def logged_in(username, password):
            client = TestClient(app)
            self.assertEqual(
                client.post(
                    "/api/auth/login",
                    json={"username": username, "password": password},
                ).status_code,
                200,
            )
            return client

        observer = logged_in("observer-one", OBSERVER_PASSWORD)
        operator = logged_in("operator-one", OPERATOR_PASSWORD)
        self.assertEqual(observer.post("/api/some-future-write").status_code, 403)
        self.assertEqual(operator.post("/api/some-future-write").status_code, 200)

    def test_the_default_scope_table_matches_what_the_routes_assume(self):
        self.assertEqual(required_scope("/api/anything", "GET"), "read")
        self.assertEqual(required_scope("/api/anything", "POST"), "operate")
        self.assertEqual(required_scope("/api/anything", "DELETE"), "operate")
        self.assertEqual(required_scope("/ws/run", "GET", "websocket"), "operate")
        self.assertIsNone(required_scope("/api/auth/password", "POST"))

    def test_a_websocket_is_operate_gated_not_merely_authenticated(self):
        """`/ws/run` 会花模型额度。它不该因为"不是 POST"就落进只读那一档。"""
        observer = self.as_observer()
        with self.assertRaises(Exception):
            with observer.websocket_connect("/ws/run"):
                pass  # pragma: no cover - 握手不该成功

    def test_without_the_middleware_the_same_requests_succeed(self):
        """把守卫拿掉，上面那批 403 必须变成 200。

        否则"被拒绝"可能只是别的东西碰巧挡住了，这一整组就没有证伪能力。
        """
        naked = FastAPI()
        naked.state.world_control_plane = self.plane
        from pns.interfaces import config as config_routes

        naked.include_router(config_routes.router)
        with TestClient(naked) as client:
            self.assertEqual(client.post("/api/config/reload").status_code, 200)


# ── 3. 撤销 ─────────────────────────────────────────────────────────────
class RevocationTests(ApiTestCase):
    def test_disabling_an_account_invalidates_its_sessions_before_the_next_call(self):
        victim = self.as_operator()
        self.assertEqual(victim.get("/api/config").status_code, 200)

        admin = self.as_admin()
        response = admin.post(
            f"/api/accounts/{self.operator.principal_id}/enabled",
            json={"enabled": False},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreaterEqual(response.json()["revoked_sessions"], 1)

        self.assertEqual(victim.get("/api/config").status_code, 401)
        self.assertEqual(victim.post("/api/config/reload").status_code, 401)

    def test_changing_a_role_invalidates_the_old_session(self):
        victim = self.as_operator()
        self.assertEqual(victim.post("/api/config/reload").status_code, 200)
        admin = self.as_admin()
        self.assertEqual(
            admin.post(
                f"/api/accounts/{self.operator.principal_id}/role",
                json={"role": "observer"},
            ).status_code,
            200,
        )
        self.assertEqual(victim.post("/api/config/reload").status_code, 401)

    def test_resetting_a_password_invalidates_the_old_session(self):
        victim = self.as_operator()
        admin = self.as_admin()
        self.assertEqual(
            admin.post(
                f"/api/accounts/{self.operator.principal_id}/password",
                json={"password": "brand-new-password-1"},
            ).status_code,
            200,
        )
        self.assertEqual(victim.get("/api/config").status_code, 401)
        # 新密码真的生效了，旧的真的没了。
        self.assertEqual(
            self.client.post(
                "/api/auth/login",
                json={"username": "operator-one", "password": OPERATOR_PASSWORD},
            ).status_code,
            401,
        )
        fresh = self.session_for("operator-one", "brand-new-password-1")
        self.assertEqual(fresh.get("/api/config").status_code, 200)

    def test_changing_your_own_password_also_kills_your_own_session(self):
        """改密码最常见的理由是"我怀疑它泄露了"。那种时候留着手上这张，
        恰好留错了。"""
        me = self.as_operator()
        response = me.post(
            "/api/auth/password",
            json={
                "current_password": OPERATOR_PASSWORD,
                "new_password": "operator-password-02",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["authenticated"])
        self.assertEqual(me.get("/api/config").status_code, 401)

    def test_a_wrong_current_password_changes_nothing(self):
        me = self.as_operator()
        response = me.post(
            "/api/auth/password",
            json={
                "current_password": "not-the-password",
                "new_password": "operator-password-02",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(me.get("/api/config").status_code, 200)

    def test_revocation_survives_a_change_this_process_never_saw(self):
        """撤销的机制是 security_revision，不是"有人记得清进程内的会话表"。

        这里模拟"另一个进程/离线命令改了权威"：直接对账户库动手，一次都不碰
        `AdminAuth.sessions`。那张 Cookie 仍然必须在下一次请求就失效。
        """
        victim = self.as_operator()
        self.assertEqual(victim.get("/api/config").status_code, 200)
        elsewhere = cheap_store(self.tmp / "accounts.sqlite3")
        self.addCleanup(elsewhere.close)
        elsewhere.set_authority(self.operator.principal_id, role="observer")
        self.assertEqual(victim.get("/api/config").status_code, 401)

    def test_the_last_admin_cannot_be_disabled_or_demoted(self):
        admin = self.as_admin()
        for path, payload in (
            (f"/api/accounts/{self.admin.principal_id}/enabled", {"enabled": False}),
            (f"/api/accounts/{self.admin.principal_id}/role", {"role": "operator"}),
        ):
            with self.subTest(path=path):
                response = admin.post(path, json=payload)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["detail"]["category"], "last_admin")
        # 会话没被顺手踢掉：一次被拒绝的操作什么都不该改。
        self.assertEqual(admin.get("/api/accounts").status_code, 200)

    def test_concurrent_demotions_cannot_both_remove_the_last_admin(self):
        """两个管理员，两个并发的降级请求。只有一个能赢。"""
        second = self.accounts.create_human("admin-two", OTHER_PASSWORD, "admin")
        one = self.as_admin()
        two = self.session_for("admin-two", OTHER_PASSWORD)
        barrier = threading.Barrier(2)
        outcomes = {}

        def demote(name, client, target):
            barrier.wait()
            response = client.post(
                f"/api/accounts/{target}/role", json={"role": "observer"}
            )
            outcomes[name] = response.status_code

        threads = [
            threading.Thread(target=demote, args=("a", one, second.principal_id)),
            threading.Thread(target=demote, args=("b", two, self.admin.principal_id)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(outcomes.values()), [200, 409], outcomes)
        self.assertEqual(self.accounts.enabled_admin_count(), 1)


# ── 4. break-glass 与人类账户互不相干 ───────────────────────────────────
class BreakGlassTests(ApiTestCase):
    def test_the_bearer_principal_is_a_service_not_a_user(self):
        view = self.client.get("/api/auth/session", headers=self.bearer).json()
        principal = view["principal"]
        self.assertEqual(principal["principal_id"], BREAK_GLASS_PRINCIPAL_ID)
        self.assertEqual(principal["kind"], "service")
        self.assertEqual(principal["via"], "bearer")
        listed = self.client.get("/api/accounts", headers=self.bearer).json()["users"]
        self.assertNotIn(
            BREAK_GLASS_PRINCIPAL_ID, [u["principal_id"] for u in listed]
        )
        self.assertTrue(all(u["kind"] == "human" for u in listed))

    def test_break_glass_keeps_working_when_every_human_is_locked_out(self):
        """这正是它存在的理由：人类那条路全断了，运维还进得来。"""
        self.accounts.set_authority(self.operator.principal_id, enabled=False)
        self.accounts.set_authority(self.observer.principal_id, enabled=False)
        self.accounts.set_password(self.admin.principal_id, "changed-by-someone-1")
        self.assertEqual(
            self.client.get("/api/accounts", headers=self.bearer).status_code, 200
        )

    def test_break_glass_has_no_password_to_change(self):
        response = self.client.post(
            "/api/auth/password",
            headers=self.bearer,
            json={"current_password": ADMIN_TOKEN, "new_password": OTHER_PASSWORD},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["category"], "not_an_account")

    def test_a_bearer_header_decides_even_when_a_session_cookie_is_present(self):
        """两份凭据的请求没有唯一答案。带着一个错的 bearer 却因为浏览器里还有
        一张有效 Cookie 而被放行，会让"这次用的是哪个凭据"说不清。"""
        client = self.as_admin()
        self.assertEqual(client.get("/api/config").status_code, 200)
        self.assertEqual(
            client.get(
                "/api/config", headers={"Authorization": "Bearer wrong"}
            ).status_code,
            401,
        )

    def test_the_bearer_actor_shows_up_in_the_audit_trail(self):
        self.client.post(
            "/api/accounts",
            headers=self.bearer,
            json={"username": "kanade", "password": OTHER_PASSWORD, "role": "observer"},
        )
        actors = [
            r["actor_principal_id"]
            for r in self.accounts.audit_records()
            if r["action"] == "account.create"
        ]
        self.assertIn(BREAK_GLASS_PRINCIPAL_ID, actors)


# ── 5. 跨源 ─────────────────────────────────────────────────────────────
class CrossOriginTests(ApiTestCase):
    def test_a_cross_origin_write_is_refused_before_authentication(self):
        admin = self.as_admin()
        response = admin.post(
            "/api/config/reload", headers={"Origin": "https://evil.example"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["category"], "cross_origin")

    def test_a_same_origin_write_goes_through(self):
        admin = self.as_admin()
        response = admin.post(
            "/api/config/reload", headers={"Origin": "http://testserver"}
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_the_public_login_route_is_covered_too(self):
        """公开面也算：一次跨站发起的 POST 不该因为它打的恰好是登录口就被放过。"""
        response = self.client.post(
            "/api/auth/login",
            headers={"Origin": "https://evil.example"},
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        self.assertEqual(response.status_code, 403)

    def test_safe_methods_are_not_affected(self):
        admin = self.as_admin()
        self.assertEqual(
            admin.get("/api/config", headers={"Origin": "https://evil.example"}).status_code,
            200,
        )

    def test_a_cross_origin_websocket_handshake_fails(self):
        admin = self.as_admin()
        with self.assertRaises(Exception):
            with admin.websocket_connect(
                "/ws/run", headers={"Origin": "https://evil.example"}
            ):
                pass  # pragma: no cover - 握手不该成功

    def test_an_opaque_origin_is_not_same_origin(self):
        admin = self.as_admin()
        self.assertEqual(
            admin.post("/api/config/reload", headers={"Origin": "null"}).status_code,
            403,
        )

    def test_a_trusted_origin_is_accepted_for_proxies_that_rewrite_host(self):
        settings = DeploymentSettings(
            mode="development",
            admin_token=ADMIN_TOKEN,
            trusted_origins=("https://pns.example.lan",),
        )
        app = create_app(
            WorldControlPlane(root=self.tmp / "trusted"),
            settings=settings,
            account_store=self.accounts,
            registry_provider=lambda: self.registry,
        )
        client = TestClient(app)
        client.post(
            "/api/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        self.assertEqual(
            client.post(
                "/api/config/reload", headers={"Origin": "https://pns.example.lan"}
            ).status_code,
            200,
        )
        self.assertEqual(
            client.post(
                "/api/config/reload", headers={"Origin": "https://other.example.lan"}
            ).status_code,
            403,
        )

    def test_tls_terminated_at_a_proxy_is_still_same_origin(self):
        """反向代理终结 TLS 之后，浏览器发 `https://host`，而应用在回环上看到
        的 scheme 是 `http`。按完整源比较的话，每一台正常的内网 TLS 部署上所有
        写操作都会 403 —— 所以判据是 authority，不是 scheme。
        """
        admin = self.as_admin()
        self.assertEqual(
            admin.post(
                "/api/config/reload", headers={"Origin": "https://testserver"}
            ).status_code,
            200,
        )
        # 但端口不同仍然算跨源。
        self.assertEqual(
            admin.post(
                "/api/config/reload", headers={"Origin": "http://testserver:9999"}
            ).status_code,
            403,
        )

    def test_duplicate_origin_headers_are_not_same_origin(self):
        """两个 Origin 头没有唯一答案，不许挑一个能过的。

        跟重复的 Authorization 头同一条纪律。浏览器只会发一个，所以这条挡的
        是刻意构造的请求。
        """
        admin = self.as_admin()
        response = admin.post(
            "/api/config/reload",
            headers=[
                ("Origin", "http://testserver"),
                ("Origin", "https://evil.example"),
            ],
        )
        self.assertEqual(response.status_code, 403)

    def test_a_malformed_trusted_origin_refuses_to_start(self):
        with self.assertRaises(DeploymentConfigError):
            DeploymentSettings(
                mode="development", admin_token=ADMIN_TOKEN,
                trusted_origins=("not-a-url",),
            )


# ── 5.1 请求体不许被抄回响应 ────────────────────────────────────────────
class ValidationErrorTests(ApiTestCase):
    """FastAPI/pydantic 的默认 422 正文里带一个 `input` 字段，装的是原样的
    提交值。在这台服务器上那意味着一个太长的密码、一把太长的模型 API Key，
    会被完整地写进一条 4xx 响应——而"凭据不出现在任何一条响应里"不该有一条
    "只在校验失败时"的例外。"""

    CANARY = "REQUEST-BODY-CANARY-" + "x" * 600

    def assert_no_echo(self, response):
        self.assertEqual(response.status_code, 422, response.text)
        self.assertNotIn("REQUEST-BODY-CANARY", response.text)
        # 但它仍然要说清楚哪个字段为什么不合法。
        detail = response.json()["detail"]
        self.assertTrue(detail)
        self.assertIn("msg", detail[0])
        self.assertIn("loc", detail[0])

    def test_a_rejected_login_never_echoes_the_password(self):
        self.assert_no_echo(
            self.client.post(
                "/api/auth/login",
                json={"username": "someone", "password": self.CANARY},
            )
        )

    def test_a_rejected_account_creation_never_echoes_the_password(self):
        self.assert_no_echo(
            self.as_admin().post(
                "/api/accounts",
                json={"username": "kanade", "password": self.CANARY, "role": "admin"},
            )
        )

    def test_a_rejected_password_change_never_echoes_either_password(self):
        self.assert_no_echo(
            self.as_operator().post(
                "/api/auth/password",
                json={"current_password": self.CANARY, "new_password": self.CANARY},
            )
        )

    def test_the_handler_is_global_not_just_on_the_auth_routes(self):
        """这个洞不只属于账户：任何一条收敏感字段的路由（比如 `POST
        /api/config` 收的模型凭据）都会在校验失败时把提交值抄回去。守卫因此
        挂在整个 app 上，而不是那几条登录路由上。
        """
        self.assert_no_echo(
            self.as_admin().post(
                "/api/review/decision",
                json={
                    "session_id": "s",
                    "turn": 1,
                    "character": "mizuki",
                    "decision": self.CANARY,
                },
            )
        )


# ── 6. 审计 ─────────────────────────────────────────────────────────────
class AuditTests(ApiTestCase):
    def test_each_action_leaves_a_record_and_none_of_them_carry_credentials(self):
        admin = self.as_admin()
        admin.post(
            "/api/accounts",
            json={"username": "kanade", "password": OTHER_PASSWORD, "role": "observer"},
        )
        admin.post(
            f"/api/accounts/{self.observer.principal_id}/role", json={"role": "operator"}
        )
        admin.post(
            f"/api/accounts/{self.observer.principal_id}/enabled", json={"enabled": False}
        )
        admin.post(
            f"/api/accounts/{self.observer.principal_id}/password",
            json={"password": "reset-by-admin-01"},
        )
        self.client.post(
            "/api/auth/login", json={"username": ADMIN_USERNAME, "password": "nope"}
        )
        admin.post("/api/auth/logout")

        payload = self.client.get("/api/accounts/audit", headers=self.bearer)
        self.assertEqual(payload.status_code, 200, payload.text)
        body = payload.text
        actions = {r["action"] for r in payload.json()["records"]}
        for expected in (
            "auth.login", "auth.logout", "account.create",
            "account.role", "account.enabled", "account.password_reset",
        ):
            self.assertIn(expected, actions)
        for secret in (
            ADMIN_PASSWORD, OTHER_PASSWORD, ADMIN_TOKEN, "reset-by-admin-01", "$argon2",
        ):
            self.assertNotIn(secret, body)

    def test_the_audit_trail_is_admin_only(self):
        self.assertEqual(self.as_operator().get("/api/accounts/audit").status_code, 403)
        self.assertEqual(self.as_observer().get("/api/accounts/audit").status_code, 403)
        self.assertEqual(self.as_admin().get("/api/accounts/audit").status_code, 200)


# ── 7. bootstrap 与生产 fail-closed ─────────────────────────────────────
class BootstrapAndProductionTests(unittest.TestCase):
    def setUp(self):
        self.registry = SimpleNamespace(
            models=SimpleNamespace(key_name="MIMO_API_KEY", api_key=KEY_CANARY),
            revision=1,
        )
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.dist = self.tmp / "dist"
        self.dist.mkdir()

    def production(self, **overrides):
        kwargs = {
            "mode": "production",
            "admin_token": ADMIN_TOKEN,
            "accounts_db": str(self.tmp / "accounts.sqlite3"),
        }
        kwargs.update(overrides)
        return DeploymentSettings(**kwargs)

    def build(self, settings, root="worlds"):
        """起一个 app，并保证它自己打开的账户库会被关掉。

        `create_app()` 打开的那条 sqlite 连接归 lifespan 关，而这些用例刻意
        不进 lifespan（它们要看的是**装配**成不成立）。不显式关的话，解释器
        收尾时会抛 ResourceWarning——那种噪音会盖住真正的资源泄漏。
        """
        app = create_app(
            WorldControlPlane(root=self.tmp / root),
            settings=settings,
            dashboard_dist=self.dist,
            registry_provider=lambda: self.registry,
        )
        store = getattr(app.state, "accounts", None)
        if store is not None:
            self.addCleanup(store.close)
        return app

    def test_production_without_an_enabled_admin_refuses_to_start(self):
        with self.assertRaises(DeploymentConfigError) as caught:
            self.build(self.production())
        self.assertIn("管理员", str(caught.exception))
        self.assertIn("hash-password", str(caught.exception))

    def test_bootstrap_variables_make_the_first_start_work_and_are_idempotent(self):
        settings = self.production(
            bootstrap_admin_username=ADMIN_USERNAME,
            bootstrap_admin_password_hash=ADMIN_PASSWORD_HASH,
        )
        app = self.build(settings)
        client = TestClient(app)
        response = client.post(
            "/api/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        self.assertEqual(response.status_code, 200, response.text)

        # 第二次启动（等于换一个容器）：账户还在，而且没有多出第二个。
        again = TestClient(self.build(settings))
        listed = again.get("/api/accounts", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json()["users"]), 1)
        # 但会话是进程内的东西，它没资格活过一次替换。
        self.assertEqual(again.get("/api/config").status_code, 401)

    def test_bootstrap_cannot_overwrite_an_existing_account(self):
        store = cheap_store(self.tmp / "accounts.sqlite3")
        original = store.create_human("someone", OTHER_PASSWORD, "admin")
        store.close()
        settings = self.production(
            bootstrap_admin_username=ADMIN_USERNAME,
            bootstrap_admin_password_hash=ADMIN_PASSWORD_HASH,
        )
        self.build(settings)
        reopened = cheap_store(self.tmp / "accounts.sqlite3")
        self.addCleanup(reopened.close)
        self.assertEqual(
            [p.principal_id for p in reopened.list_humans()], [original.principal_id]
        )

    def test_half_configured_bootstrap_refuses_to_construct(self):
        """半份配置什么都不做，而操作者以为自己建好了第一个管理员。"""
        with self.assertRaises(DeploymentConfigError):
            self.production(bootstrap_admin_username=ADMIN_USERNAME)
        with self.assertRaises(DeploymentConfigError):
            self.production(bootstrap_admin_password_hash=ADMIN_PASSWORD_HASH)

    def test_a_bootstrap_hash_that_is_not_argon2id_refuses_to_construct(self):
        for bad in ("hunter2", ADMIN_PASSWORD, "$2b$12$abcdefghijklmnopqrstuv"):
            with self.subTest(bad=bad):
                with self.assertRaises(DeploymentConfigError):
                    self.production(
                        bootstrap_admin_username=ADMIN_USERNAME,
                        bootstrap_admin_password_hash=bad,
                    )

    def test_from_env_reads_the_documented_names(self):
        settings = DeploymentSettings.from_env(
            {
                "PNS_ENV": "production",
                "PNS_ADMIN_TOKEN": ADMIN_TOKEN,
                ENV_ACCOUNTS_DB: "/srv/pns/accounts.sqlite3",
                ENV_BOOTSTRAP_USERNAME: ADMIN_USERNAME,
                ENV_BOOTSTRAP_PASSWORD_HASH: ADMIN_PASSWORD_HASH,
                ENV_TRUSTED_ORIGINS: "https://a.lan, https://b.lan",
            }
        )
        self.assertEqual(settings.accounts_db, "/srv/pns/accounts.sqlite3")
        self.assertEqual(settings.bootstrap_admin_username, ADMIN_USERNAME)
        self.assertEqual(
            settings.normalized_trusted_origins, ("https://a.lan", "https://b.lan")
        )
        # 公开视图里没有任何一样。
        self.assertEqual(set(settings.to_public_dict()), {"mode", "auth_required"})

    def test_a_development_server_with_no_accounts_creates_nothing(self):
        """import 和 create_app 都不该在磁盘上凭空造出一个空库。"""
        db = self.tmp / "never-created.sqlite3"
        settings = DeploymentSettings(
            mode="development", admin_token=None, accounts_db=str(db)
        )
        app = create_app(
            WorldControlPlane(root=self.tmp / "worlds2"), settings=settings
        )
        self.assertIsNone(getattr(app.state, "accounts", None))
        self.assertFalse(db.exists())
        client = TestClient(app)
        # 既有的开放开发行为不变。
        view = client.get("/api/auth/session").json()
        self.assertTrue(view["authenticated"])
        self.assertEqual(
            view["principal"]["principal_id"], OPEN_DEVELOPMENT_PRINCIPAL_ID
        )

    def test_an_existing_account_store_makes_authentication_required(self):
        """就算没配 token：库里有人，这台服务器就不再是开放的。"""
        db = self.tmp / "accounts.sqlite3"
        store = cheap_store(db)
        store.create_human(ADMIN_USERNAME, ADMIN_PASSWORD, "admin")
        store.close()
        settings = DeploymentSettings(
            mode="development", admin_token=None, accounts_db=str(db)
        )
        app = create_app(WorldControlPlane(root=self.tmp / "worlds3"), settings=settings)
        self.addCleanup(app.state.accounts.close)
        client = TestClient(app)
        self.assertEqual(client.get("/api/config").status_code, 401)
        self.assertTrue(client.get("/api/auth/session").json()["auth_required"])
        self.assertEqual(
            client.post(
                "/api/auth/login",
                json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            ).status_code,
            200,
        )

    def test_an_unopenable_account_store_is_a_startup_failure_not_a_fallback(self):
        """打不开就回落到"那就没有账户"，等于把一台配好的服务器变回公开的。"""
        db = self.tmp / "broken.sqlite3"
        db.write_bytes(b"this is definitely not a sqlite database" * 10)
        settings = DeploymentSettings(
            mode="development", admin_token=None, accounts_db=str(db)
        )
        with self.assertRaises(DeploymentConfigError):
            create_app(
                WorldControlPlane(root=self.tmp / "worlds4"), settings=settings
            )


if __name__ == "__main__":
    unittest.main()
