# pns/interfaces/cache_warmer.py — 提示词缓存续命（COST-2）
#
# provider 的前缀缓存只活几分钟（mimo-v2.5 约 5 分钟，v2.6 实测 8 分钟以上）。一个角色沉默 5–20 分钟
# 之后再开口，它那段 2000–4000 token 的 system 就要按未命中价重新付一遍，而
# 命中价只有未命中价的 1/120。续命就是在沉默期间每隔几分钟发一个"同前缀 +
# 一句话 + max_tokens=1"的请求，让缓存别凉。
#
# 几条硬约束（实施单 kickoff/COST_2_CACHE_WARMING_KICKOFF.md §8–§10）：
#
#   1. **只重放，不渲染。** 续命用的是被捕获的那次真实请求的完整参数（system、
#      model、采样参数……），只改 messages、max_tokens、thinking 三处。它从不
#      自己拼提示词，所以不会去暖一个没人用的前缀。
#   2. **只认能用的真实请求。** 发送时分配单调 seq，operation 判定为可用时才
#      提升为候选，且只接受比现有更大的 seq（晚完成的旧请求不覆盖新请求）。
#   3. **窗口只看真实调用。** 最后一次真实调用之后 W 秒内才续，续命成功不延长
#      窗口——否则续命会续出续命。
#   4. **续命不是世界在说话。** 它只写账本（path = warm）和自己的内存状态；
#      失败不抛出、不进认知的故障、不进账本的连续失败。
#   5. **状态属于一个世界。** 一个 CacheWarmer 只挂在 build_adapters 为这个世界
#      建的那个 MeteredClient 上；研究会话、bench 的 client 没有它。
import copy
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from pns.runtime.usage_ledger import PATH_WARM, SERVING_PATHS

# 续命请求的 user 内容。固定一句：它不进世界，模型的回答（1 个 token）也没人读。
WARM_PROMPT = "（保持连接）"

# 一个 key 连续续命失败这么多次就停手，直到同一个 key 的下一次真实、可用的调用。
DEFAULT_MAX_FAILURES = 3

Key = Tuple[str, str, str, str]  # (path, character_id, model, protocol)


@dataclass(frozen=True)
class WarmPrefix:
    """一次真实请求的完整参数，以及它是第几次发送、什么时候发的。"""

    key: Key
    request: Mapping[str, Any]
    seq: int
    sent_at: float


@dataclass
class _Slot:
    prefix: WarmPrefix
    last_real_at: float
    last_sent_at: float
    failures: int = 0


