# pns/models/action.py — 角色能做的事的类型化目录
#
# 这个模块回答的问题是：**一个角色在这个世界里，有哪些"动作"是这套运行时
# 真的知道怎么执行的**。
#
# 它不回答：角色想不想动（Agency）、该说什么（生成层）、说得像不像
# （Router）。目录只声明"存在这么一种动作、它需要什么、它落成哪一条事件"。
#
# 为什么必须是目录而不是字典：P5 的提交边界已经保证"payload 永远不会被当成
# 要写进世界状态的字典"，每种事件类型走各自写死的状态效果。动作层要守住同一
# 条线 —— 一个策略（尤其是模型驱动的策略）交回来的任意字典，不能因为里面
# 恰好有个 `clock` 键就动到世界时钟。所以：
#
#   1. 动作 ID 是枚举，不是字符串；目录外的动作不存在。
#   2. 每个动作显式声明它的目标类型（无 / 地点 / 频道）与传播边界。
#   3. 每个动作显式声明允许出现的 payload 键，多一个键就整条拒绝 ——
#      悄悄丢掉未知键会让调用方以为它生效了。
#   4. 每个动作显式声明前置条件，条件本身也是枚举（求值实现在
#      pns/runtime/agency/preconditions.py，跟曝光"结果在 models、规则在
#      runtime"是同一种分法）。
#
# 目录跟 EventType 一样刻意保持很小：一个动作算"已实现"，标准是它被接受之后
# 提交边界真的知道该把世界改成什么样。没有状态效果的动作只是个占位符。
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Mapping, Optional, Tuple
from uuid import uuid4

from pns.models.event import EventScope, EventType
from pns.models.frozen import freeze_json_value, thaw_json_value


class ActionError(ValueError):
    """动作提案本身不合法（未知动作、目标不符、payload 键没声明等）。"""


class TargetKind(str, Enum):
    """一个动作需要什么样的目标。

    "不需要目标"也是一种声明，不是"随便给不给"：`speak.here` 的落点由角色
    当前位置决定，给它一个 location_id 意味着调用方以为自己能指定说话地点，
    那是另一个动作（还不存在）。这种误解越早失败越好。
    """

    NONE = "none"
    LOCATION = "location"
    CHANNEL = "channel"


class Precondition(str, Enum):
    """动作的前置条件码。

    跟 ExposureReason 同一条规矩：每个码都对应
    pns/runtime/agency/preconditions.py 里真实存在的一个求值分支，没有
    "以后可能会用到"的占位码 —— 那是在谎报覆盖面。

    前置条件只描述**世界**的要求。"必须带一段文本"之类的形状要求属于
    payload 声明，不在这里，因为它跟世界状态无关，也不会因为时间流逝而失效。
    """

    ACTOR_KNOWN = "actor_known"  # 世界认识这个角色
    ACTOR_AWAKE = "actor_awake"  # 没睡着（睡着的人不会自己行动）
    ACTOR_HAS_LOCATION = "actor_has_location"  # 人得在某个地方
    TARGET_LOCATION_EXISTS = "target_location_exists"
    TARGET_LOCATION_IS_ELSEWHERE = "target_location_is_elsewhere"  # 不是当前所在地
    TARGET_LOCATION_REACHABLE = "target_location_reachable"  # 位置图上直接相邻
    TARGET_CHANNEL_EXISTS = "target_channel_exists"
    ACTOR_IN_TARGET_CHANNEL = "actor_in_target_channel"
    ACTOR_NOT_IN_TARGET_CHANNEL = "actor_not_in_target_channel"


class ActionId(str, Enum):
    """本阶段有完整、已实现语义的动作。

    每一条都落到一个已有状态效果的 EventType 上。想加新动作，先问它被接受
    之后世界要变成什么样 —— 答不上来的就还不该进这个枚举。
    """

    SPEAK_HERE = "speak.here"  # 在所处地点出声
    SEND_CHANNEL_MESSAGE = "message.send"  # 往频道里发消息
    JOIN_CHANNEL = "presence.join_channel"
    LEAVE_CHANNEL = "presence.leave_channel"
    MOVE_TO = "movement.move_to"  # 移动到相邻地点


