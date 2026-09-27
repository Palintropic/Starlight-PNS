# pns/runtime/rhythm.py — 作息表 → 此刻该提交的那几条事件
#
# 这一层回答的问题只有一个：**按内容作者写下的作息表，这个世界此刻还缺哪几条
# 状态变更。** 它是纯函数：读一份 WorldState 加这个会话的世界历史，产出一组
# 还没提交的 Event。
#
# 它不回答、也没有能力回答：什么时候问它（调度器推进时间之后，由协调器问）、
# 这些事件算不算数（事件提交边界）、角色因此要说什么（Agency 与生成层）。
# 所以这个模块不 import 会话、不 import 协调器，也不提交任何东西 —— 它交出
# 事件，别人决定要不要落地。
#
# 五条硬约束：
#
#   1. **"这一段有没有别人做过决定"必须能从耐久状态重新推出来。** 判据是世界
#      历史：这一段的窗口（段开始，或者本段行程更早的出发时刻）之后，这个角色
#      有没有**不带本段 segment_key** 的状态变更（活动、位置、频道进出）。有就
#      说明操作者或 Agency 替它做过决定了，作息表闭嘴到下一段开始（它是默认的
#      一天，不是笼子）。作息表自己的事件都带着本段的 segment_key，所以一趟
#      行程的出发不会把后面几跳判成"已决定"（R2-10）。没有任何"应用过了"的
#      标记活在内存里：失败的推进、进程重启、存档恢复都靠重新算一遍自愈。
#   2. **换地方就是走路，不是瞬移（Article X）。** 下一段要换地方时，按寻路算出
#      的路程倒推出发时刻，路上是 commuting；每一跳只到相邻地点，而且严格按上一
#      跳的实际时刻加路程才到下一跳。晚出门就晚到，绝不压缩路上的时间。
#   3. **跨过去的每一段都发生过。** 时钟由协调器逐边界推进（见
#      `next_boundary_after`），每一段、每一跳都以自己的时刻提交（WORLD-1 §5.1）。
#      这取代了 CONTENT-1 的"只有此刻这一段能成真"。
#   4. **走不到的段沉默，并留一次耐久处置。** 路不通（没有授予、地点不可达）
#      时不瞬移、不反复尝试；segment_key 进 SessionState.rhythm_dispositions。
#   5. **事件 id 是确定性的。** 同一个角色、同一个模拟分钟、同一类变更只可能有
#      一条；真的被应用两次时，世界历史会因为重复 id 响亮拒绝。
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from pns.models.event import Event, EventScope, EventType
from pns.models.event_store import EventStore
from pns.models.world_state import ActivityKind, WorldState
from pns.world.rhythm import MINUTES_PER_DAY, DailyRhythm, format_day_minute
from pns.world.routing import plan_route

# 作息表认作"这一段已经有人做过决定了"的那几种事件。两条都要算：只看活动的话，
# 一次只改地点的时段切换不会留下任何痕迹（见模块头第 1 条）。
_STATE_CHANGE_TYPES = (
    EventType.CHARACTER_ACTIVITY_CHANGED,
    EventType.CHARACTER_LOCATION_CHANGED,
    EventType.PRESENCE_JOINED_CHANNEL,
    EventType.PRESENCE_LEFT_CHANNEL,
)

# 作息表提交的事件 id 前缀。确定性 —— 见模块头第 4 条。
RHYTHM_EVENT_PREFIX = "rhythm"


class RhythmDirectorError(ValueError):
    """这个作息表调度对象本身就建不起来（不是 DailyRhythm、角色 ID 对不上）。"""


