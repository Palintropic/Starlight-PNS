# pns/interfaces/persistent_worlds.py — 持久世界的 HTTP 边界
#
#     GET  /api/persistent-worlds
#     GET  /api/persistent-worlds/{world_id}
#     POST /api/persistent-worlds
#     POST /api/persistent-worlds/{world_id}/restore
#     POST /api/persistent-worlds/{world_id}/checkpoint
#     POST /api/persistent-worlds/{world_id}/close
#     POST /api/persistent-worlds/{world_id}/autonomy/start
#     POST /api/persistent-worlds/{world_id}/autonomy/stop
#
# 这一层只做三件事：把请求翻译成一次生命周期调用、把 P12 的状态词汇原样交出去、
# 把失败翻译成一个稳定的类别 + 一句安全的话。它**不**判断一个世界能不能被
# checkpoint、归谁所有、是不是脏的 —— 那些判断只有一处，在
# `WorldLifecycleService` 里，而且必须只有一处：接口层复制一份，就会出现两个
# 都自称权威的答案，而浏览器看到的那个还慢一拍。
#
# 三条硬约束：
#
#   1. **绝不 `except Exception` 之后返回 200。** 每一档失败都有自己的 HTTP
#      状态和类别；认不出来的那一档是 500 `internal_error`，配一句不带堆栈、
#      不带内部结构的话。生命周期的确切失败必须**穿过**这层边界，而不是在
#      这里被抹平成"操作失败"。
#   2. **返回的是脱钩的数据。** P12 的 status() 每次返回全新结构，模型再拷一遍；
#      浏览器改自己那份，改不到进程里的任何状态。
#   3. **请求作用在服务器此刻的状态上。** 浏览器的判断可能已经过期，那就让它
#      拿到 409/404，然后重新拉一次权威状态 —— 而不是把过期的假设强推下去。
#
# 存档根是服务器配置。这里没有任何一条路径来自请求体，world_id 也只经过 P12
# 的 `validate_world_id`，不在这一层另写一套 ID 或路径规则。
from contextlib import contextmanager
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from pns.runtime.agency.policy import AgencyPolicyError
from pns.runtime.autonomy.audit import AuditError
from pns.runtime.autonomy.coordinator import AutonomyError
from pns.runtime.autonomy.driver import DriverBusy, DriverError
from pns.runtime.persistence import (
    ArchiveCorrupt,
    ArchiveError,
    ArchiveNotDurable,
    ArchiveNotFound,
    CheckpointError,
    LifecycleError,
    OwnershipError,
    OwnershipUnsupported,
    StorageError,
    WorldAlreadyOwned,
    WorldIdError,
)

from .composition import AdaptersUnavailable, ContentUnavailable, WorldControlPlane

router = APIRouter(prefix="/api/persistent-worlds", tags=["persistent-worlds"])

# 错误正文的长度上限。存档层的报错会带上出错位置和片段，敌对存档可以把它撑得
# 很长；截断让"一份坏存档"不能变成一条能撑爆后台的响应。
MAX_DETAIL_CHARS = 400


# ── 响应模型 ────────────────────────────────────────────────────────────
#
# 字段名沿用 P12 的状态词汇，一个都不为了 UI 好看而改名：后台和这一层对同一
# 件事必须用同一个词，否则读代码的人要在两套词汇之间做翻译。
class OwnerRecordModel(BaseModel):
    world_id: str
    pid: int
    host: str
    acquired_at: str
    renewed_at: str
    state: str


class CheckpointPolicyModel(BaseModel):
    every_boundaries: Optional[int] = None
    min_interval_seconds: float = 0.0
    on_close: bool = True


class DriverCadenceModel(BaseModel):
    """驱动的节拍。服务器侧配置，浏览器只能读。"""

    tick_minutes: int
    interval_seconds: float
    stop_timeout_seconds: float


class DriverTickModel(BaseModel):
    """上一次 tick 的样子。失败的那次只有 `failed`。"""

    failed: bool = False
    from_clock: Optional[str] = None
    to_clock: Optional[str] = None
    minutes: Optional[int] = None
    due: Optional[int] = None
    processed: Optional[int] = None
    outcomes: Dict[str, int] = Field(default_factory=dict)
    checkpoint_revision: Optional[int] = None


