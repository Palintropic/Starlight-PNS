# pns/runtime/persistence/archive.py — 世界存档的信封
#
# 信封回答的问题只有一个：**磁盘上这一坨字节，是不是这个世界某一刻的完整、
# 自洽的权威状态**。
#
# 它不回答（写在这里免得以后被顺手加进来）：怎么写盘（store）、谁拥有这个世界
# （ownership）、什么时候存（lifecycle）。
#
# 四条硬约束：
#
#   1. **身份、版本、修订号、时钟跟状态存在一起。** 一份只有 SessionState 的
#      存档回答不了"这是哪个世界的第几版、停在哪一刻" —— 而这三个问题里任何
#      一个答不上来，恢复就只能靠猜。
#   2. **恢复走既有构造函数。** SessionState.from_dict() 那一整套跨段校验
#      （事件不晚于时钟、排期严格晚于时钟、Agency 记录对得上投递箱和事件、
#      记忆对得上观察）是这份存档能不能用的判据。这里绝不 __dict__ 注水、
#      绝不绕过校验、也绝不"缺段就当空的"。
#   3. **信封和它包着的状态必须互相印证。** 信封说的 session_id 和时钟，必须
#      跟状态里的那一份一模一样。对不上就是两份不同时刻的东西被拼在了一起，
#      这种存档单独看每一段都合法，合起来却是一个不存在过的世界。
#   4. **存档里只有数据。** 调度器、Agency 引擎、记忆编码器、协调器、模型
#      客户端、API Key、锁、回调 —— 一个都不进去。捕获那一刻就检查，而不是
#      等到 json.dumps 抛类型错误：metadata 是个自由字典，谁都能往里塞活对象。
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from typing import Any, Dict, Mapping, Optional

from pns.models.session import SessionState
from pns.runtime.persistence.naming import validate_world_id

# 存档格式版本。改变形状就 +1，并且在这里写清楚旧版怎么升级 —— 不认识的版本
# 一律响亮拒绝，绝不"尽量读读看"。
WORLD_ARCHIVE_VERSION = 1

_ENVELOPE_FIELDS = ("version", "world_id", "session_id", "revision", "clock", "state")


class ArchiveError(ValueError):
    """这份存档不能用（形状不对、版本不认识、身份对不上、各段自相矛盾）。"""


class ArchiveCorrupt(ArchiveError):
    """磁盘上的字节根本不是一份存档（截断、乱码、不是 JSON）。"""


def _plain(value: Any, path: str = "state", depth: int = 0) -> Any:
    """校验并复制出一份纯数据。碰到活对象就响亮失败。

    顺带做归一化（元组 → 列表），这样"捕获出来的存档"和"从 JSON 读回来的
    存档"是同一个形状 —— 否则两者比较永远不相等，而这正是存档测试要比的东西。
    """
    if depth > 64:
        raise ArchiveError(f"{path}: 存档嵌套过深，大概率是循环引用")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ArchiveError(f"{path}: {value!r} 不是合法 JSON 数字")
        return value
    if isinstance(value, Mapping):
        plain = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ArchiveError(f"{path}: 字典的键必须是字符串，收到 {key!r}")
            plain[key] = _plain(item, f"{path}.{key}", depth + 1)
        return plain
    if isinstance(value, (list, tuple)):
        return [
            _plain(item, f"{path}[{index}]", depth + 1)
            for index, item in enumerate(value)
        ]
    raise ArchiveError(
        f"{path}: 存档里不能有 {type(value).__name__} —— 存档只装数据，"
        "服务实例、模型客户端、锁和回调都不进去"
    )


def _parse_clock(value, label: str) -> datetime:
    if isinstance(value, datetime):
        clock = value
    elif isinstance(value, str):
        try:
            clock = datetime.fromisoformat(value)
        except ValueError:
            raise ArchiveError(f"无法解析的{label}: {value!r}") from None
    else:
        raise ArchiveError(f"{label}必须是 ISO 时间字符串")
    if clock.tzinfo is not None:
        raise ArchiveError(f"{label}必须是不带时区的模拟时间")
    return clock


def _require_revision(revision) -> int:
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise ArchiveError("revision 必须是整数")
    if revision < 1:
        raise ArchiveError(f"revision 必须从 1 开始递增，收到 {revision}")
    return revision


def _cross_check(world_id: str, session_id: str, clock: datetime, state: Mapping) -> None:
    """信封说的和状态里写的必须是同一件事。"""
    if not isinstance(state, Mapping):
        raise ArchiveError("state 必须是字典")
    if state.get("session_id") != session_id:
        raise ArchiveError(
            f"信封说这是会话 '{session_id}' 的存档，里面的状态却属于 "
            f"'{state.get('session_id')}'"
        )
    world_payload = state.get("world_state") or {}
    if not isinstance(world_payload, Mapping) or "clock" not in world_payload:
        raise ArchiveError(
            f"世界 '{world_id}' 的存档缺少权威 WorldState —— 没有世界状态的"
            "会话不是一个可以被恢复的世界"
        )
    state_clock = _parse_clock(world_payload["clock"], "状态里的世界时钟")
    if state_clock != clock:
        raise ArchiveError(
            f"信封的时钟 {clock.isoformat()} 跟状态里的世界时钟 "
            f"{state_clock.isoformat()} 对不上"
        )


