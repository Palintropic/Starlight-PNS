# tests/test_usage_api.py — 用量账本的 HTTP 边界（COST-1）
#
# 守的线：
#   1. 未登录拿不到汇总；observer 能看、不能设锚点；operator 能设；
#   2. 锚点的校验在边界上生效：字符串、布尔、NaN、未来、无时区一律 422；
#   3. 汇总里带着连续失败段和余额估算；
#   4. 阈值环境变量写错了，进程起不来。
#
# 运行: python -m unittest discover -s tests -p test_usage_api.py
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.test_auth_api import ApiTestCase  # noqa: F401 - 只拿夹具，不重跑它的测试

from pns.interfaces import usage as usage_api
from pns.runtime import usage_ledger as ul


def _failed(at):
    return ul.LedgerRecord(
        at=at, world_id="yoake-mae", character_id="kanade", path=ul.PATH_GENERATION,
        model="mimo-v2.5-pro", protocol="anthropic", transport=ul.TRANSPORT_ERROR,
        failure=ul.FAILURE_PAYMENT, operation=ul.OPERATION_FAILED, usage=None,
        prices=None, cost_yuan=None,
    )


class UsageApiTests(ApiTestCase):
    def test_summary_needs_a_principal(self):
        self.assertEqual(self.client.get("/api/usage/summary").status_code, 401)

    def test_observer_reads_but_cannot_set_the_anchor(self):
        observer = self.as_observer()
        self.assertEqual(observer.get("/api/usage/summary").status_code, 200)
        response = observer.post("/api/usage/balance-anchor", json={"balance_yuan": 99.42})
        self.assertEqual(response.status_code, 403)

    def test_operator_sets_the_anchor_and_summary_reports_it(self):
        operator = self.as_operator()
        response = operator.post("/api/usage/balance-anchor", json={"balance_yuan": 99.42})
        self.assertEqual(response.status_code, 200, response.text)
        balance = operator.get("/api/usage/summary").json()["balance"]
        self.assertEqual(balance["anchor"]["balance_yuan"], 99.42)
        self.assertEqual(balance["estimated_yuan"], 99.42)
        self.assertTrue(balance["anchor_before_ledger"])  # 账本还是空的

    def test_anchor_validation_happens_at_the_boundary(self):
        operator = self.as_operator()
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        for body in (
            {"balance_yuan": "100"},
            {"balance_yuan": True},
            {"balance_yuan": 2_000_000},
            {"balance_yuan": 100, "at": future},
            {"balance_yuan": 100, "at": "2026-10-08T12:00:00"},
        ):
            with self.subTest(body=body):
                response = operator.post("/api/usage/balance-anchor", json=body)
                self.assertEqual(response.status_code, 422, response.text)
        # Python 的 json 解析认 Infinity / NaN，所以它们真的会进到校验里。
        for raw in ('{"balance_yuan": Infinity}', '{"balance_yuan": NaN}'):
            with self.subTest(raw=raw):
                response = operator.post(
                    "/api/usage/balance-anchor", content=raw,
                    headers={"content-type": "application/json"},
                )
                self.assertEqual(response.status_code, 422, response.text)

    def test_failure_streak_is_in_the_summary(self):
        ledger = self.plane.usage_ledger
        now = datetime.now(timezone.utc)
        for minutes in range(6, 0, -1):
            ledger.append(_failed(now - timedelta(minutes=minutes)))
        streak = self.as_observer().get("/api/usage/summary").json()["failure_streak"]
        self.assertEqual(streak["consecutive"], 6)
        self.assertTrue(streak["alert"])
        self.assertEqual(streak["last_failure"], "payment")

    def test_days_is_bounded(self):
        observer = self.as_observer()
        self.assertEqual(observer.get("/api/usage/summary?days=0").status_code, 422)
        self.assertEqual(observer.get("/api/usage/summary?days=91").status_code, 422)


class UsageSettingsTests(unittest.TestCase):
    def test_defaults(self):
        settings = usage_api.UsageSettings.from_env({})
        self.assertEqual((settings.failure_alert, settings.balance_warn_yuan), (6, 10.0))

    def test_bad_values_fail_loudly(self):
        for env in (
            {usage_api.FAILURE_ALERT_ENV: "6次"},
            {usage_api.FAILURE_ALERT_ENV: "0"},
            {usage_api.BALANCE_WARN_ENV: "十块"},
            {usage_api.BALANCE_WARN_ENV: "inf"},
        ):
            with self.subTest(env=env), self.assertRaises(usage_api.UsageSettingsError):
                usage_api.UsageSettings.from_env(env)

    def test_create_app_refuses_bad_thresholds(self):
        from pns.interfaces.app import create_app
        from pns.interfaces.composition import WorldControlPlane

        with patch.dict(os.environ, {usage_api.FAILURE_ALERT_ENV: "many"}):
            with self.assertRaises(usage_api.UsageSettingsError):
                create_app(WorldControlPlane())


if __name__ == "__main__":
    unittest.main()
