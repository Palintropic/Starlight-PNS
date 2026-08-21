# pns/models/memory.py — 角色主观记忆的结果类型与存储
#
# Memory 回答一个问题，并且只回答这一个：
#
#     这个角色从它**感知到的**东西里，留下了什么？
#
# 它不回答：世界上发生了什么（Event）、这个角色感知得到什么（Exposure /
# Observation）、此刻想起什么（Recall）。四者是四种数据产品：
#
#     世界历史  ≠  角色观察  ≠  角色记忆  ≠  当下召回
#
# 一条记忆只能由**这个角色自己的一条观察**长出来。曝光拒绝日志、事件历史里
# 没被投影出来的字段、别的角色的观察或记忆，一律不是合法输入 —— 那些渗进来
# 一条，角色就"知道"了一件它感知不到的事。
#
# 记录不可变，存储只追加。衰减不是回写：过期的 working 记忆仍然原样躺在存储
# 里，只是召回投影不再取它（见 pns/runtime/memory/recall.py）。"提示词换个问法"
# 永远不该改变已经存下来的记忆。
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Set, Tuple

from pns.models.exposure import ExposureReason
from pns.models.frozen import freeze_json_value, thaw_json_value
from pns.models.observation import Observation

# 记忆存档的格式版本。形状或派生规则一旦变化就必须进位，并在
# docs/ARCHITECTURE.md 的迁移说明里写清楚旧存档怎么处理 —— 恢复时会核对
# 记忆内容是不是能从观察重新推导出来，所以推导规则本身就是存档格式的一部分。
MEMORY_ARCHIVE_VERSION = 1

# 摘要的硬上限。它是**模型层常量**而不是预算字段：存档恢复时要用同一条规则
# 重新推导摘要来核对内容，两处用不同的上限就会把一份好存档判成损坏的。
GIST_CHARS = 80


# 这个名字**刻意**遮蔽了内建的 MemoryError（内存耗尽），跟 EventError /
# ExposureError / AgencyError 保持同一套命名。代价是：想捕获它的模块必须显式
# `from pns.models.memory import MemoryError` —— 忘了导入的话捕获到的是内建那个，
# 什么也拦不住。本阶段所有捕获点都显式导入，测试也盯着这一点。
class MemoryError(ValueError):
    """记忆记录或存储自身不合法（缺 ID、未知类别、内容非法等）。"""


class MemoryMismatch(MemoryError):
    """记忆内容与它自称的源观察对不上（存档被拼接或篡改）。"""


@dataclass(frozen=True)
class ClassBehavior:
    """一个记忆类别的**真实行为**，不是一个标签。

    三项都有执行分支盯着：衰减窗口决定它还能不能被召回，pinned 决定预算能不能
    把它挤掉，权重决定它在召回排序里的起点。没有行为的类别不进这个枚举。
    """

    # 编码之后多少模拟分钟内还召得回来；None = 不衰减。
    decay_minutes: Optional[int]
    # True = 召回预算优先保留它，挤不掉。
    pinned: bool
    # 召回打分的基础权重。
    recall_weight: int


class MemoryClass(str, Enum):
    """已实现的最小记忆类别集合。

    每一个都对应 pns/runtime/memory/encoding.py 里一条真实的编码规则，以及
    下面 _CLASS_BEHAVIOR 里一条真实的行为声明。有测试盯着这三者一一对应。
    """

    WORKING = "working"  # 短时痕迹：什么都记一下，但会过期
    EPISODIC = "episodic"  # 情节：与自己有关的那些发生
    RELATIONAL = "relational"  # 关系：某个人对我做了什么
    COMMITMENT = "commitment"  # 承诺：说出口的约定，永不衰减、挤不掉
    SEMANTIC = "semantic"  # 世界事实：谁在哪、谁在哪个频道
    IDENTITY = "identity"  # 身份相关：我承诺了什么、别人怎么说我

    @property
    def behavior(self) -> ClassBehavior:
        return _CLASS_BEHAVIOR[self]

    @property
    def decay_minutes(self) -> Optional[int]:
        return self.behavior.decay_minutes

    @property
    def pinned(self) -> bool:
        return self.behavior.pinned

    @property
    def recall_weight(self) -> int:
        return self.behavior.recall_weight