class RhythmDirector:
    """一个世界打开时锁定的那一份作息表集合。

    它跟判分器、策略一样是**冷适配器**：世界打开的那一刻从内容快照里拿一份，
    之后重载内容影响不到已经打开的世界。没有作息表的角色一个字节都不会被碰。
    """

    def __init__(self, rhythms: Optional[Mapping[str, DailyRhythm]] = None) -> None:
        entries: Dict[str, DailyRhythm] = {}
        for character_id, rhythm in dict(rhythms or {}).items():
            if not isinstance(rhythm, DailyRhythm):
                raise RhythmDirectorError(
                    f"角色 '{character_id}' 的作息表必须是 DailyRhythm"
                )
            if rhythm.character_id != character_id:
                # 一份写着别人名字的作息表会让"谁该去上学"这件事有两个答案。
                raise RhythmDirectorError(
                    f"作息表登记在 '{character_id}' 名下，但它自己写的是 "
                    f"'{rhythm.character_id}'"
                )
            entries[character_id] = rhythm
        self._rhythms = entries

    def __len__(self) -> int:
        return len(self._rhythms)

    def __bool__(self) -> bool:
        return bool(self._rhythms)

    def characters(self) -> Tuple[str, ...]:
        return tuple(sorted(self._rhythms))

    def has(self, character_id: str) -> bool:
        return character_id in self._rhythms

    def rhythm_for(self, character_id: str) -> Optional[DailyRhythm]:
        return self._rhythms.get(character_id)

    # ── 规划（纯） ──────────────────────────────────────────────────────
    def plan(
        self,
        world: WorldState,
        events: EventStore,
        *,
        correlation_id: Optional[str] = None,
        dispositions: Iterable[str] = (),
    ) -> Tuple[Event, ...]:
        """按此刻的世界时钟，算出还缺哪几条状态变更。**不改变任何状态。**"""
        return self.plan_step(
            world, events, correlation_id=correlation_id, dispositions=dispositions
        ).events

    def plan_step(
        self,
        world: WorldState,
        events: EventStore,
        *,
        correlation_id: Optional[str] = None,
        dispositions: Iterable[str] = (),
    ) -> "RhythmStep":
        """此刻该提交的事件，以及新发现的"这一段走不到"的处置。**不改变任何状态。**

        `events` 是这个会话的世界历史，只读：作息表要靠它回答"这一段里这个角色
        有没有别人替它做过决定"、"行程走到了哪一跳"。`dispositions` 是已经记下
        "走不到"的段（segment_key），它们不再规划、也不再重复记。

        产出的顺序是确定的：角色按 ID 排序；同一个角色先到地方（行程各跳），
        再换活动，最后进出频道（设计 §3.2）。
        """
        self._require_inputs(world, events)
        settled = frozenset(dispositions)
        planned: List[Event] = []
        unreachable: List[str] = []
        known = set(world.known_characters())
        for character_id in sorted(self._rhythms):
            if character_id not in known:
                # 内容包是全体角色共用的，一个世界只选两个人是正常的。
                continue
            state = self._state_of(world, events, character_id)
            if state.key in settled or state.decided:
                continue
            step, stuck = self._plan_character(world, events, state, correlation_id)
            planned.extend(step)
            if stuck:
                unreachable.append(state.key)
        return RhythmStep(events=tuple(planned), unreachable=tuple(unreachable))

    def next_boundary_after(
        self,
        world: WorldState,
        events: EventStore,
        *,
        dispositions: Iterable[str] = (),
    ) -> Optional[datetime]:
        """严格晚于此刻、作息下一次可能要提交事件的时刻。

        跟 plan_step() 同源：用的是同一份"生效段 / 行程进度"推导。候选包括下一段
        的开始、下一段行程的出发、进行中的行程下一跳的到达，以及"此刻就有逾期
        没做的事"时的下一分钟。没有作息表的世界返回 None。
        """
        self._require_inputs(world, events)
        settled = frozenset(dispositions)
        clock = world.clock
        candidates: List[datetime] = []
        # 此刻就有逾期没做的事（刚建出来的世界、恢复时落在行程窗口里）：下一分钟
        # 就是边界。晚出门就尽早出门，而不是干等到下一段开始。
        pending = self.plan_step(world, events, dispositions=settled)
        if pending.events or pending.unreachable:
            candidates.append(clock + timedelta(minutes=1))
        known = set(world.known_characters())
        for character_id in sorted(self._rhythms):
            if character_id not in known:
                continue
            state = self._state_of(world, events, character_id)
            candidates.append(state.next_start)
            if state.departure is not None and state.departure > clock:
                candidates.append(state.departure)
            if state.key not in settled and not state.decided:
                arrival = self._next_hop_due(world, events, state)
                if arrival is not None and arrival > clock:
                    candidates.append(arrival)
        future = [moment for moment in candidates if moment > clock]
        return min(future) if future else None

    # ── 推导 ────────────────────────────────────────────────────────────
    def _require_inputs(self, world, events) -> None:
        if not isinstance(world, WorldState):
            raise RhythmDirectorError("作息表规划需要一份权威 WorldState")
        if not isinstance(events, EventStore):
            raise RhythmDirectorError("作息表规划需要这个会话的世界历史")

    def _state_of(self, world, events, character_id) -> "_CharacterState":
        """这个角色此刻由哪一段作息管、窗口从哪里算起、有没有被别人决定过。

        生效段通常就是时钟所在的那一段。例外是行程窗口：下一段要换地方、而按
        路程倒推的出发时刻已经到了，那么从出发起就由下一段管（设计 §4.2）。
        """
        rhythm = self._rhythms[character_id]
        clock = world.clock
        current = rhythm.segment_at(clock)
        current_start = rhythm.segment_started_at(clock)
        following = _following(rhythm, current)
        next_start = current_start + timedelta(
            minutes=(following.at - current.at) % MINUTES_PER_DAY or MINUTES_PER_DAY
        )

        effective, effective_start, departure = current, current_start, None
        here = world.location_of(character_id)
        if (
            following.location_id is not None
            and here is not None
            and here != following.location_id
        ):
            route = self._route(world, character_id, here, following.location_id)
            if route is not None:
                departure = next_start - timedelta(minutes=route.total_minutes)
                if departure <= clock:
                    effective, effective_start = following, next_start

        key = segment_key(character_id, effective, effective_start)
        window = self._window_start(events, character_id, key, effective_start)
        decided = any(
            event.actor_id == character_id
            and event.type in _STATE_CHANGE_TYPES
            and (event.provenance or {}).get("segment_key") != key
            for event in events.since(window)
        )
        return _CharacterState(
            character_id=character_id,
            segment=effective,
            segment_start=effective_start,
            key=key,
            decided=decided,
            next_start=next_start,
            departure=departure if effective is current else None,
            channels=_managed_channels(rhythm),
        )

    @staticmethod
    def _window_start(events, character_id, key, segment_start) -> datetime:
        """这一段的窗口起点：段开始，或者本段行程更早的出发时刻。"""
        earliest = segment_start
        for event in events.since(segment_start - timedelta(days=1)):
            if (
                event.actor_id == character_id
                and (event.provenance or {}).get("segment_key") == key
                and event.occurred_at < earliest
            ):
                earliest = event.occurred_at
        return earliest

    @staticmethod
    def _route(world, character_id, here, there):
        return plan_route(
            world.locations,
            lambda location_id: world.may_enter(character_id, location_id),
            here,
            there,
        )

    def _trip_progress(self, events, state) -> Tuple[Optional[datetime], int]:
        """本段行程：出发时刻（在路上那条活动事件），以及已经走了几跳。"""
        departed_at = None
        hops = 0
        for event in events.since(state.segment_start - timedelta(days=1)):
            provenance = event.provenance or {}
            if (
                event.actor_id != state.character_id
                or provenance.get("segment_key") != state.key
            ):
                continue
            if (
                event.type is EventType.CHARACTER_ACTIVITY_CHANGED
                and event.payload.get("activity") == ActivityKind.COMMUTING.value
            ):
                departed_at = event.occurred_at
            elif event.type is EventType.CHARACTER_LOCATION_CHANGED:
                hops += 1
                last_hop = event.occurred_at
        if hops:
            return last_hop, hops
        return departed_at, 0

    def _next_hop_due(self, world, events, state) -> Optional[datetime]:
        target = state.segment.location_id
        here = world.location_of(state.character_id)
        if target is None or here is None or here == target:
            return None
        since, _hops = self._trip_progress(events, state)
        if since is None:
            return None  # 还没出发；出发时刻由 departure 候选给出
        route = self._route(world, state.character_id, here, target)
        if route is None or not route.legs:
            return None
        return since + timedelta(minutes=route.legs[0].minutes)

    def _plan_character(self, world, events, state, correlation_id):
        clock = world.clock
        character_id = state.character_id
        segment = state.segment
        stamp = clock.isoformat(timespec="minutes")
        planned: List[Event] = []

        def provenance(leg=None):
            return self._provenance(state, correlation_id, leg)

        location = world.location_of(character_id)
        activity = world.activity_of(character_id).kind
        target = segment.location_id

        # 1. 行程：一次最多走到"时间已经到了"的那一跳，严格按上一跳的实际时刻
        #    加路程算下一跳 —— 晚到可以，不许压缩路上的时间，更不许瞬移。
        if target is not None and location is not None and location != target:
            route = self._route(world, character_id, location, target)
            if route is None:
                return [], True
            since, hops = self._trip_progress(events, state)
            if since is None:
                if activity is not ActivityKind.COMMUTING:
                    planned.append(
                        self._activity_event(
                            character_id, ActivityKind.COMMUTING, clock, stamp,
                            provenance(), correlation_id,
                        )
                    )
                    activity = ActivityKind.COMMUTING
                since = clock
            for index, leg in enumerate(route.legs):
                due = since + timedelta(minutes=leg.minutes)
                if due > clock:
                    break
                planned.append(
                    Event(
                        event_id=(
                            f"{RHYTHM_EVENT_PREFIX}:{character_id}:location:"
                            f"{hops + index}@{stamp}"
                        ),
                        type=EventType.CHARACTER_LOCATION_CHANGED,
                        occurred_at=clock,
                        # 到场是别人看得见的：落点上的人由曝光判定决定能不能感知到。
                        scope=EventScope.LOCATION,
                        actor_id=character_id,
                        location_id=leg.to_id,
                        provenance=provenance(hops + index),
                        correlation_id=correlation_id,
                    )
                )
                location = leg.to_id
                # 同一时刻只允许继续走 0 分钟的连接；有路程的下一跳要等下一次。
                since = clock
                if leg.minutes:
                    break
            if location != target:
                return planned, False

        # 2. 到了（或本段不换地方）：段开始之后换成这一段的活动。
        if clock >= state.segment_start and activity is not segment.activity:
            planned.append(
                self._activity_event(
                    character_id, segment.activity, clock, stamp, provenance(),
                    correlation_id,
                )
            )

        # 3. 频道：段开始时，作息管理的频道里只留这一段声明的那个（先离开后加入）。
        if clock >= state.segment_start:
            for channel_id in sorted(state.channels):
                inside = world.is_in_channel(character_id, channel_id)
                if inside and channel_id != segment.channel_id:
                    planned.append(
                        self._presence_event(
                            EventType.PRESENCE_LEFT_CHANNEL, "leave", character_id,
                            channel_id, clock, stamp, provenance(), correlation_id,
                        )
                    )
            if segment.channel_id is not None and not world.is_in_channel(
                character_id, segment.channel_id
            ):
                planned.append(
                    self._presence_event(
                        EventType.PRESENCE_JOINED_CHANNEL, "join", character_id,
                        segment.channel_id, clock, stamp, provenance(), correlation_id,
                    )
                )
        return planned, False

    @staticmethod
    def _activity_event(character_id, activity, clock, stamp, provenance, correlation_id):
        return Event(
            event_id=f"{RHYTHM_EVENT_PREFIX}:{character_id}:activity:{activity.value}@{stamp}",
            type=EventType.CHARACTER_ACTIVITY_CHANGED,
            occurred_at=clock,
            scope=EventScope.PRIVATE,
            actor_id=character_id,
            payload={"activity": activity.value},
            provenance=provenance,
            correlation_id=correlation_id,
        )

    @staticmethod
    def _presence_event(
        kind, verb, character_id, channel_id, clock, stamp, provenance, correlation_id
    ):
        return Event(
            event_id=f"{RHYTHM_EVENT_PREFIX}:{character_id}:{verb}:{channel_id}@{stamp}",
            type=kind,
            occurred_at=clock,
            scope=EventScope.CHANNEL,
            actor_id=character_id,
            channel_id=channel_id,
            provenance=provenance,
            correlation_id=correlation_id,
        )

    @staticmethod
    def _provenance(state, correlation_id, leg=None) -> Dict:
        """系统侧信息：这条变更是哪一段作息、哪一次行程的第几跳。

        provenance 不进任何角色的观察（见 pns/runtime/exposure/projection.py），
        所以这里放的是给审计和调试看的东西，角色永远读不到它。
        """
        segment = state.segment
        provenance = {
            "kind": "daily_rhythm",
            "segment_at": format_day_minute(segment.at),
            "segment_started_at": state.segment_start.isoformat(),
            "segment_source": segment.source.value,
            "segment_key": state.key,
            "trip_leg": leg,
        }
        if correlation_id is not None:
            provenance["session_id"] = correlation_id
        return provenance


