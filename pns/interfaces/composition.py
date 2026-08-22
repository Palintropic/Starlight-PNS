# pns/interfaces/composition.py — 持久世界的应用组装边界
#
# P12 刻意停在服务面：`WorldLifecycleService` 知道一个世界怎么活、谁拥有它、
# 什么时候落盘，但它不知道**这台服务器**的存档根在哪、初始状态从哪份内容里
# 长出来、判分器背后接的是谁。那三件事是产品决定，属于这一层。
#
# 这一层只拥有四样东西，多一样都不要：
#
#   1. 一个配置好的 `FileWorldStore`，根目录由服务器决定（不接受浏览器传路径）。
#   2. 一个进程内唯一的 `WorldLifecycleService` —— 本进程里"谁拥有这个世界"
#      的唯一权威。
#   3. 从当前**已校验**的 `ContentRegistry` 造一份全新初始 `SessionState` 的
#      工厂。
#   4. 从服务器侧**冷**配置造 `RuntimeAdapters` 的工厂。
#
# 三条硬约束：
#
#   * **构造这个对象不碰磁盘。** 不建目录、不拿锁、不读存档、不起运行时。
#     `FileWorldStore` 和 `WorldLifecycleService` 的构造函数本来就是惰性的，
#     这里不许在它们前面加任何一次 I/O。import 这个模块更不许（有测试盯着）。
#   * **模型凭据、模型客户端、可调用策略、锁和活服务只从服务器侧来。** 它们
#     一个都不进请求体，也一个都不进存档。
#   * **进程收尾要说实话。** 正常关闭走 P12 的安全 close：停准入 → 等事务落定
#     → 最后一次 checkpoint → 归还所有权。这一步失败时**不**调用 release()——
#     release() 会把锁文件里的记录改成 "released"，也就是向下一个拥有者宣布
#     "上一个是干净走的"，而它不是。宁可让锁随进程消失、让下一个拥有者看到
#     `recovered_from`，也不要一句好听的假话。
#
# 配置重载与持久世界的关系（P7 × P12，这里定死）：**重载不被持久世界拒绝，
# 也影响不到已经开着的持久世界。** 理由是机制性的，不是约定：
#
#   * 重载只停 `SessionSupervisor` 里登记过的研究会话。持久世界不在那里登记，
#     也绝不该登记 —— 那等于让一次配置重载去掐一个正在跑的世界。
#   * 一个世界在打开的那一刻就把内容快照（场景、角色、模型设定）锁进了自己的
#     初始状态和适配器闭包里，跟研究会话锁快照是同一条规矩。重载换掉的是
#     全局引用，不是任何一份已经存在的 `WorldState`。
#   * 所以重载只喂**将来**的冷构造：下一次 create 用新内容，已经在跑的世界
#     的时钟、位置、频道成员、事件、观察和记忆一个字都不会被动。
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4

import pns.logic.router as router_mod
from pns.logic.simulation import GenerationTruncated, call_character
from pns.models.agency import AgencyBudget, AgencyError
from pns.models.session import SessionState
from pns.runtime.autonomy.audit import AuditRequest, RouterAuditor
from pns.runtime.autonomy.driver import DriverConfig, DriverError, DriverRegistry
from pns.runtime.autonomy.generation import AuthoredLinePolicy, GenerationError
from pns.runtime.autonomy.prompt import PromptedLineGenerator
from pns.runtime.autonomy.seeding import (
    ActivationCadence,
    SeedingError,
    seed_character_activations,
)
from pns.runtime.content_registry import ContentRegistry
from pns.runtime.memory.recall import MemoryRecall
from pns.runtime.persistence import (
    CheckpointPolicy,
    FileWorldStore,
    LifecycleError,
    RuntimeAdapters,
    WorldLifecycleService,
    validate_world_id,
)
from pns.runtime.reload import BOUNDARY
from pns.world.scene_compat import SceneMappingError

from .paths import DATA_DIR

# 存档根是**服务器配置**，不是浏览器能传的路径。环境变量在调用时才读，不在
# 导入时读 —— 导入这个模块不该固化任何一次进程环境的快照。
WORLD_ROOT_ENV = "PNS_WORLD_ROOT"
DEFAULT_WORLD_ROOT = DATA_DIR / "worlds"

