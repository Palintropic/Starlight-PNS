# pns/interfaces/app.py — FastAPI app 组装：挂路由 + 持久世界组装边界 + 兜底 dashboard SPA
#
# 组装图刻意全部发生在 `create_app()` 里，一件都不在 import 时发生：import
# 这个模块（以及 `pns.interfaces`）不建目录、不拿世界锁、不读存档、不起任何
# 运行时。`WorldControlPlane` 的构造本身也是惰性的 —— 存档根要到第一次真的
# 要写的时候才出现在磁盘上。有测试盯着这两件事。
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from . import config, persistent_worlds, review, simulate, world
from .composition import WorldControlPlane
from .paths import DASHBOARD_DIST


def create_app(control_plane: Optional[WorldControlPlane] = None) -> FastAPI:
    plane = control_plane if control_plane is not None else WorldControlPlane()

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

    app.include_router(review.router)
    app.include_router(config.router)
    app.include_router(world.router)
    app.include_router(persistent_worlds.router)
    app.include_router(simulate.router)

    # 挂在所有 API/WS 路由之后，作为兜底：`dashboard/` 是唯一前端（`npm run
    # build` 产出 dist/），未构建时给出提示而不是裸 404。
    if DASHBOARD_DIST.exists():
        app.mount("/", StaticFiles(directory=str(DASHBOARD_DIST), html=True), name="dashboard")
    else:
        @app.get("/")
        def index():
            raise HTTPException(
                503,
                "Dashboard 未构建：请先在 dashboard/ 目录运行 `npm install && npm run build`。",
            )

    return app