@dataclass(frozen=True)
class ActionDefinition:
    """目录里的一条：这个动作要什么、落成哪条事件、传播到哪。"""

    action_id: ActionId
    event_type: EventType
    event_scope: EventScope
    target_kind: TargetKind
    preconditions: Tuple[Precondition, ...] = ()
    # 必须出现的 payload 键；缺一个就整条拒绝。
    required_payload_keys: Tuple[str, ...] = ()
    # 可以出现、但不强制的 payload 键。两个元组之外的键一律拒绝。
    optional_payload_keys: Tuple[str, ...] = ()

    @property
    def allowed_payload_keys(self) -> Tuple[str, ...]:
        return tuple(self.required_payload_keys) + tuple(self.optional_payload_keys)

    @property
    def requires_authored_text(self) -> bool:
        """这个动作是否需要外部提供一段台词。

        需要的话，确定性策略就不能选它 —— 台词属于角色生成层，凭空造一句
        等于把"不知道说什么"伪装成"说了点什么"。这个判断从必填键推导，
        不另存一份，免得两处说法不一致。
        """
        return "text" in self.required_payload_keys

    def to_dict(self) -> Dict:
        return {
            "action_id": self.action_id.value,
            "event_type": self.event_type.value,
            "event_scope": self.event_scope.value,
            "target_kind": self.target_kind.value,
            "preconditions": [p.value for p in self.preconditions],
            "required_payload_keys": list(self.required_payload_keys),
            "optional_payload_keys": list(self.optional_payload_keys),
            "requires_authored_text": self.requires_authored_text,
        }


# 通用前置：世界得认识这个人，而且这个人得醒着。
_ACTOR_BASE = (Precondition.ACTOR_KNOWN, Precondition.ACTOR_AWAKE)

_CATALOGUE: Dict[ActionId, ActionDefinition] = {
    ActionId.SPEAK_HERE: ActionDefinition(
        action_id=ActionId.SPEAK_HERE,
        event_type=EventType.DIALOGUE_SPOKEN,
        event_scope=EventScope.LOCATION,
        target_kind=TargetKind.NONE,
        preconditions=_ACTOR_BASE + (Precondition.ACTOR_HAS_LOCATION,),
        required_payload_keys=("text",),
        optional_payload_keys=("char_name",),
    ),
    ActionId.SEND_CHANNEL_MESSAGE: ActionDefinition(
        action_id=ActionId.SEND_CHANNEL_MESSAGE,
        event_type=EventType.MESSAGE_SENT,
        event_scope=EventScope.CHANNEL,
        target_kind=TargetKind.CHANNEL,
        preconditions=_ACTOR_BASE
        + (
            Precondition.TARGET_CHANNEL_EXISTS,
            Precondition.ACTOR_IN_TARGET_CHANNEL,
        ),
        required_payload_keys=("text",),
        optional_payload_keys=("char_name",),
    ),
    ActionId.JOIN_CHANNEL: ActionDefinition(
        action_id=ActionId.JOIN_CHANNEL,
        event_type=EventType.PRESENCE_JOINED_CHANNEL,
        event_scope=EventScope.CHANNEL,
        target_kind=TargetKind.CHANNEL,
        preconditions=_ACTOR_BASE
        + (
            Precondition.TARGET_CHANNEL_EXISTS,
            Precondition.ACTOR_NOT_IN_TARGET_CHANNEL,
        ),
    ),
    ActionId.LEAVE_CHANNEL: ActionDefinition(
        action_id=ActionId.LEAVE_CHANNEL,
        event_type=EventType.PRESENCE_LEFT_CHANNEL,
        event_scope=EventScope.CHANNEL,
        target_kind=TargetKind.CHANNEL,
        preconditions=_ACTOR_BASE
        + (
            Precondition.TARGET_CHANNEL_EXISTS,
            Precondition.ACTOR_IN_TARGET_CHANNEL,
        ),
    ),
    ActionId.MOVE_TO: ActionDefinition(
        action_id=ActionId.MOVE_TO,
        event_type=EventType.CHARACTER_LOCATION_CHANGED,
        event_scope=EventScope.LOCATION,
        target_kind=TargetKind.LOCATION,
        preconditions=_ACTOR_BASE
        + (
            Precondition.ACTOR_HAS_LOCATION,
            Precondition.TARGET_LOCATION_EXISTS,
            Precondition.TARGET_LOCATION_IS_ELSEWHERE,
            Precondition.TARGET_LOCATION_REACHABLE,
        ),
    ),
}


