# pns/interfaces/app.py — FastAPI app 组装：部署模式 + 鉴权 + 账户 + 挂路由 +
# 持久世界组装边界 + 兜底 dashboard SPA
#
# 组装图刻意全部发生在 `create_app()` 里，一件都不在 import 时发生：import
# 这个模块（以及 `pns.interfaces`）不建目录、不拿世界锁、不读存档、不起任何
# 运行时。`WorldControlPlane` 的构造本身也是惰性的 —— 存档根要到第一次真的
# 要写的时候才出现在磁盘上。有测试盯着这两件事。
#
# 账户库遵守同一条纪律：**开发模式下、库文件还不存在、也没配 bootstrap 时，
# 什么都不建**。一台本地开发服务器不该因为有人 import 了这个模块就多出一个
# 空的 SQLite 文件。生产模式反过来——那里必须有一个可用的账户库和至少一个
# 启用着的管理员，否则 `create_app()` 抛，进程起不来。
#
# 生产模式的必填校验也在这里，而且刻意在**返回 app 之前**：缺任何一项必填
# 安全/配置项时，正确的结果是 `create_app()` 抛 `DeploymentConfigError`，
# 于是 `scripts/server.py` 在 import 阶段就失败、uvicorn 根本没起来、容器
# 以非零码退出。不存在"起来了但管理面是开的"这个中间状态。
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import accounts_api
from . import auth as auth_routes
from . import config, health, persistent_worlds, review, simulate, world
from .accounts import AccountError, AccountStore
from .composition import WorldControlPlane
from .paths import ACCOUNTS_DB_FILE, DASHBOARD_DIST
from .security import (
    ENV_ACCOUNTS_DB,
    ENV_BOOTSTRAP_PASSWORD_HASH,
    ENV_BOOTSTRAP_USERNAME,
    AdminAuth,
    AdminAuthMiddleware,
    DeploymentConfigError,
    DeploymentSettings,
)