# 自主运行的节律与生成参数。全部是**服务器侧**配置：环境变量在调用时才读，
# 浏览器一个字节都传不进来（见 AutonomySettings.from_env）。
TICK_MINUTES_ENV = "PNS_AUTONOMY_TICK_MINUTES"
INTERVAL_SECONDS_ENV = "PNS_AUTONOMY_INTERVAL_SECONDS"
STOP_TIMEOUT_ENV = "PNS_AUTONOMY_STOP_TIMEOUT"
ACTIVATION_INTERVAL_ENV = "PNS_AUTONOMY_ACTIVATION_INTERVAL_MINUTES"
FIRST_DELAY_ENV = "PNS_AUTONOMY_FIRST_DELAY_MINUTES"
STAGGER_ENV = "PNS_AUTONOMY_STAGGER_MINUTES"
MAX_TOKENS_ENV = "PNS_AUTONOMY_MAX_TOKENS"
TEMPERATURE_ENV = "PNS_AUTONOMY_TEMPERATURE"
ACTIVATIONS_PER_RUN_ENV = "PNS_AUTONOMY_ACTIVATIONS_PER_RUN"
WORLD_ACTION_CAP_ENV = "PNS_AUTONOMY_WORLD_ACTION_CAP"

# 一次生成能配到的 token 上限（配置的上界，不是默认值；默认 1024）。
# 撞到这个上限**不会**变成一句被砍掉一半的台词：那种情况在
# pns/logic/simulation.py 里响亮失败，再由这一层翻译成一次可重试的生成失败。
MAX_GENERATION_TOKENS = 8192

# 一个世界**一生**能提交多少个动作的上界（配置的上界，不是默认值）。
# 它跟单次 Start 的额度是两件事，见 AutonomySettings.world_action_cap。
MAX_WORLD_ACTION_CAP = 10_000_000


class CompositionError(RuntimeError):
    """这台服务器现在组装不出这次操作需要的东西。"""


class ContentUnavailable(CompositionError):
    """请求指定的冷内容（场景、角色）在当前已校验的内容包里不成立。"""


class AdaptersUnavailable(CompositionError):
    """运行时适配器造不出来 —— 缺凭据、缺模型配置，或者客户端起不来。

    它跟"绑定失败"是两件事：这一档是**还没拿到任何所有权**就已经知道跑不起来，
    所以它绝不会留下一个被锁住却没人跑的世界。
    """


def default_world_root() -> Path:
    """本进程的存档根。环境变量优先，否则仓库内 `data/worlds`。"""
    raw = os.environ.get(WORLD_ROOT_ENV, "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_WORLD_ROOT


def _env_number(name: str, default, cast):
    """读一个数值配置。**看不懂就响亮失败，绝不悄悄回落到默认值。**

    悄悄回落是这类配置最坏的行为：操作者把节拍从 30 秒改成 "30s"，服务器
    照常起来、照常按 30 秒跑，而他以为自己改成了别的。
    """
    raw = os.environ.get(name, "")
    if isinstance(raw, str):
        raw = raw.strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError) as e:
        raise CompositionError(f"环境变量 {name} 不是合法数值：{raw!r}") from e


