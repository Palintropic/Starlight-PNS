# pns/runtime/usage_ledger.py — 模型调用的用量账本（运维数据，不是世界数据）
#
# 这一层回答的问题只有一个：**这台服务器向模型 provider 发了多少次请求、花了
# 大概多少钱、最近是不是一直在失败。**
#
# 它属于 Constitution Article XIII 的 Operational History：不进世界存档、不进
# 快照、不进事件、观察或记忆，也不是任何权威状态。账本丢了只影响统计，世界
# 照常运行。
#
# 四条硬约束：
#
#   1. **provider 那侧的东西只有几个整数能进来。** usage 里只读列出的键，只收
#      `type(v) is int` 且非负的值；异常只经过 `classify_failure` 变成一个封闭
#      枚举。消息原文、类型名、响应体、响应里的 model 字段一概不碰——它们都
#      可能装着一把 API key（理由同 composition.build_adapters 的注释）。
#   2. **拿不到就是 null，不是 0。** usage 形状不对、模型不在价目表里、请求失败
#      了不知道扣没扣钱，`cost_yuan` 一律是 None。汇总把这些单独计数，余额估算
#      旁边写明"有几次没算进去"，而不是假装它们免费。
#   3. **记账不改变调用结果。** 写账失败只计数、打一行固定文本，绝不抛给调用方。
#   4. **一切汇总都能只凭账本文件和锚点文件重新推出来。** 连续失败、余额估算都
#      不另存状态。
import json
import math
import sys
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

# MiMo 的账单按北京时间；中国不用夏令时，所以一个固定偏移就够，不依赖 tzdata。
BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")

# ── 枚举 ─────────────────────────────────────────────────────────────────

PATH_GENERATION = "generation"
PATH_JUDGE = "judge"
PATHS = (PATH_GENERATION, PATH_JUDGE)

TRANSPORT_RESPONSE = "response"
TRANSPORT_ERROR = "error"

OPERATION_OK = "ok"
OPERATION_TRUNCATED = "truncated"
OPERATION_UNUSABLE = "unusable"
OPERATION_FAILED = "failed"
OPERATIONS = (OPERATION_OK, OPERATION_TRUNCATED, OPERATION_UNUSABLE, OPERATION_FAILED)

FAILURE_AUTH = "auth"
FAILURE_PAYMENT = "payment"
FAILURE_RATE_LIMITED = "rate_limited"
FAILURE_BAD_REQUEST = "bad_request"
FAILURE_SERVER = "server"
FAILURE_NETWORK = "network"
FAILURE_UNKNOWN = "unknown"
FAILURES = (
    FAILURE_AUTH,
    FAILURE_PAYMENT,
    FAILURE_RATE_LIMITED,
    FAILURE_BAD_REQUEST,
    FAILURE_SERVER,
    FAILURE_NETWORK,
    FAILURE_UNKNOWN,
)

# 请求可能已经在 provider 那边跑完并扣了钱、只是我们没收到 usage 的两类失败。
BILLING_UNCERTAIN = frozenset({FAILURE_NETWORK, FAILURE_SERVER})

# HTTP 状态码 → 分类。MiMo 余额不足是 402（官方错误码页，2026-10-08 ena 核）；
# 429 是频率超限或 Token Plan 额度耗尽。
_STATUS_FAILURES = MappingProxyType(
    {
        400: FAILURE_BAD_REQUEST,
        401: FAILURE_AUTH,
        402: FAILURE_PAYMENT,
        403: FAILURE_AUTH,
        404: FAILURE_BAD_REQUEST,
        413: FAILURE_BAD_REQUEST,
        422: FAILURE_BAD_REQUEST,
        429: FAILURE_RATE_LIMITED,
    }
)

DEFAULT_FAILURE_ALERT = 6
DEFAULT_BALANCE_WARN_YUAN = 10.0

ANCHOR_MIN_YUAN = -10_000.0
ANCHOR_MAX_YUAN = 1_000_000.0
ANCHOR_FUTURE_SKEW = timedelta(minutes=5)

ANCHOR_FILE = "balance_anchor.json"


# ── 失败分类 ─────────────────────────────────────────────────────────────


