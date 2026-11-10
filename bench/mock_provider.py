# bench/mock_provider.py — 确定性假 provider，零真实调用跑通整条管线。
#
# 它模拟的不是某个真实模型的质量，而是一条对 benchmark 有意义的下限行为：
# "模型会用上下文里真实出现的信息作答；没有的信息就表示不确定。"
# 于是三种条件的差别（上下文里有没有目标事件）在 mock 下必然显形：
# A/B 的上下文里有目标事件 → 回答引用它（leak 形状）；C 的投影里没有
# → 回答不确定。mock 结果只证明管线通，不证明任何真实效应量。
#
# 触发词 TRUNCATE_TRIGGER 出现在请求里时返回截断形状，用来演练
# GenerationTruncated 的排除/补跑路径（单元测试用）。
from types import SimpleNamespace

TRUNCATE_TRIGGER = "BENCH_MOCK_TRUNCATE"


class MockProvider:
    """anthropic 与 openai 两种形状都实现，run_bench 按协议取用。"""

    def __init__(self, *, marker: str, leak_answer: str, unsure_answer: str):
        self._marker = marker
        self._leak_answer = leak_answer
        self._unsure_answer = unsure_answer
        self.messages = SimpleNamespace(create=self._create_anthropic)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create_openai)
        )

    def _decide(self, text: str):
        if TRUNCATE_TRIGGER in text:
            return "truncate", ""
        if self._marker and self._marker in text:
            return "leak", self._leak_answer
        return "unsure", self._unsure_answer

    @staticmethod
    def _flatten(system, messages) -> str:
        parts = [system or ""]
        for message in messages or []:
            parts.append(str(message.get("content", "")))
        return "\n".join(parts)

    def _create_anthropic(self, *, model, max_tokens, system=None, messages=None, **_):
        text = self._flatten(system, messages)
        kind, answer = self._decide(text)
        if kind == "truncate":
            # 只有 thinking 块、没有正文，且 stop_reason=max_tokens：
            # 与真实截断同形状，call_character 会抛 GenerationTruncated。
            return SimpleNamespace(
                content=[SimpleNamespace(type="thinking", thinking="…")],
                stop_reason="max_tokens",
                usage=SimpleNamespace(
                    input_tokens=max(1, len(text) // 2),
                    output_tokens=0,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=None,
                ),
            )
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=answer)],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=max(1, len(text) // 2),
                output_tokens=max(1, len(answer)),
                cache_creation_input_tokens=0,
                cache_read_input_tokens=None,
            ),
        )

    def _create_openai(self, *, model, max_tokens, messages=None, **_):
        text = self._flatten(None, messages)
        kind, answer = self._decide(text)
        if kind == "truncate":
            choice = SimpleNamespace(
                message=SimpleNamespace(content=""), finish_reason="length"
            )
            return SimpleNamespace(
                choices=[choice],
                usage=SimpleNamespace(
                    prompt_tokens=max(1, len(text) // 2), completion_tokens=0
                ),
            )
        choice = SimpleNamespace(
            message=SimpleNamespace(content=answer), finish_reason="stop"
        )
        return SimpleNamespace(
            choices=[choice],
            usage=SimpleNamespace(
                prompt_tokens=max(1, len(text) // 2),
                completion_tokens=max(1, len(answer)),
            ),
        )