async def _validation_error_without_the_payload(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 只说**哪个字段不对**，不把收到的值抄回去。

    FastAPI/pydantic 的默认 422 正文里带一个 `input` 字段，装的是原样的提交值。
    在这个应用上那意味着：一个太长的密码、一把太长的模型 API Key，会被完整地
    写进一条 4xx 响应——而"凭据不出现在任何一条响应里"是这台服务器明确承诺过
    的边界，不该有一条"只在校验失败时"的例外。

    留下的是 `type` / `loc` / `msg`：调用方需要知道哪个字段为什么不合法，
    而这三样都不含提交的值。前端的错误解析本来读的就是 `msg`。
    """
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {
                    "type": error.get("type", "value_error"),
                    "loc": [str(part) for part in error.get("loc", ())],
                    "msg": error.get("msg", "字段不合法"),
                }
                for error in exc.errors()
            ]
        },
    )


def _verify_production_config(
    settings: DeploymentSettings,
    dashboard_dist: Path,
    registry_provider: Optional[Callable[[], object]],
) -> None:
    """生产模式下"这台服务器现在能不能干活"的必填项。

    这里检查的三样东西各自对应一种**已经发生过的**部署事故：

      * 没有已构建的 Dashboard：容器起来了，浏览器拿到 503，操作者以为是
        网络问题；
      * 没有模型凭据：世界建得出来但一动就报错，而修它的那个入口
        （Setup Wizard 写 `.env`）在生产里是被拒绝的，于是变成一个死胡同；
      * 没有管理凭据：见 `DeploymentSettings.__post_init__`。

    三样都在这里当场失败，而不是留到第一次操作时才失败。
    """
    if not settings.production:
        return
    if not dashboard_dist.exists():
        raise DeploymentConfigError(
            f"生产模式要求已构建的 Dashboard，但 {dashboard_dist} 不存在；"
            f"生产镜像应当在构建阶段产出它"
        )
    if registry_provider is None:
        return
    try:
        registry = registry_provider()
    except Exception as e:  # 配置根本构建不出来，也是一次启动失败
        raise DeploymentConfigError(f"生产模式下配置构建失败：{e}") from e
    models = getattr(registry, "models", None)
    key_name = getattr(models, "key_name", None) or "API Key"
    if not getattr(models, "api_key", ""):
        raise DeploymentConfigError(
            f"生产模式必须注入模型凭据 {key_name}；生产不接受从浏览器写 .env，"
            f"缺了它这台服务器建得出世界却一动就失败"
        )


def _account_db_path(settings: DeploymentSettings) -> Path:
    return Path(settings.accounts_db) if settings.accounts_db else ACCOUNTS_DB_FILE


def _open_account_store(settings: DeploymentSettings) -> Optional[AccountStore]:
    """打开账户库。**开发模式下没有理由就不建它。**

    "有理由"只有三种：这是生产、操作者配了 bootstrap、或者库文件已经存在。
    前两种是显式意图，第三种是既成事实。都不成立时返回 None，于是一台没配
    过任何东西的本地开发服务器保持既有的开放行为，磁盘上也不会凭空多一个
    空库。
    """
    path = _account_db_path(settings)
    wanted = (
        settings.production
        or bool(settings.bootstrap_admin_username)
        or path.exists()
    )
    if not wanted:
        return None
    try:
        return AccountStore(path)
    except (AccountError, sqlite3.Error, OSError) as e:
        # 打不开账户库时**不能**回落到"那就没有账户"：那会把一台配好了账户的
        # 服务器悄悄变回一台谁都能进的服务器。
        raise DeploymentConfigError(
            f"账户库 {path} 打不开：{e}；用 {ENV_ACCOUNTS_DB} 指到一个可写的位置，"
            f"或者按部署文档恢复它"
        ) from e


def _bootstrap_first_admin(
    settings: DeploymentSettings, store: Optional[AccountStore]
) -> None:
    """按环境变量创建第一个管理员。**幂等，且永远不覆盖已有账户。**

    幂等性由存储层在一个写事务里保证（存在性检查和插入在同一把写锁下），
    所以这两个变量可以一直留在 `.env` 里：第二次启动什么都不会发生。
    """
    if store is None or not settings.bootstrap_admin_username:
        return
    try:
        store.bootstrap_admin(
            settings.bootstrap_admin_username,
            settings.bootstrap_admin_password_hash or "",
        )
    except AccountError as e:
        raise DeploymentConfigError(
            f"{ENV_BOOTSTRAP_USERNAME} / {ENV_BOOTSTRAP_PASSWORD_HASH} 不成立：{e}"
        ) from e


def _verify_production_accounts(
    settings: DeploymentSettings, store: Optional[AccountStore]
) -> None:
    """生产模式必须有至少一个**启用着的**管理员。

    为什么这是启动失败而不是"起来但登不进去"：一台没有管理员的生产服务器，
    浏览器那一侧是彻底不可用的（登录框永远拒绝），而唯一还能动的是那把
    break-glass token——也就是说它会安静地退化成一台只能用 curl 管的服务器，
    而运维要到真的需要登录的那一刻才发现。当场失败，并且把两条修法说清楚。
    """
    if not settings.production:
        return
    if store is not None and store.has_enabled_admin():
        return
    raise DeploymentConfigError(
        f"生产模式需要至少一个启用着的管理员账户。两条修法："
        f"(1) 在 .env 里设 {ENV_BOOTSTRAP_USERNAME} 和 "
        f"{ENV_BOOTSTRAP_PASSWORD_HASH}（哈希用 "
        f"`python scripts/accounts.py hash-password` 生成），重启即可创建；"
        f"(2) 对着数据卷跑 `python scripts/accounts.py create-admin`。"
        f"详见 docs/DEPLOY_UBUNTU_DOCKER.md"
    )


def create_app(
    control_plane: Optional[WorldControlPlane] = None,
    *,
    settings: Optional[DeploymentSettings] = None,
    admin_auth: Optional[AdminAuth] = None,
    account_store: Optional[AccountStore] = None,
    dashboard_dist: Optional[Path] = None,
    registry_provider: Optional[Callable[[], object]] = None,
) -> FastAPI:
    plane = control_plane if control_plane is not None else WorldControlPlane()
    deployment = settings if settings is not None else DeploymentSettings.from_env()
    dist = Path(dashboard_dist) if dashboard_dist is not None else DASHBOARD_DIST
    if registry_provider is None and deployment.production:
        # 生产必填校验需要一份真配置。这个 import 放在函数里：模块 import
        # 期不该顺手把配置构建起来。
        from pns.runtime.reload import BOUNDARY

        registry_provider = BOUNDARY.active
    _verify_production_config(deployment, dist, registry_provider)

    # 顺序是刻意的：先拿到账户库，再 bootstrap，最后才构造 AdminAuth ——
    # `AdminAuth` 在构造时就把"这台服务器要不要凭据"定下来，所以它必须看到
    # bootstrap 之后的账户库，否则第一次启动会带着一个开放的管理面跑起来。
    store = account_store
    if store is None and admin_auth is not None:
        store = admin_auth.accounts
    # 只有**这里**打开的库才归这个 app 关。别人传进来的属于别人。
    owns_store = store is None
    if store is None:
        store = _open_account_store(deployment)
        owns_store = store is not None
    # 这一段里的任何一次失败都是"这台服务器起不来"。库已经打开了的话要在抛
    # 出去之前关掉：进程通常紧接着就退出，但一条挂在异常路径上的连接是那种
    # 只在别人复用这个函数时才发作的泄漏（测试就是这么发现它的）。
    try:
        _bootstrap_first_admin(deployment, store)
        _verify_production_accounts(deployment, store)

        auth = (
            admin_auth
            if admin_auth is not None
            else AdminAuth(deployment, accounts=store)
        )
        if auth.settings is not deployment and auth.settings != deployment:
            raise DeploymentConfigError("admin_auth 与 settings 必须是同一份部署设定")
        if auth.accounts is not store:
            raise DeploymentConfigError("admin_auth 与 account_store 必须是同一个账户库")
    except Exception:
        if owns_store and store is not None:
            store.close()
        raise

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        # 进程正常收尾：对本进程打开的每个世界尝试一次 P12 的安全关闭，并把
        # 结果如实打出来。存不下去的那个不会被说成关干净了，也不会被 release
        # ——release() 会在锁记录里写下"上一个拥有者是干净走的"，而它不是。
        #
        # flush=True 不是装饰：stdout 重定向到文件时是块缓冲的，而这份报告恰好
        # 只在进程收尾那一刻产生。不当场冲出去，"这个世界没关干净"这句唯一的
        # 告知就可能永远到不了看日志的人手里。
        for report in plane.shutdown():
            if report["closed"] and report["clean"]:
                print(
                    f"[persistent-worlds] 世界 '{report['world_id']}' 已干净关闭"
                    f"（第 {report['revision']} 版）",
                    flush=True,
                )
            else:
                print(
                    f"[persistent-worlds] 世界 '{report['world_id']}' **没有**干净关闭："
                    f"{report['error']}；能恢复到的仍然是最后一次成功 checkpoint",
                    flush=True,
                )
        # 只关自己开的那个库。别人传进来的属于别人，关掉它等于替调用方做决定。
        if owns_store and store is not None:
            store.close()

    app = FastAPI(lifespan=lifespan)
    # 422 不许把提交的值抄回响应里。见 `_validation_error_without_the_payload`。
    app.add_exception_handler(
        RequestValidationError, _validation_error_without_the_payload
    )
    # 组装边界挂在 application state 上，路由通过依赖显式取用 —— 没有模块级
    # 单例，所以同一个进程里起两个 app 不会共享一份所有权账本。
    app.state.world_control_plane = plane
    app.state.deployment = deployment
    app.state.admin_auth = auth
    app.state.accounts = store
    # /readyz 的正文。启动那一刻就定下来，健康检查因此不需要碰任何磁盘。
    # `auth_required` 取自 auth 而不是 settings：账户库里有人也算要凭据。
    app.state.readiness = {**auth.public_dict(), "dashboard": dist.exists()}

    # 鉴权包在**整个**应用外面，而不是挂在某几个路由上：它要在路由匹配和
    # 请求体解析之前决定放不放行，而且要同时管住 WebSocket。默认拒绝，公开
    # 面是 security.py 里那份显式清单；能做什么由 authz.required_scope 决定。
    app.add_middleware(AdminAuthMiddleware, auth=auth)

    app.include_router(health.router)
    app.include_router(auth_routes.router)
    app.include_router(accounts_api.router)
    app.include_router(review.router)
    app.include_router(config.router)
    app.include_router(world.router)
    app.include_router(persistent_worlds.router)
    app.include_router(simulate.router)

    # 挂在所有 API/WS 路由之后，作为兜底：`dashboard/` 是唯一前端（`npm run
    # build` 产出 dist/），未构建时给出提示而不是裸 404。
    if dist.exists():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="dashboard")
    else:
        @app.get("/")
        def index():
            raise HTTPException(
                503,
                "Dashboard 未构建：请先在 dashboard/ 目录运行 `npm install && npm run build`。",
            )

    return app