@dataclass(frozen=True)
class RhythmStep:
    """一次规划的结果：要提交的事件，以及新发现"走不到"的段。"""

    events: Tuple[Event, ...]
    unreachable: Tuple[str, ...] = ()


@dataclass(frozen=True)
class _CharacterState:
    character_id: str
    segment: object
    segment_start: datetime
    key: str
    decided: bool
    next_start: datetime
    departure: Optional[datetime]
    channels: frozenset


def segment_key(character_id: str, segment, segment_start: datetime) -> str:
    """一段作息在某一天的身份。同一段的所有事件（含整趟行程）都带着它。"""
    return f"{character_id}:{format_day_minute(segment.at)}@{segment_start.isoformat()}"


def _following(rhythm: DailyRhythm, segment):
    segments = rhythm.segments
    index = segments.index(segment)
    return segments[(index + 1) % len(segments)]


def _managed_channels(rhythm: DailyRhythm) -> frozenset:
    """作息表管理的频道：它的某一段声明过的那些。别的频道作息一概不碰。"""
    return frozenset(
        segment.channel_id for segment in rhythm.segments if segment.channel_id is not None
    )


__all__ = [
    "RHYTHM_EVENT_PREFIX",
    "RhythmDirector",
    "RhythmDirectorError",
    "RhythmStep",
    "segment_key",
]