class DriverStatusModel(BaseModel):
    """自主驱动此刻的样子。

    它跟 P12 的 `running` 是**两件事**，而且这个区分必须保住：`running` 说的
    是"这个世界的运行时还接不接受写入"，`state` 说的是"这台服务器此刻在不在
    推它"。一个 running=True 而 state=stopped 的世界就是"开着但没人推"——
    那正是新建和恢复之后的默认状态，因为自动模型调用是 opt-in 的。
    """

    world_id: str
    # running / stopping / stopped。stopping 的意思很具体：**还没停干净**，
    # 当前那次 tick 可能仍会落地一次提交。
    state: str
    running: bool
    stopping: bool
    stopped: bool
    stop_reason: Optional[str] = None
    # worker 自己收摊的原因（世界关了、运行时终局停机了）。
    exit_reason: Optional[str] = None
    ticks: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_error: Optional[str] = None
    last_tick_at: Optional[str] = None
    last_tick: Optional[DriverTickModel] = None
    # 下一条排期到期的**模拟**时刻。
    next_due_at: Optional[str] = None
    cadence: DriverCadenceModel


class WorldStatusModel(BaseModel):
    """一个世界此刻的样子。字段含义与 P12 `PersistentWorld.status()` 一致。

    `null` 与 `false` 不是一回事，而且这个区分是本模型的要害：本进程没开着的
    世界，它的 running / dirty / clean 是**不知道**（null），不是"否"。
    """

    world_id: str
    session_id: Optional[str] = None
    revision: Optional[int] = None
    # 能恢复到的那一版。正常情况下等于 revision。
    durable_revision: Optional[int] = None
    dirty: Optional[bool] = None
    closed: Optional[bool] = None
    clean: Optional[bool] = None
    # 「本进程此刻持有这个世界的所有权」。它不回答"别的进程是不是拥有它"——
    # 那个问题只能靠去抢一次锁来回答，而状态查询不该有副作用。
    owned: bool = False
    owner: Optional[OwnerRecordModel] = None
    # 上一个拥有者**崩掉**时留下的记录。干净释放过的世界这里是 null。
    recovered_from: Optional[OwnerRecordModel] = None
    last_saved_at: Optional[str] = None
    last_checkpoint_reason: Optional[str] = None
    # 磁盘上那一版的耐久性。false = 在那儿、读得回来，但掉电之后可能回到上一版；
    # null = 这份句柄由存档恢复而来，没有携带可验证的目录同步证据。
    durable: Optional[bool] = None
    directory_synced: Optional[bool] = None
    last_error: Optional[str] = None
    # 只读这个世界的状态时发生的错误（比如存档读不出来）。它不代表操作失败。
    error: Optional[str] = None
    residue: List[str] = Field(default_factory=list)
    running: Optional[bool] = None
    stop_reason: Optional[str] = None
    clock: Optional[str] = None
    archive_path: Optional[str] = None
    boundaries_since_checkpoint: Optional[int] = None
    policy: Optional[CheckpointPolicyModel] = None
    # 本进程有没有在推这个世界。`null` 的意思是**从来没为它起过驱动**，
    # 跟"起过、现在停着"不是一回事 —— 后者还带着上一次 tick 的错误。
    autonomy: Optional[DriverStatusModel] = None


class WorldListModel(BaseModel):
    worlds: List[WorldStatusModel]


class CreateWorldRequest(BaseModel):
    """建一个新世界需要的、而且**只需要**的东西。

    这里刻意收得很紧：只有 ID 和一份能被当前内容包校验的冷起始内容。模型凭据、
    模型客户端、可调用策略、锁、存档路径、会话身份一律是服务器侧的东西 ——
    它们既不该从浏览器进来，也不该进存档。
    """

    # 长度上限在这里就收掉：这三样都会变成报错文本里的字面量，不设上限的话
    # 一个巨大的请求体就能变成一条巨大的响应。真正的合法性判断仍然在服务器侧
    # （P12 的 validate_world_id、内容包查表），这里只是不让废话走那么远。
    world_id: str = Field(min_length=1, max_length=64)
    scene: str = Field(min_length=1, max_length=128)
    characters: List[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        min_length=1, max_length=32
    )


# ── 依赖 ────────────────────────────────────────────────────────────────
def get_control_plane(request: Request) -> WorldControlPlane:
    plane = getattr(request.app.state, "world_control_plane", None)
    if plane is None:  # pragma: no cover - create_app 总会装上一个
        raise _error(503, "control_plane_unavailable", "本进程没有装配持久世界组装边界")
    return plane


# ── 错误翻译 ────────────────────────────────────────────────────────────
def _safe(message: object) -> str:
    text = " ".join(str(message).split())
    if len(text) > MAX_DETAIL_CHARS:
        return text[: MAX_DETAIL_CHARS - 1] + "…"
    return text


def _error(status: int, category: str, message: object) -> HTTPException:
    return HTTPException(status, {"category": category, "message": _safe(message)})