def _sdk_classes() -> Tuple[tuple, tuple]:
    """两个 SDK 的"带状态码"与"连不上"异常类。没装的那个就跳过。"""
    status, connection = [], []
    try:
        import anthropic

        status.append(anthropic.APIStatusError)
        connection.append(anthropic.APIConnectionError)
    except ImportError:  # pragma: no cover - 生产两个都装
        pass
    try:
        import openai

        status.append(openai.APIStatusError)
        connection.append(openai.APIConnectionError)
    except ImportError:  # pragma: no cover
        pass
    return tuple(status), tuple(connection)


def classify_failure(exc: BaseException) -> str:
    """把一个 SDK 调用点当场抓到的异常变成一个封闭枚举值。

    只看两样东西：异常是不是 SDK 的某个类（`isinstance`），以及它的
    `status_code` 是不是一个真正的 int。消息、类型名、其他属性一概不读；
    认不出来的一律 `unknown`。返回值永远在 `FAILURES` 里。
    """
    status_classes, connection_classes = _sdk_classes()
    if connection_classes and isinstance(exc, connection_classes):
        # APITimeoutError 是 APIConnectionError 的子类。
        return FAILURE_NETWORK
    if status_classes and isinstance(exc, status_classes):
        code = getattr(exc, "status_code", None)
        if type(code) is not int:
            return FAILURE_UNKNOWN
        if code >= 500:
            return FAILURE_SERVER
        return _STATUS_FAILURES.get(code, FAILURE_UNKNOWN)
    return FAILURE_UNKNOWN


# ── 价目表 ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Prices:
    """一个模型的四个单价，单位：元 / 百万 token。"""

    input: float
    cache_write: float
    cache_read: float
    output: float

    def to_dict(self) -> Dict[str, float]:
        return {
            "input": self.input,
            "cache_write": self.cache_write,
            "cache_read": self.cache_read,
            "output": self.output,
        }


PRICE_FIELDS = ("input", "cache_write", "cache_read", "output")


def parse_price_table(raw: Any) -> Mapping[str, Prices]:
    """解析 config.yaml 的 `pricing:` 段。没有这一段就是空表。

    形状不对抛 ValueError：价目表写错了应该让这份配置不通过，而不是让账本
    悄悄按错价记一个月。
    """
    if raw is None:
        return MappingProxyType({})
    if not isinstance(raw, Mapping):
        raise ValueError(f"pricing 必须是 模型名 → 单价 的映射，实际是 {type(raw).__name__}")
    table: Dict[str, Prices] = {}
    for model, entry in raw.items():
        if not isinstance(model, str) or not model:
            raise ValueError(f"pricing 的模型名必须是非空字符串，实际是 {model!r}")
        if not isinstance(entry, Mapping):
            raise ValueError(f"pricing.{model} 必须是映射")
        unknown = set(entry) - set(PRICE_FIELDS)
        if unknown:
            raise ValueError(f"pricing.{model} 有不认识的键：{sorted(map(str, unknown))}")
        values = {}
        for field in PRICE_FIELDS:
            if field not in entry:
                raise ValueError(f"pricing.{model} 缺少 {field}")
            value = entry[field]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"pricing.{model}.{field} 必须是非负有限数，实际是 {value!r}")
            values[field] = float(value)
        table[model] = Prices(**values)
    return MappingProxyType(table)


# ── usage 归一化 ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Usage:
    fresh_input: int
    cache_write: int
    cache_read: int
    output: int

    def to_dict(self) -> Dict[str, int]:
        return {
            "fresh_input": self.fresh_input,
            "cache_write": self.cache_write,
            "cache_read": self.cache_read,
            "output": self.output,
        }


class _Malformed(Exception):
    pass


_MISSING = object()


def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return _MISSING
    if isinstance(obj, Mapping):
        return obj.get(name, _MISSING)
    return getattr(obj, name, _MISSING)


def _count(value: Any) -> int:
    # bool 是 int 的子类，所以必须比较类型本身。
    if type(value) is not int or value < 0:
        raise _Malformed()
    return value