def catalogue_ids() -> Tuple[ActionId, ...]:
    """目录里全部动作，顺序确定（按 ID 字典序）。"""
    return tuple(sorted(_CATALOGUE, key=lambda action_id: action_id.value))


def action_definition(action_id) -> ActionDefinition:
    """取一条动作声明；不在目录里就响亮失败。"""
    try:
        action_id = ActionId(action_id)
    except ValueError:
        raise ActionError(f"未知的动作: {action_id!r}") from None
    definition = _CATALOGUE.get(action_id)
    if definition is None:
        # 枚举里有、目录里没有 = 有人加了个还没实现的动作。
        raise ActionError(f"动作 '{action_id.value}' 没有目录声明，不能提案")
    return definition


def catalogue() -> Dict[ActionId, ActionDefinition]:
    """只读副本，给调试投影和测试用。"""
    return dict(_CATALOGUE)


def new_proposal_id(prefix: str = "prop") -> str:
    """给没有天然稳定 ID 的提案生成一个。

    运行时自带的策略都从 due_id 推导提案 ID（推导出来的身份存档往返后不变），
    这个生成器留给外部调用方。
    """
    return f"{prefix}_{uuid4().hex}"


@dataclass(frozen=True)
class LegalAction:
    """在当前世界里，某个角色可以合法执行的一个 (动作, 目标) 组合。

    它是 Agency 上下文的一部分：策略只能从这份枚举里挑，挑目录里有但这里
    没有的组合会被判为非法。枚举与前置条件求值是同一套判断，不是两套 ——
    "枚举出来的正好是前置条件全过的那些"这条等式有测试盯着。
    """

    action_id: ActionId
    target_id: Optional[str] = None

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        try:
            set_(self, "action_id", ActionId(self.action_id))
        except ValueError:
            raise ActionError(f"未知的动作: {self.action_id!r}") from None
        if self.target_id is not None and (
            not isinstance(self.target_id, str) or not self.target_id
        ):
            raise ActionError("target_id 必须是非空字符串或 None")

    @property
    def definition(self) -> ActionDefinition:
        return action_definition(self.action_id)

    @property
    def requires_authored_text(self) -> bool:
        return self.definition.requires_authored_text

    @property
    def sort_key(self) -> Tuple[str, str]:
        """确定性排序键。目标为 None 排在同名动作的最前面。"""
        return (self.action_id.value, self.target_id or "")

    def to_dict(self) -> Dict:
        return {
            "action_id": self.action_id.value,
            "target_id": self.target_id,
            "requires_authored_text": self.requires_authored_text,
        }


