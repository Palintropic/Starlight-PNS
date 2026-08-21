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
# 记忆内容、类别资格和显著度是不是都能从观察重新推导出来，所以推导规则本身
# 就是存档格式的一部分。
#
#   1 → 2  摘要不再原样保留短台词（改为"结构描述 + 有上限的片段"），并且
#          恢复时要重判类别资格与显著度。版本 1 只在本分支存在过、从未发布，
#          没有需要迁移的真实存档，所以它直接被拒绝而不是就地升级。
MEMORY_ARCHIVE_VERSION = 2

# 字面片段的硬上限。它们是**模型层常量**而不是预算字段：存档恢复时要用同一
# 条规则重新推导内容来核对，两处用不同的上限就会把一份好存档判成损坏的。
#
# 三条一起保证"记忆里绝不会出现一句完整台词"：片段最多这么长、最多占原文
# 一半、短到取不出有意义片段时干脆一个字都不留。世界历史里有精确原文供审计，
# 记忆保留的是要点和少量有特征的措辞（架构文档 §18）。
FRAGMENT_CHARS = 24
FRAGMENT_RATIO = 2  # 最多取原文的 1/2
MIN_FRAGMENT = 4


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


def collapse(text) -> str:
    """折叠空白，拿到一段可以稳定推导的文本。"""
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())