def normalize_usage(protocol: str, usage: Any) -> Optional[Usage]:
    """把一个响应的 usage 归一成四个非负整数；形状不对返回 None（= 无法估算）。

    anthropic 协议（MiMo 当前就是）：`input_tokens` **不含**缓存命中，
    `cache_read_input_tokens` 另算，二者相加才是总输入。把命中当成包含在
    input_tokens 里，正是 MODEL-1 那次少算约 20 倍的错。

    openai 协议：`prompt_tokens` **已经包含** `cached_tokens`，所以要减掉；
    减之前必须确认 cached <= prompt，否则会算出负数。
    """
    if usage is None:
        return None
    try:
        if protocol == "openai":
            prompt = _count(_field(usage, "prompt_tokens"))
            output = _count(_field(usage, "completion_tokens"))
            details = _field(usage, "prompt_tokens_details")
            if details is _MISSING or details is None:
                cached = 0
            else:
                raw_cached = _field(details, "cached_tokens")
                cached = 0 if raw_cached is _MISSING or raw_cached is None else _count(raw_cached)
            if cached > prompt:
                raise _Malformed()
            return Usage(prompt - cached, 0, cached, output)
        fresh = _count(_field(usage, "input_tokens"))
        output = _count(_field(usage, "output_tokens"))
        # MiMo 不提供缓存写入字段；缺省或 null 都按 0（它不单独计费）。
        raw_write = _field(usage, "cache_creation_input_tokens")
        write = 0 if raw_write is _MISSING or raw_write is None else _count(raw_write)
        # 缓存命中：MiMo 没读到缓存时给 null，读到了给整数（2026-10-09 生产实测：
        # 同样形状的请求，命中的那次是 1024，其余都是 null）。所以 null 就是 0。
        # 键根本不存在仍算形状不对：那不是"没命中"，是这家 provider 不报这一项。
        raw_read = _field(usage, "cache_read_input_tokens")
        if raw_read is _MISSING:
            raise _Malformed()
        cache_read = 0 if raw_read is None else _count(raw_read)
        return Usage(fresh, write, cache_read, output)
    except _Malformed:
        return None


def estimate_cost(usage: Optional[Usage], prices: Optional[Prices]) -> Optional[float]:
    """按记下来的单价估算一次调用的花费（元）；任一缺失返回 None。"""
    if usage is None or prices is None:
        return None
    return (
        usage.fresh_input * prices.input
        + usage.cache_write * prices.cache_write
        + usage.cache_read * prices.cache_read
        + usage.output * prices.output
    ) / 1_000_000


# ── 账本记录 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LedgerRecord:
    at: datetime
    world_id: str
    character_id: str
    path: str
    model: str
    protocol: str
    transport: str
    failure: Optional[str]
    operation: str
    usage: Optional[Usage]
    prices: Optional[Prices]
    cost_yuan: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        usage = self.usage.to_dict() if self.usage is not None else {}
        return {
            "at": self.at.astimezone(timezone.utc).isoformat(),
            "world_id": self.world_id,
            "character_id": self.character_id,
            "path": self.path,
            "model": self.model,
            "protocol": self.protocol,
            "transport": self.transport,
            "failure": self.failure,
            "operation": self.operation,
            "fresh_input": usage.get("fresh_input"),
            "cache_write": usage.get("cache_write"),
            "cache_read": usage.get("cache_read"),
            "output": usage.get("output"),
            "prices": self.prices.to_dict() if self.prices is not None else None,
            "cost_yuan": self.cost_yuan,
        }


def _parse_at(raw: Any) -> datetime:
    if not isinstance(raw, str):
        raise ValueError("at")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError("at")
    return parsed


def _record_from_dict(raw: Any) -> LedgerRecord:
    """读回一行。任何字段不合规都抛 ValueError，由调用方计入损坏行。"""
    if not isinstance(raw, dict):
        raise ValueError("row")
    counts = [raw.get(k) for k in ("fresh_input", "cache_write", "cache_read", "output")]
    if all(c is None for c in counts):
        usage = None
    else:
        try:
            usage = Usage(*(_count(c) for c in counts))
        except _Malformed as e:
            raise ValueError("usage") from e
    prices_raw = raw.get("prices")
    prices = None
    if prices_raw is not None:
        prices = parse_price_table({"_": prices_raw})["_"]
    cost = raw.get("cost_yuan")
    if cost is not None and (type(cost) not in (int, float) or not math.isfinite(cost)):
        raise ValueError("cost_yuan")
    operation = raw.get("operation")
    transport = raw.get("transport")
    failure = raw.get("failure")
    path = raw.get("path")
    if operation not in OPERATIONS or transport not in (TRANSPORT_RESPONSE, TRANSPORT_ERROR):
        raise ValueError("enum")
    if failure is not None and failure not in FAILURES:
        raise ValueError("failure")
    # 写入路径上这几个字段是一起定的：拿到响应就没有失败分类；SDK 抛了异常
    # 就一定有分类，而且这次操作一定是 failed。对不上的行不是我们写的。
    if transport == TRANSPORT_RESPONSE and failure is not None:
        raise ValueError("consistency")
    if transport == TRANSPORT_ERROR and (failure is None or operation != OPERATION_FAILED):
        raise ValueError("consistency")
    if path not in PATHS:
        raise ValueError("path")
    strings = [raw.get(k) for k in ("world_id", "character_id", "model", "protocol")]
    if not all(isinstance(s, str) for s in strings):
        raise ValueError("strings")
    if cost is not None and (usage is None or prices is None):
        raise ValueError("consistency")
    return LedgerRecord(
        at=_parse_at(raw.get("at")),
        world_id=strings[0],
        character_id=strings[1],
        path=path,
        model=strings[2],
        protocol=strings[3],
        transport=transport,
        failure=failure,
        operation=operation,
        usage=usage,
        prices=prices,
        cost_yuan=float(cost) if cost is not None else None,
    )


