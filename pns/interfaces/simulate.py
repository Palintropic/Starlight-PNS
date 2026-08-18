# pns/interfaces/simulate.py — 实时对话模拟 WebSocket
# 只负责协议消息（accept/receive_json/send_json）和逐轮编排；角色调用、
# Router 判分、历史归档的实际逻辑都在 pns.logic.simulation 里。
import asyncio
import json
import os
from datetime import datetime

from fastapi import APIRouter, WebSocket

import pns.world as world_mod
from pns.logic.simulation import call_character_async, judge_async, save_history
from pns.world.characters import registry as character_registry
import pns.logic.router as router_mod

from .paths import DRIFT_SCORES_FILE, HISTORY_DIR

router = APIRouter(tags=["simulate"])


@router.websocket("/ws/run")
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
    character_ids = params.get("characters") or ["mizuki", "ena"]

    if len(character_ids) < 2:
        await ws.send_json({"type": "error", "message": "至少需要2个角色才能开始会话"})
        await ws.close()
        return

    # 提前校验角色是否存在于 pack（不要求 ready，只要求存在；允许 partial/not_ready
    # 参与，调用时如果真的没 prompt 再报运行时错误，报错粒度精确到具体某一轮）
    for cid in character_ids:
        try:
            character_registry.get_character_metadata(cid)
        except ValueError:
            await ws.send_json({"type": "error", "message": f"角色 '{cid}' 不在当前角色包（{character_registry.ACTIVE_PACK}）中"})
            await ws.close()
            return

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
        cid: [{"role": "user", "content": f"【场景】{scene['trigger']}" + ("\n请开始对话。" if i == 0 else "")}]
        for i, cid in enumerate(character_ids)
    }
    stats = {"ooc_count": 0, "scores": [], "corrections": 0}
    current_idx = 0
    pending_corrections = {cid: None for cid in character_ids}
    turn_log = []

    for turn in range(1, max_turns + 1):
        current = character_ids[current_idx]
        char_key = current
        meta = character_registry.get_character_metadata(current)
        char_name = meta.get("name", current)
        others = [cid for cid in character_ids if cid != current]

        await ws.send_json({"type": "generating", "turn": turn, "character": char_key, "char_name": char_name})

        generation_history = list(histories[current])
        original_request = next(
            (
                item.get("content", "")
                for item in reversed(generation_history)
                if item.get("role") == "user"
            ),
            scene.get("trigger", ""),
        )
        correction_applied = pending_corrections[current]

        try:
            reply = await call_character_async(client, current, generation_history, scene, model, max_tokens, temperature, correction_applied)
        except character_registry.CharacterNotReadyError as e:
            await ws.send_json({"type": "error", "turn": turn, "character": current, "message": f"角色 '{current}' 尚未准备好：{e.detail}"})
            break
        except Exception as e:
            await ws.send_json({"type": "error", "turn": turn, "message": str(e)})
            break

        histories[current].append({"role": "assistant", "content": f"{char_name}：{reply}"})
        for other in others:
            histories[other].append({"role": "user", "content": f"{char_name}：{reply}"})

        await ws.send_json({"type": "judging", "turn": turn, "character": char_key, "char_name": char_name})

        result = await judge_async(
            client,
            current,
            reply,
            turn,
            scene,
            original_request=original_request,
            recent_history=generation_history,
            correction_applied=correction_applied,
        )
        score   = result.get("drift_score", 0)
        is_ooc  = result.get("is_ooc", False)

        stats["scores"].append(score)
        if is_ooc:
            stats["ooc_count"] += 1
            pending_corrections[current] = result.get("correction")
            if pending_corrections[current]:
                stats["corrections"] += 1
        else:
            pending_corrections[current] = None

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
            "dimensions": result.get("dimensions", {}),
            "dimensions_complete": result.get("dimensions_complete", False),
            "methodology_version": result.get("methodology_version", ""),
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
            "scene_id": result.get("scene_id", ""),
            "lore_tag": result.get("lore_tag", ""),
            "router_reference_status": result.get("router_reference_status", ""),
            "dimensions": result.get("dimensions", {}),
            "dimensions_complete": result.get("dimensions_complete", False),
            "methodology_version": result.get("methodology_version", ""),
            "original_request": original_request,
            "correction_applied": correction_applied,
            "timestamp": datetime.now().isoformat(),
        }
        with DRIFT_SCORES_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(drift_record, ensure_ascii=False) + "\n")

        await asyncio.sleep(api_delay)
        current_idx = (current_idx + 1) % len(character_ids)

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
            saved_path = save_history(HISTORY_DIR, session_id, scene, model, turn_log, final_stats)
        except Exception as e:
            print(f"[server] 历史记录保存失败: {e}")

    await ws.send_json({
        "type": "done",
        "session_id": session_id,
        "stats": final_stats,
        "history_file": str(saved_path) if saved_path else None,
    })
