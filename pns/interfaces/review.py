# pns/interfaces/review.py — 历史审核 API
# /api/review/turns 直接读取 /ws/run 实时写入的 drift_scores.jsonl，字段与
# dashboard/src/types.ts 的 Turn 对齐（写入端见 pns.logic.simulation.run_turn）。
import json
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from .paths import DRIFT_SCORES_FILE, REVIEW_DECISIONS_FILE

router = APIRouter(prefix="/api/review", tags=["review"])


class ReviewDecision(BaseModel):
    session_id: str
    turn: int
    character: str
    decision: Literal["approve", "reject", "rewrite"]
    note: Optional[str] = None


def _decision_key(session_id: str, turn: int) -> str:
    return f"{session_id}:{turn}"


@router.get("/turns")
def get_review_turns():
    if not DRIFT_SCORES_FILE.exists():
        return []
    turns = []
    with DRIFT_SCORES_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            turns.append(json.loads(line))
    return turns


@router.get("/decisions")
def get_review_decisions():
    decisions: dict[str, dict] = {}
    if REVIEW_DECISIONS_FILE.exists():
        with REVIEW_DECISIONS_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                decisions[_decision_key(record["session_id"], record["turn"])] = record
    return decisions


@router.post("/decision")
def post_review_decision(decision: ReviewDecision):
    record = decision.model_dump()
    record["decided_at"] = datetime.now().isoformat()
    with REVIEW_DECISIONS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record
