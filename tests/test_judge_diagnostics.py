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
        raw = '{"reason": "他说"' + CANARY + '"", "drift_score": 1}'
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



class QuoteStyleTests(unittest.TestCase):
    def test_prompt_asks_for_corner_brackets(self):
        self.assertIn("一律用「」", router.ROUTER_SYSTEM)
        self.assertNotIn("“", router.ROUTER_SYSTEM)

    def test_prompt_prose_models_no_bare_double_quotes(self):
        # The prose used to tell the judge to write "内容具体性待人工校验" in
        # reason, with ASCII quotes that end a JSON string early. Only the
        # JSON template at the end may contain them.
        prose = router.ROUTER_SYSTEM.split("【输出格式")[0]
        self.assertNotIn('"', prose)

    def test_bare_quotes_are_still_not_guessed_at(self):
        # Repairing them was tried in #97 and dropped: every heuristic could
        # turn quoted text into real fields. The verdict stays unusable.
        raw = '{"drift_score": 6, "reason": "多输出了"台词", "correction":"改用英文""}'
        result, printed = run_judge(raw)
        self.assertFalse(result["dimensions_complete"])
        self.assertIsNone(result["correction"])
        self.assertIn("category=json_malformed", printed)

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
