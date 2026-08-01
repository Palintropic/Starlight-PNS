# server.py — PNS Web 前端服务
import os
import json
import asyncio
import time
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from typing import Literal, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from world import get_ena_system, get_mzk_system, get_ena_system_compat, get_mzk_system_compat, SCENES, DEFAULT_SCENE
from router import create_client, judge, API_FORMAT, _get_api_key

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def index():
    return FileResponse("static/index.html")


# ─── Review Dashboard API ────────────────────────────────────────────
# 数据来源尚未定案（见 PNS_Web_Dashboard_开工工单.md 第4节），这里先用
# 手搓样例数据把 Dashboard 的三栏交互跑通。字段对齐 pns/models/drift_score.py
# 的 DriftScore，外加 text（台词原文）和 session_id（会话分组）。
# 真正数据源定下来后，把 SAMPLE_TURNS 换成读取实际 log 即可，接口形状不用变。

REVIEW_DECISIONS_FILE = Path("review_decisions.jsonl")

SAMPLE_TURNS = [
    {
        "session_id": "sample-001", "turn": 0, "character": "mzk", "char_name": "瑞希",
        "text": "绘名绘名！今天的天空颜色超好看的，感觉可以直接拿来当新曲的封面色喵！",
        "drift_score": 1, "confidence": 0.92, "drift_type": "无",
        "reason": "语气活泼、话题跳跃，符合瑞希日常状态。", "needs_human_review": False,
        "correction": None, "timestamp": "2026-07-30T10:00:00",
    },
    {
        "session_id": "sample-001", "turn": 1, "character": "ena", "char_name": "绘名",
        "text": "……嗯，是挺好看的。",
        "drift_score": 2, "confidence": 0.8, "drift_type": "无",
        "reason": "省略号收尾+简短回应，与瑞希互动时的克制符合设定。", "needs_human_review": False,
        "correction": None, "timestamp": "2026-07-30T10:00:40",
    },
    {
        "session_id": "sample-001", "turn": 2, "character": "mzk", "char_name": "瑞希",
        "text": "如果你需要，我可以帮你整理一份关于天空配色的参考资料，你觉得怎么样？要不要我先列个大纲？",
        "drift_score": 7, "confidence": 0.88, "drift_type": "助手化A",
        "reason": "把决定权交还用户（'你觉得怎么样'），语气偏向客服式建议，情绪浓度与句子密度不匹配。",
        "needs_human_review": True,
        "correction": "去掉征询式收尾，直接用瑞希的方式把感受说完，不留选择给用户。",
        "timestamp": "2026-07-30T10:01:10",
    },
    {
        "session_id": "sample-001", "turn": 3, "character": "ena", "char_name": "绘名",
        "text": "没关系的，随便都行，你决定就好。",
        "drift_score": 8, "confidence": 0.9, "drift_type": "内容OOC",
        "reason": "'没关系'、'随便都行'是典型绘名OOC信号，此话题（与瑞希）本应收敛而非过度顺从退让。",
        "needs_human_review": True,
        "correction": "换成绘名式的迂回克制，而不是讨好式的全盘让步。",
        "timestamp": "2026-07-30T10:01:50",
    },
]


class ReviewDecision(BaseModel):
    session_id: str
    turn: int
    character: str
    decision: Literal["approve", "reject", "rewrite"]
    note: Optional[str] = None


def _decision_key(session_id: str, turn: int) -> str:
    return f"{session_id}:{turn}"


@app.get("/api/review/turns")
def get_review_turns():
    return SAMPLE_TURNS


@app.get("/api/review/decisions")
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


@app.post("/api/review/decision")
def post_review_decision(decision: ReviewDecision):
    record = decision.model_dump()
    record["decided_at"] = datetime.now().isoformat()
    with REVIEW_DECISIONS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record

@app.get("/api/scenes")
def get_scenes():
    return {k: {"id": v["id"], "label": v["label"], "trigger": v["trigger"]} for k, v in SCENES.items()}

