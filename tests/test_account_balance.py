# tests/test_account_balance.py — 按计费账户的余额（CFG-1 §4.3、§8.1 manual、§8.2、§11.10）
#
# 守的线：
#   1. 每个账户只扣归属它的行；没有 account 的旧行归 `env`；切到别的账户之后
#      的花费不从 `env` 余额里扣（复审 F7）；顶层 `balance` 只是 `env` 那一份；
#   2. 币种不是人民币的账户不相减，只给锚点和锚点之后的花费；
#   3. 锚点在账户退役、账户被重建（created_at 对不上）、币种对不上、内容坏掉时
#      失效，给出原因、不估算；
#   4. 锚点早于汇总窗口时，窗口之前、锚点之后的行照样扣；
#   5. 接口：带 account 设锚点、不带就是 env；没有的账户、退役账户、两个数额
#      字段都给或都不给一律 422；锚点文件读不出来时汇总照给但不估余额、设锚点
#      不改文件；并发给两个账户设锚点，两个都留下。
#
# 运行: python -m unittest tests.test_account_balance -v
import dataclasses
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tests.test_auth_api import ApiTestCase  # noqa: F401 - 只拿夹具，不重跑它的测试

from pns.runtime import account_anchors as aa
from pns.runtime import content_registry
from pns.runtime import usage_ledger as ul
from pns.runtime.env_migration import ANCHORS_FILE
from pns.runtime.provider_settings import AccountCatalog, env_account

T0 = datetime(2026, 10, 27, 4, tzinfo=timezone.utc)  # 北京 12:00
CREATED = "2026-10-25T00:00:00+00:00"
PRICES = ul.Prices(1.0, 1.0, 1.0, 1.0)


def _row(at, cost, account=None, *, transport=ul.TRANSPORT_RESPONSE, failure=None):
    # cost 元 = cost × 1e6 个输出 token × 1 元 / 百万
    usage = ul.Usage(0, 0, 0, int(cost * 1_000_000)) if cost is not None else None
    return ul.LedgerRecord(
        at=at, world_id="w", character_id="mizuki", path=ul.PATH_GENERATION,
        model="m", protocol="anthropic", transport=transport, failure=failure,
        operation=ul.OPERATION_OK if failure is None else ul.OPERATION_FAILED,
        usage=usage, prices=PRICES if usage is not None else None,
        cost_yuan=ul.estimate_cost(usage, PRICES if usage is not None else None),
        account=account,
    )


def _catalog(**extra):
    base = env_account(created_at=CREATED)
    accounts = {
        "env": base,
        "mimo": dataclasses.replace(base, account_id="mimo", kind="mimo"),
        "or": dataclasses.replace(base, account_id="or", kind="openrouter", currency="USD"),
    }
    accounts.update(extra)
    return AccountCatalog(accounts=accounts)


