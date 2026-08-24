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
# 账户库（AUTH-1）。它跟评分/审核记录一样是**运行时数据**，所以也在 data/ 下：
# 容器重建不会把用户和审计抹掉，而备份 data/ 卷就等于备份了它。位置可用
# PNS_ACCOUNTS_DB 改到别处。
ACCOUNTS_DB_FILE = DATA_DIR / "accounts.sqlite3"
