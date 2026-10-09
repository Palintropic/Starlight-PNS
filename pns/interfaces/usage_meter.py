# pns/interfaces/usage_meter.py — 把模型调用记进用量账本（COST-1）
#
# 计量分两层，因为两件事只有在各自那一层才看得清：
#
#   * **传输层**：`MeteredClient` 包住 SDK client，只拦 `messages.create` 和
#     `chat.completions.create`——这是唯一真正碰到 provider 的地方。它看得见
#     "拿到响应没有、usage 是多少、抛了什么异常"。不能只在 `router.judge()`
#     外面包一层：judge 会把所有异常吞成 drift_type=error 的固定结果，外层永远
#     看不到失败。
#   * **操作层**：`UsageMeter.operation()`，由 build_adapters 里的生成、判分两个
#     闭包开启。它知道这次调用**最后有没有产出能用的东西**——HTTP 200 但没有
#     文本、判分解析失败，世界一样是哑的。
#
# 两层合成一行账：操作层开一个上下文（ContextVar，线程池里每次调用各自一份），
# 传输层往里填，操作层结束时写出。没有上下文的 SDK 调用照常放行、不记账。
#
# 这一层对调用结果是透明的：同一个响应原样返回，同一个异常原样抛出（不包装、
# 不改消息）。从异常里只拿 `classify_failure` 给的枚举值。
#
# COST-2：这里也是续命（cache_warmer.py）唯一的取样点和发送闸门。发送那一刻
# 捕获请求的完整参数，operation 判定为可用之后才交给续命；同一个世界的真实
# 请求与续命请求在 `send_lock` 里串行，不靠"只有一个线程会调"的约定（没有续命
# 的 meter 不加锁）。
import threading
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, List, Mapping, Optional

from pns.runtime.usage_ledger import (
    FAILURE_UNKNOWN,
    OPERATION_FAILED,
    OPERATION_OK,
    OPERATION_UNUSABLE,
    TRANSPORT_ERROR,
    TRANSPORT_RESPONSE,
    LedgerRecord,
    Prices,
    Usage,
    UsageLedger,
    classify_failure,
    estimate_cost,
    normalize_usage,
)


@dataclass
class _Call:
    transport: str
    failure: Optional[str]
    usage: Optional[Usage]


@dataclass
class Operation:
    """一次生成或一次判分。`outcome` 由开启它的人在结束前填。"""

    world_id: str
    character_id: str
    path: str
    model: str
    protocol: str
    calls: List[_Call] = field(default_factory=list)
    outcome: Optional[str] = None
    # 这次 operation 最后一次真实发出的请求（COST-2）。只有 outcome 是 ok 才会
    # 被交给续命。
    prefix: Optional[Any] = None

    @property
    def transport_failed(self) -> bool:
        return any(call.transport == TRANSPORT_ERROR for call in self.calls)

    def settle(self, ok: bool) -> None:
        """没拿到能用的结果时，区分"provider 没通"和"通了但结果不能用"。"""
        if ok:
            self.outcome = OPERATION_OK
        elif self.transport_failed:
            self.outcome = OPERATION_FAILED
        else:
            self.outcome = OPERATION_UNUSABLE


_CURRENT: ContextVar[Optional[Operation]] = ContextVar("pns_usage_operation", default=None)