def memory_fragment(text) -> str:
    """台词里留下来的那一小段有特征的措辞；短到取不出来就返回空串。

    **绝不是**逐字抄写，而且对短台词同样不是：能留下的长度既受 FRAGMENT_CHARS
    限制，也不能超过原文的一半，于是任何长度的台词都不可能被完整存进记忆。
    低于 MIN_FRAGMENT 时一个字都不留 —— 一句"好啊"截成"好"没有意义，不如
    只记"说了一句话"。
    """
    collapsed = collapse(text)
    allowed = min(FRAGMENT_CHARS, len(collapsed) // FRAGMENT_RATIO)
    if allowed < MIN_FRAGMENT:
        return ""
    return collapsed[:allowed] + "…"


def _said_phrase(text) -> str:
    """"说了什么"的结构化描述：规模 + 可选的片段，绝不含完整原文。"""
    collapsed = collapse(text)
    if not collapsed:
        return "说了句什么"
    length = len(collapsed)
    scale = "一句话" if length <= 20 else ("一段话" if length <= 80 else "很长一段")
    fragment = memory_fragment(collapsed)
    return f"说了{scale}：「{fragment}」" if fragment else f"说了{scale}"


def _where(perceived: Mapping) -> Dict:
    """这条观察里"在哪"的那部分；只取观察里真的有的锚点。"""
    where = {}
    for key in ("location_id", "channel_id"):
        value = perceived.get(key)
        if value is not None:
            where[key] = value
    return where


def describe_observation(observation: Observation) -> str:
    """一条观察在记忆里的摘要。确定性，不调用任何模型。

    刻意**不带行动者**：谁做的已经在内容的 about / by 字段里了，摘要只说
    "做了什么"。提示投影再把两者拼起来，于是同一条观察在不同类别下的正文
    完全一致，渲染时能合并成一行。
    """
    perceived = observation.perceived
    kind = perceived.get("type")
    channel = perceived.get("channel_id")
    location = perceived.get("location_id")
    if kind in _UTTERANCE_TYPES:
        return _said_phrase(perceived.get("text"))
    if kind == "presence.joined_channel":
        return f"进入了频道 {channel}" if channel else "上线了"
    if kind == "presence.left_channel":
        return f"离开了频道 {channel}" if channel else "下线了"
    if kind == "character.location_changed":
        return f"去了 {location}" if location else "换了地方"
    return "那边有点动静"


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


# ── 资格：这条观察该长出哪几类记忆 ──────────────────────────────────────
#
# 跟 memory_content() 一样，这里是**唯一**一处定义资格的地方：编码器照它产出
# 记忆，存档恢复照它重判。两处共用一份声明，验证就不可能比构造更松。
#
# 因此每一条规则的输入都必须能从观察本身推导出来。这不是洁癖：任何一个只有
# 编码那一刻才知道的信号（比如一张外部别名表），恢复时都重算不出来，于是那一
# 类记忆的资格就变成"存档说了算"—— 一句路人的闲话可以被改写成一条永不衰减、
# 预算也挤不掉的承诺，而每个字段单独看都合法。
#
# 代价写在这里：认不认得出"这句话是冲我说的"，目前只看角色 ID 和被点名的
# 参与者名单，不认显示名。显示名要参与判断，就得先让它成为观察里可验证的
# 一部分，而不是编码器手里的一张表。
COMMITMENT_MARKERS = (
    "约定",
    "答应",
    "保证",
    "一定会",
    "说好了",
    "約束",
    "必ず",
    "promise",
    "i will",
    "i'll",
)

# 情节记忆的门槛。低于它的观察只留短时痕迹 —— 听见路人说了句话，两小时后
# 想不起来是正常的。
EPISODIC_THRESHOLD = 20

# 能长出记忆的观察类型。跟曝光投影的 payload 白名单同一条规矩：没登记的类型
# 什么都不记。world.time_advanced 刻意不在里面 —— 时钟前进是系统心跳，不是
# 角色经验（而且曝光那一层根本不会为它生成观察）。
ENCODABLE_TYPES = (
    "dialogue.spoken",
    "message.sent",
    "presence.joined_channel",
    "presence.left_channel",
    "character.location_changed",
)


@dataclass(frozen=True)
class EncodingSignals:
    """从一条观察里读出来的编码信号。全部只依赖这条观察本身。"""

    actor_id: Optional[str]
    is_self: bool
    is_utterance: bool
    addressed: bool
    has_commitment: bool
    encodable: bool


def _mentions(text, owner_id: str) -> bool:
    collapsed = collapse(text).lower()
    return bool(collapsed and owner_id and owner_id.lower() in collapsed)


def read_signals(observation: Observation) -> EncodingSignals:
    """把一条观察读成编码信号。只看这条观察，不接受任何外部输入。"""
    if not isinstance(observation, Observation):
        raise MemoryError("只能从 Observation 读取编码信号")
    perceived = observation.perceived
    kind = perceived.get("type")
    actor = perceived.get("actor_id")
    owner = observation.observer_id
    text = perceived.get("text")
    participants = tuple(perceived.get("participants") or ())
    is_utterance = kind in _UTTERANCE_TYPES
    addressed = bool(
        actor
        and actor != owner
        and (_mentions(text, owner) or owner in participants)
    )
    has_commitment = bool(
        is_utterance
        and isinstance(text, str)
        and any(marker in text.lower() for marker in COMMITMENT_MARKERS)
    )
    return EncodingSignals(
        actor_id=actor,
        is_self=observation.reason is ExposureReason.SELF_ACTION,
        is_utterance=is_utterance,
        addressed=addressed,
        has_commitment=has_commitment,
        encodable=kind in ENCODABLE_TYPES,
    )


def derived_salience(observation: Observation) -> int:
    """这条观察的显著度：0..100 的整数。

    用整数是为了排序完全确定；从观察推导（而不是让编码器随便给一个数）是为了
    恢复时能重算 —— 否则把显著度改成 100 就能让一条无关的痕迹压过所有记忆。
    """
    signals = read_signals(observation)
    score = 0
    if signals.is_self:
        score += 40
    if signals.addressed:
        score += 30
    if signals.has_commitment:
        score += 20
    score += 10 if signals.is_utterance else 5
    return max(0, min(100, score))


def _wants_commitment(observation, s: EncodingSignals) -> bool:
    return bool(s.is_utterance and s.has_commitment and s.actor_id)


def _wants_identity(observation, s: EncodingSignals) -> bool:
    # 两种身份相关的经验：我自己说出口的承诺，以及别人冲着我说的话。
    if s.is_self and s.has_commitment:
        return True
    return bool(s.addressed and s.actor_id)


def _wants_relational(observation, s: EncodingSignals) -> bool:
    # 关系记忆问的是"这个人对我做了什么"，所以只在互动确实指向我时才产生；
    # 旁听到的一句话不会自动变成一条关系记忆。
    return bool(s.addressed and s.actor_id and s.actor_id != observation.observer_id)


def _wants_semantic(observation, s: EncodingSignals) -> bool:
    return world_fact(observation) is not None


def _wants_episodic(observation, s: EncodingSignals) -> bool:
    return derived_salience(observation) >= EPISODIC_THRESHOLD


def _wants_working(observation, s: EncodingSignals) -> bool:
    return True  # 白名单内的观察都留一条会过期的短时痕迹


# 顺序就是持久度顺序：一条观察长出的记忆超过预算时，从后往前丢。丢掉一条
# 短时痕迹的代价，比丢掉一条承诺小得多。
_RULES = (
    (MemoryClass.COMMITMENT, _wants_commitment),
    (MemoryClass.IDENTITY, _wants_identity),
    (MemoryClass.RELATIONAL, _wants_relational),
    (MemoryClass.SEMANTIC, _wants_semantic),
    (MemoryClass.EPISODIC, _wants_episodic),
    (MemoryClass.WORKING, _wants_working),
)

# 每个类别都必须有一条规则 —— 没有规则的类别是个空标签。用显式 raise 而不是
# assert：assert 在 -O 下会被剥掉，而这条是结构约束，不是调试断言。
if {memory_class for memory_class, _ in _RULES} != set(MemoryClass):
    raise MemoryError("每一个记忆类别都必须有一条编码规则")


def eligible_classes(observation: Observation) -> Tuple[MemoryClass, ...]:
    """这条观察够资格长出哪几类记忆，按持久度从高到低。纯函数。"""
    signals = read_signals(observation)
    if not signals.encodable:
        return ()
    eligible = []
    for memory_class, wants in _RULES:
        if not wants(observation, signals):
            continue
        try:
            memory_content(memory_class, observation)
        except MemoryError:
            # 规则说要，内容却推导不出来。这是"不记"，不是崩溃 —— 但它不该
            # 悄悄发生，所以两边的条件是对齐的，走到这里说明有一边被改松了。
            continue
        eligible.append(memory_class)
    return tuple(eligible)


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

    核对四件事，缺一件就有一种伪造能过关：

      1. 身份与时间对得上（谁的记忆、哪条事件、什么时候感知的）。
      2. **类别资格**：这条观察够不够格长出这一类记忆。少了这一条，把一句
         路人闲话的类别改成 commitment、再按新类别重算 ID 和内容，就能拼出
         一条永不衰减、预算也挤不掉的"承诺"，而每个字段单独看都合法。
      3. 内容与观察一致（谁说的、说了什么、事实取值）。
      4. **显著度**：它由观察推导，不是策略随便给的标量。少了这一条，把它
         改成 100 就能让一条无关的痕迹压过所有真正重要的记忆。

    provenance 里只核对可推导的那一项（感知通道）；encoder 名字是策略字符串，
    不是关于世界的断言，跟 P9 里 policy 的处理一致。
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
    eligible = eligible_classes(observation)
    if record.memory_class not in eligible:
        raise MemoryMismatch(
            f"记忆 '{record.memory_id}' 声称的类别 "
            f"'{record.memory_class.value}' 在这条观察上没有资格产生；"
            f"这条观察只够格长出 "
            f"{[c.value for c in eligible] if eligible else '（什么都不长）'}"
        )
    expected_salience = derived_salience(observation)
    if record.salience != expected_salience:
        raise MemoryMismatch(
            f"记忆 '{record.memory_id}' 的显著度 {record.salience} 与从源观察"
            f"推导出的 {expected_salience} 不一致"
        )
    reason = record.provenance.get("reason")
    if reason != observation.reason.value:
        raise MemoryMismatch(
            f"记忆 '{record.memory_id}' 记的感知通道 {reason!r} 与源观察的 "
            f"'{observation.reason.value}' 不一致"
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
    "COMMITMENT_MARKERS",
    "ENCODABLE_TYPES",
    "EPISODIC_THRESHOLD",
    "FRAGMENT_CHARS",
    "MEMORY_ARCHIVE_VERSION",
    "MIN_FRAGMENT",
    "ClassBehavior",
    "EncodingSignals",
    "MemoryClass",
    "MemoryError",
    "MemoryMismatch",
    "MemoryRecord",
    "MemoryStore",
    "collapse",
    "derive_memory_id",
    "derived_salience",
    "describe_observation",
    "eligible_classes",
    "memory_content",
    "memory_fragment",
    "read_signals",
    "verify_memory_against_observation",
    "world_fact",
]
