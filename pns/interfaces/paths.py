# pns/interfaces/paths.py — 各路由模块共用的仓库内路径常量
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DASHBOARD_DIST = ROOT_DIR / "dashboard" / "dist"
DATA_DIR = ROOT_DIR / "data"
HISTORY_DIR = ROOT_DIR / "history"
DRIFT_SCORES_FILE = DATA_DIR / "drift_scores.jsonl"
# 审核决策跟评分记录一样是**运行时数据**，所以它跟着 data/ 走，而不是躺在
# 仓库根上：容器部署时 data/ 是一个卷，重建容器不会把它抹掉。仓库根上的旧
# 文件（DEPLOY-1 之前的位置）需要手动搬一次，见 docs/DEPLOY_UBUNTU_DOCKER.md。
REVIEW_DECISIONS_FILE = DATA_DIR / "review_decisions.jsonl"