class UsageMeter:
    """一个世界的计量器：知道往哪本账写、按哪张价目表算、用哪个协议读 usage。"""

    def __init__(
        self,
        ledger: UsageLedger,
        *,
        world_id: str,
        protocol: str,
        prices: Mapping[str, Prices],
        warmer: Optional[Any] = None,
    ) -> None:
        self._ledger = ledger
        self._world_id = world_id
        self._protocol = protocol
        self._prices = prices
        self._warmer = warmer
        # 只有挂了续命的世界才串行发送：续命请求与真实请求不交错。没有续命的
        # client（研究会话、bench）对并发保持完全透明，跟 COST-1 时一样。
        self._send_lock: Any = threading.Lock() if warmer is not None else nullcontext()

    @property
    def warmer(self) -> Optional[Any]:
        return self._warmer

    @property
    def send_lock(self) -> Any:
        return self._send_lock

    @property
    def protocol(self) -> str:
        return self._protocol

    @contextmanager
    def operation(self, path: str, character_id: str, model: str) -> Iterator[Operation]:
        op = Operation(
            world_id=self._world_id,
            character_id=character_id,
            path=path,
            model=model,
            protocol=self._protocol,
        )
        token = _CURRENT.set(op)
        try:
            yield op
        except BaseException:
            if op.outcome is None:
                op.settle(False)
            raise
        finally:
            _CURRENT.reset(token)
            self._write(op)
            if (
                self._warmer is not None
                and op.prefix is not None
                and op.outcome == OPERATION_OK
            ):
                try:
                    self._warmer.promote(op.prefix)
                except Exception:  # 续命的簿记不许影响调用结果
                    pass

    def capture(self, request: Mapping[str, Any]) -> None:
        """一次真实请求就要发出：交给续命记下它的完整参数（没有续命或上下文就什么都不做）。"""
        op = _CURRENT.get()
        if op is None or self._warmer is None:
            return
        try:
            prefix = self._warmer.capture(
                op.path, op.character_id, str(request.get("model", op.model)), request
            )
        except Exception:
            return
        if prefix is not None:
            op.prefix = prefix

    def _write(self, op: Operation) -> None:
        if not op.calls:
            # 根本没发出请求（比如提示词渲染先失败了）：没有调用就没有账。
            return
        outcome = op.outcome if op.outcome is not None else OPERATION_UNUSABLE
        prices = self._prices.get(op.model)
        at = self._ledger.now()
        for call in op.calls:
            self._ledger.append(
                LedgerRecord(
                    at=at,
                    world_id=op.world_id,
                    character_id=op.character_id,
                    path=op.path,
                    model=op.model,
                    protocol=op.protocol,
                    transport=call.transport,
                    failure=call.failure,
                    operation=outcome,
                    usage=call.usage,
                    prices=prices,
                    # 失败的请求不知道扣没扣钱：null，不是 0。
                    cost_yuan=estimate_cost(call.usage, prices),
                )
            )

    def record_response(self, response: Any) -> None:
        op = _CURRENT.get()
        if op is None:
            return
        try:
            usage = normalize_usage(self._protocol, getattr(response, "usage", None))
        except Exception:  # 读 usage 本身出错也不许影响调用结果
            usage = None
        op.calls.append(_Call(TRANSPORT_RESPONSE, None, usage))

    def record_error(self, exc: BaseException) -> None:
        op = _CURRENT.get()
        if op is None:
            return
        try:
            failure = classify_failure(exc)
        except Exception:
            failure = FAILURE_UNKNOWN
        op.calls.append(_Call(TRANSPORT_ERROR, failure, None))


class _MeteredMethod:
    def __init__(self, meter: UsageMeter, method: Callable[..., Any]) -> None:
        self._meter = meter
        self._method = method

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not args:
            self._meter.capture(kwargs)
        try:
            with self._meter.send_lock:
                response = self._method(*args, **kwargs)
        except Exception as e:
            self._meter.record_error(e)
            raise
        self._meter.record_response(response)
        return response


class _Namespace:
    """`client.messages` / `client.chat` / `client.chat.completions` 的代理。"""

    def __init__(self, meter: UsageMeter, target: Any, metered: Mapping[str, Any]) -> None:
        self._meter = meter
        self._target = target
        self._metered = metered

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._target, name)
        spec = self._metered.get(name)
        if spec is None:
            return value
        if spec is True:
            return _MeteredMethod(self._meter, value)
        return _Namespace(self._meter, value, spec)


# 只有这两条路径会真正向 provider 发请求。别的属性原样透传。
_ANTHROPIC = {"messages": {"create": True}}
_OPENAI = {"chat": {"completions": {"create": True}}}


class MeteredClient(_Namespace):
    def __init__(self, client: Any, meter: UsageMeter) -> None:
        spec = _OPENAI if meter.protocol == "openai" else _ANTHROPIC
        super().__init__(meter, client, spec)


__all__ = ["MeteredClient", "Operation", "UsageMeter"]
