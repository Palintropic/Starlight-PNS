# pns/runtime/autonomy/context.py — 交给生成层的、角色作用域的输入
#
# 这一层唯一的职责跟 Agency 的上下文构造器完全一样：**收窄**。区别只在收窄
# 之后交给谁 —— 那边交给"要不要动"的判断，这边交给"说什么"的生成。两者的
# 危险面也一样：一个能看见全知数据的生成上下文，会让角色说出它不可能知道
# 的事，而且这种泄漏在输出里往往看不出来。
#
# 所以这个模块刻意只接受两样东西：一份**已经收窄过的** AgencyContext，和
# 这个角色**自己的**召回结果渲染成的那几行。它不接受 SessionState，也就没有
# 任何路径能读到：
#
#   事件历史       全知的客观世界历史，含 payload 与 provenance。角色感知得到
#                  的部分已经在它自己的观察里了。
#   曝光判定日志   含拒绝理由。"这个角色不知道某件事"必须包含"它也不知道自己
#                  被拒绝过"。
#   记忆存储       别人的记忆。召回服务在调用点就收窄成 for_owner(...)，那一行
#                  在协调器里，是显式的、可以一眼审查完的。
#
# 有 AST 测试盯着这个模块不出现 `.events` / `.exposures` / `.memories` /
# `.agency` / `.turns` 的属性访问，也不出现 SessionState 这个名字 ——
# 不是"现在没有"，是"以后也不许有"。
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Sequence, Tuple

from pns.models.action import ActionId, LegalAction
from pns.models.activation import ActivationDue
from pns.models.observation import Observation


class GenerationContextError(ValueError):
    """无法为这个角色构造生成上下文（观察串了台、动作对不上等）。"""


@dataclass(frozen=True)
class GenerationContext:
    """一次"这个角色此刻说什么"所能依据的全部信息。

    跟 AgencyContext 一样是不可变的值对象：生成器拿到它之后不可能反过来改
    世界，也不可能把它留着下次再用（下次的世界不是这个世界）。
    """

    character_id: str
    activation: ActivationDue
    # 生成依据的那一刻模拟时钟。判分与提交都会拿它对一次。
    now: datetime
    # 要为哪一个动作写这句话。生成层不挑动作 —— 挑动作是 Agency 的事，
    # 这里只回答"那这句话是什么"。
    action_id: ActionId
    target_id: Optional[str] = None
    location_id: Optional[str] = None
    channel_ids: Tuple[str, ...] = ()
    availability: str = "available"
    # 此刻这个角色感知得到的其他角色。
    perceived_characters: Tuple[str, ...] = ()
    # 这个角色**自己的**观察，按感知顺序。
    observations: Tuple[Observation, ...] = ()
    # 这个角色**自己的**记忆此刻想起的那几行（已经过提示投影的白名单删减：
    # 没有记忆 ID、没有曝光理由码、没有显著度、没有 provenance）。
    recalled: Tuple[str, ...] = ()
    legal_actions: Tuple[LegalAction, ...] = ()
    observations_truncated: bool = False
    legal_actions_truncated: bool = False
    recall_truncated: bool = False

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        if not isinstance(self.character_id, str) or not self.character_id:
            raise GenerationContextError("character_id 必须是非空字符串")
        if not isinstance(self.activation, ActivationDue):
            raise GenerationContextError("activation 必须是一条 ActivationDue")
        if not isinstance(self.now, datetime):
            raise GenerationContextError("now 必须是 datetime（模拟时钟时间）")
        try:
            set_(self, "action_id", ActionId(self.action_id))
        except ValueError:
            raise GenerationContextError(f"未知的动作: {self.action_id!r}") from None
        for observation in self.observations:
            if not isinstance(observation, Observation):
                raise GenerationContextError("observations 只能包含 Observation")
            if observation.observer_id != self.character_id:
                # 别人的观察混进来就是一次泄漏，而且是最难发现的那种。
                raise GenerationContextError(
                    f"观察 '{observation.source_event_id}' 属于 "
                    f"'{observation.observer_id}'，不能进 '{self.character_id}' "
                    "的生成上下文"
                )
        for line in self.recalled:
            if not isinstance(line, str):
                raise GenerationContextError("recalled 只能包含字符串")

    @property
    def observed_lines(self) -> Tuple[str, ...]:
        """自己的观察里能渲染成一行对话的那些，按感知顺序。

        渲染逻辑在 Observation 自己身上（P6 就定好了），这里不另写一份 ——
        两份渲染迟早会有一份忘记删掉某个不该出现的字段。
        """
        lines = []
        for observation in self.observations:
            line = observation.render_line()
            if line is not None:
                lines.append(line)
        return tuple(lines)

    def to_dict(self) -> Dict:
        """JSON 安全的只读投影，给调试、测试和提示词渲染用。

        这份投影就是**提示词渲染能看见的全部** —— 测试直接在它的 JSON 上搜
        关键词，来证明没观察到的事件、别人的记忆、曝光理由码一个字都没渗进来。

        观察在这里再删减一次，不直接用 Observation.to_dict()：那一份带着
        曝光理由码（"我是因为在频道里才听见的"）。它不是拒绝信息 —— 拒绝
        根本长不出观察 —— 但它仍然是曝光系统的簿记，不是角色经验（架构文档
        §15）。跟记忆的提示投影同一条规矩：读的记录带 provenance，渲染出来
        的东西不带。
        """
        return {
            "character_id": self.character_id,
            "activation": self.activation.to_dict(),
            "now": self.now.isoformat(),
            "action_id": self.action_id.value,
            "target_id": self.target_id,
            "location_id": self.location_id,
            "channel_ids": list(self.channel_ids),
            "availability": self.availability,
            "perceived_characters": list(self.perceived_characters),
            "observations": [_perceived(o) for o in self.observations],
            "observed_lines": list(self.observed_lines),
            "recalled": list(self.recalled),
            "legal_actions": [legal.to_dict() for legal in self.legal_actions],
            "observations_truncated": self.observations_truncated,
            "legal_actions_truncated": self.legal_actions_truncated,
            "recall_truncated": self.recall_truncated,
        }


