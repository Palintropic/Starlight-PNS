# pns/runtime/autonomy/clock_worker.py — 世界时钟 worker
#
# 一个打开的持久世界里，时间一直在走（WORLD-1 设计 §2、§7）。这个 worker 就是
# "谁在推时间"：它跟着世界打开而启动、跟着世界关闭而停止，按锚点把世界一步
# 一步推到现实时间换算出的那一分钟，途中到期的激活由它处理。
#
# 它跟被它替换掉的 WorldDriver 最大的区别：**Start/Stop 不启停它。** Start/Stop
# 只改认知时间线——认知不可用时，时间、作息、行程照走，到期以
# REJECTED_UNAVAILABLE 收尾，不调模型。所以"自动模型调用是 opt-in"仍然成立，
# 只是落在时间线上，而不是落在一个线程在不在。
#
# 几条硬约束：
#
#   1. **一个世界一个 worker**，由世界句柄持有；句柄登记之后才启动，关闭时
#      第一个停。
#   2. **进程内用 monotonic 推算现实时间。** 打开时记下 (UTC, monotonic)，之后
#      wall_now = UTC₀ + (monotonic − monotonic₀)：系统时钟被拨动不影响推进。
#      跨进程才读 UTC（恢复时由生命周期完成）。
#   3. **失败不忙等。** 每一轮之间恒有一次有界等待；连续失败达到阈值进入
#      faulted，按指数退避探测（上限 10 分钟），认知时间线记一段 fault。恢复
#      之后补跑，经过故障时段的到期带 fault 收尾。
#   4. **一轮有界。** 一次长补跑切成若干段（max_steps_per_iteration），段与段
#      之间可以停下来；补跑时不等满节拍，但每一段都必须真的推进了时钟。
#   5. **状态里不出现 provider 那侧的任何原文。** 非本仓库的异常只留一句固定
#      的话（理由同 composition.py：异常类型名装得下一把 API Key）。
#
# 这个模块不 import 持久化层，它只鸭子类型地用世界句柄的四样东西：`world_id`、
# `runtime`、`closed`、`checkpoint_if_due()`。import 它不建线程、不碰磁盘。
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Optional

from pns.models.cognition import CognitionCause, OPERATOR_CLEARABLE

# 自动 checkpoint 的理由码。它会进存档的 last_checkpoint_reason。
CHECKPOINT_REASON = "clock_step"

MAX_ERROR_CHARS = 300
OPAQUE_ERROR = "时钟 worker 遇到未预期类型的错误"

MAX_INTERVAL_SECONDS = 3600.0
MAX_STOP_TIMEOUT_SECONDS = 300.0
MAX_ACTIVATIONS_PER_RUN = 100_000
MAX_STEPS_PER_ITERATION = 100_000
MAX_BACKOFF_SECONDS = 600.0

CLOCK_CATCHING_UP = "catching_up"
CLOCK_HEALTHY = "healthy"
CLOCK_FAULTED = "faulted"


class ClockWorkerError(RuntimeError):
    """这次操作不该发生（世界已经关了、配置不合法、生产环境不许快进）。"""


