# pns/runtime/rhythm.py — 作息表 → 此刻该提交的那几条事件
#
# 这一层回答的问题只有一个：**按内容作者写下的作息表，这个世界此刻还缺哪几条
# 状态变更。** 它是纯函数：读一份 WorldState，产出一组还没提交的 Event。
#
# 它不回答、也没有能力回答：什么时候问它（调度器推进时间之后，由协调器问）、
# 这些事件算不算数（事件提交边界）、角色因此要说什么（Agency 与生成层）。
# 所以这个模块不 import 会话、不 import 协调器，也不提交任何东西 —— 它交出
# 事件，别人决定要不要落地。
#
# 四条硬约束：
#
#   1. **"这一段说过话了没有"必须能从耐久状态重新推出来。** 判据是角色当前
#      活动记录的 since：它比这一段的起点早（或者根本没有记录），就说明作息表
#      还没为这一段说过话。于是一次失败的推进、一次进程重启、一次存档恢复，
#      都靠重新算一遍自动补上 —— 没有任何"应用过了"的标记活在内存里，也没有
#      任何新字段进存档。
#   2. **段内做过的决定压过作息表。** 操作者改了活动、或者 Agency 让角色移动
#      了，那条记录的 since 落在这一段之内，作息表这一段就不再开口，直到下一段
#      开始。作息表是默认的一天，不是笼子。
#   3. **只有"此刻这一段"能成真。** 一次推进跨过了整整几段，那几段并没有发生过
#      —— 世界的时钟是跳过去的。所以这里永远只把世界对齐到当前段，不补演。
#   4. **事件 id 是确定性的。** 同一个角色、同一个模拟分钟、同一类变更只可能有
#      一条。真的被应用两次时，世界历史会因为重复 id 响亮拒绝，而不是悄悄留下
#      两条一模一样的状态变更。
from typing import Dict, List, Mapping, Optional, Tuple

from pns.models.event import Event, EventScope, EventType
from pns.models.world_state import ActivityKind, WorldState
from pns.world.rhythm import DailyRhythm, format_day_minute

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
        self, world: WorldState, *, correlation_id: Optional[str] = None
    ) -> Tuple[Event, ...]:
        """按此刻的世界时钟，算出还缺哪几条状态变更。**不改变任何状态。**

        产出的顺序是确定的：角色按 ID 排序，同一个角色先到地方再开始做事。
        """
        if not isinstance(world, WorldState):
            raise RhythmDirectorError("作息表规划需要一份权威 WorldState")
        if not self._rhythms:
            return ()

        known = set(world.known_characters())
        events: List[Event] = []
        for character_id in sorted(self._rhythms):
            if character_id not in known:
                # 排作息的时候角色在这个世界里，现在不在了（或者这个世界根本
                # 没选它）。跳过，不报错 —— 内容包是全体角色共用的，一个世界
                # 只选两个人是正常的。
                continue
            events.extend(
                self._plan_character(world, character_id, correlation_id)
            )
        return tuple(events)

    def _plan_character(
        self, world: WorldState, character_id: str, correlation_id: Optional[str]
    ) -> List[Event]:
        rhythm = self._rhythms[character_id]
        segment = rhythm.segment_at(world.clock)
        started_at = rhythm.segment_started_at(world.clock)
        current = world.activity_of(character_id)

        if current.kind is not ActivityKind.UNSPECIFIED and current.since >= started_at:
            # 这一段之内已经有人替这个角色定过当前活动 —— 可能就是作息表自己，
            # 也可能是操作者。两种都算数，作息表这一段不再开口。
            return []

        stamp = world.clock.isoformat(timespec="minutes")
        events: List[Event] = []
        if (
            segment.location_id is not None
            and world.location_of(character_id) != segment.location_id
        ):
            events.append(
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
            events.append(
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
        return events

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