# 一条观察在生成上下文里可以出现的字段。白名单式的 —— 没登记的键一个都
# 不出现，尤其是曝光理由码。黑名单迟早会漏。
_PERCEIVED_KEYS = ("type", "actor_id", "text", "char_name", "location_id", "channel_id")


def _perceived(observation: Observation) -> Dict:
    """一条**自己的**观察，删减成提示词那一侧可以看见的样子。"""
    perceived = observation.perceived
    return {
        "source_event_id": observation.source_event_id,
        "observed_at": observation.observed_at.isoformat(),
        "line": observation.render_line(),
        **{
            key: perceived[key] for key in _PERCEIVED_KEYS if key in perceived
        },
    }


def build_generation_context(
    agency_context,
    choice: LegalAction,
    recalled: Sequence[str] = (),
    *,
    recall_truncated: bool = False,
) -> GenerationContext:
    """把一份已经收窄过的 Agency 上下文，转成生成层要的那一份。

    刻意只做**转写**，不做取数：所有字段都来自传进来的这两样东西。这个函数
    拿不到会话、拿不到世界，所以"喂给它的到底是谁的东西"这个问题，答案完整
    地写在调用点的那两行里。
    """
    if not isinstance(choice, LegalAction):
        raise GenerationContextError("choice 必须是一个 LegalAction")
    character_id = getattr(agency_context, "character_id", None)
    if not isinstance(character_id, str) or not character_id:
        raise GenerationContextError("agency_context 必须带上 character_id")
    return GenerationContext(
        character_id=character_id,
        activation=agency_context.activation,
        now=agency_context.observed_at,
        action_id=choice.action_id,
        target_id=choice.target_id,
        location_id=agency_context.location_id,
        channel_ids=tuple(agency_context.channel_ids),
        availability=agency_context.availability,
        perceived_characters=tuple(agency_context.perceived_characters),
        observations=tuple(agency_context.observations),
        recalled=tuple(recalled),
        legal_actions=tuple(agency_context.legal_actions),
        observations_truncated=agency_context.observations_truncated,
        legal_actions_truncated=agency_context.legal_actions_truncated,
        recall_truncated=bool(recall_truncated),
    )


__all__ = [
    "GenerationContext",
    "GenerationContextError",
    "build_generation_context",
]
