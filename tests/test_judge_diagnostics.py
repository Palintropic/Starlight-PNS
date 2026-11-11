"""An unusable judge response must say why, without showing what the provider sent."""
import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from pns.logic import router

CANARY = "PROVIDER_TEXT_CANARY"


def complete_dimensions(*missing):
    return {
        key: {"score": 1, "reason": CANARY}
        for key in router.DIMENSION_KEYS
        if key not in missing
    }


def run_judge(raw):
    out = io.StringIO()
    with mock.patch.object(router, "_call", return_value=raw), contextlib.redirect_stdout(out):
        result = router.judge(object(), "ena", "台词", 1)
    return result, out.getvalue()


class UnusableCategoryTests(unittest.TestCase):
    def assert_category(self, raw, category):
        result, printed = run_judge(raw)
        self.assertFalse(result["dimensions_complete"])
        self.assertIn(f"category={category}", printed)
        self.assertNotIn(CANARY, printed)
        return printed

    def test_empty_response(self):
        self.assert_category("", "empty")

    def test_json_cut_off_before_it_closes(self):
        self.assert_category('{"drift_score": 1, "reason": "' + CANARY, "json_unclosed")

    def test_closed_json_that_still_does_not_parse(self):
        self.assert_category('{"reason": ' + CANARY + "}", "json_malformed")

    def test_parser_error_and_position_are_reported(self):
        # The trailing comma keeps it broken after the inner-quote repair, so the
        # error reported must still be the first one, at the original position.
        raw = '{"reason": "他说"' + CANARY + '"", "drift_score": 1,}'
        printed = self.assert_category(raw, "json_malformed")
        self.assertIn("error=Expecting ',' delimiter", printed)
        self.assertIn(f"pos={raw.index(CANARY)}", printed)
        self.assertIn("after_quote=1", printed)

    def test_failure_not_right_after_a_quote_says_so(self):
        printed = self.assert_category('{"drift_score": 1,}', "json_malformed")
        self.assertIn("after_quote=0", printed)

    def test_prose_with_no_json(self):
        self.assert_category(CANARY + " 这句没问题", "no_json")

    def test_json_that_is_not_an_object(self):
        self.assert_category(json.dumps([CANARY]), "not_object")

    def test_missing_dimensions_are_named(self):
        gone = router.DIMENSION_KEYS[:2]
        printed = self.assert_category(
            json.dumps({"drift_score": 1, "dimensions": complete_dimensions(*gone)}),
            "dimensions_incomplete",
        )
        self.assertIn("missing=" + ",".join(gone), printed)

    def test_response_length_is_reported(self):
        raw = '{"reason": "' + CANARY
        self.assertIn(f"chars={len(raw)}", run_judge(raw)[1])

    def test_usable_response_prints_nothing(self):
        result, printed = run_judge(json.dumps({"drift_score": 1, "dimensions": complete_dimensions()}))
        self.assertTrue(result["dimensions_complete"])
        self.assertEqual(printed, "")


def judge_text_with_bare_quotes(*, score=3, quote_in="task_compliance"):
    """A full judge response whose `quote_in` reason quotes a line with bare `"`."""
    parts = []
    for key in router.DIMENSION_KEYS:
        reason = '没有照"只输出下一句话"做' if key == quote_in else "没问题"
        parts.append(f'"{key}": {{"score": {score}, "reason": "{reason}"}}')
    return (
        '{"character": "ena", "drift_score": ' + str(score) + ', '
        '"dimensions": {' + ", ".join(parts) + '}, '
        '"confidence": 0.8, "drift_type": "无", "reason": "他说"好"。", '
        '"needs_human_review": false, "correction": null}'
    )