def _translate(
    e: Exception, plane: WorldControlPlane, world_id: Optional[str], op: str
) -> HTTPException:
    """把一次生命周期失败翻译成一个稳定的类别。

    顺序是按继承关系排的，子类在前：`ArchiveNotFound` 和 `ArchiveNotDurable`
    都是 `StorageError`，`ArchiveCorrupt` 是 `ArchiveError`，`CheckpointError`
    是 `LifecycleError`。排错了就会把一次 404 说成 500。
    """
    if isinstance(e, WorldIdError):
        return _error(400, "invalid_world_id", e)
    if isinstance(e, ContentUnavailable):
        return _error(400, "invalid_content", e)
    if isinstance(e, AdaptersUnavailable):
        return _error(503, "adapters_unavailable", e)
    if isinstance(e, DriverBusy):
        # 上一个 worker 还没走干净。这不是"已经在跑"（那是幂等成功），
        # 是说不清 —— 所以它必须是一次失败，让操作者再等一下重试。
        return _error(409, "autonomy_busy", e)
    if isinstance(e, DriverError):
        return _error(409, "autonomy_refused", e)
    if isinstance(e, WorldAlreadyOwned):
        return _error(409, "world_already_open", e)
    if isinstance(e, OwnershipUnsupported):
        return _error(503, "ownership_unsupported", e)
    if isinstance(e, OwnershipError):
        return _error(409, "ownership_lost", e)
    if isinstance(e, ArchiveNotFound):
        return _error(404, "archive_not_found", e)
    if isinstance(e, ArchiveNotDurable):
        # 这一档方向特殊：那一版**已经在磁盘上**了，只是耐久性证实不了。
        # 所以它不是"没保存"，而是"保存了但保证不到"。
        return _error(500, "archive_not_durable", e)
    if isinstance(e, CheckpointError) and isinstance(e.__cause__, ArchiveNotDurable):
        # P12 会把这一档包进 CheckpointError（它必须抛，否则会被当成一次干净的
        # checkpoint），但包装不该把区分也一起吃掉：后台要知道该重试，还是该去
        # 确认那块盘。判据取自 `__cause__` 的类型，不是错误文本。
        return _error(500, "archive_not_durable", e)
    if isinstance(e, ArchiveCorrupt):
        return _error(422, "archive_corrupt", e)
    if isinstance(e, ArchiveError):
        return _error(422, "archive_unusable", e)
    if isinstance(e, CheckpointError):
        return _error(500, "checkpoint_failed", e)
    if isinstance(e, StorageError):
        return _error(500, "storage_failed", e)
    if isinstance(e, (AutonomyError, AgencyPolicyError, AuditError)):
        # 所有权已经拿到、状态也恢复出来了，但服务绑不上去或起不来。
        # P12 已经把所有权还回去了，所以这里不需要善后，只需要说清楚。
        return _error(500, "adapter_binding_failed", e)
    if isinstance(e, LifecycleError):
        # `LifecycleError` 是 P12 的"这次操作不被允许"，它涵盖几种含义完全不同
        # 的拒绝，而它们都不带自己的类型。分类用**已经发生的事实**（存档在不在、
        # 世界开没开着），不去猜错误文本 —— 文本会改，事实不会。判据取自
        # store / service，不是这一层自己记的账。
        if world_id is not None:
            try:
                if op == "create" and plane.store.exists(world_id):
                    return _error(409, "archive_already_exists", e)
                if op in (
                    "checkpoint",
                    "close",
                    "autonomy_start",
                    "autonomy_stop",
                ) and (
                    plane.service.opened(world_id) is None
                ):
                    return _error(409, "world_not_open", e)
            except Exception:  # pragma: no cover - 分类失败不该盖住原始错误
                pass
        return _error(409, "lifecycle_refused", e)
    # 认不出来的那一档：对外只给一句不带内部结构的话，真相留在服务器日志里。
    print(f"[persistent-worlds] 未预期的失败: {type(e).__name__}: {e}", flush=True)
    return _error(500, "internal_error", "服务器内部错误，详情见服务器日志")


@contextmanager
def _translated(plane: WorldControlPlane, op: str, world_id: Optional[str] = None):
    try:
        yield
    except HTTPException:
        raise
    except Exception as e:
        raise _translate(e, plane, world_id, op) from e


def _status(payload: Dict[str, Any]) -> WorldStatusModel:
    return WorldStatusModel.model_validate(payload)


# ── 路由 ────────────────────────────────────────────────────────────────
@router.get("", response_model=WorldListModel)
def list_persistent_worlds(plane: WorldControlPlane = Depends(get_control_plane)):
    """磁盘上已知的世界，加上本进程开着的那些。"""
    with _translated(plane, "list"):
        return WorldListModel(worlds=[_status(item) for item in plane.list_worlds()])


