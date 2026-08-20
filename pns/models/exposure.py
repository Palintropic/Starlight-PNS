# pns/models/exposure.py — 曝光判定的结果类型
#
# Exposure 回答一个问题，并且只回答这一个：
#
#     这个角色**有没有可能**感知到这条已提交事件？
#
# 它不回答：角色在不在意（Attention）、要不要回应（Agency）、记不记得住
# （Memory）。这三件事都在后续阶段，任何一条渗进这一层都是设计错误。
#
# 这里只放"结果"这个数据类型；判定规则在 pns/runtime/exposure/rules.py。
# 分开是有意的：结果要跟着会话被序列化、被回滚、被 UI 读，所以它属于领域
# 模型层；规则要读 WorldState 做推导，属于运行时层。
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Set, Tuple

from pns.models.frozen import freeze_json_value, thaw_json_value


class ExposureError(ValueError):
    """曝光记录自身不合法（缺 ID、未知理由码、detail 非法等）。"""


class ExposureReason(Enum):
    """稳定的判定理由码。

    用枚举而不是散文，是因为这些码要被测试断言、被 UI 分类、被后续阶段
    当作输入。散文可以改，码不能随便改。

    每个码都对应规则里真实存在的一条分支 —— 这里不放"以后可能会用到"的
    占位码：一个没有规则支撑的理由码是在谎报覆盖面。
    """

    # ── 判定为可感知 ────────────────────────────────────────────────────
    SELF_ACTION = "self_action"  # 自己做的事，走自观察通道
    EXPLICIT_PARTICIPANT = "explicit_participant"  # 事件显式点名的参与者
    CHANNEL_MEMBER = "channel_member"  # 在该频道里
    SAME_LOCATION = "same_location"  # 就在事发地点
    AUDIBLE_FROM = "audible_from"  # 地点元数据声明的可闻范围
    PUBLIC_VISIBLE = "public_visible"  # 公开事件且当下就在感知范围内

    # ── 判定为不可感知 ──────────────────────────────────────────────────
    PRIVATE_SCOPE_DENIED = "private_scope_denied"  # 私密事件，同处一地也不给
    NOT_A_PARTICIPANT = "not_a_participant"  # 参与者范围事件，没被点名
    NO_CHANNEL_ACCESS = "no_channel_access"  # 不在该频道
    WRONG_LOCATION = "wrong_location"  # 不在事发地点
    PUBLIC_NOT_PERCEIVED = "public_not_perceived"  # 公开≠自动知道，当下没撞上
    UNAVAILABLE = "unavailable"  # 睡着了，感知不到外界
    UNKNOWN_CHARACTER = "unknown_character"  # 世界里没有这个角色

    @property
    def exposed(self) -> bool:
        return self in _EXPOSING_REASONS


_EXPOSING_REASONS = frozenset(
    {
        ExposureReason.SELF_ACTION,
        ExposureReason.EXPLICIT_PARTICIPANT,
        ExposureReason.CHANNEL_MEMBER,
        ExposureReason.SAME_LOCATION,
        ExposureReason.AUDIBLE_FROM,
        ExposureReason.PUBLIC_VISIBLE,
    }
)


