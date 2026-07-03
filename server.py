# server.py — PNS Web 前端服务
import os
import json
import asyncio
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from world import get_ena_system, get_mzk_system, get_ena_system_compat, get_mzk_system_compat, SCENES, DEFAULT_SCENE
from router import create_client, judge, API_FORMAT, _get_api_key

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def index():
    return FileResponse("static/index.html")

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

    history = [{"role": "user", "content": f"【场景】{scene['trigger']}\n请开始对话。"}]
    stats = {"ooc_count": 0, "scores": [], "corrections": 0}
    current = "mzk"
    correction_next = None

    for turn in range(1, max_turns + 1):
        char_key  = current
        char_name = "瑞希" if current == "mzk" else "绘名"

        await ws.send_json({"type": "generating", "turn": turn, "character": char_key, "char_name": char_name})

        try:
            reply = await call_character_async(client, current, history, scene, model, max_tokens, temperature, correction_next)
        except Exception as e:
            await ws.send_json({"type": "error", "turn": turn, "message": str(e)})
            break

        role = "assistant" if len(history) % 2 == 1 else "user"
        history.append({"role": role, "content": f"{char_name}：{reply}"})

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

        await ws.send_json({
            "type": "turn",
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
        })

        await asyncio.sleep(api_delay)
        current = "ena" if current == "mzk" else "mzk"

    avg_score = sum(stats["scores"]) / len(stats["scores"]) if stats["scores"] else 0
    await ws.send_json({
        "type": "done",
        "stats": {
            "total_turns": len(stats["scores"]),
            "ooc_count": stats["ooc_count"],
            "corrections": stats["corrections"],
            "avg_score": round(avg_score, 2),
            "max_score": max(stats["scores"]) if stats["scores"] else 0,
        }
    })


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