_CLASS_BEHAVIOR: Dict[MemoryClass, ClassBehavior] = {
    # 短时痕迹两小时后就召不回来了。这不是"删掉"：记录还在存储里，只是不再
    # 进入召回投影 —— 编码与召回分开，衰减属于召回那一侧。
    MemoryClass.WORKING: ClassBehavior(decay_minutes=120, pinned=False, recall_weight=10),
    MemoryClass.EPISODIC: ClassBehavior(decay_minutes=None, pinned=False, recall_weight=20),
    MemoryClass.SEMANTIC: ClassBehavior(decay_minutes=None, pinned=False, recall_weight=25),
    MemoryClass.RELATIONAL: ClassBehavior(decay_minutes=None, pinned=False, recall_weight=30),
    # 承诺与身份相关的经验有更强的持久化保证（架构文档 §17）：不衰减，
    # 而且召回预算优先给它们留位置。
    MemoryClass.IDENTITY: ClassBehavior(decay_minutes=None, pinned=True, recall_weight=50),
    MemoryClass.COMMITMENT: ClassBehavior(decay_minutes=None, pinned=True, recall_weight=60),
}


# ── 从观察推导记忆内容 ──────────────────────────────────────────────────
#
# 这一段是**唯一**一处定义"一条记忆该长什么样"的地方：编码器拿它构造内容，
# 存档恢复拿它核对内容。两处共用一份声明，验证就不可能比构造更松。
# （跟 pns/models/action.py 的 agency_event_fields()/verify_agency_event() 同一条规矩。）
_UTTERANCE_TYPES = ("dialogue.spoken", "message.sent")


def memory_gist(text) -> str:
    """台词的语义摘要。

    **不是**逐字抄写：空白折叠，超过 GIST_CHARS 就截断。世界历史保留精确原文
    供审计（架构文档 §18），角色记忆保留的是要点和少量有特征的措辞。
    """
    if not isinstance(text, str):
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) <= GIST_CHARS:
        return collapsed
    return collapsed[:GIST_CHARS] + "…"


def _where(perceived: Mapping) -> Dict:
    """这条观察里"在哪"的那部分；只取观察里真的有的锚点。"""
    where = {}
    for key in ("location_id", "channel_id"):
        value = perceived.get(key)
        if value is not None:
            where[key] = value
    return where


def describe_observation(observation: Observation) -> str:
    """一条观察在记忆里的一句话摘要。确定性，不调用任何模型。"""
    perceived = observation.perceived
    kind = perceived.get("type")
    actor = perceived.get("actor_id") or "某人"
    if kind in _UTTERANCE_TYPES:
        return memory_gist(perceived.get("text"))
    channel = perceived.get("channel_id")
    location = perceived.get("location_id")
    if kind == "presence.joined_channel":
        return f"{actor} 进入了频道 {channel}" if channel else f"{actor} 上线了"
    if kind == "presence.left_channel":
        return f"{actor} 离开了频道 {channel}" if channel else f"{actor} 下线了"
    if kind == "character.location_changed":
        return f"{actor} 去了 {location}" if location else f"{actor} 换了地方"
    return f"{actor} 那边发生了点什么"


def world_fact(observation: Observation) -> Optional[Tuple[str, str]]:
    """这条观察里可以当成世界事实存下来的 (fact, value)；没有就返回 None。

    事实的身份里带着它所描述的对象，值是当下的取值。同一个事实被重新观察到
    同一个值不再产生新记忆（幂等），值变了才产生新的一条。
    """
    perceived = observation.perceived
    kind = perceived.get("type")
    actor = perceived.get("actor_id")
    if not actor:
        return None
    if kind == "character.location_changed":
        location = perceived.get("location_id")
        return (f"location:{actor}", location) if location else None
    if kind in ("presence.joined_channel", "presence.left_channel"):
        channel = perceived.get("channel_id")
        if not channel:
            return None
        value = "in" if kind == "presence.joined_channel" else "out"
        return (f"channel:{actor}:{channel}", value)
    return None


