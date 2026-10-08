# tests/test_usage_ledger.py — 用量账本的纯函数部分（COST-1）
#
# 守的线：
#   1. 两种协议的 usage 口径：MiMo/anthropic 的 input_tokens 不含缓存命中；
#      openai 的 prompt_tokens 含 cached_tokens，且 cached 不能大于 prompt；
#   2. 拿不到就是 null 不是 0：形状不对、null、bool、负数、没定价都不算成 0；
#   3. 失败分类是封闭枚举，异常里的任何字符串都出不来；
#   4. 只有 operation=ok 能让连续失败归零，读账按 at 排序不按行序；
#   5. 账本按北京日期分文件，截断的尾行跳过并计数，并发追加不串行；
#   6. 余额锚点的校验与估算。
#
# 运行: python -m unittest discover -s tests -p test_usage_ledger.py
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import httpx2
import openai

from pns.runtime import usage_ledger as ul

KEY = "sk-test-0123456789abcdef"
MIMO_PRO = ul.Prices(input=3.0, cache_write=0.0, cache_read=0.025, output=6.0)
UTC = timezone.utc


def _at(hour, minute=0, day=8):
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def _record(at, *, operation=ul.OPERATION_OK, transport=ul.TRANSPORT_RESPONSE,
            failure=None, usage=ul.Usage(100, 0, 900, 50), prices=MIMO_PRO,
            character="kanade", path=ul.PATH_GENERATION, world="yoake-mae"):
    return ul.LedgerRecord(
        at=at, world_id=world, character_id=character, path=path,
        model="mimo-v2.5-pro", protocol="anthropic", transport=transport,
        failure=failure, operation=operation, usage=usage, prices=prices,
        cost_yuan=ul.estimate_cost(usage, prices),
    )


def _failed(at, failure=ul.FAILURE_PAYMENT):
    return _record(at, operation=ul.OPERATION_FAILED, transport=ul.TRANSPORT_ERROR,
                   failure=failure, usage=None)


def _status_error(code, sdk="anthropic"):
    if sdk == "anthropic":
        response = httpx2.Response(code, request=httpx2.Request("POST", "http://x"))
        return anthropic.APIStatusError(KEY, response=response, body={"error": KEY})
    response = httpx.Response(code, request=httpx.Request("POST", "http://x"))
    return openai.APIStatusError(KEY, response=response, body={"error": KEY})


