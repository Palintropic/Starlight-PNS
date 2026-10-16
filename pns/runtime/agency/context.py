# pns/runtime/agency/context.py — 交给策略的、角色作用域的输入
#
# 这一层唯一的职责是**收窄**：把"世界现在是什么样"砍成"这个角色此刻知道
# 什么、能做什么"。策略只能看见这份上下文，看不见别的。
#
# 因此这个模块刻意只接受四样东西：世界、角色 ID、那条到期资格、以及**这个
# 角色自己的**观察序列。它不接受 SessionState，也就没有任何路径能读到：
#
#   曝光判定日志   包括拒绝理由。"这个角色不知道某件事"必须包含"它也不知道
#                  自己被拒绝过" —— 一条 no_channel_access 本身就是情报。
#   事件历史       全知的客观世界历史，含 payload 和 provenance。角色能感知
#                  到的部分已经在它自己的观察里了，观察是投影，事件是全貌。
#
# 有一条 AST 测试盯着这个模块不出现 `.exposures` / `.events` 属性访问 ——
# 不是"现在没有"，是"以后也不许有"。
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Dict, Optional, Sequence, Tuple

from pns.models.action import LegalAction
from pns.models.activation import ActivationDue
from pns.models.event import EventType
from pns.models.observation import DEPARTED_PERCEIVED_TYPE, Observation
from pns.models.world_state import WorldState
from pns.runtime.agency.preconditions import legal_actions


class AgencyContextError(ValueError):
    """无法为这个角色构造上下文（世界不认识它、到期记录对不上等）。"""


@dataclass(frozen=True)
class AgencyContext:
    """一次"要不要行动"的判断所能依据的全部信息。

    刻意是个不可变的值对象：策略拿到它之后不可能反过来改世界，也不可能把它
    留着下次再用（下次的世界不是这个世界）。
    """

    character_id: str
    activation: ActivationDue
    # 判断发生的模拟时刻。它等于世界时钟，提交时会拿它对一次：对不上说明
    # 这个判断回答的已经不是当初那个时刻了。
    observed_at: datetime
    location_id: Optional[str] = None
    channel_ids: Tuple[str, ...] = ()
    availability: str = "available"
    activity: str = "unspecified"
    # 此刻这个角色感知得到的其他角色：同处一地的，以及同在某个频道里的。
    perceived_characters: Tuple[str, ...] = ()
    # 上面那份兼容并集的两个明确来源。提示词必须区分“同处一室”和“在线同频”，
    # 否则两个各自在家的角色会被描述成物理上“在一起”。
    co_located_characters: Tuple[str, ...] = ()
    channel_characters: Tuple[str, ...] = ()
    observations: Tuple[Observation, ...] = ()
    legal_actions: Tuple[LegalAction, ...] = ()
    legal_actions_truncated: bool = False
    observations_truncated: bool = False
    arrangement_cues: Tuple[Mapping, ...] = ()
    # 最近一小时里从这个角色此刻所在地点离开、之后没再见到、此刻也不在身边
    # 的人：(角色 ID, 看见 ta 走的时刻)。在观察被裁剪**之前**从完整的自己的
    # 观察里派生，所以离场那条观察被数量预算挤掉了也还点得出名（FIX-46）。
    recent_departures: Tuple[Tuple[str, datetime], ...] = ()

    def has_legal(self, action_id, target_id: Optional[str] = None) -> bool:
        candidate = LegalAction(action_id=action_id, target_id=target_id)
        return candidate in self.legal_actions

    def legal_without_authored_text(self) -> Tuple[LegalAction, ...]:
        """不需要外部提供台词的那些合法动作。

        确定性策略只能从这里挑：台词属于角色生成层，凭空造一句就是把"不知道
        说什么"伪装成"说了点什么"。
        """
        return tuple(
            legal for legal in self.legal_actions if not legal.requires_authored_text
        )

    def to_dict(self) -> Dict:
        """JSON 安全的只读投影，给调试和测试用。

        这份投影就是策略能看见的全部 —— 测试直接在它的 JSON 上搜关键词，
        来证明没观察到的事件一个字都没渗进来。
        """
        return {
            "character_id": self.character_id,
            "activation": self.activation.to_dict(),
            "observed_at": self.observed_at.isoformat(),
            "location_id": self.location_id,
            "channel_ids": list(self.channel_ids),
            "availability": self.availability,
            "activity": self.activity,
            "perceived_characters": list(self.perceived_characters),
            "co_located_characters": list(self.co_located_characters),
            "channel_characters": list(self.channel_characters),
            "observations": [o.to_dict() for o in self.observations],
            "legal_actions": [legal.to_dict() for legal in self.legal_actions],
            "legal_actions_truncated": self.legal_actions_truncated,
            "observations_truncated": self.observations_truncated,
            "recent_departures": [
                {"actor_id": actor, "at": at.isoformat()}
                for actor, at in self.recent_departures
            ],
        }


def _co_located_characters(world: WorldState, character_id: str) -> Tuple[str, ...]:
    found = set()
    location_id = world.location_of(character_id)
    if location_id is not None:
        found.update(world.characters_at(location_id))
    found.discard(character_id)
    return tuple(sorted(found))