@dataclass(frozen=True)
class ExposureDecision:
    """一次「某角色对某事件」的判定结果。

    可比较、可哈希、字段全部不可变：同一个事件 + 同一份世界快照必须产出
    完全相等的决策，这条不变量靠 dataclass 的结构相等直接可测。

    evaluated_at 记的是**模拟时钟**，不是墙上时间 —— 墙上时间会让"相同输入
    产出相同决策"这条不变量自己失效。
    """

    event_id: str
    character_id: str
    reason: ExposureReason
    evaluated_at: datetime
    detail: Mapping = field(default_factory=dict)

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ExposureError("event_id 必须是非空字符串")
        if not isinstance(self.character_id, str) or not self.character_id:
            raise ExposureError("character_id 必须是非空字符串")
        try:
            set_(self, "reason", ExposureReason(self.reason))
        except ValueError:
            raise ExposureError(f"未知的曝光理由码: {self.reason!r}") from None
        if not isinstance(self.evaluated_at, datetime):
            raise ExposureError("evaluated_at 必须是 datetime（模拟时钟时间）")
        if not isinstance(self.detail, Mapping):
            raise ExposureError("detail 必须是字典")
        set_(
            self,
            "detail",
            freeze_json_value(self.detail, path="detail", error=ExposureError),
        )

    def __hash__(self) -> int:
        # 冻结后的 detail 不可哈希，而一条判定的身份本来就是
        # (事件, 角色)：同一对只会有一条判定。按身份哈希，免得把决策放进
        # set()/dict() 时撞上一个跟真实问题无关的 TypeError。
        return hash((self.event_id, self.character_id))

    @property
    def exposed(self) -> bool:
        """判定结果本身就藏在理由码里，不额外存一个可能跟它对不上的布尔。"""
        return self.reason.exposed

    def to_dict(self) -> Dict:
        return {
            "event_id": self.event_id,
            "character_id": self.character_id,
            "exposed": self.exposed,
            "reason": self.reason.value,
            "evaluated_at": self.evaluated_at.isoformat(),
            "detail": thaw_json_value(self.detail),
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "ExposureDecision":
        return cls(
            event_id=payload["event_id"],
            character_id=payload["character_id"],
            reason=payload["reason"],
            evaluated_at=datetime.fromisoformat(payload["evaluated_at"]),
            detail=payload.get("detail", {}),
        )


class ExposureLog:
    """会话里所有曝光判定的只追加解释日志。

    这是**系统侧**数据：给测试、给调试 UI、给以后的审计看的。它绝对不能
    流进角色的主观上下文 —— 角色不该知道"有一件事我没被曝光到"。所以它跟
    ObservationLog 是两个容器，而不是同一个容器上的一个过滤器：物理隔离比
    "记得别读那半边" 可靠。
    """

    def __init__(self, decisions: Iterable[ExposureDecision] = ()):
        self._decisions: List[ExposureDecision] = []
        self._keys: Set[Tuple[str, str]] = set()
        for decision in decisions:
            self._append(decision)

    # ── 写入（只供提交边界使用） ────────────────────────────────────────
    def _append(self, decision: ExposureDecision) -> int:
        if not isinstance(decision, ExposureDecision):
            raise ExposureError("只能向曝光日志追加 ExposureDecision")
        key = (decision.event_id, decision.character_id)
        if key in self._keys:
            raise ExposureError(
                "曝光日志里已存在事件 "
                f"'{decision.event_id}' 对角色 '{decision.character_id}' 的判定"
            )
        self._decisions.append(decision)
        self._keys.add(key)
        return len(self._decisions) - 1

    def _rollback_to(self, length: int) -> None:
        if not isinstance(length, int) or isinstance(length, bool):
            raise ExposureError("回滚长度必须是整数")
        if length < 0 or length > len(self._decisions):
            raise ExposureError(f"回滚长度越界: {length}")
        del self._decisions[length:]
        self._keys = {
            (decision.event_id, decision.character_id)
            for decision in self._decisions
        }

    # ── 读取 ────────────────────────────────────────────────────────────
    def __len__(self) -> int:
        return len(self._decisions)

    def __iter__(self) -> Iterator[ExposureDecision]:
        return iter(tuple(self._decisions))

    def decisions(self) -> Tuple[ExposureDecision, ...]:
        return tuple(self._decisions)

    def for_event(self, event_id: str) -> Tuple[ExposureDecision, ...]:
        return tuple(d for d in self._decisions if d.event_id == event_id)

    def for_character(self, character_id: str) -> Tuple[ExposureDecision, ...]:
        return tuple(d for d in self._decisions if d.character_id == character_id)

    def explain(self, event_id: str, character_id: str) -> Optional[ExposureDecision]:
        """某个角色对某条事件的判定；同一对只会有一条。"""
        for decision in self._decisions:
            if decision.event_id == event_id and decision.character_id == character_id:
                return decision
        return None

    # ── 序列化 ──────────────────────────────────────────────────────────
    def to_dict(self) -> Dict:
        return {"decisions": [d.to_dict() for d in self._decisions]}

    @classmethod
    def from_dict(cls, payload: Dict) -> "ExposureLog":
        if not isinstance(payload, dict):
            raise ExposureError("曝光日志必须是字典")
        entries = payload.get("decisions", [])
        if not isinstance(entries, list):
            raise ExposureError("曝光日志的 decisions 必须是数组")
        return cls(ExposureDecision.from_dict(entry) for entry in entries)
