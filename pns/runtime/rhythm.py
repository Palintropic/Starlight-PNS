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
# 四条硬约束：
#
#   1. **"这一段说过话了没有"必须能从耐久状态重新推出来。** 判据是世界历史：
#      当前时段开始之后，这个角色有没有提交过状态变更（活动或位置）。有就说明
#      这一段已经有人做过决定了。于是一次失败的推进、一次进程重启、一次存档
#      恢复，都靠重新算一遍自动补上 —— 没有任何"应用过了"的标记活在内存里，
#      也没有任何新字段进存档。
#
#      判据刻意**不是**当前活动记录的 since。since 只是"最后一条活动事件的
#      时间"的代理，而一次只改地点的时段切换根本不产生活动事件：相邻两段活动
#      相同、地点不同时，since 会停在上一段，于是这个角色在新时段里自己走的路
#      每一拍都会被作息表拉回去。世界历史没有这个盲区 —— 位置变更也是事件。
#   2. **段内做过的决定压过作息表，不论是谁做的。** 操作者改了活动、Agency 让
#      角色移动了、或者作息表自己已经在这一段开过口，留下的都是同一种东西：
#      一条落在这一段之内的状态变更事件。作息表因此闭嘴到下一段开始。
#      它是默认的一天，不是笼子。
#   3. **只有"此刻这一段"能成真。** 一次推进跨过了整整几段，那几段并没有发生过
#      —— 世界的时钟是跳过去的。所以这里永远只把世界对齐到当前段，不补演。
#   4. **事件 id 是确定性的。** 同一个角色、同一个模拟分钟、同一类变更只可能有
#      一条。真的被应用两次时，世界历史会因为重复 id 响亮拒绝，而不是悄悄留下
#      两条一模一样的状态变更。
from typing import Dict, List, Mapping, Optional, Tuple

from pns.models.event import Event, EventScope, EventType
from pns.models.event_store import EventStore
from pns.models.world_state import WorldState
from pns.world.rhythm import DailyRhythm, format_day_minute

# 作息表认作"这一段已经有人做过决定了"的那几种事件。两条都要算：只看活动的话，
# 一次只改地点的时段切换不会留下任何痕迹（见模块头第 1 条）。
_STATE_CHANGE_TYPES = (
    EventType.CHARACTER_ACTIVITY_CHANGED,
    EventType.CHARACTER_LOCATION_CHANGED,
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
    ) -> Tuple[Event, ...]:
        """按此刻的世界时钟，算出还缺哪几条状态变更。**不改变任何状态。**

        `events` 是这个会话的世界历史，只读：作息表要靠它回答"当前时段里这个
        角色有没有发生过状态变更"。这是判据的唯一来源（见模块头第 1 条），
        所以它是必填参数，不是可选增强。

        产出的顺序是确定的：角色按 ID 排序，同一个角色先到地方再开始做事。
        """
        if not isinstance(world, WorldState):
            raise RhythmDirectorError("作息表规划需要一份权威 WorldState")
        if not isinstance(events, EventStore):
            raise RhythmDirectorError("作息表规划需要这个会话的世界历史")
        if not self._rhythms:
            return ()

        known = set(world.known_characters())
        planned: List[Event] = []
        for character_id in sorted(self._rhythms):
            if character_id not in known:
                # 排作息的时候角色在这个世界里，现在不在了（或者这个世界根本
                # 没选它）。跳过，不报错 —— 内容包是全体角色共用的，一个世界
                # 只选两个人是正常的。
                continue
            planned.extend(
                self._plan_character(world, events, character_id, correlation_id)
            )
        return tuple(planned)

    @staticmethod
    def _decided_in_segment(events: EventStore, character_id: str, started_at) -> bool:
        """当前时段里，这个角色有没有提交过状态变更。

        扫描窗口只到时段起点为止（EventStore.since 保证这一点），所以这次判断
        的成本跟世界活了多久无关。
        """
        for event in events.since(started_at):
            if event.actor_id == character_id and event.type in _STATE_CHANGE_TYPES:
                return True
        return False

    def _plan_character(
        self,
        world: WorldState,
        events: EventStore,
        character_id: str,
        correlation_id: Optional[str],
    ) -> List[Event]:
        rhythm = self._rhythms[character_id]
        segment = rhythm.segment_at(world.clock)
        started_at = rhythm.segment_started_at(world.clock)

        if self._decided_in_segment(events, character_id, started_at):
            # 这一段之内已经有人替这个角色定过状态 —— 作息表自己、操作者、
            # 或者这个角色自己走的路。三种都算数，作息表这一段不再开口。
            return []

        current = world.activity_of(character_id)
        stamp = world.clock.isoformat(timespec="minutes")
        planned: List[Event] = []
        if (
            segment.location_id is not None
            and world.location_of(character_id) != segment.location_id
        ):
            planned.append(
                Event(
                    event_id=f"{RHYTHM_EVENT_PREFIX}:{character_id}:location@{stamp}",
                    type=EventType.CHARACTER_LOCATION_CHANGED,
                    occurred_at=world.clock,
                    # 到场是别人看得见的：落点上的人由曝光判定决定能不能感知到。
                    scope=EventScope.LOCATION,
                    actor_id=character_id,
                    location_id=segment.location_id,
                    provenance=self._provenance(segment, started_at, correlation_id),
                    correlation_id=correlation_id,
                )
            )
        if current.kind is not segment.activity:
            planned.append(
                Event(
                    event_id=f"{RHYTHM_EVENT_PREFIX}:{character_id}:activity@{stamp}",
                    type=EventType.CHARACTER_ACTIVITY_CHANGED,
                    occurred_at=world.clock,
                    scope=EventScope.PRIVATE,
                    actor_id=character_id,
                    payload={"activity": segment.activity.value},
                    provenance=self._provenance(segment, started_at, correlation_id),
                    correlation_id=correlation_id,
                )
            )
        return planned

    @staticmethod
    def _provenance(segment, started_at, correlation_id) -> Dict:
        """系统侧信息：这条变更是哪一段作息、从哪一刻起算的。

        provenance 不进任何角色的观察（见 pns/runtime/exposure/projection.py），
        所以这里放的是给审计和调试看的东西，角色永远读不到它。
        """
        provenance = {
            "kind": "daily_rhythm",
            "segment_at": format_day_minute(segment.at),
            "segment_started_at": started_at.isoformat(),
        }
        if correlation_id is not None:
            provenance["session_id"] = correlation_id
        return provenance


__all__ = ["RHYTHM_EVENT_PREFIX", "RhythmDirector", "RhythmDirectorError"]
