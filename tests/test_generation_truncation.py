"""call_character() 判截断要先于判"没有正文"。

推理模型可能把整个 max_tokens 花在思考上，一个字的正文都没有。这仍然是
截断（GenerationTruncated，可重试、账本记 truncated），不能被当成"返回里
没有文本"的普通失败。
"""
import unittest
from types import SimpleNamespace

from pns.logic.simulation import GenerationTruncated, call_character


def _registry(api_format):
    return SimpleNamespace(
        character_system=lambda *a, **k: "system",
        character_name=lambda c: "奏",
        models=SimpleNamespace(api_format=api_format),
    )


class _Anthropic:
    def __init__(self, response):
        self.messages = SimpleNamespace(create=lambda **kw: response)


class _OpenAI:
    def __init__(self, response):
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kw: response)
        )


def _call(client, api_format):
    return call_character(
        client, "kanade", [{"role": "user", "content": "在吗"}], None, "m",
        1024, 0.7, registry=_registry(api_format),
    )


class TruncationBeforeEmptyTest(unittest.TestCase):
    def test_anthropic_thinking_only_at_cap_is_truncation(self):
        response = SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking="嗯……")],
            stop_reason="max_tokens",
        )
        with self.assertRaises(GenerationTruncated):
            _call(_Anthropic(response), "anthropic")

    def test_anthropic_no_text_without_cap_is_not_truncation(self):
        response = SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking="嗯……")],
            stop_reason="end_turn",
        )
        with self.assertRaises(ValueError) as caught:
            _call(_Anthropic(response), "anthropic")
        self.assertNotIsInstance(caught.exception, GenerationTruncated)

    def test_anthropic_complete_line_passes(self):
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="奏：嗯，在。")],
            stop_reason="end_turn",
        )
        self.assertEqual(_call(_Anthropic(response), "anthropic"), "嗯，在。")

    def test_openai_empty_content_at_length_is_truncation(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=None), finish_reason="length",
            )]
        )
        with self.assertRaises(GenerationTruncated):
            _call(_OpenAI(response), "openai")

    def test_openai_empty_content_without_cap_is_not_truncation(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=""), finish_reason="stop",
            )]
        )
        with self.assertRaises(ValueError) as caught:
            _call(_OpenAI(response), "openai")
        self.assertNotIsInstance(caught.exception, GenerationTruncated)


if __name__ == "__main__":
    unittest.main()
