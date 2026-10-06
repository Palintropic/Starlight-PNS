# tests/test_world2_api.py — WORLD-2 C8b：三个 break-glass 接口与运维脚本。
#
# 守的线（实施单 §4.8、审查 F2 / §5）：
#   1. 鉴权：未认证 401、observer / operator / admin 会话 403、open-development 403、
#      合法 bearer 通过、非法 bearer 401；不合格主体发畸形请求体先得到鉴权拒绝；
#      无 Origin 的运维调用通过、跨源写拒绝；
#   2. 接口：扩展经 HTTP 预检 → 执行 → durable，GET 查询同一笔；入住预检给出不可行
#      原因；畸形请求 422、被拒 409，都带 outcome=rejected；世界没开着不是 200；
#   3. 脚本：只经这三个接口；execute 先把请求体记到文件，--request 原样重发得到
#      retry；status 只读。
#
# 运行: python -m unittest discover -s tests -p test_world2_api.py
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts"), str(REPO_ROOT / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from fastapi.testclient import TestClient  # noqa: E402

import admit_residents  # noqa: E402
from pns.interfaces.app import create_app  # noqa: E402
from pns.interfaces.composition import WorldControlPlane  # noqa: E402
from pns.interfaces.security import DeploymentSettings  # noqa: E402

from tests.test_auth_api import ADMIN_TOKEN, ApiTestCase  # noqa: E402
from tests.test_world2_content import _old_graph  # noqa: E402

BASE = "/api/persistent-worlds/yoake-mae/world2"


class World2ApiTestCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        with patch("pns.runtime.content_registry.build_default_location_graph", _old_graph):
            self.plane.create_formal("yoake-mae")

    def post(self, path, body, client=None, headers=None):
        client = client or self.client
        return client.post(BASE + path, json=body, headers=headers if headers is not None else self.bearer)