def memory_content(memory_class, observation: Observation) -> Dict:
    """某个类别的记忆在这条观察上应该长成什么样。

    只用观察里已经存在的东西推导 —— 于是存档恢复时能一字不差地重新算一遍。
    编码期那些**不进内容**的信号（比如"这句话点到了我"，需要角色别名表才能
    判断）只影响规则触不触发和 salience，不影响这里的结果。
    """
    memory_class = MemoryClass(memory_class)
    if not isinstance(observation, Observation):
        raise MemoryError("推导记忆内容需要一条 Observation")

    perceived = observation.perceived
    actor = perceived.get("actor_id")
    is_self = observation.reason is ExposureReason.SELF_ACTION
    summary = describe_observation(observation)
    where = _where(perceived)

    if memory_class is MemoryClass.WORKING:
        content = {"kind": "trace", "about": actor, "summary": summary}
        if where:
            content["where"] = where
        return content
    if memory_class is MemoryClass.EPISODIC:
        content = {
            "kind": "episode",
            "about": actor,
            "summary": summary,
            "self": is_self,
        }
        if where:
            content["where"] = where
        return content
    if memory_class is MemoryClass.RELATIONAL:
        if not actor or actor == observation.observer_id:
            raise MemoryError("关系记忆必须关于另一个角色")
        return {"kind": "interaction", "about": actor, "summary": summary}
    if memory_class is MemoryClass.COMMITMENT:
        if not actor:
            raise MemoryError("承诺记忆必须有做出承诺的人")
        return {"kind": "commitment", "by": actor, "summary": summary, "self": is_self}
    if memory_class is MemoryClass.SEMANTIC:
        fact = world_fact(observation)
        if fact is None:
            raise MemoryError("这条观察里没有可以存成世界事实的东西")
        return {"kind": "world_fact", "about": actor, "fact": fact[0], "value": fact[1]}
    if memory_class is MemoryClass.IDENTITY:
        return {
            "kind": "self_relevant",
            "about": actor,
            "summary": summary,
            # 两种来源都由观察本身决定，不依赖别名表：自己说出口的承诺，
            # 以及别人对着我说的话。
            "source": "self_commitment" if is_self else "addressed_by_other",
        }
    raise MemoryError(f"未实现的记忆类别: {memory_class!r}")


def derive_memory_id(owner_id: str, source_event_id: str, memory_class) -> str:
    """记忆的身份：由 (拥有者, 源观察, 类别) 推导，不随机生成。

    重复编码因此天然幂等 —— 同一条观察再编码一次算出的是同一个 ID，存储的
    唯一性约束会挡住它。存档往返之后它也还是同一个 ID（跟 ActivationDue.due_id
    同样的理由）。
    """
    memory_class = MemoryClass(memory_class)
    return f"{owner_id}@{source_event_id}#{memory_class.value}"


