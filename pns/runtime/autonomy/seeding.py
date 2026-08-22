# pns/runtime/autonomy/seeding.py — 新世界的初始排期
#
# 一个刚建出来的世界，队列是空的。空队列意味着时间怎么推进都不会有任何东西
# 到期，也就没有任何角色会被考虑 —— 世界"开着"，但永远不会发生任何事。
# 这个模块回答的问题只有一个：**一个新世界开局时，队列里该有什么。**
#
# 它不回答：角色到点了要不要动（Agency）、说什么（生成层）、这条排期为什么
# 存在（内容作者的作息表，属于 CONTENT-*）。这里排下去的是本阶段唯一有完整
# 语义的那一种激活：CHARACTER_ACTIVATION —— "该轮到这个角色考虑行动了"。
#
# 四条硬约束：
#
#   1. **只在创建时播种，恢复时一次都不。** 存档里已经带着这个世界自己的
#      队列了（周期激活换成下一次触发之后仍在队列里，一次性的触发完就摘掉）。
#      恢复时再播一遍，就会得到两条同名排期 —— 或者更糟，两条不同名但语义
#      重复的排期，于是这个角色每个周期被叫醒两次、花两份 API 额度。
#      执行这条的是机制不是约定：ID 是确定性的，撞车会响亮失败；而恢复路径
#      根本拿不到播种器（见 RuntimeAdapters.seed 与 WorldLifecycleService）。
#   2. **首次到期要错开。** 所有角色同一分钟到期，就是同一瞬间几个模型调用
#      一起打出去。错开是确定性的（按传入顺序），不是随机的 —— 同样的输入
#      要得到同样的队列。
#   3. **只有 cue 是角色看得见的。** 排期 payload 默认对角色不可见（见
#      pns/runtime/autonomy/context.py 的白名单）。这里刻意**只**放 cue，
#      一个系统簿记的键都不放：放进去的迟早会被某一版提示词渲染出来。
#   4. **播种失败必须让整次创建失败。** 一个"看起来建好了、其实永远不会动"
#      的世界，比一次响亮的创建失败糟糕得多 —— 前者要等操作者盯着屏幕几个
#      小时才发现。
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional, Sequence, Tuple

from pns.models.activation import ActivationKind, ScheduledActivation
from pns.runtime.autonomy.context import MAX_CUE_CHARS

# 播种出来的激活 ID 前缀。确定性 —— 同一个角色在同一个世界里只可能有一条。
SEED_PREFIX = "seed.activation"

# 各项节律的上界。它们是**安全预算**，不是审美：一个手滑写成 100000 的周期
# 会让世界永远不动，一个写成 0 的周期会让它每分钟都在花钱。
MAX_INTERVAL_MINUTES = 7 * 24 * 60
MAX_FIRST_DELAY_MINUTES = 7 * 24 * 60
MAX_STAGGER_MINUTES = 24 * 60


class SeedingError(ValueError):
    """这个世界的初始排期播不下去。"""


@dataclass(frozen=True)
class ActivationCadence:
    """服务器侧决定的开局节律。**不是**浏览器能传的任意排期表。

    三个数字各管一件事：
      * `interval_minutes`：一个角色隔多久被再考虑一次（模拟分钟）。
      * `first_delay_minutes`：第一个角色在开局之后多久第一次被考虑。
      * `stagger_minutes`：相邻两个角色的首次到期错开多少。
    """

    interval_minutes: int = 15
    first_delay_minutes: int = 5
    stagger_minutes: int = 5
    # 内容作者写给**角色**看的一句提示。默认没有 —— 没有话要说的时候塞一句
    # "该说话了"，等于替角色决定了它此刻在想什么。
    cue: Optional[str] = None

    def __post_init__(self) -> None:
        _bounded(self.interval_minutes, "interval_minutes", 1, MAX_INTERVAL_MINUTES)
        _bounded(
            self.first_delay_minutes, "first_delay_minutes", 1, MAX_FIRST_DELAY_MINUTES
        )
        _bounded(self.stagger_minutes, "stagger_minutes", 1, MAX_STAGGER_MINUTES)
        if self.cue is not None:
            if not isinstance(self.cue, str) or not self.cue.strip():
                raise SeedingError("cue 必须是非空字符串，或者干脆不给")
            if len(self.cue) > MAX_CUE_CHARS:
                raise SeedingError(
                    f"cue 超过 {MAX_CUE_CHARS} 字（收到 {len(self.cue)}），不接受截断"
                )

    def first_due_offset(self, index: int) -> int:
        """第 index 个角色的首次到期，距离开局多少分钟。"""
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise SeedingError(f"index 必须是非负整数，收到 {index!r}")
        return self.first_delay_minutes + index * self.stagger_minutes

    def to_dict(self) -> dict:
        return {
            "interval_minutes": self.interval_minutes,
            "first_delay_minutes": self.first_delay_minutes,
            "stagger_minutes": self.stagger_minutes,
            "cue": self.cue,
        }