class AnthropicUsageTest(unittest.TestCase):
    def test_mimo_cache_read_is_on_top_of_input_tokens(self):
        usage = ul.normalize_usage(
            "anthropic",
            SimpleNamespace(input_tokens=200, output_tokens=150,
                            cache_read_input_tokens=9800,
                            cache_creation_input_tokens=None),
        )
        self.assertEqual(usage, ul.Usage(200, 0, 9800, 150))
        # 200×3 + 9800×0.025 + 150×6 = 600 + 245 + 900 = 1745 元/百万
        self.assertAlmostEqual(ul.estimate_cost(usage, MIMO_PRO), 0.001745)

    def test_missing_cache_creation_counts_as_zero(self):
        usage = ul.normalize_usage(
            "anthropic", {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3}
        )
        self.assertEqual(usage, ul.Usage(1, 0, 3, 2))

    def test_null_cache_read_means_no_cache_hit(self):
        # 生产实测（10-09）：MiMo 没命中缓存时这一项是 null。
        usage = ul.normalize_usage(
            "anthropic",
            {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": None},
        )
        self.assertEqual(usage, ul.Usage(1, 0, 0, 2))

    def test_a_missing_cache_read_key_is_still_malformed(self):
        self.assertIsNone(
            ul.normalize_usage("anthropic", {"input_tokens": 1, "output_tokens": 2})
        )

    def test_malformed_values_make_the_whole_usage_unknown(self):
        good = {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3}
        for key in good:
            for bad in (True, -1, "5", 1.0, None):
                if key == "cache_read_input_tokens" and bad is None:
                    continue  # null 是"没命中"，单独测
                with self.subTest(key=key, bad=bad):
                    self.assertIsNone(ul.normalize_usage("anthropic", {**good, key: bad}))
        self.assertIsNone(ul.normalize_usage("anthropic", None))
        self.assertIsNone(ul.normalize_usage("anthropic", {"output_tokens": 2}))


class OpenAIUsageTest(unittest.TestCase):
    def test_cached_tokens_are_subtracted_not_double_counted(self):
        usage = ul.normalize_usage(
            "openai",
            {"prompt_tokens": 10000, "completion_tokens": 40,
             "prompt_tokens_details": {"cached_tokens": 9800}},
        )
        self.assertEqual(usage, ul.Usage(200, 0, 9800, 40))

    def test_cached_larger_than_prompt_is_rejected(self):
        usage = ul.normalize_usage(
            "openai",
            {"prompt_tokens": 100, "completion_tokens": 10,
             "prompt_tokens_details": {"cached_tokens": 200}},
        )
        self.assertIsNone(usage)

    def test_missing_details_means_no_cache(self):
        self.assertEqual(
            ul.normalize_usage("openai", {"prompt_tokens": 7, "completion_tokens": 3}),
            ul.Usage(7, 0, 0, 3),
        )
        self.assertEqual(
            ul.normalize_usage("openai", {"prompt_tokens": 7, "completion_tokens": 3,
                                          "prompt_tokens_details": None}),
            ul.Usage(7, 0, 0, 3),
        )

    def test_bool_cached_tokens_is_malformed(self):
        self.assertIsNone(
            ul.normalize_usage("openai", {"prompt_tokens": 7, "completion_tokens": 3,
                                          "prompt_tokens_details": {"cached_tokens": True}})
        )


class PriceTableTest(unittest.TestCase):
    def test_parses_and_freezes(self):
        table = ul.parse_price_table(
            {"mimo-v2.5-pro": {"input": 3, "cache_write": 0, "cache_read": 0.025, "output": 6}}
        )
        self.assertEqual(table["mimo-v2.5-pro"], MIMO_PRO)
        with self.assertRaises(TypeError):
            table["x"] = MIMO_PRO

    def test_none_is_empty(self):
        self.assertEqual(dict(ul.parse_price_table(None)), {})

    def test_rejects_bad_shapes(self):
        full = {"input": 1, "cache_write": 0, "cache_read": 0.1, "output": 2}
        for bad in (
            [],
            {"m": []},
            {"m": {k: v for k, v in full.items() if k != "output"}},
            {"m": {**full, "extra": 1}},
            {"m": {**full, "input": -1}},
            {"m": {**full, "input": True}},
            {"m": {**full, "input": "3"}},
            {"m": {**full, "input": float("nan")}},
            {"m": {**full, "input": float("inf")}},
            {"": full},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ul.parse_price_table(bad)

    def test_unpriced_model_costs_none_not_zero(self):
        self.assertIsNone(ul.estimate_cost(ul.Usage(1, 0, 0, 1), None))
        self.assertIsNone(ul.estimate_cost(None, MIMO_PRO))


class ClassifyFailureTest(unittest.TestCase):
    def test_status_codes_map_to_fixed_categories(self):
        cases = {
            401: ul.FAILURE_AUTH, 403: ul.FAILURE_AUTH, 402: ul.FAILURE_PAYMENT,
            429: ul.FAILURE_RATE_LIMITED, 400: ul.FAILURE_BAD_REQUEST,
            500: ul.FAILURE_SERVER, 503: ul.FAILURE_SERVER, 418: ul.FAILURE_UNKNOWN,
        }
        for sdk in ("anthropic", "openai"):
            for code, expected in cases.items():
                with self.subTest(sdk=sdk, code=code):
                    self.assertEqual(ul.classify_failure(_status_error(code, sdk)), expected)

    def test_connection_and_timeout_are_network(self):
        request = httpx2.Request("POST", "http://x")
        self.assertEqual(
            ul.classify_failure(anthropic.APITimeoutError(request=request)), ul.FAILURE_NETWORK
        )
        self.assertEqual(
            ul.classify_failure(
                openai.APIConnectionError(request=httpx.Request("POST", "http://x"))
            ),
            ul.FAILURE_NETWORK,
        )

    def test_non_int_status_code_is_unknown(self):
        error = _status_error(402)
        for bad in (KEY, "402", True, 402.0, None):
            error.status_code = bad
            with self.subTest(bad=bad):
                self.assertEqual(ul.classify_failure(error), ul.FAILURE_UNKNOWN)

    def test_foreign_exceptions_carry_nothing_out(self):
        hostile = type(KEY, (RuntimeError,), {"status_code": 402})(KEY)
        category = ul.classify_failure(hostile)
        self.assertEqual(category, ul.FAILURE_UNKNOWN)
        self.assertIn(category, ul.FAILURES)

    def test_every_result_is_in_the_closed_set(self):
        for error in (_status_error(402), ValueError(KEY), KeyboardInterrupt()):
            self.assertIn(ul.classify_failure(error), ul.FAILURES)


class FailureStreakTest(unittest.TestCase):
    def test_only_operation_ok_resets(self):
        records = [
            _record(_at(1)),
            _failed(_at(2)),
            _record(_at(3), operation=ul.OPERATION_UNUSABLE),
            _record(_at(4), operation=ul.OPERATION_TRUNCATED),
            _failed(_at(5)),
        ]
        streak = ul.failure_streak(records)
        self.assertEqual(streak["consecutive"], 4)
        self.assertEqual(streak["since"], _at(2).isoformat())
        self.assertEqual(streak["last_failure"], ul.FAILURE_PAYMENT)

    def test_unusable_last_reports_operation(self):
        streak = ul.failure_streak([_record(_at(1), operation=ul.OPERATION_UNUSABLE)])
        self.assertEqual(streak["last_failure"], ul.OPERATION_UNUSABLE)

    def test_ok_after_failures_clears(self):
        streak = ul.failure_streak([_failed(_at(1)), _record(_at(2))])
        self.assertEqual(streak, {"consecutive": 0, "since": None, "last_failure": None})


class LedgerFileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "usage"
        self.ledger = ul.UsageLedger(self.root, clock=lambda: _at(14))

    def tearDown(self):
        self._tmp.cleanup()

    def test_files_split_on_beijing_midnight(self):
        self.ledger.append(_record(datetime(2026, 10, 8, 15, 59, tzinfo=UTC)))
        self.ledger.append(_record(datetime(2026, 10, 8, 16, 1, tzinfo=UTC)))
        self.assertEqual(
            sorted(p.name for p in self.root.glob("*.jsonl")),
            ["2026-10-08.jsonl", "2026-10-09.jsonl"],
        )

    def test_round_trip_keeps_only_listed_fields(self):
        record = _record(_at(3))
        self.ledger.append(record)
        line = json.loads((self.root / "2026-10-08.jsonl").read_text().strip())
        self.assertEqual(
            set(line),
            {"at", "world_id", "character_id", "path", "model", "protocol", "transport",
             "failure", "operation", "fresh_input", "cache_write", "cache_read", "output",
             "prices", "cost_yuan"},
        )
        records, corrupt = self.ledger.read()
        self.assertEqual((records, corrupt), ([record], 0))

    def test_truncated_tail_line_is_skipped_and_counted(self):
        self.ledger.append(_record(_at(1)))
        self.ledger.append(_record(_at(2)))
        path = self.root / "2026-10-08.jsonl"
        path.write_text(path.read_text() + '{"at": "2026-10-08T03:00:00+00:00", "wor')
        records, corrupt = self.ledger.read()
        self.assertEqual(len(records), 2)
        self.assertEqual(corrupt, 1)

    def test_rows_with_foreign_values_are_corrupt_not_trusted(self):
        self.ledger.append(_record(_at(1)))
        path = self.root / "2026-10-08.jsonl"
        row = json.loads(path.read_text())
        bad_rows = [
            {**row, "failure": KEY},
            {**row, "operation": "great"},
            {**row, "at": "2026-10-08T01:00:00"},
            {**row, "fresh_input": True},
            {**row, "cost_yuan": "1"},
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in bad_rows))
        records, corrupt = self.ledger.read()
        self.assertEqual((records, corrupt), ([], len(bad_rows)))

    def test_read_sorts_by_at_not_by_line_order(self):
        # 两次并发调用：失败的那次先写进文件，但成功那次的 at 更早。
        self.ledger.append(_failed(_at(5)))
        self.ledger.append(_record(_at(4)))
        records, _ = self.ledger.read()
        self.assertEqual([r.at for r in records], [_at(4), _at(5)])
        # 按行序会把 ok 当成最后一行而归零；按 at 才知道失败在后面。
        self.assertEqual(ul.failure_streak(records)["consecutive"], 1)

    def test_concurrent_appends_produce_whole_lines(self):
        def worker(n):
            for i in range(50):
                self.ledger.append(_record(_at(1, minute=i % 60), character=f"c{n}"))

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        records, corrupt = self.ledger.read()
        self.assertEqual((len(records), corrupt), (400, 0))

    def test_unwritable_root_counts_failure_and_does_not_raise(self):
        blocker = Path(self._tmp.name) / "blocked"
        blocker.write_text("not a directory")
        ledger = ul.UsageLedger(blocker / "usage")
        import contextlib
        import io

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(ledger.append(_record(_at(1))))
        self.assertEqual(ledger.write_failures, 1)
        self.assertEqual(err.getvalue().strip(), ul.LEDGER_WRITE_FAILED)


class AnchorTest(unittest.TestCase):
    NOW = _at(14)

    def test_rejects_non_finite_and_out_of_range(self):
        for bad in (float("nan"), float("inf"), -float("inf"), True, "100",
                    ul.ANCHOR_MAX_YUAN + 1, ul.ANCHOR_MIN_YUAN - 1):
            with self.subTest(bad=bad), self.assertRaises(ul.AnchorError):
                ul.validate_anchor(bad, None, self.NOW)

    def test_rejects_naive_and_future_time(self):
        with self.assertRaises(ul.AnchorError):
            ul.validate_anchor(100, datetime(2026, 10, 8, 1), self.NOW)
        with self.assertRaises(ul.AnchorError):
            ul.validate_anchor(100, datetime(2099, 1, 1, tzinfo=UTC), self.NOW)
        # 五分钟以内的钟差可以接受
        ul.validate_anchor(100, self.NOW + timedelta(minutes=4), self.NOW)

    def test_defaults_to_now(self):
        self.assertEqual(ul.validate_anchor(99.42, None, self.NOW).at, self.NOW)

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            anchor = ul.BalanceAnchor(99.42, self.NOW)
            ul.save_anchor(Path(tmp), anchor)
            self.assertEqual(ul.load_anchor(Path(tmp)), anchor)
            self.assertFalse(list(Path(tmp).glob(".*.tmp")))

    def test_estimate_only_counts_calls_after_anchor(self):
        records = [_record(_at(1)), _record(_at(3)), _failed(_at(4), ul.FAILURE_NETWORK),
                   _record(_at(5), prices=None)]
        anchor = ul.BalanceAnchor(99.42, _at(2))
        estimate = ul.balance_estimate(records, anchor, earliest=_at(1), warn_yuan=10)
        per_call = ul.estimate_cost(ul.Usage(100, 0, 900, 50), MIMO_PRO)
        self.assertAlmostEqual(estimate["spent_since_anchor_yuan"], per_call)
        self.assertAlmostEqual(estimate["estimated_yuan"], 99.42 - per_call)
        self.assertEqual(estimate["uncounted_calls"], 2)
        self.assertEqual(estimate["billing_uncertain_calls"], 1)
        self.assertFalse(estimate["anchor_before_ledger"])
        self.assertFalse(estimate["low"])

    def test_anchor_before_ledger_is_flagged(self):
        estimate = ul.balance_estimate(
            [_record(_at(5))], ul.BalanceAnchor(5, _at(1)), earliest=_at(5), warn_yuan=10
        )
        self.assertTrue(estimate["anchor_before_ledger"])
        self.assertTrue(estimate["low"])


class SummaryTest(unittest.TestCase):
    def test_buckets_and_counters(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ul.UsageLedger(Path(tmp), clock=lambda: _at(14))
            ledger.append(_record(_at(1), character="kanade"))
            ledger.append(_record(_at(2), character="mafuyu", path=ul.PATH_JUDGE))
            ledger.append(_record(_at(3), usage=None))  # 有响应、没 usage
            ledger.append(_failed(_at(4), ul.FAILURE_SERVER))
            ledger.append(_record(_at(1, day=1)))  # 窗口之外
            summary = ul.summarize(ledger, days=7, anchor=None, alert_threshold=1)
        total = summary["total"]
        self.assertEqual(total["calls"], 4)
        self.assertEqual(total["no_usage"], 1)
        self.assertEqual(total["failed"], 1)
        self.assertEqual(total["billing_uncertain"], 1)
        self.assertAlmostEqual(total["cache_hit_rate"], 0.9)
        self.assertEqual(set(summary["by_character"]), {"kanade", "mafuyu"})
        self.assertEqual(set(summary["by_path"]), {"generation", "judge"})
        self.assertTrue(summary["failure_streak"]["alert"])
        self.assertIsNone(summary["balance"])
        self.assertNotIn(KEY, json.dumps(summary))


class EdgeSemanticsTest(unittest.TestCase):
    """ena 实现审 F2/F3 与未来记录、读回一致性。"""

    def test_anchor_excludes_a_call_ending_at_the_same_instant(self):
        records = [_record(_at(3)), _record(_at(4))]
        estimate = ul.balance_estimate(
            records, ul.BalanceAnchor(100, _at(3)), earliest=_at(3), warn_yuan=10
        )
        per_call = ul.estimate_cost(ul.Usage(100, 0, 900, 50), MIMO_PRO)
        self.assertAlmostEqual(estimate["spent_since_anchor_yuan"], per_call)

    def test_ties_do_not_depend_on_line_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = []
            for order in ((_failed(_at(5)), _record(_at(5))), (_record(_at(5)), _failed(_at(5)))):
                root = Path(tmp) / str(len(results))
                ledger = ul.UsageLedger(root)
                for record in order:
                    ledger.append(record)
                records, _ = ledger.read()
                results.append(ul.failure_streak(records))
        self.assertEqual(results[0], results[1])
        # 平局偏保守：跟失败同时结束的成功不清掉这次失败。
        self.assertEqual(results[0]["consecutive"], 1)

    def test_future_rows_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ul.UsageLedger(Path(tmp), clock=lambda: _at(14))
            ledger.append(_record(_at(13)))
            ledger.append(_failed(_at(15)))
            summary = ul.summarize(ledger, days=1, anchor=None)
        self.assertEqual(summary["total"]["calls"], 1)
        self.assertEqual(summary["failure_streak"]["consecutive"], 0)

    def test_inconsistent_rows_are_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ul.UsageLedger(Path(tmp))
            ledger.append(_record(_at(1)))
            path = Path(tmp) / "2026-10-08.jsonl"
            row = json.loads(path.read_text())
            bad = [
                {**row, "failure": "payment"},  # 有响应却有失败分类
                {**row, "transport": "error", "failure": None, "operation": "failed"},
                {**row, "transport": "error", "failure": "payment", "operation": "ok"},
                {**row, "fresh_input": None, "cache_write": None, "cache_read": None,
                 "output": None},  # 没有用量却有花费
            ]
            path.write_text("".join(json.dumps(r) + "\n" for r in bad))
            records, corrupt = ledger.read()
        self.assertEqual((records, corrupt), ([], len(bad)))


class BoundedReadTest(unittest.TestCase):
    """汇总只读需要的那几天，但连续失败段和锚点不能因此读错。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ledger = ul.UsageLedger(self.root, clock=lambda: _at(14, day=20))

    def tearDown(self):
        self._tmp.cleanup()

    def _poison(self, day):
        with open(self.root / f"2026-10-{day:02d}.jsonl", "a") as fh:
            fh.write("{broken\n")

    def test_old_files_are_not_opened_when_today_is_healthy(self):
        self.ledger.append(_record(_at(1, day=3)))
        self._poison(3)
        self.ledger.append(_record(_at(1, day=20)))
        summary = ul.summarize(self.ledger, days=1, anchor=None)
        self.assertEqual(summary["corrupt_lines"], 0)
        self.assertEqual(summary["total"]["calls"], 1)

    def test_a_streak_older_than_the_window_is_still_found(self):
        self.ledger.append(_record(_at(1, day=10)))
        for day in range(11, 21):
            self.ledger.append(_failed(_at(2, day=day)))
        streak = ul.summarize(self.ledger, days=1, anchor=None)["failure_streak"]
        self.assertEqual(streak["consecutive"], 10)
        self.assertEqual(streak["since"], _at(2, day=11).isoformat())

    def test_anchor_older_than_the_window_still_counts_its_spending(self):
        self.ledger.append(_record(_at(1, day=5)))
        self.ledger.append(_record(_at(3, day=12)))
        self.ledger.append(_record(_at(1, day=20)))
        anchor = ul.BalanceAnchor(10.0, _at(2, day=5))
        balance = ul.summarize(self.ledger, days=1, anchor=anchor)["balance"]
        per_call = ul.estimate_cost(ul.Usage(100, 0, 900, 50), MIMO_PRO)
        self.assertAlmostEqual(balance["spent_since_anchor_yuan"], 2 * per_call)
        self.assertFalse(balance["anchor_before_ledger"])

    def test_anchor_before_the_first_row_is_flagged(self):
        self.ledger.append(_record(_at(5, day=12)))
        anchor = ul.BalanceAnchor(10.0, _at(1, day=12))
        balance = ul.summarize(self.ledger, days=1, anchor=anchor)["balance"]
        self.assertTrue(balance["anchor_before_ledger"])


if __name__ == "__main__":
    unittest.main()