class CacheWarmer:
    """一个世界的续命状态。

    `capture()` 由 MeteredClient 在真实请求发出时调用，`promote()` 在那次
    operation 判定为可用之后调用；`warm_once()` 由时钟 worker 在一轮干净的
    推进之后调用。三者可能在不同线程里，所以内存状态由一把锁护着；真正的
    provider 发送则由 UsageMeter 的 send lock 跟真实请求串行。
    """

    def __init__(
        self,
        *,
        window_seconds: float,
        interval_seconds: float,
        protocol: str,
        max_failures: int = DEFAULT_MAX_FAILURES,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not window_seconds > 0:
            raise ValueError("window_seconds 必须大于 0；不续命就不要建 CacheWarmer")
        if not interval_seconds > 0:
            raise ValueError("interval_seconds 必须大于 0")
        if max_failures < 1:
            raise ValueError("max_failures 至少是 1")
        self._window = float(window_seconds)
        self._interval = float(interval_seconds)
        self._protocol = protocol
        self._max_failures = max_failures
        self._now = monotonic
        self._lock = threading.Lock()
        self._seq = 0
        self._slots: Dict[Key, _Slot] = {}
        self._client: Any = None
        self._client_factory: Optional[Callable[[], Any]] = None
        self._meter: Any = None

    # ── 接线 ────────────────────────────────────────────────────────────
    def attach(
        self, client: Any, meter: Any, *, client_factory: Optional[Callable[[], Any]] = None
    ) -> None:
        """续命请求要走同一个 meter：同一本账、同一把 send lock。

        `client` 应当是**关掉 SDK 自动重试**的那一份（composition 用
        `with_options(max_retries=0)` 建）：续命的超时必须就是它的总时长上限，
        否则一次超时后 SDK 再试两次，时钟就被拖住了（实现审 P1）。也可以只交
        `client_factory`，第一次续命时才建。
        """
        if (client is None) == (client_factory is None):
            raise ValueError("client 与 client_factory 必须且只能给一个")
        self._client = client
        self._client_factory = client_factory
        self._meter = meter

    def _resolved_client(self) -> Any:
        if self._client is None and self._client_factory is not None:
            self._client = self._client_factory()
        return self._client

    @property
    def window_seconds(self) -> float:
        return self._window

    @property
    def interval_seconds(self) -> float:
        return self._interval

    # ── 捕获与提升 ───────────────────────────────────────────────────────
    def capture(self, path: str, character_id: str, model: str, request: Mapping[str, Any]) -> Optional[WarmPrefix]:
        """真实请求发出的那一刻：记下它的完整参数，分配 seq。不是服务路径就不记。"""
        if path not in SERVING_PATHS:
            return None
        frozen = copy.deepcopy(dict(request))
        with self._lock:
            self._seq += 1
            seq = self._seq
        return WarmPrefix(
            key=(path, character_id, model, self._protocol),
            request=frozen,
            seq=seq,
            sent_at=self._now(),
        )

    def promote(self, prefix: WarmPrefix) -> bool:
        """那次 operation 可用了：它成为这个 key 的续命候选（只接受更新的 seq）。"""
        with self._lock:
            slot = self._slots.get(prefix.key)
            if slot is not None and slot.prefix.seq >= prefix.seq:
                return False
            if slot is None:
                self._slots[prefix.key] = _Slot(
                    prefix=prefix,
                    last_real_at=prefix.sent_at,
                    last_sent_at=prefix.sent_at,
                )
            else:
                slot.prefix = prefix
                slot.last_real_at = max(slot.last_real_at, prefix.sent_at)
                slot.last_sent_at = max(slot.last_sent_at, prefix.sent_at)
                # 同一个 key 的真实、可用调用：解除失败暂停。
                slot.failures = 0
            return True

    # ── 续命 ────────────────────────────────────────────────────────────
    def due(self) -> Optional[WarmPrefix]:
        """此刻最该续的那一个：仍在窗口内、距上一次请求已满 interval、没被暂停。"""
        now = self._now()
        best: Optional[_Slot] = None
        with self._lock:
            for slot in self._slots.values():
                if slot.failures >= self._max_failures:
                    continue
                if now - slot.last_real_at > self._window:
                    continue
                if now - slot.last_sent_at < self._interval:
                    continue
                if best is None or slot.last_sent_at < best.last_sent_at:
                    best = slot
            return best.prefix if best is not None else None

    def warm_request(self, prefix: WarmPrefix) -> Dict[str, Any]:
        """被捕获的请求，只改 messages、max_tokens、thinking 三处。"""
        request = copy.deepcopy(dict(prefix.request))
        if self._protocol == "openai":
            messages = list(request.get("messages") or [])
            system = [m for m in messages[:1] if isinstance(m, Mapping) and m.get("role") == "system"]
            request["messages"] = system + [{"role": "user", "content": WARM_PROMPT}]
        else:
            request["messages"] = [{"role": "user", "content": WARM_PROMPT}]
            # 开/关思考共用同一份缓存（10-09 实测，v2.5-pro 与 v2.6-pro 两个方向都命中），
            # 所以续命关掉思考，只付 1 个输出 token。
            request["thinking"] = {"type": "disabled"}
        request["max_tokens"] = 1
        return request

    def warm_once(self, timeout: float, admit: Callable[[], bool] = lambda: True) -> bool:
        """续一个最该续的 key。返回是否真的发了请求。从不抛出。

        `admit` 在**拿到 send lock 之后、发送之前**再问一次（实现审 P1）：调用方
        先问过一次只是为了不白等锁；真正算数的是锁里这一次。它跟真实生成同一个
        语义 —— 判定通过之后才算"开始"；判定之后才生效的 Stop 拦不住这一戳，
        也拦不住一次已经判定过的真实生成。
        """
        if (self._client is None and self._client_factory is None) or self._meter is None:
            return False
        prefix = self.due()
        if prefix is None:
            return False
        path, character_id, model, _ = prefix.key
        request = self.warm_request(prefix)
        ok = False
        with self._meter.send_lock:
            try:
                if not admit():
                    return False
            except Exception:  # noqa: BLE001 - 问不出来就不续
                return False
            with self._lock:
                slot = self._slots.get(prefix.key)
                if slot is not None:
                    slot.last_sent_at = self._now()
            try:
                with self._meter.operation(PATH_WARM, character_id, model) as op:
                    client = self._resolved_client()
                    if self._protocol == "openai":
                        client.chat.completions.create(**request, timeout=timeout)
                    else:
                        client.messages.create(**request, timeout=timeout)
                    op.settle(True)
                    ok = True
            except Exception:  # noqa: BLE001 - 续命失败只记账、只计数，绝不影响世界
                ok = False
        with self._lock:
            slot = self._slots.get(prefix.key)
            # 只记到它续的那一版：这期间真实请求已经换了前缀的话，失败不算到新前缀头上。
            if slot is not None and slot.prefix.seq == prefix.seq:
                slot.failures = 0 if ok else slot.failures + 1
        return True

    def status(self) -> Dict[str, Any]:
        now = self._now()
        with self._lock:
            return {
                "window_seconds": self._window,
                "interval_seconds": self._interval,
                "keys": len(self._slots),
                "active": sum(
                    1 for s in self._slots.values()
                    if now - s.last_real_at <= self._window and s.failures < self._max_failures
                ),
                "paused": sum(1 for s in self._slots.values() if s.failures >= self._max_failures),
            }


__all__ = ["CacheWarmer", "WarmPrefix", "WARM_PROMPT"]
