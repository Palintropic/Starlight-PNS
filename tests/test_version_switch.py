# tests/test_version_switch.py — 最近一次旧版本 → 新版本切换（CFG-1 §9）
#
# 守的线：
#   1. 第一次升级上来、记录读不出来：这次启动就是切换；
#   2. 之后重启、只有新代码写过账：切换时刻不动；
#   3. 账本最后一行没有 account 且晚于上次切换（回滚期间旧代码写的）：这次启动
#      是新的切换；比上次切换早的旧行不算；
#   4. 找最后一行时跳过坏行和只有坏行的那天；
#   5. 汇总里早于切换的锚点标出来，晚于的不标。
#
# 运行: python -m unittest tests.test_version_switch -v
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pns.runtime import usage_ledger as ul
from pns.runtime import version_switch as vs
from tests.test_account_balance import T0, _catalog, _row
from pns.runtime import account_anchors as aa


class VersionSwitchTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "usage"
        self.ledger = ul.UsageLedger(self.root, clock=lambda: T0)

    def start(self, at):
        return vs.note_startup(self.ledger, at)

    def test_first_start_is_a_switch_and_restarts_are_not(self):
        self.ledger.append(_row(T0 - timedelta(days=2), 1.0))  # 升级之前旧代码写的
        first = T0
        self.assertEqual(self.start(first), first)
        self.assertEqual(vs.read_switch(self.root), first)
        # 重启：还没写过新行；旧行早于切换，不算。
        self.assertEqual(self.start(first + timedelta(hours=1)), first)
        self.ledger.append(_row(first + timedelta(hours=2), 1.0, "env"))
        self.assertEqual(self.start(first + timedelta(hours=3)), first)

    def test_old_rows_after_the_switch_mean_a_rollback_happened(self):
        self.start(T0)
        self.ledger.append(_row(T0 + timedelta(hours=1), 1.0, "env"))
        self.ledger.append(_row(T0 + timedelta(hours=2), 1.0))  # 回滚期间旧代码
        back = T0 + timedelta(hours=3)
        self.assertEqual(self.start(back), back)
        self.assertEqual(vs.read_switch(self.root), back)

    def test_an_unreadable_record_counts_as_a_switch(self):
        self.root.mkdir(parents=True)
        for body in ("not json", '{"version": 2, "switched_at": "2026-10-27T04:00:00+00:00"}',
                     '{"version": 1, "switched_at": "2026-10-27T04:00:00"}'):
            with self.subTest(body=body):
                (self.root / vs.SWITCH_FILE).write_text(body, encoding="utf-8")
                self.assertIsNone(vs.read_switch(self.root))
                self.assertEqual(self.start(T0), T0)

    def test_latest_skips_corrupt_lines_and_days(self):
        self.ledger.append(_row(T0, 1.0))
        self.ledger.append(_row(T0 + timedelta(minutes=1), 1.0, "env"))
        today = self.root / f"{(T0 + timedelta(minutes=1)).astimezone(ul.BEIJING).date()}.jsonl"
        with open(today, "a", encoding="utf-8") as fh:
            fh.write("garbage\n")
        later = self.root / f"{(T0 + timedelta(days=1)).astimezone(ul.BEIJING).date()}.jsonl"
        later.write_text("garbage\n", encoding="utf-8")
        self.assertEqual(self.ledger.latest().account, "env")
        self.assertIsNone(ul.UsageLedger(self.root / "none").latest())

    def test_summary_flags_anchors_older_than_the_switch(self):
        now = T0 + timedelta(hours=5)
        ledger = ul.UsageLedger(self.root, clock=lambda: now)
        catalog = _catalog()
        aa.save_account_anchor(self.root, catalog.get("env"), 100, T0, now)
        aa.save_account_anchor(self.root, catalog.get("mimo"), 50, T0 + timedelta(hours=2), now)
        switched = T0 + timedelta(hours=1)
        summary = ul.summarize(
            ledger, days=1, accounts=aa.account_views(self.root, catalog), switched_at=switched,
        )
        balances = summary["balances"]
        self.assertEqual(
            (balances["env"]["anchor_before_switch"], balances["mimo"]["anchor_before_switch"]),
            (True, False),
        )
        self.assertIsNone(balances["or"]["anchor_before_switch"])  # 没锚点
        unflagged = ul.summarize(ledger, days=1, accounts=aa.account_views(self.root, catalog))
        self.assertFalse(unflagged["balances"]["env"]["anchor_before_switch"])


if __name__ == "__main__":
    unittest.main()
