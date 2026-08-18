# scripts/server.py — PNS Web 服务入口
# 路由/业务逻辑都在 pns/interfaces/（挂路由 + SPA 兜底）和 pns/logic/simulation.py
# （角色调用、判分、归档）里；这里只做进程启动前必须最先发生的两件事——
# 把仓库根目录和本目录加入 sys.path、加载 .env——然后组装并运行 app。
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
for _p in (ROOT_DIR, ROOT_DIR / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from pns.interfaces import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
