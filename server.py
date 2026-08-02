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

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

import importlib

import pns.world as world_mod
import pns.world.scenes as scenes_submod
import pns.world.facts as facts_submod
from pns.world import get_ena_system, get_mzk_system, get_ena_system_compat, get_mzk_system_compat
from pns.world import codegen
import pns.logic.router as router_mod
from oobe import PROVIDERS, write_env

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def index():
    return FileResponse("static/index.html")


# ─── Review Dashboard API ────────────────────────────────────────────
# /api/review/turns 直接读取 /ws/run 实时写入的 drift_scores.jsonl，字段与
# dashboard/src/types.ts 的 Turn 对齐（写入端见 run_simulation 里的 drift_record）。

REVIEW_DECISIONS_FILE = Path("review_decisions.jsonl")
DRIFT_SCORES_FILE = Path("drift_scores.jsonl")


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
    return {k: {"id": v["id"], "label": v["label"], "trigger": v["trigger"]} for k, v in world_mod.SCENES.items()}

@app.get("/api/config")
def get_config():
    key = router_mod._get_api_key()
    return {
        "has_key": bool(key),
        "model": os.environ.get("MODEL", "mimo-v2.5-pro"),
        "api_format": router_mod.API_FORMAT,
        "default_scene": world_mod.DEFAULT_SCENE,
    }


class ConfigPayload(BaseModel):
    provider_key: str  # "1"/"2"/"3"/"4"，对应 oobe.PROVIDERS 的 key
    model: str
    api_key: str


@app.post("/api/config")
def post_config(payload: ConfigPayload):
    provider = PROVIDERS.get(payload.provider_key)
    if not provider:
        raise HTTPException(400, f"未知的 provider_key: {payload.provider_key}")
    if not payload.model:
        raise HTTPException(400, "model 不能为空")
    if not payload.api_key:
        raise HTTPException(400, "api_key 不能为空")

    write_env(provider, payload.model, payload.api_key)

    # 写入 .env 后让当前进程感知新配置：load_dotenv 更新 os.environ，
    # 但 router_mod 的 API_FORMAT/BASE_URL/_KEY_NAME 是模块导入时算好的
    # 常量，光靠 load_dotenv 不会变，所以还要 reload 这个模块本身
    # （跟下面 _reload_world() reload 世界模块是同一套路）。
    load_dotenv(override=True)
    importlib.reload(router_mod)

    return {"configured": True}


@app.get("/api/config/providers")
def get_config_providers():
    return {
        k: {"name": v["name"], "models": v["models"]}
        for k, v in PROVIDERS.items()
    }

# ─── World Editor API ────────────────────────────────────────────────
# 图形化编辑 pns/world/scenes.py / facts.py。写回逻辑（JSON⇄Python源码、备份、
# 校验）都在 pns/world/codegen.py 里，这里只负责路由、reload、报错转换。

def _reload_world():
    """scenes.py / facts.py 写盘后，让正在跑的进程也看到新内容。"""
    importlib.reload(scenes_submod)
    importlib.reload(facts_submod)
    importlib.reload(world_mod)


class Scene(BaseModel):
    id: str
    label: str
    time: str
    location: str
    weather: str
    day_phase: Literal["morning", "afternoon", "evening", "late_night"]
    scene_type: str
    lore_tag: Literal["硬事实", "软推断", "待验证"]
    trigger: str
    gate_triggers: Optional[dict[str, str]] = None
    gate_opening_note: Optional[str] = None
    auto_next: Optional[str] = None
    auto_turns: Optional[int] = None


class FactsPayload(BaseModel):
    facts: dict[str, str]


class SourcePayload(BaseModel):
    source: str


@app.get("/api/world/scenes")
def get_world_scenes():
    return world_mod.SCENES