@app.get("/api/config")
def get_config():
    key = _get_api_key()
    return {
        "has_key": bool(key),
        "model": os.environ.get("MODEL", "mimo-v2.5-pro"),
        "api_format": API_FORMAT,
        "default_scene": DEFAULT_SCENE,
    }

def _strip_prefix(text: str, char_name: str) -> str:
    prefix = char_name + "："
    while text.startswith(prefix):
        text = text[len(prefix):]
    return text

async def call_character_async(client, character: str, history: list, scene: dict, model: str, max_tokens: int, temperature: float, correction: str = None) -> str:
    use_compat = "flash-lite" in model.lower()
    if character == "ena":
        system = get_ena_system_compat(scene) if use_compat else get_ena_system(scene)
        char_name = "绘名"
    else:
        system = get_mzk_system_compat(scene) if use_compat else get_mzk_system(scene)
        char_name = "瑞希"

    if correction:
        system += f"\n\n【注意】{correction}"

    loop = asyncio.get_event_loop()

    def _call():
        if API_FORMAT == "openai":
            oai_history = [{"role": "system", "content": system}] + history
            response = client.chat.completions.create(
                model=model, max_tokens=max_tokens, temperature=temperature,
                messages=oai_history,
            )
            content = response.choices[0].message.content
            if not content:
                raise ValueError(f"API返回空内容，finish_reason: {response.choices[0].finish_reason}")
            return _strip_prefix(content.strip(), char_name)
        else:
            response = client.messages.create(
                model=model, max_tokens=max_tokens, temperature=temperature,
                system=system, messages=history,
            )
            return _strip_prefix(response.content[0].text.strip(), char_name)

    return await loop.run_in_executor(None, _call)


async def judge_async(client, character: str, message: str, turn: int) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, judge, client, character, message, turn)