class _Usage(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "usage"
        self.now = T0 + timedelta(hours=1)
        self.ledger = ul.UsageLedger(self.root, clock=lambda: self.now)

    def anchor(self, account, balance, at, catalog=None):
        catalog = catalog or _catalog()
        return aa.save_account_anchor(self.root, catalog.get(account), balance, at, self.now)

    def summary(self, catalog=None, days=1):
        views = aa.account_views(self.root, catalog or _catalog())
        return ul.summarize(self.ledger, days=days, accounts=views)


class PerAccountBalanceTest(_Usage):
    def test_each_account_pays_only_for_its_own_rows(self):
        self.anchor("env", 100, T0)
        self.anchor("mimo", 50, T0)
        self.ledger.append(_row(T0 + timedelta(minutes=1), 2.0))          # 旧行：归 env
        self.ledger.append(_row(T0 + timedelta(minutes=2), 1.0, "env"))
        # 切到 mimo 账户之后：这些钱不许从 env 里扣。
        self.ledger.append(_row(T0 + timedelta(minutes=3), 5.0, "mimo"))
        self.ledger.append(_row(T0 + timedelta(minutes=4), 0.5, "mimo"))
        self.ledger.append(_row(T0 + timedelta(minutes=5), 7.0, "or"))
        summary = self.summary()
        balances = summary["balances"]
        self.assertEqual(balances["env"]["estimated"], 97.0)
        self.assertEqual(balances["env"]["spent_since_anchor_yuan"], 3.0)
        self.assertEqual(balances["mimo"]["estimated"], 44.5)
        # 顶层 balance 是 env 那一份、旧形状。
        self.assertEqual(summary["balance"]["estimated_yuan"], 97.0)
        self.assertEqual(summary["balance"]["anchor"]["balance_yuan"], 100)
        self.assertEqual(
            {k: v["cost_yuan"] for k, v in summary["by_account"].items()},
            {"env": 3.0, "mimo": 5.5, "or": 7.0},
        )

    def test_rows_before_the_anchor_and_uncounted_rows(self):
        self.ledger.append(_row(T0 - timedelta(minutes=1), 9.0, "mimo"))
        self.ledger.append(_row(T0, 9.0, "mimo"))  # 同一时刻算在锚点之前
        self.anchor("mimo", 50, T0)
        self.ledger.append(_row(T0 + timedelta(minutes=1), None, "mimo",
                                transport=ul.TRANSPORT_ERROR, failure=ul.FAILURE_NETWORK))
        mimo = self.summary()["balances"]["mimo"]
        self.assertEqual(mimo["estimated"], 50.0)
        self.assertEqual((mimo["uncounted_calls"], mimo["billing_uncertain_calls"]), (1, 1))
        self.assertFalse(mimo["anchor_before_ledger"])

    def test_a_non_cny_account_is_not_subtracted(self):
        self.anchor("or", 20, T0)
        self.ledger.append(_row(T0 + timedelta(minutes=1), 3.0, "or"))
        usd = self.summary()["balances"]["or"]
        self.assertEqual(usd["anchor"], {"balance": 20.0, "currency": "USD", "at": T0.isoformat()})
        self.assertEqual(usd["spent_since_anchor_yuan"], 3.0)
        self.assertEqual((usd["estimated"], usd["low"], usd["warn_yuan"]), (None, False, None))

    def test_low_balance_is_per_account(self):
        self.anchor("env", 100, T0)
        self.anchor("mimo", 12, T0)
        self.ledger.append(_row(T0 + timedelta(minutes=1), 5.0, "mimo"))
        balances = self.summary()["balances"]
        self.assertEqual((balances["env"]["low"], balances["mimo"]["low"]), (False, True))

    def test_an_anchor_older_than_the_window_still_counts_rows_in_between(self):
        self.now = T0 + timedelta(days=3)
        self.anchor("mimo", 50, T0)
        self.ledger.append(_row(T0 + timedelta(days=1), 4.0, "mimo"))  # 窗口之外
        self.ledger.append(_row(T0 + timedelta(days=3) - timedelta(minutes=1), 1.0, "mimo"))
        summary = self.summary(days=1)
        self.assertEqual(summary["balances"]["mimo"]["estimated"], 45.0)
        self.assertEqual(summary["by_account"]["mimo"]["cost_yuan"], 1.0)

    def test_accounts_without_anchor_are_listed_without_estimate(self):
        balances = self.summary()["balances"]
        self.assertEqual(set(balances), {"env", "mimo", "or"})
        for view in balances.values():
            self.assertEqual((view["anchor"], view["anchor_problem"], view["estimated"]), (None, None, None))


class AnchorValidityTest(_Usage):
    def test_invalid_anchors_say_why_and_estimate_nothing(self):
        self.anchor("mimo", 50, T0)
        self.ledger.append(_row(T0 + timedelta(minutes=1), 1.0, "mimo"))
        base = _catalog().get("mimo")
        cases = {
            aa.PROBLEM_RETIRED: dataclasses.replace(base, retired_at=CREATED),
            aa.PROBLEM_ACCOUNT_RECREATED: dataclasses.replace(base, created_at="2026-10-27T00:00:00+00:00"),
            aa.PROBLEM_CURRENCY_MISMATCH: dataclasses.replace(base, currency="USD"),
        }
        for problem, account in cases.items():
            with self.subTest(problem=problem):
                view = self.summary(_catalog(mimo=account))["balances"]["mimo"]
                self.assertEqual(view["anchor_problem"], problem)
                self.assertEqual((view["anchor"], view["estimated"], view["spent_since_anchor_yuan"]),
                                 (None, None, None))
        self.assertIsNone(self.summary()["balances"]["mimo"]["anchor_problem"])

    def test_a_malformed_entry_is_reported_not_guessed(self):
        self.anchor("mimo", 50, T0)
        path = self.root / ANCHORS_FILE
        payload = json.loads(path.read_text(encoding="utf-8"))
        for bad in ({"balance": "50"}, {"balance": float("inf")}, {"at": "2026-10-27T04:00:00"},
                    {"currency": 1}, {"account_created_at": None}):
            with self.subTest(bad=bad):
                entry = {**payload["anchors"]["mimo"], **bad}
                path.write_text(json.dumps({**payload, "anchors": {"mimo": entry}}), encoding="utf-8")
                view = self.summary()["balances"]["mimo"]
                self.assertEqual((view["anchor_problem"], view["estimated"]), (aa.PROBLEM_MALFORMED, None))

    def test_saving_refuses_missing_and_retired_accounts(self):
        retired = dataclasses.replace(_catalog().get("mimo"), retired_at=CREATED)
        with self.assertRaises(aa.AccountAnchorError):
            self.anchor("nope", 1, None)
        with self.assertRaises(aa.AccountAnchorError):
            self.anchor("mimo", 1, None, _catalog(mimo=retired))
        self.assertFalse((self.root / ANCHORS_FILE).exists())

    def test_saving_keeps_the_other_anchors_and_the_migration_mark(self):
        self.root.mkdir(parents=True)
        (self.root / ANCHORS_FILE).write_text(json.dumps({
            "version": 1, "migrated_single_anchor": True,
            "anchors": {"env": {"balance": 7.0, "currency": "CNY", "at": T0.isoformat(),
                                "account_created_at": CREATED}},
        }), encoding="utf-8")
        self.anchor("mimo", 50, T0)
        payload = json.loads((self.root / ANCHORS_FILE).read_text(encoding="utf-8"))
        self.assertTrue(payload["migrated_single_anchor"])
        self.assertEqual(set(payload["anchors"]), {"env", "mimo"})
        self.assertEqual(payload["anchors"]["mimo"]["account_created_at"], CREATED)

    def test_concurrent_saves_for_two_accounts_both_land(self):
        real = aa.read_anchors
        both_read = threading.Barrier(2, timeout=0.3)

        def slow_read(root):
            current = real(root)
            try:
                both_read.wait()  # 没有锁时两边都读到旧内容，后写的覆盖先写的
            except threading.BrokenBarrierError:
                pass
            return current

        with patch.object(aa, "read_anchors", slow_read):
            threads = [threading.Thread(target=self.anchor, args=(a, 10, T0)) for a in ("env", "mimo")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        payload = json.loads((self.root / ANCHORS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(set(payload["anchors"]), {"env", "mimo"})


class AccountBalanceApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = Path(tmp.name) / "settings"
        self.settings.mkdir()
        catalog = _catalog()
        (self.settings / "accounts.json").write_text(json.dumps(catalog.to_file()), encoding="utf-8")
        p = patch.object(content_registry, "SETTINGS_DIR", self.settings)
        p.start()
        self.addCleanup(p.stop)
        self.usage_root = self.plane.usage_ledger.root

    def post(self, body):
        return self.as_operator().post("/api/usage/balance-anchor", json=body)

    def test_anchor_per_account_and_env_by_default(self):
        response = self.post({"balance_yuan": 99.42})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["account"], response.json()["balance_yuan"]), ("env", 99.42))
        response = self.post({"account": "or", "balance": 12.5})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["currency"], "USD")
        self.ledger_row(_row(datetime.now(timezone.utc), 1.0, "mimo"))
        summary = self.as_observer().get("/api/usage/summary").json()
        self.assertFalse(summary["balances_unavailable"])
        self.assertEqual(summary["balances"]["env"]["estimated"], 99.42)
        self.assertEqual(summary["balance"]["estimated_yuan"], 99.42)
        self.assertEqual(summary["balances"]["or"]["anchor"]["balance"], 12.5)
        self.assertIsNone(summary["balances"]["mimo"]["anchor"])

    def ledger_row(self, record):
        self.assertTrue(self.plane.usage_ledger.append(record))

    def test_bad_requests(self):
        retired = dataclasses.replace(_catalog().get("mimo"), retired_at=CREATED)
        (self.settings / "accounts.json").write_text(
            json.dumps(_catalog(mimo=retired).to_file()), encoding="utf-8")
        for body in ({"account": "nope", "balance": 1}, {"account": "mimo", "balance": 1},
                     {"balance": 1, "balance_yuan": 1}, {"account": "env"}, {"account": 3, "balance": 1}):
            with self.subTest(body=body):
                self.assertEqual(self.post(body).status_code, 422)
        self.assertFalse((self.usage_root / ANCHORS_FILE).exists())

    def test_unreadable_anchor_file(self):
        self.usage_root.mkdir(parents=True, exist_ok=True)
        (self.usage_root / ANCHORS_FILE).write_text("not json", encoding="utf-8")
        summary = self.as_observer().get("/api/usage/summary")
        self.assertEqual(summary.status_code, 200)
        body = summary.json()
        self.assertEqual((body["balances_unavailable"], body["balance"], body["balances"]), (True, None, {}))
        response = self.post({"balance": 5})
        self.assertEqual((response.status_code, response.json()["detail"]["category"]), (500, "storage_error"))
        self.assertEqual((self.usage_root / ANCHORS_FILE).read_text(encoding="utf-8"), "not json")


if __name__ == "__main__":
    unittest.main()