def _channel_characters(world: WorldState, character_id: str) -> Tuple[str, ...]:
    found = set()
    for channel_id in world.channels_for(character_id):
        found.update(world.channel_participants(channel_id))
    found.discard(character_id)
    return tuple(sorted(found))


# 离场线索只回看这么久。更早的离场不能凭"裁剪前还在"进线索。
RECENT_DEPARTURE_WINDOW = timedelta(hours=1)


def recent_departures(
    observations: Sequence[Observation],
    *,
    character_id: str,
    location_id: Optional[str],
    co_located: Sequence[str],
    now: datetime,
) -> Tuple[Tuple[str, datetime], ...]:
    """从这个角色**自己的**完整观察里，派生"刚才从这里离开的人"。

    按观察顺序（就是提交顺序）走一遍，记下每个别人最后一条到场 / 离场观察；
    只留最后一条是离场、离开的就是此刻这个地点、发生在最近一小时内、此刻
    又不在身边的人。只含 ID 与时刻，不含去向 —— 离场观察里本来就没有去向。
    """
    if location_id is None:
        return ()
    last = {}
    for observation in observations:
        if observation.observer_id != character_id:
            raise AgencyContextError(
                f"观察 '{observation.source_event_id}' 属于 "
                f"'{observation.observer_id}'，不能进 '{character_id}' 的离场线索"
            )
        perceived = observation.perceived
        kind = perceived.get("type")
        actor = perceived.get("actor_id")
        if not actor or actor == character_id:
            continue
        if kind in (DEPARTED_PERCEIVED_TYPE, EventType.CHARACTER_LOCATION_CHANGED.value):
            last[actor] = (kind, perceived.get("location_id"), observation.observed_at)
    present = set(co_located)
    found = []
    for actor in sorted(last):
        kind, where, at = last[actor]
        if (
            kind == DEPARTED_PERCEIVED_TYPE
            and where == location_id
            and now - RECENT_DEPARTURE_WINDOW <= at <= now
            and actor not in present
        ):
            found.append((actor, at))
    return tuple(found)


def build_agency_context(
    world: WorldState,
    character_id: str,
    activation: ActivationDue,
    observations: Sequence[Observation] = (),
    *,
    max_legal_actions: Optional[int] = None,
    max_observations: Optional[int] = None,
) -> AgencyContext:
    """为一个角色构造一次判断所需的全部输入。

    observations 由调用方给出，而且必须只包含**这个角色自己的**观察。这是
    刻意的接口形状：这个模块拿不到会话，也就没办法自己去别人的观察里翻，
    "只喂它自己的观察"这件事在调用点是显式的、可审查的一行。
    """
    if not isinstance(world, WorldState):
        raise AgencyContextError("构造上下文需要一份权威 WorldState")
    if not isinstance(character_id, str) or not character_id:
        raise AgencyContextError("character_id 必须是非空字符串")
    if not isinstance(activation, ActivationDue):
        raise AgencyContextError("activation 必须是一条 ActivationDue")
    if activation.character_id is not None and activation.character_id != character_id:
        raise AgencyContextError(
            f"到期记录 '{activation.due_id}' 属于角色 "
            f"'{activation.character_id}'，不能用来判断 '{character_id}'"
        )

    own = []
    for observation in observations:
        if not isinstance(observation, Observation):
            raise AgencyContextError("observations 只能包含 Observation")
        if observation.observer_id != character_id:
            # 别人的观察混进来就是一次泄漏，而且是最难发现的那种。
            raise AgencyContextError(
                f"观察 '{observation.source_event_id}' 属于 "
                f"'{observation.observer_id}'，不能进 '{character_id}' 的上下文"
            )
        own.append(observation)

    location_id = world.location_of(character_id)
    co_located = _co_located_characters(world, character_id)
    # 裁剪之前：离场线索看的是完整的自己的观察。
    departures = recent_departures(
        own,
        character_id=character_id,
        location_id=location_id,
        co_located=co_located,
        now=world.clock,
    )

    observations_truncated = False
    if max_observations is not None and len(own) > max_observations:
        # 保留最新的那些：越近的感知越可能与"现在要不要动"有关。
        own = own[-max_observations:]
        observations_truncated = True

    legal, truncated = legal_actions(world, character_id, limit=max_legal_actions)

    co_located_ids = set(co_located)
    channel = tuple(
        cid
        for cid in _channel_characters(world, character_id)
        if cid not in co_located_ids
    )
    perceived = tuple(sorted(set(co_located) | set(channel)))

    return AgencyContext(
        character_id=character_id,
        activation=activation,
        observed_at=world.clock,
        location_id=world.location_of(character_id),
        channel_ids=tuple(world.channels_for(character_id)),
        availability=world.availability_of(character_id).value,
        activity=world.activity_of(character_id).kind.value,
        perceived_characters=perceived,
        co_located_characters=co_located,
        channel_characters=channel,
        observations=tuple(own),
        legal_actions=legal,
        legal_actions_truncated=truncated,
        observations_truncated=observations_truncated,
        recent_departures=departures,
    )


__all__ = [
    "AgencyContext",
    "AgencyContextError",
    "RECENT_DEPARTURE_WINDOW",
    "build_agency_context",
    "recent_departures",
]