@dataclass(frozen=True)
class ActionProposal:
    """一条"这个角色打算做这件事"的提案。

    **提案不是世界真相**。它没有时间戳、没有序号、不进世界历史，也不改任何
    状态；只有通过校验并被接受之后，才由 P5 的提交边界产出一条事件。构造一条
    提案永远不改变任何东西 —— 这条不变量比任何注释都重要，因为模型驱动的策略
    产出的就是这种对象。

    这里只做**形状**校验（动作在不在目录里、目标类型对不对、payload 键有没有
    声明过）。世界相关的校验是前置条件，属于运行时，而且必须在提交那一刻重来
    一遍：提案做出来的时候合法，不代表提交的时候还合法。
    """

    proposal_id: str
    character_id: str
    action_id: ActionId
    target_id: Optional[str] = None
    payload: Mapping = field(default_factory=dict)

    def __post_init__(self) -> None:
        set_ = object.__setattr__

        if not isinstance(self.proposal_id, str) or not self.proposal_id:
            raise ActionError("proposal_id 必须是非空字符串")
        if not isinstance(self.character_id, str) or not self.character_id:
            raise ActionError("character_id 必须是非空字符串")
        try:
            set_(self, "action_id", ActionId(self.action_id))
        except ValueError:
            raise ActionError(f"未知的动作: {self.action_id!r}") from None

        definition = action_definition(self.action_id)
        self._validate_target(definition)

        if not isinstance(self.payload, Mapping):
            raise ActionError("payload 必须是字典")
        allowed = set(definition.allowed_payload_keys)
        extra = sorted(set(self.payload) - allowed)
        if extra:
            # 未声明的键一律拒绝，不是丢掉。任意字典改不动世界，是因为它根本
            # 走不到提交边界，而不是因为提交边界"恰好没读那几个键"。
            raise ActionError(
                f"动作 '{self.action_id.value}' 不接受 payload 键: {', '.join(extra)}"
            )
        for key in definition.required_payload_keys:
            if key not in self.payload:
                raise ActionError(
                    f"动作 '{self.action_id.value}' 缺少必填 payload 键: {key}"
                )
        if definition.requires_authored_text:
            text = self.payload.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ActionError(
                    f"动作 '{self.action_id.value}' 的 payload.text 必须是非空字符串"
                )
        set_(
            self,
            "payload",
            freeze_json_value(self.payload, path="payload", error=ActionError),
        )

    def _validate_target(self, definition: ActionDefinition) -> None:
        kind = definition.target_kind
        if kind is TargetKind.NONE:
            if self.target_id is not None:
                raise ActionError(
                    f"动作 '{self.action_id.value}' 不接受目标，收到 "
                    f"{self.target_id!r}"
                )
            return
        if not isinstance(self.target_id, str) or not self.target_id:
            raise ActionError(
                f"动作 '{self.action_id.value}' 需要一个 {kind.value} 目标"
            )

    def __hash__(self) -> int:
        # 冻结后的 payload 不可哈希；提案的身份本来就是 proposal_id。
        return hash(self.proposal_id)

    @property
    def definition(self) -> ActionDefinition:
        return action_definition(self.action_id)

    @property
    def legal_action(self) -> LegalAction:
        """这条提案对应的 (动作, 目标) 组合，用来跟合法枚举对照。"""
        return LegalAction(action_id=self.action_id, target_id=self.target_id)

    def derived_event_id(self, session_id: str) -> str:
        """这条提案被接受之后，那条事件的 ID。

        **唯一**一处定义这个推导。效果层按它构造事件，会话存档按它校验
        "acted 记录指着的确实是自己产出的那条事件"——两处各写一份格式串的话，
        存档校验迟早会变成"这条事件存在就行"，那等于没校验。
        """
        if not isinstance(session_id, str) or not session_id:
            raise ActionError("session_id 必须是非空字符串")
        return f"{session_id}:agency:{self.proposal_id}"

    def event_payload(self) -> Dict:
        """构造事件时允许带过去的 payload —— 只有声明过的键。"""
        allowed = set(self.definition.allowed_payload_keys)
        return {
            key: value
            for key, value in thaw_json_value(self.payload).items()
            if key in allowed
        }

    def to_dict(self) -> Dict:
        return {
            "proposal_id": self.proposal_id,
            "character_id": self.character_id,
            "action_id": self.action_id.value,
            "target_id": self.target_id,
            "payload": thaw_json_value(self.payload),
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "ActionProposal":
        if not isinstance(payload, Mapping):
            raise ActionError("提案必须是字典")
        for required in ("proposal_id", "character_id", "action_id"):
            if required not in payload:
                raise ActionError(f"提案缺少必填字段: {required}")
        return cls(
            proposal_id=payload["proposal_id"],
            character_id=payload["character_id"],
            action_id=payload["action_id"],
            target_id=payload.get("target_id"),
            payload=payload.get("payload", {}),
        )