@app.post("/api/world/scenes")
def post_world_scenes(scenes: dict[str, Scene]):
    for key, scene in scenes.items():
        if scene.id != key:
            raise HTTPException(400, f"scene key '{key}' 与内部 id '{scene.id}' 不一致")
    payload = {key: scene.model_dump() for key, scene in scenes.items()}
    try:
        codegen.save_scenes(payload)
    except codegen.CodegenError as e:
        raise HTTPException(400, str(e))
    _reload_world()
    return world_mod.SCENES


@app.get("/api/world/scenes/source")
def get_world_scenes_source():
    return {"source": codegen.SCENES_PATH.read_text(encoding="utf-8")}


@app.post("/api/world/scenes/source")
def post_world_scenes_source(payload: SourcePayload):
    try:
        codegen.save_scenes_source(payload.source)
    except codegen.CodegenError as e:
        raise HTTPException(400, str(e))
    _reload_world()
    return {"source": codegen.SCENES_PATH.read_text(encoding="utf-8")}


@app.get("/api/world/facts")
def get_world_facts():
    return {"facts": world_mod.WORLD_FACTS, "groups": codegen.FACT_GROUPS}


@app.post("/api/world/facts")
def post_world_facts(payload: FactsPayload):
    try:
        codegen.save_facts(payload.facts)
    except codegen.CodegenError as e:
        raise HTTPException(400, str(e))
    _reload_world()
    return {"facts": world_mod.WORLD_FACTS, "groups": codegen.FACT_GROUPS}


@app.get("/api/world/facts/source")
def get_world_facts_source():
    return {"source": codegen.FACTS_PATH.read_text(encoding="utf-8")}


@app.post("/api/world/facts/source")
def post_world_facts_source(payload: SourcePayload):
    try:
        codegen.save_facts_source(payload.source)
    except codegen.CodegenError as e:
        raise HTTPException(400, str(e))
    _reload_world()
    return {"source": codegen.FACTS_PATH.read_text(encoding="utf-8")}


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
        if router_mod.API_FORMAT == "openai":
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
    return await loop.run_in_executor(None, router_mod.judge, client, character, message, turn)


def save_history(session_id: str, scene: dict, model: str, turns: list, stats: dict) -> Path:
    history_dir = Path("history")
    history_dir.mkdir(exist_ok=True)

    filename = history_dir / f"{session_id}.md"

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

    scene_id   = params.get("scene", world_mod.DEFAULT_SCENE)
    max_turns  = int(params.get("max_turns", 8))
    model      = params.get("model") or os.environ.get("MODEL", "mimo-v2.5-pro")
    max_tokens = int(params.get("max_tokens", 1024))
    temperature = float(params.get("temperature", 0.85))
    api_delay  = float(params.get("api_delay", 1.0))

    scene = world_mod.SCENES.get(scene_id, world_mod.SCENES[world_mod.DEFAULT_SCENE])
    api_key = router_mod._get_api_key()

    if not api_key:
        await ws.send_json({"type": "error", "message": "找不到 API Key，请刷新页面完成配置向导，或运行 python oobe.py"})
        await ws.close()
        return

    client = router_mod.create_client(api_key)

    session_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{scene['id']}"

    await ws.send_json({
        "type": "start",
        "session_id": session_id,
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
        score   = result.get("drift_score", 0)
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

        drift_record = {
            "session_id": session_id,
            "turn": turn,
            "character": char_key,
            "char_name": char_name,
            "text": reply,
            "drift_score": score,
            "confidence": result.get("confidence", 0.0),
            "drift_type": result.get("drift_type", ""),
            "reason": result.get("reason", ""),
            "needs_human_review": result.get("needs_human_review", False),
            "correction": result.get("correction"),
            "timestamp": datetime.now().isoformat(),
        }
        with DRIFT_SCORES_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(drift_record, ensure_ascii=False) + "\n")

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
            saved_path = save_history(session_id, scene, model, turn_log, final_stats)
        except Exception as e:
            print(f"[server] 历史记录保存失败: {e}")

    await ws.send_json({
        "type": "done",
        "session_id": session_id,
        "stats": final_stats,
        "history_file": str(saved_path) if saved_path else None,
    })


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
