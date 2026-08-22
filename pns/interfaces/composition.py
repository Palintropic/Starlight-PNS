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
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4

import pns.logic.router as router_mod
from pns.models.session import SessionState
from pns.runtime.autonomy.audit import AuditRequest, RouterAuditor
from pns.runtime.content_registry import ContentRegistry
from pns.runtime.persistence import (
    CheckpointPolicy,
    FileWorldStore,
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
        # 仓库里唯一安全的产品级默认：手动 checkpoint + 干净关闭时存一次。
        # 自动 checkpoint 需要一个驱动方按边界来问（`checkpoint_if_due`），
        # WEB-1 没有那个驱动方，所以在这里开自动策略等于承诺一件没人兑现的事。
        self._policy = (
            checkpoint_policy if checkpoint_policy is not None else CheckpointPolicy()
        )
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
    def build_adapters(self, registry: ContentRegistry) -> RuntimeAdapters:
        """造这个世界跑起来需要、但存档里没有的那些东西。

        判分器是真的 —— `RouterAuditor` 包着 `pns.logic.router.judge`，判分
        失败绝不退化成通过（见 audit.py）。凭据从服务器侧的内容快照来，
        既不进请求体也不进存档。

        `policy_factory` 留空，于是 Agency 引擎用仓库自己的默认策略
        `AbstainPolicy`：**没有接生成层的运行时，最诚实的行为是什么都不做。**
        让角色真的开口需要一个模型驱动的 `LineGenerator` 适配器，仓库里目前
        只有确定性的 `ScriptedLineGenerator`（参照实现，不是产品实现）。补上
        那个适配器是 MVP-1 的事，不是 WEB-1 的 —— 在这里塞一个占位策略，等于
        让操作台宣布一件它做不到的事。
        """
        models = registry.models
        api_key = models.api_key
        if not api_key:
            raise AdaptersUnavailable(
                "还没有配置模型 API Key，判分器建不起来；请先在设置里完成配置"
            )
        try:
            client = self._client_factory(api_key, settings=models)
        except Exception as e:
            raise AdaptersUnavailable(
                f"判分模型客户端建不起来: {type(e).__name__}: {e}"
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

        return RuntimeAdapters(
            auditor=RouterAuditor(
                judge,
                threshold=models.ooc_threshold,
                evaluator_model=models.evaluator_model,
                evaluator_provider=models.provider,
                generator_model=models.generator_model,
                generator_provider=models.provider,
            )
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
        # 适配器先造：它失败时还没有任何所有权被拿走，不会留下一个锁着却
        # 没人跑的世界。
        adapters = self.build_adapters(registry)
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
        return world.status()

    def restore(self, world_id: str) -> Dict:
        name = validate_world_id(world_id)
        adapters = self.build_adapters(self.registry())
        world = self._service.restore(
            name,
            adapters=adapters,
            checkpoint_policy=self._policy,
        )
        return world.status()

    def checkpoint(self, world_id: str) -> Dict:
        return self._service.checkpoint(world_id, "manual")

    def close(self, world_id: str) -> Dict:
        # 刻意不透出 force：`close(force=True)` 会在最后一次 checkpoint 失败时
        # 照样归还所有权，代价是丢掉上一次成功 checkpoint 之后的全部工作。
        # 那是一次明确的人为放弃决定，不该做成一个后台按钮。
        return self._service.close(world_id, "closed")

    def status(self, world_id: str) -> Dict:
        return self._service.status(world_id)

    def list_worlds(self) -> Tuple[Dict, ...]:
        return self._service.list_worlds()

    # ── 进程收尾 ────────────────────────────────────────────────────────
    def shutdown(self, reason: str = "server shutdown") -> List[Dict]:
        """对本进程打开的每个世界尝试一次安全关闭，并如实报告结果。

        失败的那个**不**被 release：release() 会把锁记录写成 "released"，
        下一个拥有者于是看不到 `recovered_from`，也就是被告知"上一个是干净
        走的"。最后一次 checkpoint 都没成的世界不配这句话。锁本来就跟进程
        同生死，让内核收走它、让下一个拥有者看见真相，比一句好听的假话好。

        这里不额外承诺什么：进程被强杀时能恢复到的仍然只有最后一次成功的
        checkpoint（P12 的恢复边界，WEB-1 不加 WAL）。
        """
        with self._shutdown_lock:
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
    "DEFAULT_WORLD_ROOT",
    "WORLD_ROOT_ENV",
    "AdaptersUnavailable",
    "CompositionError",
    "ContentUnavailable",
    "WorldControlPlane",
    "default_world_root",
]