# ── 账本文件 ─────────────────────────────────────────────────────────────

LEDGER_WRITE_FAILED = "[usage-ledger] 用量账本写入失败；这次调用照常进行，统计会少一行"


def _order(record: "LedgerRecord") -> Tuple[datetime, int]:
    """读账的顺序：按 `at`；同一时刻时，不成功的排在成功的后面。

    这样同一份账本不论行序如何，汇总都一样；而且平局时偏保守——一次跟失败
    同时结束的成功不会把告警清掉。"""
    return (record.at, 0 if record.operation == OPERATION_OK else 1)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UsageLedger:
    """按北京日期分文件的追加式 JSONL 账本。

    追加持一把进程内的锁，每次写完整一行。写失败不抛：计数、往 stderr 打一行
    固定文本。这个可见性只到"当前进程 + 容器日志"，进程重启后计数归零。
    """

    def __init__(self, root: Path, *, clock: Callable[[], datetime] = _utcnow):
        self._root = Path(root)
        self._clock = clock
        self._lock = threading.Lock()
        self._write_failures = 0

    @property
    def root(self) -> Path:
        return self._root

    @property
    def write_failures(self) -> int:
        with self._lock:
            return self._write_failures

    def now(self) -> datetime:
        return self._clock()

    def _file_for(self, day: date) -> Path:
        return self._root / f"{day.isoformat()}.jsonl"

    def append(self, record: LedgerRecord) -> bool:
        line = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        day = record.at.astimezone(BEIJING).date()
        with self._lock:
            try:
                self._root.mkdir(parents=True, exist_ok=True)
                with open(self._file_for(day), "a", encoding="utf-8") as fh:
                    fh.write(line)
                return True
            except Exception:
                self._write_failures += 1
                print(LEDGER_WRITE_FAILED, file=sys.stderr)
                return False

    def read(
        self, since: Optional[datetime] = None, *, until_day: Optional[date] = None
    ) -> Tuple[List[LedgerRecord], int]:
        """读出 `since`（含）之后的所有记录，按 `at` 排序；返回 (记录, 损坏行数)。

        只打开日期不早于 `since` 那天（北京日期）、且不晚于 `until_day` 的文件。
        """
        if not self._root.is_dir():
            return [], 0
        first_day = since.astimezone(BEIJING).date() if since is not None else None
        records: List[LedgerRecord] = []
        corrupt = 0
        for path in sorted(self._root.glob("*.jsonl")):
            try:
                day = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if first_day is not None and day < first_day:
                continue
            if until_day is not None and day > until_day:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                corrupt += 1
                continue
            for line in lines:
                if not line.strip():
                    continue
                try:
                    record = _record_from_dict(json.loads(line))
                except (ValueError, TypeError, KeyError):
                    corrupt += 1
                    continue
                if since is not None and record.at < since:
                    continue
                records.append(record)
        records.sort(key=_order)
        return records, corrupt

    def _days(self) -> List[date]:
        if not self._root.is_dir():
            return []
        days = []
        for path in self._root.glob("*.jsonl"):
            try:
                days.append(date.fromisoformat(path.stem))
            except ValueError:
                continue
        return sorted(days)

    def earliest(self) -> Optional[datetime]:
        """账本里最早一行的时刻。只读最早那个有内容的文件。"""
        for day in self._days():
            start = datetime.combine(day, datetime.min.time(), BEIJING)
            records, _ = self.read(since=start, until_day=day)
            if records:
                return records[0].at
        return None


