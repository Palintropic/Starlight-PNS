# pns/interfaces/paths.py — 各路由模块共用的仓库内路径常量
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DASHBOARD_DIST = ROOT_DIR / "dashboard" / "dist"
DATA_DIR = ROOT_DIR / "data"
HISTORY_DIR = ROOT_DIR / "history"
DRIFT_SCORES_FILE = DATA_DIR / "drift_scores.jsonl"
REVIEW_DECISIONS_FILE = ROOT_DIR / "review_decisions.jsonl"