def _bounded(value, label: str, low: int, high: int) -> int:
    # bool 是 int 的子类：True 当成 1 分钟会让一个明显写错的配置跑起来。
    if isinstance(value, bool) or not isinstance(value, int):
        raise SeedingError(f"{label} 必须是整数，收到 {value!r}")
    if not low <= value <= high:
        raise SeedingError(f"{label} 必须落在 {low}–{high}，收到 {value}")
    return value


def seed_activation_id(character_id: str) -> str:
    """这个角色在这个世界里那条开局排期的 ID。确定性，所以撞车会被发现。"""
    if not isinstance(character_id, str) or not character_id:
        raise SeedingError("character_id 必须是非空字符串")
    return f"{SEED_PREFIX}:{character_id}"


def seed_character_activations(
    scheduler,
    character_ids: Sequence[str],
    cadence: ActivationCadence,
) -> Tuple[str, ...]:
    """给一个**新**世界排上每个角色的周期激活，返回排下去的 ID。

    能预先判断的全部在任何一次排入之前判完：节律、角色列表、世界认不认识
    这些角色、时钟对齐、ID 撞车。所以正常路径上不存在"播了一半"。

    真的排到一半失败（调度器又发现了别的问题）时，队列里会留下前几条 ——
    这种情况下**调用方必须整体作废这个世界**，不能接着用。创建路径就是这么
    做的：播种在写第一份存档之前，失败会把所有权还回去、一个字节都不落盘。
    """
    if not isinstance(cadence, ActivationCadence):
        raise SeedingError("cadence 必须是 ActivationCadence")
    ids = list(character_ids)
    if not ids:
        raise SeedingError("一个世界至少需要一个角色才能播种排期")
    if len(set(ids)) != len(ids):
        raise SeedingError("角色列表不能包含重复角色")

    clock = scheduler.clock
    if clock.second or clock.microsecond:
        # 排期只落在整分钟上（见 ScheduledActivation）。时钟带秒的话，
        # 算出来的到期时间也会带秒，于是这条排期永远不会被正好命中。
        raise SeedingError(
            f"世界时钟 {clock.isoformat()} 不在整分钟上，无法确定性地播种排期"
        )

    known = set(scheduler.world.known_characters())
    planned = []
    for index, character_id in enumerate(ids):
        if character_id not in known:
            raise SeedingError(
                f"世界里没有角色 '{character_id}'，不能给它排期"
            )
        activation_id = seed_activation_id(character_id)
        if scheduler.queue.has(activation_id):
            # 已经有了。这只可能是"在一个不该播种的世界上播种"——比如恢复
            # 路径漏了一道闸。响亮失败，绝不静默跳过：静默跳过会让"播了两遍"
            # 和"播对了一遍"看起来一模一样。
            raise SeedingError(
                f"排期 '{activation_id}' 已经在队列里了 —— 这个世界不该被重复播种"
            )
        planned.append(
            ScheduledActivation(
                activation_id=activation_id,
                kind=ActivationKind.CHARACTER_ACTIVATION,
                due_at=clock + timedelta(minutes=cadence.first_due_offset(index)),
                character_id=character_id,
                interval_minutes=cadence.interval_minutes,
                # 角色可见的**只有** cue。这里没有第二个键，也不该有。
                payload={"cue": cadence.cue} if cadence.cue else {},
            )
        )

    for activation in planned:
        scheduler.schedule(activation)
    return tuple(activation.activation_id for activation in planned)


__all__ = [
    "MAX_FIRST_DELAY_MINUTES",
    "MAX_INTERVAL_MINUTES",
    "MAX_STAGGER_MINUTES",
    "SEED_PREFIX",
    "ActivationCadence",
    "SeedingError",
    "seed_activation_id",
    "seed_character_activations",
]