# ── 余额锚点 ─────────────────────────────────────────────────────────────


class AnchorError(ValueError):
    """锚点不合规。消息是给操作者看的固定措辞。"""


@dataclass(frozen=True)
class BalanceAnchor:
    balance_yuan: float
    at: datetime

    def to_dict(self) -> Dict[str, Any]:
        return {
            "balance_yuan": self.balance_yuan,
            "at": self.at.astimezone(timezone.utc).isoformat(),
        }


def validate_anchor(balance_yuan: Any, at: Optional[datetime], now: datetime) -> BalanceAnchor:
    if type(balance_yuan) not in (int, float) or not math.isfinite(balance_yuan):
        raise AnchorError("余额必须是有限的数字")
    if not ANCHOR_MIN_YUAN <= balance_yuan <= ANCHOR_MAX_YUAN:
        raise AnchorError(
            f"余额必须在 {ANCHOR_MIN_YUAN:g} 到 {ANCHOR_MAX_YUAN:g} 元之间"
        )
    if at is None:
        at = now
    if at.tzinfo is None:
        raise AnchorError("时间必须带时区")
    if at > now + ANCHOR_FUTURE_SKEW:
        raise AnchorError("时间不能在未来")
    return BalanceAnchor(float(balance_yuan), at)


def load_anchor(root: Path) -> Optional[BalanceAnchor]:
    path = Path(root) / ANCHOR_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        balance = raw["balance_yuan"]
        if type(balance) not in (int, float) or not math.isfinite(balance):
            return None
        return BalanceAnchor(float(balance), _parse_at(raw["at"]))
    except (OSError, ValueError, TypeError, KeyError):
        return None