@router.get("/{world_id}", response_model=WorldStatusModel)
def get_persistent_world(
    world_id: str, plane: WorldControlPlane = Depends(get_control_plane)
):
    """一个世界此刻的样子。

    状态面刻意不因为存档**读不出来**而失败：回答"这个世界怎么了"正是它的用处，
    一份坏存档要能在列表里显示成"坏了"。真去恢复它仍然会响亮失败。

    "根本没有这个世界"是另一回事，而且这个区分要保住：坏存档是一个存在的、
    出了问题的资源（200 + error），没有存档也没开着的 ID 是一个不存在的资源
    （404）。把后者也报成 200，后台就分不清"这个世界坏了"和"我打错了字"。
    """
    with _translated(plane, "status", world_id):
        status = plane.status(world_id)
        if not status["owned"]:
            try:
                known = plane.store.exists(world_id)
            except Exception:
                # 说不清就不说"没有"：宁可把一个存疑的世界如实报成"读不出来"，
                # 也不要宣布一个可能存在的世界不存在。
                known = True
            if not known:
                raise _error(404, "archive_not_found", f"世界 '{world_id}' 还没有存档")
        return _status(status)


@router.post("", response_model=WorldStatusModel, status_code=201)
def create_persistent_world(
    payload: CreateWorldRequest, plane: WorldControlPlane = Depends(get_control_plane)
):
    """建一个新世界，并当场写下第 1 版存档。绝不覆盖已经存在的存档。"""
    with _translated(plane, "create", payload.world_id):
        return _status(
            plane.create(
                world_id=payload.world_id,
                scene_id=payload.scene,
                character_ids=payload.characters,
            )
        )


@router.post("/{world_id}/restore", response_model=WorldStatusModel)
def restore_persistent_world(
    world_id: str, plane: WorldControlPlane = Depends(get_control_plane)
):
    """把最后一次成功 checkpoint 的那个世界拿回来。没有空世界兜底。"""
    with _translated(plane, "restore", world_id):
        return _status(plane.restore(world_id))


@router.post("/{world_id}/checkpoint", response_model=WorldStatusModel)
def checkpoint_persistent_world(
    world_id: str, plane: WorldControlPlane = Depends(get_control_plane)
):
    """在一个安全边界上完整存一次。

    2xx 的含义很具体：这一版**真的**写下去了。写不下去是 500 `checkpoint_failed`，
    而且磁盘上仍然是上一版。目录同步证实不了时是 500 `archive_not_durable`——
    那一版在磁盘上，但掉电之后可能回到上一版。两者都不算成功。
    """
    with _translated(plane, "checkpoint", world_id):
        return _status(plane.checkpoint(world_id))


@router.post("/{world_id}/autonomy/start", response_model=WorldStatusModel)
def start_world_autonomy(
    world_id: str, plane: WorldControlPlane = Depends(get_control_plane)
):
    """开始自动推这个世界的时间。

    这是**唯一**一个会让服务器自己开始花 API 额度的入口，所以它是显式的：
    建世界、恢复世界、重启进程都不会替操作者按下它。

    幂等：已经在跑的驱动再启动一次，返回同一份状态，不会出现第二个 worker。
    上一次停机还没停干净时是 409 `autonomy_busy` —— 那一档说不清，而说不清
    不能报成成功。
    """
    with _translated(plane, "autonomy_start", world_id):
        return _status(plane.start_autonomy(world_id))


@router.post("/{world_id}/autonomy/stop", response_model=WorldStatusModel)
def stop_world_autonomy(
    world_id: str, plane: WorldControlPlane = Depends(get_control_plane)
):
    """请驱动暂停，并有界地等当前这一次 tick 落定。

    它是**可重启的暂停**，不是关闭：世界仍然开着、仍然属于本进程，P11 的
    运行时也仍然接受写入（`running` 不变）。稍后可以再 Start。

    返回的 `autonomy.state` 有两种可能，而且区分是要害：`stopped` 表示当前
    tick 已经整个结束、之后不会再有；`stopping` 表示等超时了 —— 那次 tick
    还在跑（多半卡在一次模型调用上），它仍然可能落地一次提交。这时**不**谎称
    已经停了。
    """
    with _translated(plane, "autonomy_stop", world_id):
        return _status(plane.stop_autonomy(world_id))


@router.post("/{world_id}/close", response_model=WorldStatusModel)
def close_persistent_world(
    world_id: str, plane: WorldControlPlane = Depends(get_control_plane)
):
    """按 P12 的固定顺序干净关闭：停准入 → 等事务落定 → 存 → 归还所有权。

    最后一次 checkpoint 失败时既不宣布干净关闭，也不归还所有权 —— 归还等于
    宣布"磁盘上那一份是安全的"。那种情况下这里返回 500，世界仍然开着、仍然
    属于本进程。
    """
    with _translated(plane, "close", world_id):
        return _status(plane.close(world_id))
