# scripts/server.py — PNS Web 服务入口
# 路由/业务逻辑都在 pns/interfaces/（挂路由 + SPA 兜底）和 pns/logic/simulation.py
# （角色调用、判分、归档）里；这里先做进程启动前必须最先发生的三件事——
# 把仓库根目录和本目录加入 sys.path、加载 .env、把 stdout/stderr 换成会遮蔽
# 凭据的版本（见 process_setup.py，维护脚本共用同一份）——然后组装并运行 app。
#
# 遮蔽为什么必须在 create_app() 之前：装配过程本身就可能失败并打印异常，而
# 那条异常路径正是最容易把凭据带出去的地方。
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "scripts"))

from process_setup import prepare, secret_env_names  # noqa: E402,F401

prepare()

from pns.interfaces import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("PNS_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 7860))
    # 优雅停机的连接等待上限。它必须**小于** Compose 的 stop_grace_period，
    # 否则容器会在应用还没走到最后一次 checkpoint 之前就被 SIGKILL——那等于
    # 把一次本可以干净的关闭变成一次崩溃恢复。算式写在
    # docs/DEPLOY_UBUNTU_DOCKER.md 里。
    graceful = float(os.environ.get("PNS_GRACEFUL_TIMEOUT", "30"))
    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=False,
        timeout_graceful_shutdown=graceful,
    )
