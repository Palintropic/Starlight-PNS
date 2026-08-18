# pns/interfaces/app.py — FastAPI app 组装：挂路由 + 兜底 dashboard SPA
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from . import config, review, simulate, world
from .paths import DASHBOARD_DIST


def create_app() -> FastAPI:
    app = FastAPI()

    app.include_router(review.router)
    app.include_router(config.router)
    app.include_router(world.router)
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