def save_anchor(root: Path, anchor: BalanceAnchor) -> None:
    """原子写：临时文件写完再 rename，读的人只会看到旧的或新的。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / ANCHOR_FILE
    tmp = root / f".{ANCHOR_FILE}.tmp"
    tmp.write_text(json.dumps(anchor.to_dict(), sort_keys=True), encoding="utf-8")
    tmp.replace(target)


# ── 汇总 ─────────────────────────────────────────────────────────────────


def failure_streak(records: Iterable[LedgerRecord]) -> Dict[str, Any]:
    """最近一次 operation=ok 之后的连续不成功段。

    只有 ok 能归零：拿到 HTTP 200 但没文本、被过滤、判分解析失败，世界一样
    是哑的。`records` 必须已按 `at` 排好。
    """
    tail: List[LedgerRecord] = []
    for record in records:
        if record.operation == OPERATION_OK:
            tail = []
        else:
            tail.append(record)
    if not tail:
        return {"consecutive": 0, "since": None, "last_failure": None}
    last = tail[-1]
    last_failure = last.failure if last.failure is not None else last.operation
    return {
        "consecutive": len(tail),
        "since": tail[0].at.astimezone(timezone.utc).isoformat(),
        "last_failure": last_failure,
    }


def _blank_bucket() -> Dict[str, Any]:
    return {
        "calls": 0,
        "fresh_input": 0,
        "cache_write": 0,
        "cache_read": 0,
        "output": 0,
        "cost_yuan": 0.0,
        "unpriced": 0,
        "no_usage": 0,
        "billing_uncertain": 0,
        "failed": 0,
    }


def _add(bucket: Dict[str, Any], record: LedgerRecord) -> None:
    bucket["calls"] += 1
    if record.usage is not None:
        for key, value in record.usage.to_dict().items():
            bucket[key] += value
    elif record.transport == TRANSPORT_RESPONSE:
        bucket["no_usage"] += 1
    if record.cost_yuan is not None:
        bucket["cost_yuan"] += record.cost_yuan
    elif record.usage is not None:
        bucket["unpriced"] += 1
    if record.transport == TRANSPORT_ERROR:
        bucket["failed"] += 1
        if record.failure in BILLING_UNCERTAIN:
            bucket["billing_uncertain"] += 1


def _finish(bucket: Dict[str, Any]) -> Dict[str, Any]:
    total_input = bucket["fresh_input"] + bucket["cache_read"]
    bucket["cache_hit_rate"] = bucket["cache_read"] / total_input if total_input else None
    bucket["cost_yuan"] = round(bucket["cost_yuan"], 8)
    return bucket


def balance_estimate(
    records: Iterable[LedgerRecord],
    anchor: Optional[BalanceAnchor],
    *,
    earliest: Optional[datetime],
    warn_yuan: float,
) -> Optional[Dict[str, Any]]:
    if anchor is None:
        return None
    # 锚点是"此刻的余额"：同一时刻已经结束的那次调用算在锚点之前。
    after = [r for r in records if r.at > anchor.at]
    spent = sum(r.cost_yuan for r in after if r.cost_yuan is not None)
    estimated = anchor.balance_yuan - spent
    return {
        "anchor": anchor.to_dict(),
        "spent_since_anchor_yuan": round(spent, 8),
        "estimated_yuan": round(estimated, 8),
        "uncounted_calls": sum(1 for r in after if r.cost_yuan is None),
        "billing_uncertain_calls": sum(
            1 for r in after if r.transport == TRANSPORT_ERROR and r.failure in BILLING_UNCERTAIN
        ),
        "anchor_before_ledger": earliest is None or anchor.at < earliest,
        "low": estimated < warn_yuan,
        "warn_yuan": warn_yuan,
    }


def summarize(
    ledger: UsageLedger,
    *,
    days: int,
    anchor: Optional[BalanceAnchor],
    alert_threshold: int = DEFAULT_FAILURE_ALERT,
    warn_yuan: float = DEFAULT_BALANCE_WARN_YUAN,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """`days` 个北京日（含今天）的汇总、连续失败段、余额估算。"""
    now = now or ledger.now()
    today = now.astimezone(BEIJING).date()
    first_day = today - timedelta(days=max(days, 1) - 1)
    window_start = datetime.combine(first_day, datetime.min.time(), BEIJING)

    # 只读需要的那几天：窗口起点和锚点里更早的那个。连续失败段如果一直延伸到
    # 读取范围的开头，说明更早的文件里可能还有，才往前补读整本账。
    read_from = window_start
    if anchor is not None and anchor.at < read_from:
        read_from = anchor.at
    read_from = datetime.combine(read_from.astimezone(BEIJING).date(), datetime.min.time(), BEIJING)
    all_records, corrupt = ledger.read(since=read_from)
    if not any(r.operation == OPERATION_OK for r in all_records):
        days = ledger._days()
        if days and days[0] < read_from.date():
            all_records, corrupt = ledger.read()
    # 时钟跳回、或者有人手写了未来的行：还没发生的调用不算。
    all_records = [r for r in all_records if r.at <= now]
    window = [r for r in all_records if r.at >= window_start]

    by_day: Dict[str, Dict[str, Any]] = {}
    by_world: Dict[str, Dict[str, Any]] = {}
    by_path: Dict[str, Dict[str, Any]] = {}
    by_character: Dict[str, Dict[str, Any]] = {}
    total = _blank_bucket()
    for record in window:
        day_key = record.at.astimezone(BEIJING).date().isoformat()
        for table, key in (
            (by_day, day_key),
            (by_world, record.world_id),
            (by_path, record.path),
            (by_character, record.character_id),
        ):
            _add(table.setdefault(key, _blank_bucket()), record)
        _add(total, record)

    def finish_all(table: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        return {key: _finish(table[key]) for key in sorted(table)}

    streak = failure_streak(all_records)
    streak["alert"] = streak["consecutive"] >= alert_threshold
    streak["alert_threshold"] = alert_threshold
    return {
        "now": now.astimezone(timezone.utc).isoformat(),
        "timezone": "Asia/Shanghai",
        "days": [first_day.isoformat(), today.isoformat()],
        "total": _finish(total),
        "by_day": finish_all(by_day),
        "by_world": finish_all(by_world),
        "by_path": finish_all(by_path),
        "by_character": finish_all(by_character),
        "failure_streak": streak,
        "balance": balance_estimate(
            all_records,
            anchor,
            earliest=ledger.earliest() if anchor is not None else None,
            warn_yuan=warn_yuan,
        ),
        "ledger_write_failures": ledger.write_failures,
        "corrupt_lines": corrupt,
    }


__all__ = [
    "BEIJING",
    "BILLING_UNCERTAIN",
    "AnchorError",
    "BalanceAnchor",
    "LedgerRecord",
    "Prices",
    "Usage",
    "UsageLedger",
    "balance_estimate",
    "classify_failure",
    "estimate_cost",
    "failure_streak",
    "load_anchor",
    "normalize_usage",
    "parse_price_table",
    "save_anchor",
    "summarize",
    "validate_anchor",
]