@dataclass(frozen=True)
class MemoryRecord:
    """一条角色留下来的记忆。

    身份、来源、时间、内容全部不可变。内容在构造时深冻结：拿到记录的人改不动
    权威存储里的那一份。
    """

    owner_id: str
    memory_class: MemoryClass
    source_event_id: str
    observed_at: datetime
    encoded_at: datetime
    content: Mapping
    salience: int = 0
    provenance: Mapping = field(default_factory=dict)

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        if not isinstance(self.owner_id, str) or not self.owner_id:
            raise MemoryError("owner_id 必须是非空字符串")
        if not isinstance(self.source_event_id, str) or not self.source_event_id:
            raise MemoryError("source_event_id 必须是非空字符串")
        try:
            set_(self, "memory_class", MemoryClass(self.memory_class))
        except ValueError:
            raise MemoryError(f"未知的记忆类别: {self.memory_class!r}") from None

        for name in ("observed_at", "encoded_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime):
                raise MemoryError(f"{name} 必须是 datetime（模拟时钟时间）")
            if value.tzinfo is not None:
                raise MemoryError(f"{name} 必须是 timezone-naive 的模拟时间")
        if self.encoded_at < self.observed_at:
            # 在感知到之前就记住了 —— 那是两段来自不同时刻的状态被拼在了一起。
            raise MemoryError(
                f"编码时间 {self.encoded_at.isoformat()} 早于观察时间 "
                f"{self.observed_at.isoformat()}"
            )

        if isinstance(self.salience, bool) or not isinstance(self.salience, int):
            raise MemoryError("salience 必须是整数")
        if not 0 <= self.salience <= 100:
            raise MemoryError(f"salience 必须落在 0..100，收到 {self.salience}")

        if not isinstance(self.content, Mapping) or not self.content:
            raise MemoryError("content 必须是非空字典")
        if not isinstance(self.content.get("kind"), str) or not self.content["kind"]:
            raise MemoryError("content 必须声明非空的 kind")
        if not isinstance(self.provenance, Mapping):
            raise MemoryError("provenance 必须是字典")
        set_(
            self,
            "content",
            freeze_json_value(self.content, path="content", error=MemoryError),
        )
        set_(
            self,
            "provenance",
            freeze_json_value(self.provenance, path="provenance", error=MemoryError),
        )

    def __hash__(self) -> int:
        return hash(self.memory_id)

    @property
    def source_observation_id(self) -> str:
        """这条记忆长自哪一条观察。观察的身份就是 (观察者, 事件)。"""
        return f"{self.owner_id}@{self.source_event_id}"

    @property
    def memory_id(self) -> str:
        return derive_memory_id(self.owner_id, self.source_event_id, self.memory_class)

    @property
    def pinned(self) -> bool:
        return self.memory_class.pinned

    def is_decayed_at(self, now: datetime) -> bool:
        """到了这一刻还召不召得回来。**只读**：衰减不改写记录本身。"""
        if not isinstance(now, datetime):
            raise MemoryError("now 必须是 datetime（模拟时钟时间）")
        window = self.memory_class.decay_minutes
        if window is None:
            return False
        return (now - self.encoded_at).total_seconds() > window * 60

    def to_dict(self) -> Dict:
        return {
            "memory_id": self.memory_id,
            "source_observation_id": self.source_observation_id,
            "owner_id": self.owner_id,
            "memory_class": self.memory_class.value,
            "source_event_id": self.source_event_id,
            "observed_at": self.observed_at.isoformat(),
            "encoded_at": self.encoded_at.isoformat(),
            "salience": self.salience,
            "content": thaw_json_value(self.content),
            "provenance": thaw_json_value(self.provenance),
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "MemoryRecord":
        if not isinstance(payload, Mapping):
            raise MemoryError("记忆记录必须是字典")
        for required in (
            "owner_id",
            "memory_class",
            "source_event_id",
            "observed_at",
            "encoded_at",
            "content",
        ):
            if required not in payload:
                raise MemoryError(f"记忆记录缺少必填字段: {required}")
        record = cls(
            owner_id=payload["owner_id"],
            memory_class=payload["memory_class"],
            source_event_id=payload["source_event_id"],
            observed_at=_parse_time(payload["observed_at"], "observed_at"),
            encoded_at=_parse_time(payload["encoded_at"], "encoded_at"),
            content=payload["content"],
            salience=payload.get("salience", 0),
            provenance=payload.get("provenance", {}),
        )
        # 存档里回显的派生身份必须跟字段推导出来的一致：对不上说明有人改了
        # 字段却留着旧 ID（或者反过来）。
        for label, stored, derived in (
            ("memory_id", payload.get("memory_id"), record.memory_id),
            (
                "source_observation_id",
                payload.get("source_observation_id"),
                record.source_observation_id,
            ),
        ):
            if stored is not None and stored != derived:
                raise MemoryError(
                    f"记忆记录的 {label} '{stored}' 与字段推导出的 '{derived}' 不一致"
                )
        return record


def _parse_time(value, label: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            raise MemoryError(f"无法解析的 {label}: {value!r}") from None
    raise MemoryError(f"{label} 必须是 ISO 时间字符串")


def verify_memory_against_observation(
    record: MemoryRecord, observation: Observation
) -> None:
    """这条记忆确实是从这条观察长出来的吗？

    存档恢复时用。只对上 ID 是不够的：ID 由字段推导，把 ID 保持正确、却改掉
    记忆内容（谁说的、说了什么、事实的取值），就能拼出一份"记忆说 A、观察说 B"
    而两边 ID 又对得上的存档。所以这里核对的是**内容**，走的是当初构造它的
    那段声明（memory_content），不是另写一套更松的规则。

    salience 不在核对范围内：它是编码策略给的一个标量，不是关于世界的断言
    （范围由 MemoryRecord 自己校验）。这跟 P9 里 policy 字符串的处理一致。
    """
    if not isinstance(observation, Observation):
        raise MemoryMismatch("核对记忆需要一条 Observation")
    if observation.observer_id != record.owner_id:
        raise MemoryMismatch(
            f"记忆 '{record.memory_id}' 属于 '{record.owner_id}'，源观察属于 "
            f"'{observation.observer_id}'"
        )
    if observation.source_event_id != record.source_event_id:
        raise MemoryMismatch(
            f"记忆 '{record.memory_id}' 指向事件 '{record.source_event_id}'，"
            f"源观察指向 '{observation.source_event_id}'"
        )
    if observation.observed_at != record.observed_at:
        raise MemoryMismatch(
            f"记忆 '{record.memory_id}' 记的观察时间 "
            f"{record.observed_at.isoformat()} 与源观察的 "
            f"{observation.observed_at.isoformat()} 不一致"
        )
    try:
        expected = memory_content(record.memory_class, observation)
    except MemoryError as e:
        raise MemoryMismatch(
            f"记忆 '{record.memory_id}' 的类别在这条观察上根本推导不出来: {e}"
        ) from e
    actual = thaw_json_value(record.content)
    if actual != expected:
        raise MemoryMismatch(
            f"记忆 '{record.memory_id}' 的内容与源观察对不上：期望 {expected!r}，"
            f"存档里是 {actual!r}"
        )


class MemoryStore:
    """一个会话里唯一一份角色记忆存储。

    只追加、不改写。对外只提供读取；带下划线的写入/回滚方法只供编码事务使用
    —— 绕开事务写进来的记忆回滚不掉，会留下"记住了一件没发生过的事"。

    存储本身是系统侧容器（它装着所有角色的记忆）。任何面向角色的读取都必须
    经过 for_owner()：召回层拿不到别的入口，也有测试盯着这一点。
    """

    def __init__(self, records: Iterable[MemoryRecord] = ()):
        self._records: List[MemoryRecord] = []
        self._ids: Set[str] = set()
        for record in records:
            self._append(record)

    # ── 写入（只供编码事务使用） ────────────────────────────────────────
    def _check_can_append(self, record: MemoryRecord) -> None:
        if not isinstance(record, MemoryRecord):
            raise MemoryError("只能向记忆存储追加 MemoryRecord")
        if record.memory_id in self._ids:
            raise MemoryError(f"重复的 memory_id: {record.memory_id}")
        if self._records and record.encoded_at < self._records[-1].encoded_at:
            # 编码时刻只会随模拟时钟前进。倒流意味着有人在拼接两个时刻的状态。
            raise MemoryError(
                f"记忆 '{record.memory_id}' 的编码时间 "
                f"{record.encoded_at.isoformat()} 早于上一条 "
                f"{self._records[-1].encoded_at.isoformat()}"
            )

    def _append(self, record: MemoryRecord) -> int:
        self._check_can_append(record)
        self._records.append(record)
        self._ids.add(record.memory_id)
        return len(self._records) - 1

    def _rollback_to(self, length: int) -> None:
        if not isinstance(length, int) or isinstance(length, bool):
            raise MemoryError("回滚长度必须是整数")
        if length < 0 or length > len(self._records):
            raise MemoryError(f"回滚长度越界: {length}")
        for record in self._records[length:]:
            self._ids.discard(record.memory_id)
        del self._records[length:]

    # ── 读取 ────────────────────────────────────────────────────────────
    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[MemoryRecord]:
        return iter(tuple(self._records))

    def has(self, memory_id: str) -> bool:
        return memory_id in self._ids

    def get(self, memory_id: str) -> MemoryRecord:
        for record in self._records:
            if record.memory_id == memory_id:
                return record
        raise MemoryError(f"未知的 memory_id: {memory_id}")

    def records(self) -> Tuple[MemoryRecord, ...]:
        """全部记忆。**系统侧读取**：给序列化、存档校验和调试用。

        召回一律走 for_owner() —— 角色作用域的收窄必须是一个显式的调用点，
        不能是"记得别读全量"。
        """
        return tuple(self._records)

    def for_owner(self, owner_id: str) -> Tuple[MemoryRecord, ...]:
        """某个角色留下的全部记忆，按编码顺序。召回层唯一的入口。"""
        return tuple(r for r in self._records if r.owner_id == owner_id)

    def owners(self) -> Tuple[str, ...]:
        return tuple(sorted({record.owner_id for record in self._records}))

    def for_class(self, memory_class) -> Tuple[MemoryRecord, ...]:
        memory_class = MemoryClass(memory_class)
        return tuple(r for r in self._records if r.memory_class is memory_class)

    def latest(self) -> Optional[MemoryRecord]:
        return self._records[-1] if self._records else None

    # ── 序列化 ──────────────────────────────────────────────────────────
    def to_dict(self) -> Dict:
        return {
            "records": [
                {"sequence": index, **record.to_dict()}
                for index, record in enumerate(self._records)
            ]
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "MemoryStore":
        if not isinstance(payload, Mapping):
            raise MemoryError("记忆存储必须是字典")
        entries = payload.get("records", [])
        if not isinstance(entries, list):
            raise MemoryError("记忆存储的 records 必须是数组")
        store = cls()
        for expected, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                raise MemoryError(f"记忆存储第 {expected} 项必须是字典")
            sequence = entry.get("sequence")
            if (
                not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or sequence != expected
            ):
                raise MemoryError(
                    f"记忆存储 sequence 不连续：第 {expected} 项收到 {sequence!r}"
                )
            store._append(MemoryRecord.from_dict(entry))
        return store


__all__ = [
    "ClassBehavior",
    "GIST_CHARS",
    "MEMORY_ARCHIVE_VERSION",
    "MemoryClass",
    "MemoryError",
    "MemoryMismatch",
    "MemoryRecord",
    "MemoryStore",
    "derive_memory_id",
    "describe_observation",
    "memory_content",
    "memory_gist",
    "verify_memory_against_observation",
    "world_fact",
]