def _number(value, name: str, low: float, high: float, *, integer: bool = False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClockWorkerError(f"{name} 必须是数字，收到 {value!r}")
    if integer and not isinstance(value, int):
        raise ClockWorkerError(f"{name} 必须是整数，收到 {value!r}")
    if not low <= value <= high:
        raise ClockWorkerError(f"{name} 必须落在 [{low:g}, {high:g}]，收到 {value!r}")
    return value


@dataclass(frozen=True)
class ClockConfig:
    """时钟 worker 的节拍与边界。服务器侧配置，不接受浏览器传值。"""

    # 两轮之间的**真实**秒数。时钟不落后时就按这个节拍追锚点。
    interval_seconds: float = 5.0
    # 模拟时间相对现实时间的倍率。生产必须是 1（见 require_real_time）。
    rate: float = 1.0
    # 生产环境：rate 不是 1 就拒绝（fail-closed），不是"降级成 1"。
    require_real_time: bool = False
    # 一轮最多走几个时钟步。补跑按段进行，段与段之间可以停。
    max_steps_per_iteration: int = 240
    # 连续失败多少次进入 faulted。
    fault_threshold: int = 5
    # faulted 之后退避的上限。
    max_backoff_seconds: float = MAX_BACKOFF_SECONDS
    # 关闭时最多等当前这一轮落定多少**真实**秒。
    stop_timeout_seconds: float = 10.0
    # 一次 Start 装满的单次额度：最多多少次认知（一次生成 + 一次判分）。
    max_activations_per_run: int = 200

    def __post_init__(self) -> None:
        _number(self.interval_seconds, "interval_seconds", 1e-3, MAX_INTERVAL_SECONDS)
        _number(self.rate, "rate", 1e-3, 3600.0)
        if not isinstance(self.require_real_time, bool):
            raise ClockWorkerError("require_real_time 必须是布尔值")
        if self.require_real_time and float(self.rate) != 1.0:
            raise ClockWorkerError(
                f"生产环境的世界时间必须与现实 1:1（rate=1），收到 rate={self.rate}"
            )
        _number(
            self.max_steps_per_iteration,
            "max_steps_per_iteration",
            1,
            MAX_STEPS_PER_ITERATION,
            integer=True,
        )
        _number(self.fault_threshold, "fault_threshold", 1, 1000, integer=True)
        _number(self.max_backoff_seconds, "max_backoff_seconds", 1e-3, MAX_BACKOFF_SECONDS)
        _number(
            self.stop_timeout_seconds, "stop_timeout_seconds", 1e-3, MAX_STOP_TIMEOUT_SECONDS
        )
        _number(
            self.max_activations_per_run,
            "max_activations_per_run",
            1,
            MAX_ACTIVATIONS_PER_RUN,
            integer=True,
        )

    def to_dict(self) -> Dict:
        return {
            "interval_seconds": float(self.interval_seconds),
            "rate": float(self.rate),
            "max_steps_per_iteration": self.max_steps_per_iteration,
            "fault_threshold": self.fault_threshold,
            "stop_timeout_seconds": float(self.stop_timeout_seconds),
            "max_activations_per_run": self.max_activations_per_run,
        }


def safe_error(error: BaseException) -> str:
    """一次失败对外能说的那句话：本仓库的异常原文，别的只留固定的一句。"""
    module = getattr(type(error), "__module__", "") or ""
    if module == "pns" or module.startswith("pns."):
        text = " ".join(f"{type(error).__name__}: {error}".split())
        if len(text) > MAX_ERROR_CHARS:
            return text[: MAX_ERROR_CHARS - 1] + "…"
        return text
    print("[clock-worker] 遇到未预期类型的错误（原文不外传）", flush=True)
    return OPAQUE_ERROR


def monotonic_wall() -> Callable[[], datetime]:
    """进程内的现实时间：UTC 起点 + monotonic 增量。"""
    start_wall = datetime.now(timezone.utc)
    start_mono = time.monotonic()
    return lambda: start_wall + timedelta(seconds=time.monotonic() - start_mono)


class ClockWorker:
    """一个打开的世界的时钟 worker。进程内状态，不进存档。"""

    def __init__(
        self,
        world,
        config: ClockConfig,
        *,
        wall_clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        for attribute in ("world_id", "runtime", "checkpoint_if_due"):
            if not hasattr(world, attribute):
                raise ClockWorkerError(f"时钟 worker 需要一个持久世界句柄（缺 {attribute}）")
        if not isinstance(config, ClockConfig):
            raise ClockWorkerError("config 必须是 ClockConfig")
        self._world = world
        self._config = config
        self._wall_now = wall_clock if wall_clock is not None else monotonic_wall()
        # 只保护自己的簿记。持有它时不调世界或运行时的方法。
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._exit_reason: Optional[str] = None
        self._iterations = 0
        self._failures = 0
        self._consecutive_failures = 0
        self._clock_state = CLOCK_CATCHING_UP
        self._fault_since: Optional[str] = None
        self._fault_recorded = False
        self._last_error: Optional[str] = None
        self._last_progress_at: Optional[str] = None
        self._last_tick_at: Optional[str] = None
        self._last_tick: Optional[Dict] = None

    # ── 读 ──────────────────────────────────────────────────────────────
    @property
    def world_id(self) -> str:
        return self._world.world_id

    @property
    def config(self) -> ClockConfig:
        return self._config

    def wall_now(self) -> datetime:
        return self._wall_now()

    @property
    def alive(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    # ── 启停 ────────────────────────────────────────────────────────────
    def start(self) -> None:
        """起 worker 线程。只能起一次：它的一生就是这个世界句柄打开的那一段。"""
        with self._lock:
            if self._thread is not None:
                raise ClockWorkerError(f"世界 '{self.world_id}' 的时钟 worker 已经起过了")
            thread = threading.Thread(
                target=self._run, name=f"pns-clock-{self.world_id}", daemon=True
            )
            thread.start()
            self._thread = thread

    def stop(self, timeout: Optional[float] = None) -> bool:
        """请 worker 停下并有界地等它退出。返回它是否真的退出了。

        等不到（多半卡在一次模型调用上）就如实返回 False：调用方据此决定是拒绝
        关闭，还是先终局停机再放手（晚到的提交会被运行时拒绝）。
        """
        wait = self._config.stop_timeout_seconds if timeout is None else float(timeout)
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(max(0.0, wait))
        return not thread.is_alive()

    # ── 操作员 ──────────────────────────────────────────────────────────
    def start_cognition(self) -> Dict:
        """操作员 Start：认知从下一个完整模拟分钟起可用，装满单次额度。

        额度 N 取自创建这个 worker 时的配置，记进时间线；带续额策略的世界之后
        每天续满的也是这个记下来的 N，不在边界上重读配置。
        """
        self._require_open()
        self._world.runtime.start_cognition(
            self._config.max_activations_per_run, wall=self._wall_now()
        )
        return self.status()

    def stop_cognition(self) -> Dict:
        """操作员 Stop：认知从此刻起不可用。时间照走。"""
        self._require_open()
        self._world.runtime.stop_cognition(wall=self._wall_now())
        return self.status()

    # ── worker ──────────────────────────────────────────────────────────
    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                if not self._should_run():
                    break
                delay = self._iterate()
                self._stop_event.wait(delay)
        finally:
            with self._lock:
                if self._exit_reason is None:
                    self._exit_reason = "stopped"

    def _should_run(self) -> bool:
        if getattr(self._world, "closed", False):
            self._finish("world_closed")
            return False
        if not self._world.runtime.running:
            self._finish("runtime_stopped")
            return False
        return True

    def _iterate(self) -> float:
        """跑一轮，返回到下一轮之前要等多少秒。"""
        try:
            report, lag = self._step()
        except BaseException as e:  # noqa: BLE001 - 记下来，绝不让 worker 静默死掉
            return self._record_failure(e)
        checkpointed = None
        if report["minutes"] > 0 or report["results"]:
            # 只有真的过了一个权威边界才问：没东西变的一轮不算边界，否则一个
            # 静止的世界也会每分钟多写一版存档。
            try:
                checkpointed = self._world.checkpoint_if_due(CHECKPOINT_REASON)
            except BaseException as e:  # noqa: BLE001
                return self._record_failure(e)
        self._record_success(report, checkpointed, lag)
        if report["minutes"] > 0 and lag > 0:
            # 还在补跑，而且这一段确实推进了：不等满节拍，接着走下一段。
            return 0.0
        return float(self._config.interval_seconds)

    def _step(self):
        runtime = self._world.runtime
        wall = self._wall_now()
        state = runtime.state
        target = state.anchor.minute_at(wall)
        causes = state.cognition.current.causes
        if target < runtime.clock and CognitionCause.WALL_CLOCK_BEHIND not in causes:
            runtime.mark_wall_clock_behind(wall=wall)
        elif target >= runtime.clock and CognitionCause.WALL_CLOCK_BEHIND in causes:
            runtime.mark_wall_clock_caught_up(wall=wall)
        report = runtime.advance_to_anchor(
            wall,
            max_steps=self._config.max_steps_per_iteration,
            keep_going=lambda: not self._stop_event.is_set(),
        )
        with self._lock:
            recorded = self._fault_recorded
        if recorded:
            # 推进成功了才算故障解除。这一段补跑里触发的到期仍在 fault 区间里
            # 收尾；解除之后、补跑剩下的那些由 backlog 带上 fault（设计 §7.2）。
            runtime.clear_fault(wall=wall)
            with self._lock:
                self._fault_recorded = False
        lag = int((target - runtime.clock).total_seconds() // 60)
        return report, lag

    def _record_success(self, report: Dict, checkpointed, lag: int) -> None:
        results = report.get("results") or []
        outcomes: Dict[str, int] = {}
        for result in results:
            key = str(result.get("outcome", "unknown"))
            outcomes[key] = outcomes.get(key, 0) + 1
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            self._iterations += 1
            self._consecutive_failures = 0
            self._last_error = None
            self._fault_since = None
            self._clock_state = CLOCK_CATCHING_UP if lag > 1 else CLOCK_HEALTHY
            self._last_tick_at = now
            if report.get("minutes"):
                self._last_progress_at = now
            self._last_tick = {
                "from_clock": report.get("from_clock"),
                "to_clock": report.get("to_clock"),
                "minutes": report.get("minutes"),
                "due": len(report.get("due_ids") or []),
                "processed": len(results),
                "outcomes": outcomes,
                "checkpoint_revision": (
                    checkpointed.get("revision") if checkpointed else None
                ),
            }

    def _record_failure(self, error: BaseException) -> float:
        message = safe_error(error)
        with self._lock:
            self._iterations += 1
            self._failures += 1
            self._consecutive_failures += 1
            self._last_error = message
            self._last_tick_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._last_tick = {"failed": True}
            failures = self._consecutive_failures
            entering = (
                failures >= self._config.fault_threshold
                and self._clock_state != CLOCK_FAULTED
            )
            if entering:
                self._clock_state = CLOCK_FAULTED
                self._fault_since = self._wall_now().isoformat(timespec="seconds")
        if entering:
            # 故障区间写进认知时间线；写不进去（比如正是事务在失败）就只留在
            # 进程内状态里，崩溃后由恢复的 process_stopped 覆盖——精度下降，不失实。
            try:
                self._world.runtime.begin_fault(wall=self._wall_now())
                with self._lock:
                    self._fault_recorded = True
            except BaseException:  # noqa: BLE001
                pass
        if failures >= self._config.fault_threshold:
            overshoot = failures - self._config.fault_threshold
            return min(
                float(self._config.max_backoff_seconds),
                float(self._config.interval_seconds) * (2 ** min(overshoot, 20)),
            )
        return float(self._config.interval_seconds)

    def _finish(self, reason: str) -> None:
        with self._lock:
            if self._exit_reason is None:
                self._exit_reason = reason

    def _require_open(self) -> None:
        if getattr(self._world, "closed", False):
            raise ClockWorkerError(f"世界 '{self.world_id}' 已经关闭")

    # ── 状态 ────────────────────────────────────────────────────────────
    def status(self) -> Dict:
        """worker 与认知此刻的样子。形状沿用 WorldDriver 的状态投影，另加时钟字段。

        `state` 回答的仍是"服务器会不会替这些角色花模型调用"：Start 过、没有
        Stop、额度和世界上限都还没到顶，就是 running。时钟本身是否在走看
        `clock_state`。
        """
        runtime = self._world.runtime
        wall = self._wall_now()
        cognition = runtime.cognition_status(wall) or {}
        causes = set(cognition.get("causes") or [])
        operator_off = bool(causes & {cause.value for cause in OPERATOR_CLEARABLE})
        capped = CognitionCause.WORLD_ACTION_CAP.value in causes
        running = bool(cognition) and not operator_off and not capped
        exit_reason = None
        if CognitionCause.RUN_BUDGET_EXHAUSTED.value in causes:
            exit_reason = "run_budget_exhausted"
        elif capped:
            exit_reason = "world_action_cap"
        next_due = None
        try:
            due_at = runtime.scheduler.next_due_at()
            next_due = due_at.isoformat() if due_at is not None else None
        except Exception:  # pragma: no cover - 状态查询不该因为读队列而失败
            next_due = None
        # 三种额度要分开说：还没 Start（显示 Start 会给的配置值）、有限、不限额。
        # 不限额是一次 Start(None) 装下的授权，不能拿配置值把它冒充成有限的。
        unlimited = (
            bool(cognition)
            and cognition.get("run_allowance") is None
            and CognitionCause.NOT_STARTED.value not in causes
        )
        if unlimited:
            limit = used = remaining = None
        else:
            limit = cognition.get("run_allowance") or self._config.max_activations_per_run
            remaining = cognition.get("run_remaining")
            used = (limit - remaining) if remaining is not None else 0
            if remaining is None:
                remaining = limit
        with self._lock:
            thread_alive = self._thread is not None and self._thread.is_alive()
            return {
                "world_id": self.world_id,
                "state": "running" if running else "stopped",
                "running": running,
                "stopping": False,
                "stopped": not running,
                "stop_reason": "operator" if CognitionCause.OPERATOR_PAUSED.value in causes else None,
                "exit_reason": exit_reason,
                "ticks": self._iterations,
                "failures": self._failures,
                "consecutive_failures": self._consecutive_failures,
                "last_error": self._last_error,
                "last_tick_at": self._last_tick_at,
                "last_tick": dict(self._last_tick) if self._last_tick else None,
                "next_due_at": next_due,
                "cadence": self._config.to_dict(),
                "run_budget": {
                    "limit": limit,
                    "used": used,
                    "remaining": remaining,
                    "renewal": cognition.get("renewal"),
                    "renews_at": cognition.get("renews_at"),
                    "day_start": cognition.get("day_start"),
                },
                "world_actions": self._world_actions(runtime),
                # 时钟（设计 §7.1）
                "worker_alive": thread_alive,
                "worker_exit_reason": self._exit_reason if not thread_alive else None,
                "clock_state": self._clock_state,
                "clock_lag_minutes": cognition.get("lag_minutes"),
                "last_clock_progress": self._last_progress_at,
                "fault_since": self._fault_since,
                "last_clock_error": self._last_error,
                "cognition_available": bool(cognition.get("available")),
                "cognition_causes": sorted(causes),
            }

    @staticmethod
    def _world_actions(runtime) -> Dict:
        committed = cap = None
        try:
            committed = runtime.state.agency.committed_actions()
            cap = runtime.agency.budget.max_committed_actions_per_session
        except Exception:  # pragma: no cover - 读不到就当不知道
            pass
        remaining = None
        if isinstance(committed, int) and isinstance(cap, int):
            remaining = max(0, cap - committed)
        return {"committed": committed, "cap": cap, "remaining": remaining}


__all__ = [
    "CHECKPOINT_REASON",
    "CLOCK_CATCHING_UP",
    "CLOCK_FAULTED",
    "CLOCK_HEALTHY",
    "MAX_ERROR_CHARS",
    "OPAQUE_ERROR",
    "ClockConfig",
    "ClockWorker",
    "ClockWorkerError",
    "monotonic_wall",
    "safe_error",
]
