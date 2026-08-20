# pns/models/observation.py — 事件在单个角色视角下的投影
#
# Observation 是"这个角色感知到了什么"，不是"世界上发生了什么"（那是 Event），
# 也不是"这个角色记住了什么"（那是 Memory，后续阶段）。三者必须分开：
#
#     世界历史  ≠  角色观察  ≠  角色记忆
#
# 一条观察里只允许出现该角色确实感知得到的信息。事件里那些属于系统侧的字段
# —— provenance（哪个模型生成的、Router 打了几分、是不是 OOC）、correlation_id、
# causation_id —— 一律不进观察：系统过程不等于角色经验。
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Set, Tuple

from pns.models.exposure import ExposureReason
from pns.models.frozen import freeze_json_value, thaw_json_value


class ObservationError(ValueError):
    """观察记录自身不合法（缺 ID、未知理由码、perceived 非法等）。"""


@dataclass(frozen=True)
class Observation:
    """一条已提交事件在某个角色感知里的样子。"""

    source_event_id: str
    observer_id: str
    reason: ExposureReason
    observed_at: datetime
    perceived: Mapping = field(default_factory=dict)

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        if not isinstance(self.source_event_id, str) or not self.source_event_id:
            raise ObservationError("source_event_id 必须是非空字符串")
        if not isinstance(self.observer_id, str) or not self.observer_id:
            raise ObservationError("observer_id 必须是非空字符串")
        try:
            reason = ExposureReason(self.reason)
        except ValueError:
            raise ObservationError(f"未知的曝光理由码: {self.reason!r}") from None
        if not reason.exposed:
            # 没被曝光却生成了观察，是这一层最严重的错误：角色会"知道"一件
            # 它感知不到的事。这里直接拦死，不留一条靠调用方自觉的路。
            raise ObservationError(
                f"理由码 {reason.value} 表示未曝光，不能据此生成观察"
            )
        set_(self, "reason", reason)
        if not isinstance(self.observed_at, datetime):
            raise ObservationError("observed_at 必须是 datetime（模拟时钟时间）")
        if not isinstance(self.perceived, Mapping):
            raise ObservationError("perceived 必须是字典")
        set_(
            self,
            "perceived",
            freeze_json_value(self.perceived, path="perceived", error=ObservationError),
        )

    def __hash__(self) -> int:
        # 同 ExposureDecision：perceived 冻结后不可哈希，身份是 (事件, 观察者)。
        return hash((self.source_event_id, self.observer_id))

    @property
    def is_self_observation(self) -> bool:
        return self.reason is ExposureReason.SELF_ACTION

    def render_line(self) -> Optional[str]:
        """渲染成遗留角色历史里的那一行；不是台词类观察就返回 None。

        放在模型上而不是运行时投影层，是为了让 SessionState 能在不反向依赖
        pns.runtime 的前提下，把兼容用的角色历史真正**从观察推导出来**，
        而不是另起一套按角色 ID 复制文本的逻辑。
        """
        text = self.perceived.get("text")
        if not isinstance(text, str) or not text:
            return None
        speaker = self.perceived.get("char_name") or self.perceived.get("actor_id")
        return f"{speaker}：{text}" if speaker else text

    def to_dict(self) -> Dict:
        return {
            "source_event_id": self.source_event_id,
            "observer_id": self.observer_id,
            "reason": self.reason.value,
            "observed_at": self.observed_at.isoformat(),
            "perceived": thaw_json_value(self.perceived),
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "Observation":
        return cls(
            source_event_id=payload["source_event_id"],
            observer_id=payload["observer_id"],
            reason=payload["reason"],
            observed_at=datetime.fromisoformat(payload["observed_at"]),
            perceived=payload.get("perceived", {}),
        )


class ObservationLog:
    """会话里所有角色观察的只追加日志。

    按角色分流的读取接口在这里，但存储是一条时间线：观察之间的先后顺序
    本身就是信息，按角色拆成几个列表反而会把它丢掉。
    """

    def __init__(self, observations: Iterable[Observation] = ()):
        self._observations: List[Observation] = []
        self._keys: Set[Tuple[str, str]] = set()
        for observation in observations:
            self._append(observation)

    # ── 写入（只供提交边界使用） ────────────────────────────────────────
    def _append(self, observation: Observation) -> int:
        if not isinstance(observation, Observation):
            raise ObservationError("只能向观察日志追加 Observation")
        key = (observation.source_event_id, observation.observer_id)
        if key in self._keys:
            raise ObservationError(
                "观察日志里已存在事件 "
                f"'{observation.source_event_id}' 对角色 "
                f"'{observation.observer_id}' 的观察"
            )
        self._observations.append(observation)
        self._keys.add(key)
        return len(self._observations) - 1

    def _rollback_to(self, length: int) -> None:
        if not isinstance(length, int) or isinstance(length, bool):
            raise ObservationError("回滚长度必须是整数")
        if length < 0 or length > len(self._observations):
            raise ObservationError(f"回滚长度越界: {length}")
        del self._observations[length:]
        self._keys = {
            (observation.source_event_id, observation.observer_id)
            for observation in self._observations
        }

    # ── 读取 ────────────────────────────────────────────────────────────
    def __len__(self) -> int:
        return len(self._observations)

    def __iter__(self) -> Iterator[Observation]:
        return iter(tuple(self._observations))

    def observations(self) -> Tuple[Observation, ...]:
        return tuple(self._observations)

    def for_character(self, character_id: str) -> Tuple[Observation, ...]:
        """某个角色按感知顺序看到的一切 —— 后续记忆管线的唯一入口。"""
        return tuple(o for o in self._observations if o.observer_id == character_id)

    def for_event(self, event_id: str) -> Tuple[Observation, ...]:
        return tuple(o for o in self._observations if o.source_event_id == event_id)

    def observers_of(self, event_id: str) -> Tuple[str, ...]:
        return tuple(o.observer_id for o in self.for_event(event_id))

    # ── 序列化 ──────────────────────────────────────────────────────────
    def to_dict(self) -> Dict:
        return {"observations": [o.to_dict() for o in self._observations]}

    @classmethod
    def from_dict(cls, payload: Dict) -> "ObservationLog":
        if not isinstance(payload, dict):
            raise ObservationError("观察日志必须是字典")
        entries = payload.get("observations", [])
        if not isinstance(entries, list):
            raise ObservationError("观察日志的 observations 必须是数组")
        return cls(Observation.from_dict(entry) for entry in entries)