def save_history(scene: dict, model: str, turns: list, stats: dict) -> Path:
    history_dir = Path("history")
    history_dir.mkdir(exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = history_dir / f"{ts}_{scene['id']}.md"

    lines = []
    lines.append(f"# {scene['label']}")
    lines.append(f"")
    lines.append(f"> {scene['trigger']}")
    lines.append(f"")
    lines.append(f"| | |")
    lines.append(f"|---|---|")
    lines.append(f"| 时间 | {scene['time']} |")
    lines.append(f"| 地点 | {scene['location']} |")
    lines.append(f"| 模型 | {model} |")
    lines.append(f"| 生成时间 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |")
    lines.append(f"")
    lines.append(f"---")
    lines.append(f"")

    for t in turns:
        char_name = t["char_name"]
        reply     = t["reply"]
        score     = t["score"]
        is_ooc    = t["is_ooc"]
        drift     = t.get("drift_type", "")
        reason    = t.get("reason", "")
        correction = t.get("correction")
        review    = t.get("needs_human_review", False)

        score_icon = "🟢" if score <= 2 else ("🟡" if score <= 5 else "🔴")

        lines.append(f"**第 {t['turn']} 轮 · {char_name}**")
        lines.append(f"")
        lines.append(reply)
        lines.append(f"")

        router_line = f"{score_icon} Router {score}/10 · {drift}"
        if reason:
            router_line += f" — {reason}"
        if review:
            router_line += " ⚑待人工校验"
        lines.append(f"<sub>{router_line}</sub>")

        if correction:
            lines.append(f"")
            lines.append(f"> ⚡ **纠正** {correction}")

        lines.append(f"")
        lines.append(f"---")
        lines.append(f"")

    lines.append(f"## 统计")
    lines.append(f"")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|---|---|")
    lines.append(f"| 总轮次 | {stats['total_turns']} |")
    lines.append(f"| OOC次数 | {stats['ooc_count']} |")
    lines.append(f"| Router介入 | {stats['corrections']} 次 |")
    lines.append(f"| 平均漂移分数 | {stats['avg_score']}/10 |")
    lines.append(f"| 最高漂移分数 | {stats['max_score']}/10 |")

    filename.write_text("\n".join(lines), encoding="utf-8")
    return filename


@app.websocket("/ws/run")
async def run_simulation(ws: WebSocket):
    await ws.accept()

    try:
        params = await ws.receive_json()
    except Exception:
        await ws.close(code=1003)
        return

    scene_id   = params.get("scene", DEFAULT_SCENE)
    max_turns  = int(params.get("max_turns", 8))
    model      = params.get("model") or os.environ.get("MODEL", "mimo-v2.5-pro")
    max_tokens = int(params.get("max_tokens", 1024))
    temperature = float(params.get("temperature", 0.85))
    api_delay  = float(params.get("api_delay", 1.0))

    scene = SCENES.get(scene_id, SCENES[DEFAULT_SCENE])
    api_key = _get_api_key()

    if not api_key:
        await ws.send_json({"type": "error", "message": "找不到 API Key，请先运行 python oobe.py"})
        await ws.close()
        return

    client = create_client(api_key)

    await ws.send_json({
        "type": "start",
        "scene": {"id": scene["id"], "label": scene["label"], "trigger": scene["trigger"],
                  "time": scene["time"], "location": scene["location"]},
        "max_turns": max_turns,
        "model": model,
    })

    histories = {
        "mzk": [{"role": "user", "content": f"【场景】{scene['trigger']}\n请开始对话。"}],
        "ena": [{"role": "user", "content": f"【场景】{scene['trigger']}"}],
    }
    stats = {"ooc_count": 0, "scores": [], "corrections": 0}
    current = "mzk"
    correction_next = None
    turn_log = []

    for turn in range(1, max_turns + 1):
        char_key  = current
        char_name = "瑞希" if current == "mzk" else "绘名"
        other = "ena" if current == "mzk" else "mzk"

        await ws.send_json({"type": "generating", "turn": turn, "character": char_key, "char_name": char_name})

        try:
            reply = await call_character_async(client, current, histories[current], scene, model, max_tokens, temperature, correction_next)
        except Exception as e:
            await ws.send_json({"type": "error", "turn": turn, "message": str(e)})
            break

        histories[current].append({"role": "assistant", "content": f"{char_name}：{reply}"})
        histories[other].append({"role": "user", "content": f"{char_name}：{reply}"})

        await ws.send_json({"type": "judging", "turn": turn, "character": char_key, "char_name": char_name})

        result = await judge_async(client, current, reply, turn)
        score   = result.get("score", 0)
        is_ooc  = result.get("is_ooc", False)

        stats["scores"].append(score)
        if is_ooc:
            stats["ooc_count"] += 1
            correction_next = result.get("correction")
            if correction_next:
                stats["corrections"] += 1
        else:
            correction_next = None

        turn_data = {
            "turn": turn,
            "character": char_key,
            "char_name": char_name,
            "reply": reply,
            "score": score,
            "is_ooc": is_ooc,
            "drift_type": result.get("drift_type", ""),
            "reason": result.get("reason", ""),
            "correction": result.get("correction"),
            "needs_human_review": result.get("needs_human_review", False),
        }
        turn_log.append(turn_data)
        await ws.send_json({"type": "turn", **turn_data})

        await asyncio.sleep(api_delay)
        current = "ena" if current == "mzk" else "mzk"

    avg_score = sum(stats["scores"]) / len(stats["scores"]) if stats["scores"] else 0
    final_stats = {
        "total_turns": len(stats["scores"]),
        "ooc_count": stats["ooc_count"],
        "corrections": stats["corrections"],
        "avg_score": round(avg_score, 2),
        "max_score": max(stats["scores"]) if stats["scores"] else 0,
    }

    saved_path = None
    if turn_log:
        try:
            saved_path = save_history(scene, model, turn_log, final_stats)
        except Exception as e:
            print(f"[server] 历史记录保存失败: {e}")

    await ws.send_json({
        "type": "done",
        "stats": final_stats,
        "history_file": str(saved_path) if saved_path else None,
    })


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