class AuthTests(World2ApiTestCase):
    routes = (
        ("POST", "/preflight", {"kind": "extension"}),
        ("POST", "/operations", {"kind": "extension"}),
        ("GET", "/operations/op-1", None),
    )

    def call(self, client, method, path, body, headers=None):
        if method == "GET":
            return client.get(BASE + path, headers=headers or {})
        return client.post(BASE + path, json=body, headers=headers or {})

    def test_unauthenticated_is_401(self):
        for method, path, body in self.routes:
            with self.subTest(route=path, method=method):
                self.assertEqual(self.call(self.client, method, path, body).status_code, 401)

    def test_a_bad_bearer_is_401(self):
        bad = {"Authorization": "Bearer " + "x" * 48}
        for method, path, body in self.routes:
            with self.subTest(route=path, method=method):
                self.assertEqual(self.call(self.client, method, path, body, bad).status_code, 401)

    def test_every_browser_session_is_403_even_admin(self):
        for session in (self.as_observer(), self.as_operator(), self.as_admin()):
            for method, path, body in self.routes:
                with self.subTest(route=path, method=method):
                    self.assertEqual(self.call(session, method, path, body).status_code, 403)

    def test_open_development_is_403(self):
        app = create_app(
            WorldControlPlane(root=self.tmp / "dev-worlds"),
            settings=DeploymentSettings(mode="development", admin_token=None, accounts_db=str(self.tmp / "none.db")),
        )
        client = TestClient(app)
        for method, path, body in self.routes:
            with self.subTest(route=path, method=method):
                self.assertEqual(self.call(client, method, path, body).status_code, 403)

    def test_the_bearer_passes(self):
        response = self.call(self.client, "POST", "/preflight", {"kind": "extension"}, self.bearer)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["feasible"])

    def test_auth_comes_before_body_parsing(self):
        operator = self.as_operator()
        response = operator.post(BASE + "/operations", content=b"{not json", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post(BASE + "/operations", content=b"{not json", headers={**self.bearer, "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 422)

    def test_cross_origin_writes_are_refused(self):
        response = self.client.post(
            BASE + "/preflight",
            json={"kind": "extension"},
            headers={**self.bearer, "Origin": "https://evil.example"},
        )
        self.assertEqual(response.status_code, 403)


class PrincipalRuleTests(unittest.TestCase):
    """主体依赖的两半各自成立：只认 break-glass 这个主体，而且只认它经 bearer 来。"""

    def principal(self, **fields):
        from dataclasses import replace

        from pns.interfaces.authz import break_glass_principal

        return replace(break_glass_principal(), **fields)

    def check(self, principal):
        from types import SimpleNamespace

        from fastapi import HTTPException

        from pns.interfaces.authz import PRINCIPAL_SCOPE_KEY
        from pns.interfaces.persistent_worlds import require_break_glass

        request = SimpleNamespace(scope={PRINCIPAL_SCOPE_KEY: principal})
        try:
            require_break_glass(request)
        except HTTPException as e:
            return e.status_code
        return 200

    def test_break_glass_via_bearer_passes(self):
        self.assertEqual(self.check(self.principal()), 200)

    def test_another_bearer_principal_is_refused(self):
        self.assertEqual(self.check(self.principal(principal_id="svc-automation")), 403)

    def test_break_glass_not_via_bearer_is_refused(self):
        self.assertEqual(self.check(self.principal(via="session")), 403)


class OperationTests(World2ApiTestCase):
    def test_extension_over_http(self):
        pre = self.post("/preflight", {"kind": "extension"}).json()
        body = {"kind": "extension", "operation_id": "ext-1", **pre["fingerprints"]}
        result = self.post("/operations", body)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["outcome"], "durable")
        again = self.post("/operations", body).json()
        self.assertTrue(again["retry"])
        queried = self.client.get(BASE + "/operations/ext-1", headers=self.bearer).json()
        self.assertEqual(queried["outcome"], "durable")
        self.assertEqual(queried["event_id"], result.json()["event_id"])

    def test_admission_preflight_reports_why_not_now(self):
        self.post("/operations", {"kind": "extension", "operation_id": "ext-1", **self.post("/preflight", {"kind": "extension"}).json()["fingerprints"]})
        pre = self.post(
            "/preflight",
            {"kind": "admission", "character_id": "airi", "roommates": ["minori", "haruka", "shizuku"], "ordinal": 0},
        )
        self.assertEqual(pre.status_code, 200, pre.text)
        payload = pre.json()
        self.assertFalse(payload["feasible"])  # 开局 19:00，不在睡眠段
        self.assertEqual(payload["reasons"][0]["code"], "not_asleep")
        self.assertIsNotNone(payload["next_window"])

    def test_rejections_carry_the_outcome(self):
        bad = self.post("/operations", {"kind": "extension", "operation_id": "ext-1"})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(bad.json()["detail"]["outcome"], "rejected")
        stale = self.post("/operations", {"kind": "extension", "operation_id": "ext-1", "graph_before": "0" * 64, "graph_after": "0" * 64})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"]["code"], "stale_preflight")

    def test_a_world_that_is_not_open(self):
        self.plane.close("yoake-mae")
        response = self.post("/preflight", {"kind": "extension"})
        self.assertNotEqual(response.status_code, 200)
        self.assertIn(response.status_code, (404, 409))


class ScriptTests(World2ApiTestCase):
    def sender(self):
        def send(method, path, body):
            if method == "GET":
                response = self.client.get(path, headers=self.bearer)
            else:
                response = self.client.post(path, json=body, headers=self.bearer)
            return response.status_code, response.json()

        return send

    def test_preflight_execute_resend_and_status(self):
        send = self.sender()
        self.assertEqual(admit_residents.run(["preflight", "extension"], send=send), 0)
        record = self.tmp / "ext.json"
        code = admit_residents.run(
            ["execute", "extension", "--operation-id", "ext-1", "--record", str(record)], send=send
        )
        self.assertEqual(code, 0)
        body = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(body["operation_id"], "ext-1")
        # 重发同一份请求：只查询或补存。
        self.assertEqual(admit_residents.run(["execute", "--request", str(record)], send=send), 0)
        self.assertEqual(admit_residents.run(["status", "ext-1"], send=send), 0)
        events = self.plane.service.opened("yoake-mae").state.events.events()
        self.assertEqual(sum(1 for e in events if e.event_id == "world2:ext-1"), 1)

    def test_infeasible_preflight_exits_2_and_sends_nothing(self):
        send = self.sender()
        record = self.tmp / "airi.json"
        code = admit_residents.run(
            ["execute", "admission", "--operation-id", "a-1", "--character", "airi",
             "--roommates", "minori,haruka,shizuku", "--ordinal", "0", "--record", str(record)],
            send=send,
        )
        self.assertEqual(code, 2)
        self.assertFalse(record.exists())

    def test_exit_codes_follow_the_outcome(self):
        for outcome, expected in (("durable", 0), ("visible_not_durable", 3), ("committed", 3)):
            with self.subTest(outcome=outcome):
                def send(method, path, body, outcome=outcome):
                    return 200, {"outcome": outcome}

                record = self.tmp / f"{outcome}.json"
                record.write_text(json.dumps({"kind": "extension"}), encoding="utf-8")
                self.assertEqual(admit_residents.run(["execute", "--request", str(record)], send=send), expected)

    def test_the_token_comes_from_the_environment(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(admit_residents.run(["status", "x"]), 1)


if __name__ == "__main__":
    unittest.main()