class InnerQuoteRepairTests(unittest.TestCase):
    def test_production_shape_is_broken_without_the_repair(self):
        with self.assertRaises(json.JSONDecodeError) as caught:
            json.loads(judge_text_with_bare_quotes())
        exc = caught.exception
        self.assertEqual(exc.msg, "Expecting ',' delimiter")
        self.assertEqual(exc.doc[exc.pos - 1], '"')

    def test_bare_quotes_in_reasons_are_repaired_and_scored(self):
        result, printed = run_judge(judge_text_with_bare_quotes(score=6))
        self.assertTrue(result["dimensions_complete"])
        self.assertEqual(result["drift_score"], 6)
        self.assertTrue(result["is_ooc"])
        self.assertEqual(
            result["dimensions"]["task_compliance"]["reason"], '没有照"只输出下一句话"做')
        self.assertEqual(result["reason"], '他说"好"。')
        self.assertEqual(printed, "[Router] judge repaired category=inner_quotes\n")

    def test_repair_message_carries_no_provider_text(self):
        raw = judge_text_with_bare_quotes().replace("没问题", CANARY)
        _, printed = run_judge(raw)
        self.assertIn("category=inner_quotes", printed)
        self.assertNotIn(CANARY, printed)

    def test_escaped_quotes_are_left_alone(self):
        self.assertIsNone(router._escape_inner_quotes('{"reason": "他说\\"好\\"", "a": [1, "x"]}'))

    def test_valid_json_is_never_rewritten(self):
        raw = json.dumps({"reason": '他说"好"', "n": {"k": ["a", "b"]}}, ensure_ascii=False)
        self.assertIsNone(router._escape_inner_quotes(raw))

    def test_quote_before_a_comma_inside_a_reason_stays_unusable(self):
        # Ambiguous: the repair takes `"好",` as the end of the string and the
        # rest no longer parses. It must stay unusable rather than guess.
        raw = '{"drift_score": 1, "reason": "他说"好", 然后走了"}'
        result, printed = run_judge(raw)
        self.assertFalse(result["dimensions_complete"])
        self.assertEqual(result["drift_type"], "解析失败")
        self.assertIn("category=json_malformed", printed)
        self.assertNotIn("repaired", printed)

    def assert_repair_refused(self, raw):
        result, printed = run_judge(raw)
        self.assertFalse(result["dimensions_complete"])
        self.assertEqual(result["drift_type"], "解析失败")
        self.assertTrue(result["needs_human_review"])
        self.assertIn("category=json_malformed", printed)
        self.assertNotIn("repaired", printed)

    def test_quoted_json_in_a_reason_cannot_overwrite_a_score(self):
        # Review finding on #97: a reason quoting `"score":1` used to re-parse
        # with a duplicate key, and the later 1 silently replaced the real 8.
        raw = judge_text_with_bare_quotes(score=1).replace(
            '"unsupported_invention": {"score": 1, "reason": "没问题"}',
            '"unsupported_invention": {"score": 8, '
            '"reason": "包含"台词", "score":1, "reason":"后文""}',
        )
        self.assertIn('"score": 8', raw)
        self.assert_repair_refused(raw)

    def test_quoted_json_cannot_add_fields_outside_the_format(self):
        raw = judge_text_with_bare_quotes().replace(
            '"reason": "他说"好"。"',
            '"reason": "他说"好", "accepted": true, "x":"。"',
        )
        self.assert_repair_refused(raw)

    def test_quoted_json_cannot_add_a_dimension_field(self):
        raw = judge_text_with_bare_quotes().replace(
            '"timeline_boundary": {"score": 3, "reason": "没问题"}',
            '"timeline_boundary": {"score": 3, "reason": "说"好", "weight":"0"}',
        )
        self.assert_repair_refused(raw)

    def test_double_quoted_scores_cannot_fall_back_to_zero(self):
        # Second review finding on #97: `""8""` was repaired into the string
        # `"8"` with quotes, which _bounded_score turned into 0 and accepted.
        raw = judge_text_with_bare_quotes(score=0).replace(
            '"drift_score": 0', '"drift_score": ""8""'
        ).replace(
            '"unsupported_invention": {"score": 0,',
            '"unsupported_invention": {"score": ""8"",',
        )
        self.assertIn('""8""', raw)
        self.assert_repair_refused(raw)

    def test_a_quoted_boolean_cannot_clear_human_review(self):
        raw = judge_text_with_bare_quotes().replace(
            '"needs_human_review": false', '"needs_human_review": ""true""'
        )
        self.assert_repair_refused(raw)

    def test_prompt_asks_for_corner_brackets(self):
        self.assertIn("一律用「」", router.ROUTER_SYSTEM)
        self.assertNotIn("“", router.ROUTER_SYSTEM)


class TruncationTests(unittest.TestCase):
    def call(self, api_format, response):
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response)),
            messages=SimpleNamespace(create=lambda **_: response),
        )
        settings = SimpleNamespace(api_format=api_format, base_url="", judge_thinking="off")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            router._call(client, "m", "s", "u", settings=settings)
        return out.getvalue()

    @staticmethod
    def openai(reason):
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason=reason, message=SimpleNamespace(content="{}"))])

    @staticmethod
    def anthropic(reason):
        return SimpleNamespace(stop_reason=reason, content=[SimpleNamespace(type="text", text="{}")])

    def test_openai_length_is_reported(self):
        self.assertIn("truncated", self.call("openai", self.openai("length")))

    def test_openai_stop_is_quiet(self):
        self.assertEqual(self.call("openai", self.openai("stop")), "")

    def test_anthropic_max_tokens_is_reported(self):
        self.assertIn("truncated", self.call("anthropic", self.anthropic("max_tokens")))

    def test_anthropic_end_turn_is_quiet(self):
        self.assertEqual(self.call("anthropic", self.anthropic("end_turn")), "")

    def test_unknown_reason_is_never_printed(self):
        self.assertEqual(self.call("openai", self.openai(CANARY)), "")


if __name__ == "__main__":
    unittest.main()
