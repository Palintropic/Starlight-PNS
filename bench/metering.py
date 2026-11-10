# bench/metering.py — 传输层计量包装。
#
# 为什么不用 pns.interfaces.usage_meter.UsageMeter：它绑世界账本
# （UsageLedger + 世界生命周期），且 pns.interfaces 包的导入会连带拉起
# 服务器应用链，独立脚本环境里根本导不进来（scripts/model1_compare.py
# 因此自带了 Meter）。bench 沿用那个先例：只包 SDK client 的两个生成
# 入口，usage 的归一化与成本估算仍走 pns.runtime.usage_ledger 的
# normalize_usage / estimate_cost，不另写一套口径。
import time
from dataclasses import dataclass, field
from typing import List, Optional

from pns.runtime.usage_ledger import Usage, estimate_cost, normalize_usage


@dataclass
class CallRecord:
    latency_ms: float
    usage: Optional[Usage] = None
    error: Optional[str] = None  # 只记异常类名：异常文本可能带凭据，不进记录


@dataclass
class MeteringClient:
    """包住一个 provider SDK client（anthropic 或 openai 形状）。

    call_character 只碰 client.messages.create 与
    client.chat.completions.create 两个入口，这里就只拦这两个。
    对调用完全透明：响应原样返回，异常原样抛出（先记账再抛）。"""

    inner: object
    protocol: str  # "anthropic" | "openai"，决定 usage 怎么归一化
    calls: List[CallRecord] = field(default_factory=list)

    def __post_init__(self):
        outer = self

        class _Messages:
            def create(self, **kwargs):
                return outer._invoke("messages", kwargs)

        class _Completions:
            def create(self, **kwargs):
                return outer._invoke("chat", kwargs)

        class _Chat:
            completions = _Completions()

        self.messages = _Messages()
        self.chat = _Chat()

    def _invoke(self, kind: str, kwargs):
        if kind == "messages":
            fn = self.inner.messages.create
        else:
            fn = self.inner.chat.completions.create
        started = time.perf_counter()
        try:
            response = fn(**kwargs)
        except BaseException as e:
            self.calls.append(
                CallRecord(
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    error=type(e).__name__,
                )
            )
            raise
        latency_ms = (time.perf_counter() - started) * 1000.0
        usage = normalize_usage(self.protocol, getattr(response, "usage", None))
        self.calls.append(CallRecord(latency_ms=latency_ms, usage=usage))
        return response

    # ── 汇总 ────────────────────────────────────────────────────────
    @property
    def call_count(self) -> int:
        return len(self.calls)

    def token_totals(self) -> dict:
        """按 usage_ledger.Usage 的四分量汇总（fresh_input / cache_write /
        cache_read / output），外加一个含缓存命中的总输入 input_total。
        口径跟账本一致：anthropic 协议的 input_tokens 不含缓存命中，
        相加才是总输入——MODEL-1 踩过的坑，不在这里重踩。"""
        total = {"fresh_input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
        for call in self.calls:
            if call.usage is None:
                continue
            total["fresh_input"] += call.usage.fresh_input
            total["cache_write"] += call.usage.cache_write
            total["cache_read"] += call.usage.cache_read
            total["output"] += call.usage.output
        total["input_total"] = (
            total["fresh_input"] + total["cache_write"] + total["cache_read"]
        )
        return total

    def cost_total(self, prices) -> Optional[float]:
        """prices 是 usage_ledger.Prices；任一调用缺 usage 就返回 None
        （成本"不可用"必须显式，不许按 0 充数）。"""
        if prices is None or not self.calls:
            return None
        total = 0.0
        for call in self.calls:
            cost = estimate_cost(call.usage, prices)
            if cost is None:
                return None
            total += cost
        return total
