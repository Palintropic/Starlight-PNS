# pns/interfaces/app.py — FastAPI app 组装：部署模式 + 鉴权 + 挂路由 +
# 持久世界组装边界 + 兜底 dashboard SPA
#
# 组装图刻意全部发生在 `create_app()` 里，一件都不在 import 时发生：import
# 这个模块（以及 `pns.interfaces`）不建目录、不拿世界锁、不读存档、不起任何
# 运行时。`WorldControlPlane` 的构造本身也是惰性的 —— 存档根要到第一次真的
# 要写的时候才出现在磁盘上。有测试盯着这两件事。
#
# 生产模式的必填校验也在这里，而且刻意在**返回 app 之前**：缺任何一项必填
# 安全/配置项时，正确的结果是 `create_app()` 抛 `DeploymentConfigError`，
# 于是 `scripts/server.py` 在 import 阶段就失败、uvicorn 根本没起来、容器
# 以非零码退出。不存在"起来了但管理面是开的"这个中间状态。
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from . import auth as auth_routes
from . import config, health, persistent_worlds, review, simulate, world
from .composition import WorldControlPlane
from .paths import DASHBOARD_DIST
from .security import (
    AdminAuth,
    AdminAuthMiddleware,
    DeploymentConfigError,
    DeploymentSettings,
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


def create_app(
    control_plane: Optional[WorldControlPlane] = None,
    *,
    settings: Optional[DeploymentSettings] = None,
    admin_auth: Optional[AdminAuth] = None,
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

    auth = admin_auth if admin_auth is not None else AdminAuth(deployment)
    if auth.settings is not deployment and auth.settings != deployment:
        raise DeploymentConfigError("admin_auth 与 settings 必须是同一份部署设定")

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

    app = FastAPI(lifespan=lifespan)
    # 组装边界挂在 application state 上，路由通过依赖显式取用 —— 没有模块级
    # 单例，所以同一个进程里起两个 app 不会共享一份所有权账本。
    app.state.world_control_plane = plane
    app.state.deployment = deployment
    app.state.admin_auth = auth
    # /readyz 的正文。启动那一刻就定下来，健康检查因此不需要碰任何磁盘。
    app.state.readiness = {**deployment.to_public_dict(), "dashboard": dist.exists()}

    # 鉴权包在**整个**应用外面，而不是挂在某几个路由上：它要在路由匹配和
    # 请求体解析之前决定放不放行，而且要同时管住 WebSocket。默认拒绝，公开
    # 面是 security.py 里那份显式清单。
    app.add_middleware(AdminAuthMiddleware, auth=auth)

    app.include_router(health.router)
    app.include_router(auth_routes.router)
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