@dataclass(frozen=True)
class AutonomySettings:
    """自主运行的服务器侧设定：节拍、开局排期节律、生成参数。

    它整份是**冷配置**：一个世界在打开的那一刻把它锁进自己的适配器闭包，
    之后改环境变量影响不到已经在跑的世界（跟内容快照同一条规矩）。
    """

    driver: DriverConfig
    cadence: ActivationCadence
    max_tokens: int = 1024
    temperature: float = 0.85
    # 这个世界**一生**能提交多少个动作（P9 的 `AgencyBudget`，计数从耐久的
    # Agency 日志推导，所以跨重启、跨恢复都成立）。
    #
    # P9 的默认值是 128，那个数字是给**研究会话**定的 —— 一局几十轮，128
    # 是个宽松的安全网。持久世界的"一个会话"就是这个世界的一辈子，于是按
    # 默认双角色节拍跑一个半小时就会撞上它，而且撞上之后每一条激活都被静静
    # 判成 rejected_budget，恢复存档也救不回来（计数就在存档里）。那不是
    # 安全网，那是定时哑火。
    #
    # 所以这里给的是一个**世界一生**尺度的数字，而"一次 Start 花多少"由
    # `driver.max_activations_per_run` 单独管、并且按 Start 重置。这个数字
    # 到顶时驱动会响亮停机并说明怎么解开（调高它，然后重新打开这个世界），
    # 不会让世界在没人看得出原因的情况下永远失声。
    world_action_cap: int = 100_000
    # 进程收尾时最多等每个驱动多少秒。比 driver.stop_timeout_seconds 短：
    # 停机不该被一次慢模型调用无限期拖住，而真正挡住"晚到的提交"的是 P11
    # 的终局 stop()，不是这次等待。
    shutdown_timeout_seconds: float = 3.0

    def __post_init__(self) -> None:
        if not isinstance(self.driver, DriverConfig):
            raise CompositionError("driver 必须是 DriverConfig")
        if not isinstance(self.cadence, ActivationCadence):
            raise CompositionError("cadence 必须是 ActivationCadence")
        if isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int):
            raise CompositionError(f"max_tokens 必须是整数，收到 {self.max_tokens!r}")
        if not 1 <= self.max_tokens <= MAX_GENERATION_TOKENS:
            raise CompositionError(
                f"max_tokens 必须落在 1–{MAX_GENERATION_TOKENS}，收到 {self.max_tokens}"
            )
        if isinstance(self.temperature, bool) or not isinstance(
            self.temperature, (int, float)
        ):
            raise CompositionError(f"temperature 必须是数字，收到 {self.temperature!r}")
        if not 0.0 <= float(self.temperature) <= 2.0:
            raise CompositionError(
                f"temperature 必须落在 0–2，收到 {self.temperature}"
            )
        if isinstance(self.shutdown_timeout_seconds, bool) or not isinstance(
            self.shutdown_timeout_seconds, (int, float)
        ):
            raise CompositionError("shutdown_timeout_seconds 必须是数字")
        if not 0 < float(self.shutdown_timeout_seconds) <= 60:
            raise CompositionError("shutdown_timeout_seconds 必须落在 (0, 60]")
        if isinstance(self.world_action_cap, bool) or not isinstance(
            self.world_action_cap, int
        ):
            raise CompositionError(
                f"world_action_cap 必须是整数，收到 {self.world_action_cap!r}"
            )
        if not 1 <= self.world_action_cap <= MAX_WORLD_ACTION_CAP:
            raise CompositionError(
                f"world_action_cap 必须落在 1–{MAX_WORLD_ACTION_CAP}，"
                f"收到 {self.world_action_cap}"
            )
        try:
            self.agency_budget()
        except AgencyError as e:
            raise CompositionError(f"世界一生的动作上限不合法：{e}") from e

    @classmethod
    def from_env(cls) -> "AutonomySettings":
        """从环境变量读一份设定。校验全部由被构造的那几个对象自己做。"""
        try:
            driver = DriverConfig(
                tick_minutes=_env_number(TICK_MINUTES_ENV, 5, int),
                interval_seconds=_env_number(INTERVAL_SECONDS_ENV, 30.0, float),
                stop_timeout_seconds=_env_number(STOP_TIMEOUT_ENV, 10.0, float),
                max_activations_per_run=_env_number(
                    ACTIVATIONS_PER_RUN_ENV, 200, int
                ),
            )
            cadence = ActivationCadence(
                interval_minutes=_env_number(ACTIVATION_INTERVAL_ENV, 15, int),
                first_delay_minutes=_env_number(FIRST_DELAY_ENV, 5, int),
                stagger_minutes=_env_number(STAGGER_ENV, 5, int),
            )
        except (DriverError, SeedingError) as e:
            raise CompositionError(f"自主运行配置不合法：{e}") from e
        return cls(
            driver=driver,
            cadence=cadence,
            max_tokens=_env_number(MAX_TOKENS_ENV, 1024, int),
            temperature=_env_number(TEMPERATURE_ENV, 0.85, float),
            world_action_cap=_env_number(WORLD_ACTION_CAP_ENV, 100_000, int),
        )

    def agency_budget(self) -> AgencyBudget:
        """这个世界的 P9 预算。**每次新造一份** —— 它是冷配置，不共享实例。

        除了那条一生的动作上限，其余项一律沿用 P9 自己的默认：它们是每次
        判断的形状预算（一次激活最多几条提案、枚举多少合法动作、喂多少条
        观察），跟世界活多久没关系。
        """
        return AgencyBudget(max_committed_actions_per_session=self.world_action_cap)

    def to_dict(self) -> Dict:
        return {
            **self.driver.to_dict(),
            "activation": self.cadence.to_dict(),
            "max_tokens": self.max_tokens,
            "temperature": float(self.temperature),
            "world_action_cap": self.world_action_cap,
        }