@dataclass(frozen=True)
class WorldArchive:
    """一个世界某一刻的完整存档，连同它的身份与版本。"""

    world_id: str
    session_id: str
    revision: int
    clock: datetime
    saved_at: str
    state: Dict = field(default_factory=dict)
    version: int = WORLD_ARCHIVE_VERSION

    # ── 捕获 ────────────────────────────────────────────────────────────
    @classmethod
    def capture(
        cls,
        world_id: str,
        state: SessionState,
        *,
        revision: int,
        saved_at: Optional[str] = None,
    ) -> "WorldArchive":
        """从一份活的权威状态捕获存档。

        调用方必须自己保证捕获的那一刻状态是自洽的（生命周期层是在 P11 的
        提交边界里取的快照）—— 这里只负责把它变成一份完整、纯数据的存档。
        """
        if not isinstance(state, SessionState):
            raise ArchiveError("只能从 SessionState 捕获世界存档")
        if state.world_state is None:
            raise ArchiveError("没有权威 WorldState 的会话不是一个世界")
        return cls.from_state_payload(
            world_id, state.to_dict(), revision=revision, saved_at=saved_at
        )

    @classmethod
    def from_state_payload(
        cls,
        world_id: str,
        payload: Mapping,
        *,
        revision: int,
        saved_at: Optional[str] = None,
    ) -> "WorldArchive":
        """从**已经取好的**状态快照建信封。

        生命周期层用这条路：快照必须在提交边界之内取，序列化和写盘可以在边界
        之外做。分开这两步，一次写盘就不会把停机和提交一起堵住。
        """
        world_id = validate_world_id(world_id)
        revision = _require_revision(revision)
        state = _plain(payload)
        session_id = state.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ArchiveError("状态里缺少 session_id")
        world_payload = state.get("world_state") or {}
        if not isinstance(world_payload, Mapping) or "clock" not in world_payload:
            raise ArchiveError("没有权威 WorldState 的会话不是一个世界")
        clock = _parse_clock(world_payload["clock"], "世界时钟")
        _cross_check(world_id, session_id, clock, state)
        return cls(
            world_id=world_id,
            session_id=session_id,
            revision=revision,
            clock=clock,
            saved_at=saved_at if saved_at is not None else datetime.now().isoformat(),
            state=state,
            version=WORLD_ARCHIVE_VERSION,
        )

    # ── 序列化 ──────────────────────────────────────────────────────────
    def to_dict(self) -> Dict:
        """完整公开形状；返回值是全新的可变结构，改它影响不到这份存档。"""
        return {
            "version": self.version,
            "world_id": self.world_id,
            "session_id": self.session_id,
            "revision": self.revision,
            "clock": self.clock.isoformat(),
            "saved_at": self.saved_at,
            "state": deepcopy(self.state),
        }

    @classmethod
    def from_dict(cls, payload) -> "WorldArchive":
        """从磁盘形状恢复信封。**只校验信封**，状态的校验在 restore_state()。"""
        if not isinstance(payload, Mapping):
            raise ArchiveError("世界存档必须是字典")
        for required in _ENVELOPE_FIELDS:
            if required not in payload:
                raise ArchiveError(f"世界存档缺少必填字段: {required}")

        version = payload["version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise ArchiveError("version 必须是整数")
        if version != WORLD_ARCHIVE_VERSION:
            raise ArchiveError(
                f"不支持的世界存档格式版本 {version}（本进程只认 "
                f"{WORLD_ARCHIVE_VERSION}）。升级存档是一次明确的人为决定，"
                "不是恢复路径可以替人做的猜测。"
            )

        world_id = validate_world_id(payload["world_id"])
        session_id = payload["session_id"]
        if not isinstance(session_id, str) or not session_id:
            raise ArchiveError("session_id 必须是非空字符串")
        revision = _require_revision(payload["revision"])
        clock = _parse_clock(payload["clock"], "存档时钟")
        saved_at = payload.get("saved_at")
        if saved_at is not None and not isinstance(saved_at, str):
            raise ArchiveError("saved_at 必须是 ISO 时间字符串")
        state = _plain(payload["state"])
        _cross_check(world_id, session_id, clock, state)
        return cls(
            world_id=world_id,
            session_id=session_id,
            revision=revision,
            clock=clock,
            saved_at=saved_at if saved_at is not None else "",
            state=state,
            version=version,
        )

    # ── 恢复 ────────────────────────────────────────────────────────────
    def restore_state(self) -> SessionState:
        """恢复出一份**冷**的权威状态：数据齐全，一个服务都没绑。

        绑定调度器 / Agency / 记忆 / 自主运行时是下一步，而且必须由调用方交出
        冷适配器来做（见 lifecycle.py）。分开这两步是刻意的：存档里没有、也
        不该有任何能变成一个活服务的东西。
        """
        try:
            state = SessionState.from_dict(deepcopy(self.state))
        except ArchiveError:
            raise
        except (ValueError, TypeError, KeyError, IndexError, RuntimeError) as e:
            # 敌对存档能让任何一层的构造函数抛任何一种异常。全部翻译成同一句
            # "这份存档不能用"，但**保留原文和原异常**：既不吞掉，也不让调用方
            # 去猜要 catch 哪十种错误。
            raise ArchiveError(
                f"世界 '{self.world_id}' 的存档没通过校验: {type(e).__name__}: {e}"
            ) from e
        if state.session_id != self.session_id:
            raise ArchiveError("恢复出来的会话身份跟信封对不上")
        if state.world_state is None or state.world_state.clock != self.clock:
            raise ArchiveError("恢复出来的世界时钟跟信封对不上")
        return state