def _new_session_id(world_id: str) -> str:
    """新世界的会话身份。由服务器生成 —— 浏览器不许决定一个世界叫什么。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{world_id}_{stamp}_{uuid4().hex[:12]}"


class WorldControlPlane:
    """一个进程里持久世界的组装边界。

    `registry_provider` 和 `client_factory` 是显式的依赖缝，不是开关：生产上
    它们就是 `BOUNDARY.active` 和 `pns.logic.router.create_client`，测试可以
    换掉它们从而不联网跑完整条接口路径。没有第三条"测试模式"分支。
    """

    def __init__(
        self,
        *,
        root: Optional[Path] = None,
        registry_provider: Optional[Callable[[], ContentRegistry]] = None,
        client_factory: Optional[Callable[..., object]] = None,
        checkpoint_policy: Optional[CheckpointPolicy] = None,
        autonomy: Optional[AutonomySettings] = None,
    ) -> None:
        self._root = Path(root) if root is not None else default_world_root()
        # 两个构造函数都不碰磁盘。这一行之后，进程里依然没有目录、没有锁。
        self._store = FileWorldStore(self._root)
        self._service = WorldLifecycleService(self._store)
        self._registry_provider = (
            registry_provider if registry_provider is not None else BOUNDARY.active
        )
        self._client_factory = (
            client_factory if client_factory is not None else router_mod.create_client
        )
        self._autonomy = autonomy if autonomy is not None else AutonomySettings.from_env()
        if not isinstance(self._autonomy, AutonomySettings):
            raise CompositionError("autonomy 必须是 AutonomySettings")
        # 自动 checkpoint 需要一个驱动方在**已经完成的权威边界**上来问一句
        # （`checkpoint_if_due`）。WEB-1 没有那个驱动方，所以那时开自动策略
        # 等于承诺一件没人兑现的事；MVP-1 有了（见 pns/runtime/autonomy/
        # driver.py），所以这里才敢默认开着：每个边界问一次，最快一分钟落
        # 一次盘。合并规则由 P12 的 CheckpointPolicy 自己管，这里不另写一份。
        self._policy = (
            checkpoint_policy
            if checkpoint_policy is not None
            else CheckpointPolicy(every_boundaries=1, min_interval_seconds=60.0)
        )
        # 进程内"哪个世界有人在推"的账本。它跟生命周期服务是两本账，而且
        # 刻意不合并：恢复一个世界不该顺手把它的模型调用也接着跑起来。
        self._drivers = DriverRegistry(self._autonomy.driver)
        self._shutdown_lock = threading.Lock()

    # ── 读 ──────────────────────────────────────────────────────────────
    @property
    def root(self) -> Path:
        return self._root

    @property
    def store(self) -> FileWorldStore:
        return self._store

    @property
    def service(self) -> WorldLifecycleService:
        """本进程里生命周期的唯一权威。接口层不在它之外维护任何一份真相。"""
        return self._service

    @property
    def checkpoint_policy(self) -> CheckpointPolicy:
        return self._policy

    @property
    def autonomy(self) -> AutonomySettings:
        return self._autonomy

    @property
    def drivers(self) -> DriverRegistry:
        return self._drivers

    def registry(self) -> ContentRegistry:
        return self._registry_provider()

    # ── 冷内容 → 一份全新的初始状态 ─────────────────────────────────────
    def new_session_state(
        self,
        *,
        world_id: str,
        scene_id: str,
        character_ids: Sequence[str],
        registry: ContentRegistry,
    ) -> SessionState:
        """从当前已校验内容造一份**新的**初始 SessionState。

        场景 id 在这里显式查表，不走 `registry.scene()` —— 那条路对未知 id 会
        静静回落到默认场景，而"我建的是 A，服务器给了我 B"是一次不该发生的
        无声替换。
        """
        if scene_id not in registry.scenes:
            raise ContentUnavailable(
                f"场景 '{scene_id}' 不在当前内容包（{registry.pack_name}）中"
            )
        scene = registry.scene(scene_id)

        ids = list(character_ids)
        if not ids:
            raise ContentUnavailable("一个世界至少需要一个角色")
        if len(set(ids)) != len(ids):
            raise ContentUnavailable("角色列表不能包含重复角色")
        for cid in ids:
            if not registry.has_character(cid):
                raise ContentUnavailable(
                    f"角色 '{cid}' 不在当前角色包（{registry.pack_name}）中"
                )
            if registry.character(cid).system_prompt is None:
                # 没有提示词的角色进得了世界，但一句话也说不出来：它的每一次
                # 激活都会在生成那一步失败。MVP-1 之前这只是"不会说话"，
                # 现在它是"每个周期烧一次失败的激活"，所以在建世界的时候就
                # 拒绝 —— 而且是在拿所有权之前。
                raise ContentUnavailable(
                    f"角色 '{cid}' 在当前角色包（{registry.pack_name}）里还没有"
                    "可用的提示词，不能进一个要自己开口的世界"
                )

        try:
            world_state = registry.new_world_state(scene, ids)
        except SceneMappingError as e:
            raise ContentUnavailable(str(e)) from e

        state = SessionState(
            session_id=_new_session_id(world_id),
            scene=scene["id"],
            characters=ids,
        )
        state.attach_world_state(world_state)
        state.initialize_runtime(scene["trigger"])
        return state

    # ── 服务器侧冷配置 → 运行时适配器 ───────────────────────────────────
    def build_adapters(
        self,
        registry: ContentRegistry,
        *,
        seed: Optional[Callable[[SessionState], None]] = None,
    ) -> RuntimeAdapters:
        """造这个世界跑起来需要、但存档里没有的那些东西。

        判分器是真的 —— `RouterAuditor` 包着 `pns.logic.router.judge`，判分
        失败绝不退化成通过（见 audit.py）。凭据从服务器侧的内容快照来，
        既不进请求体也不进存档。

        策略也是真的：`AuthoredLinePolicy` 绑一个走真实模型的生成器
        （`PromptedLineGenerator`）。WEB-1 时这里刻意留空、退回
        `AbstainPolicy`，因为那时没有生成层 —— 现在有了，所以**绝不**再退回去：
        配置不全就在这里响亮失败（此刻还没有任何所有权被拿走），而不是开一个
        看起来正常、其实永远不说话的世界。

        四样东西的来源写死在这里，一样都不来自模型输出或请求体：

          * 客户端与凭据 —— 服务器侧的内容快照，`client_factory` 造一次；
          * 显示名 —— 冻结的 `ContentRegistry`；
          * 召回 —— 绑在**这一份**恢复/新建出来的状态上（所以是工厂不是实例）；
          * 判分 —— 真实 `RouterAuditor`，是台词进世界历史的唯一通道。

        `seed` 只在**创建**新世界时传进来。恢复路径不传，而且 P12 那侧会拒绝
        一份带播种器的恢复（见 `WorldLifecycleService.restore`）。
        """
        models = registry.models
        api_key = models.api_key
        if not api_key:
            raise AdaptersUnavailable(
                "还没有配置模型 API Key，判分器建不起来；请先在设置里完成配置"
            )
        if not models.generator_model:
            raise AdaptersUnavailable(
                "还没有配置生成模型，角色开不了口；请先在设置里完成配置"
            )
        try:
            client = self._client_factory(api_key, settings=models)
        except Exception as e:
            # 这个工厂**收到过 API Key**，所以从它这里出来的任何东西都是不可信
            # 数据 —— 包括异常本身。
            #
            # 消息原文会带 key（"provider rejected sk-…" 是真实形状），这一点
            # 显而易见。不显而易见的是**类型名也会**：
            #
            #     raise type(api_key, (RuntimeError,), {})("rejected")
            #
            # Python 不校验类名，于是 `type(e).__name__` 就是那把 key。所以
            # "类型名装不下一把 key"是错的，而只要还有**任何**一处从异常派生
            # 的数据能过边界，这条边界就还是漏的。
            #
            # 因此对外这句话是**完全固定**的：不含 str(e)、repr(e)、类型名、
            # 也不含异常的任何属性。原始异常留在 __cause__ 里，谁在服务器侧
            # 调试谁自己去看，不经过这条边界；这里同样刻意不打日志 —— 把原文
            # 打出去只是换个地方泄漏。
            raise AdaptersUnavailable(
                "判分模型客户端建不起来；请检查服务器侧的 provider 与凭据配置"
            ) from e

        def judge(request: AuditRequest) -> object:
            # `recent_lines` 是这个角色生成前自己看到的几行，只进判分提示，
            # 不进事件、观察或记忆（见 audit.py 的 AuditRequest）。
            return router_mod.judge(
                client,
                request.character_id,
                request.text,
                # 自主路径上没有"第几轮"这个概念；它只出现在判分提示的措辞里。
                0,
                recent_history=[
                    {"role": "assistant", "content": line}
                    for line in request.recent_lines
                ],
                registry=registry,
            )

        # 地点图与频道表是**冻结内容**（cold），只用来把 id 渲染成显示名。
        # 每个世界拿一份新的，不跨世界共享可变对象。
        locations = registry.new_location_graph()
        channels = registry.new_channel_registry()

        def call_model(character_id: str, view, history: List[Dict]) -> object:
            # `view` 是角色作用域的世界投影（pns/runtime/autonomy/prompt.py），
            # **不是** WorldState：问它别人在哪，它抛错而不是回答。
            try:
                return call_character(
                    client,
                    character_id,
                    history,
                    view,
                    models.generator_model,
                    self._autonomy.max_tokens,
                    self._autonomy.temperature,
                    registry=registry,
                )
            except GenerationTruncated as e:
                # 拿到了半句话。它长得跟一句完整的话一模一样，所以必须在这里
                # 就拦掉：提交下去就是让角色说了一句它没说完的话。可重试 ——
                # 下一次采样很可能就说得完。消息是我们自己的话，不含 provider
                # 那侧的任何东西。
                raise GenerationError(
                    "模型输出在长度上限处被截断，这一句没说完", retryable=True
                ) from e

        # 显示名整份从冻结内容包取一次：生成上下文里出现的每一个名字都来自
        # 这里，绝不来自模型输出。
        names = {
            cid: registry.character_name(cid) for cid in sorted(registry.characters)
        }
        generator = PromptedLineGenerator(
            call_model, locations=locations, channels=channels, names=names
        )

        def policy_factory(state: SessionState) -> AuthoredLinePolicy:
            # 召回绑在**这一份**状态上，所以每个角色只可能召回自己的记忆
            # （收窄那一行在 AuthoredLinePolicy._generation_context 里）。
            # 显示名来自冻结内容包，绝不来自模型输出。
            return AuthoredLinePolicy(
                generator, recall=MemoryRecall(state), names=names
            )

        return RuntimeAdapters(
            auditor=RouterAuditor(
                judge,
                threshold=models.ooc_threshold,
                evaluator_model=models.evaluator_model,
                evaluator_provider=models.provider,
                generator_model=models.generator_model,
                generator_provider=models.provider,
            ),
            policy_factory=policy_factory,
            # 世界一生的动作上限。P9 的默认 128 是给研究会话定的，用在一个
            # 持久世界上等于给它设了个定时哑火（见 AutonomySettings）。
            budget=self._autonomy.agency_budget(),
            seed=seed,
        )

    # ── 生命周期操作 ────────────────────────────────────────────────────
    #
    # 这些方法只做组装和转交。所有权、修订号、dirty、running 的判断一律在
    # `WorldLifecycleService` 那一侧，这里不复制、不缓存、不预判。
    def create(
        self, *, world_id: str, scene_id: str, character_ids: Sequence[str]
    ) -> Dict:
        # 先过 ID：不合法的 ID 不该先把一份初始世界造出来再被拒。
        name = validate_world_id(world_id)
        registry = self.registry()

        def seed(bound: SessionState) -> None:
            # 开局排期。它在第一份存档写下去**之前**跑，所以要么这个世界带着
            # 排期诞生，要么它根本没诞生 —— 不存在"建好了但永远不会动"的世界。
            try:
                seed_character_activations(
                    bound.scheduler, bound.characters, self._autonomy.cadence
                )
            except SeedingError as e:
                raise ContentUnavailable(f"这个世界的开局排期播不下去：{e}") from e

        # 适配器先造：它失败时还没有任何所有权被拿走，也还没有白造一份初始
        # 世界出来。
        adapters = self.build_adapters(registry, seed=seed)
        state = self.new_session_state(
            world_id=name,
            scene_id=scene_id,
            character_ids=character_ids,
            registry=registry,
        )
        world = self._service.create(
            name,
            state,
            adapters=adapters,
            checkpoint_policy=self._policy,
        )
        # 新世界的驱动是**停着**的。自动模型调用必须由操作者显式开启。
        return self._with_driver(world.status())

    def restore(self, world_id: str) -> Dict:
        name = validate_world_id(world_id)
        # 恢复不带播种器：存档里已经有这个世界自己的排期队列了。
        adapters = self.build_adapters(self.registry())
        world = self._service.restore(
            name,
            adapters=adapters,
            checkpoint_policy=self._policy,
        )
        # 这个世界上一次可能是"在跑着"被关掉/被杀掉的，但恢复之后驱动仍然
        # 是停着的：一次进程重启不该自己接着烧 API 额度。
        self._drivers.discard(name)
        return self._with_driver(world.status())

    def checkpoint(self, world_id: str) -> Dict:
        return self._with_driver(self._service.checkpoint(world_id, "manual"))

    def close(self, world_id: str) -> Dict:
        # 刻意不透出 force：`close(force=True)` 会在最后一次 checkpoint 失败时
        # 照样归还所有权，代价是丢掉上一次成功 checkpoint 之后的全部工作。
        # 那是一次明确的人为放弃决定，不该做成一个后台按钮。
        name = validate_world_id(world_id)
        # 先请驱动停下并有界地等它落定。等不到也照样往下走：真正挡住"晚到的
        # 提交"的不是这次等待，而是 close() 里 P11 的终局 stop() —— 它返回
        # 之后没有任何提交能落地。这次等待只是为了让常见情况干净一点。
        self._drivers.stop(name, "world closing")
        status = self._service.close(name, "closed")
        # 世界已经终局关闭，晚到的 worker 已经不可能提交任何东西了。再有界地
        # 收一次尾，只为不留下一个空转的线程 —— 这次等待很短：运行时已经停了，
        # worker 下一轮就自己收摊，而 stop() 会把它从节拍等待里叫醒。
        self._drivers.stop(
            name, "world closed", self._autonomy.shutdown_timeout_seconds
        )
        # 关成功了才丢掉驱动。没关成功的世界还开着，操作者可能重试。
        self._drivers.discard(name)
        return self._with_driver(status)

    def status(self, world_id: str) -> Dict:
        return self._with_driver(self._service.status(world_id))

    def list_worlds(self) -> Tuple[Dict, ...]:
        return tuple(self._with_driver(item) for item in self._service.list_worlds())

    # ── 自主驱动（MVP-1）────────────────────────────────────────────────
    #
    # 这两个操作跟生命周期是**两件事**，而且必须看得出来是两件事：P12 的
    # `running` 说的是"这个世界的运行时还接不接受写入"，驱动的 `state` 说的
    # 是"这台服务器此刻在不在推它"。一个世界完全可以 running=True 而驱动
    # 停着 —— 那正是"开着但没人推"，也是新建/恢复之后的默认状态。
    def start_autonomy(self, world_id: str) -> Dict:
        """开始自动推这个世界。这是**唯一**会让服务器自己花 API 额度的入口。"""
        world = self._require_open(world_id)
        self._drivers.for_world(world).start()
        return self._with_driver(world.status())

    def stop_autonomy(self, world_id: str, reason: str = "operator") -> Dict:
        """请驱动暂停。它是可重启的暂停，不动 P11 的终局停机。"""
        world = self._require_open(world_id)
        driver = self._drivers.get(world.world_id)
        if driver is not None:
            driver.stop(reason)
        return self._with_driver(world.status())

    def autonomy_status(self, world_id: str) -> Optional[Dict]:
        driver = self._drivers.get(validate_world_id(world_id))
        return driver.status() if driver is not None else None

    def _require_open(self, world_id: str):
        name = validate_world_id(world_id)
        world = self._service.opened(name)
        if world is None:
            raise LifecycleError(
                f"世界 '{name}' 没有在本进程里开着 —— 先创建或恢复它"
            )
        return world

    def _with_driver(self, status: Dict) -> Dict:
        """把驱动状态挂到一份世界状态上。没有驱动就是 null，不是"停着"。

        `null` 的意思很具体：**这台服务器从来没为这个世界起过驱动**。它跟
        "起过、现在停着"不是一回事，而后者要能被看见 —— 一个刚被停下来的
        驱动还带着上一次 tick 的错误，那正是操作者要看的东西。
        """
        status["autonomy"] = self.autonomy_status(status["world_id"])
        return status

    # ── 进程收尾 ────────────────────────────────────────────────────────
    def shutdown(self, reason: str = "server shutdown") -> List[Dict]:
        """对本进程打开的每个世界尝试一次安全关闭，并如实报告结果。

        失败的那个**不**被 release：release() 会把锁记录写成 "released"，
        下一个拥有者于是看不到 `recovered_from`，也就是被告知"上一个是干净
        走的"。最后一次 checkpoint 都没成的世界不配这句话。锁本来就跟进程
        同生死，让内核收走它、让下一个拥有者看见真相，比一句好听的假话好。

        这里不额外承诺什么：进程被强杀时能恢复到的仍然只有最后一次成功的
        checkpoint（P12 的恢复边界，WEB-1 不加 WAL）。

        顺序是刻意的：**先请所有驱动停下**，再逐个关世界。等待是有界的
        （`shutdown_timeout_seconds`），因为一次慢模型调用不该把整个停机拖住；
        真正挡住"晚到的提交"的仍然是 close() 里 P11 的终局 stop()。
        """
        with self._shutdown_lock:
            self._drivers.stop_all(reason, self._autonomy.shutdown_timeout_seconds)
            reports: List[Dict] = []
            for summary in self._service.list_worlds():
                name = summary["world_id"]
                if self._service.opened(name) is None:
                    continue
                try:
                    status = self._service.close(name, reason)
                except Exception as e:
                    reports.append(
                        {
                            "world_id": name,
                            "closed": False,
                            "clean": False,
                            "revision": summary.get("revision"),
                            "error": f"{type(e).__name__}: {e}",
                        }
                    )
                    continue
                self._drivers.discard(name)
                reports.append(
                    {
                        "world_id": name,
                        "closed": bool(status["closed"]),
                        "clean": bool(status["clean"]),
                        "revision": status["revision"],
                        "error": status["last_error"],
                    }
                )
            return reports


__all__ = [
    "ACTIVATIONS_PER_RUN_ENV",
    "ACTIVATION_INTERVAL_ENV",
    "DEFAULT_WORLD_ROOT",
    "FIRST_DELAY_ENV",
    "INTERVAL_SECONDS_ENV",
    "MAX_GENERATION_TOKENS",
    "MAX_TOKENS_ENV",
    "MAX_WORLD_ACTION_CAP",
    "STAGGER_ENV",
    "STOP_TIMEOUT_ENV",
    "TEMPERATURE_ENV",
    "TICK_MINUTES_ENV",
    "WORLD_ACTION_CAP_ENV",
    "WORLD_ROOT_ENV",
    "AdaptersUnavailable",
    "AutonomySettings",
    "CompositionError",
    "ContentUnavailable",
    "WorldControlPlane",
    "default_world_root",
]
