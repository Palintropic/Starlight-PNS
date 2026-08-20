# pns/runtime/session_runtime.py — 会话编排层
# 承接原来焊在 pns.interfaces.simulate 里的角色池校验、round-robin 轮转、
# correction 队列、逐轮统计与归档触发；生成/判分/归档的实际实现仍在
# pns.logic.simulation。run() 产出的字典就是 WS 消息本体，调用方
# （目前是 /ws/run）只负责逐条 send_json，不重塑消息形状。
import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Optional

import pns.world as world_mod
import pns.logic.router as router_mod
from pns.logic.simulation import (
    append_drift_record,
    call_character_async,
    judge_async,
    save_history,
)
from pns.models.session import SessionState, Turn
from pns.world.characters import registry as character_registry

# 不从 pns.interfaces.paths 导入：pns.interfaces 包的 __init__ 会连带拉起
# app.py -> config.py -> oobe，这条链只在服务器自己的启动上下文里能跑通。
# session_runtime 需要能在没有 FastAPI/服务器上下文的地方独立导入和测试，
# 所以在这里就地算出同样的仓库根目录，不依赖 interfaces 包。
_ROOT_DIR = Path(__file__).resolve().parent.parent.parent
_DEFAULT_HISTORY_DIR = _ROOT_DIR / "history"
_DEFAULT_DRIFT_SCORES_FILE = _ROOT_DIR / "data" / "drift_scores.jsonl"


class SessionSetupError(Exception):
    """会话参数在开跑前校验失败（角色数量、角色是否存在、API Key 缺失等）。"""


class SessionRuntime:
    def __init__(
        self,
        *,
        session_id: str,
        scene: dict,
        character_ids: list,
        client,
        model: str,
        generator_provider: str,
        max_tokens: int,
        temperature: float,
        api_delay: float,
        max_turns: int,
        history_dir: Path = _DEFAULT_HISTORY_DIR,
        drift_scores_file: Path = _DEFAULT_DRIFT_SCORES_FILE,
    ):
        self.session_id = session_id
        self.scene = scene
        self.character_ids = character_ids
        self.client = client
        self.model = model
        self.generator_provider = generator_provider
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.api_delay = api_delay
        self.max_turns = max_turns
        self.history_dir = history_dir
        self.drift_scores_file = drift_scores_file

        self.state = SessionState(
            session_id=session_id, scene=scene["id"], characters=list(character_ids)
        )

        self._histories = {
            cid: [
                {
                    "role": "user",
                    "content": f"【场景】{scene['trigger']}" + ("\n请开始对话。" if i == 0 else ""),
                }
            ]
            for i, cid in enumerate(character_ids)
        }
        self._stats = {"ooc_count": 0, "scores": [], "corrections": 0}
        self._current_idx = 0
        self._pending_corrections = {cid: None for cid in character_ids}
        self._turn_log = []

    @classmethod
    def create(
        cls,
        params: dict,
        *,
        history_dir: Path = _DEFAULT_HISTORY_DIR,
        drift_scores_file: Path = _DEFAULT_DRIFT_SCORES_FILE,
    ) -> "SessionRuntime":
        scene_id = params.get("scene", world_mod.DEFAULT_SCENE)
        max_turns = int(params.get("max_turns", 8))
        model = params.get("model") or os.environ.get("GENERATOR_MODEL") or os.environ.get("MODEL", "mimo-v2.5-pro")
        generator_provider = os.environ.get("PROVIDER", "")
        max_tokens = int(params.get("max_tokens", 1024))
        temperature = float(params.get("temperature", 0.85))
        api_delay = float(params.get("api_delay", 1.0))
        character_ids = params.get("characters") or ["mizuki", "ena"]

        if len(character_ids) < 2:
            raise SessionSetupError("至少需要2个角色才能开始会话")

        for cid in character_ids:
            try:
                character_registry.get_character_metadata(cid)
            except ValueError:
                raise SessionSetupError(
                    f"角色 '{cid}' 不在当前角色包（{character_registry.ACTIVE_PACK}）中"
                )

        scene = world_mod.SCENES.get(scene_id, world_mod.SCENES[world_mod.DEFAULT_SCENE])
        api_key = router_mod._get_api_key()
        if not api_key:
            raise SessionSetupError("找不到 API Key，请刷新页面完成配置向导，或运行 python oobe.py")

        client = router_mod.create_client(api_key)
        session_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{scene['id']}"

        return cls(
            session_id=session_id,
            scene=scene,
            character_ids=character_ids,
            client=client,
            model=model,
            generator_provider=generator_provider,
            max_tokens=max_tokens,
            temperature=temperature,
            api_delay=api_delay,
            max_turns=max_turns,
            history_dir=history_dir,
            drift_scores_file=drift_scores_file,
        )

    async def run(self) -> AsyncIterator[dict]:
        scene = self.scene

        yield {
            "type": "start",
            "session_id": self.session_id,
            "scene": {
                "id": scene["id"],
                "label": scene["label"],
                "trigger": scene["trigger"],
                "time": scene["time"],
                "location": scene["location"],
            },
            "max_turns": self.max_turns,
            "model": self.model,
        }

        for turn in range(1, self.max_turns + 1):
            current = self.character_ids[self._current_idx]
            char_key = current
            meta = character_registry.get_character_metadata(current)
            char_name = meta.get("name", current)
            others = [cid for cid in self.character_ids if cid != current]

            yield {"type": "generating", "turn": turn, "character": char_key, "char_name": char_name}

            generation_history = list(self._histories[current])
            original_request = next(
                (
                    item.get("content", "")
                    for item in reversed(generation_history)
                    if item.get("role") == "user"
                ),
                scene.get("trigger", ""),
            )
            correction_applied = self._pending_corrections[current]

            try:
                reply = await call_character_async(
                    self.client, current, generation_history, scene, self.model,
                    self.max_tokens, self.temperature, correction_applied,
                )
            except character_registry.CharacterNotReadyError as e:
                yield {
                    "type": "error", "turn": turn, "character": current,
                    "message": f"角色 '{current}' 尚未准备好：{e.detail}",
                }
                break
            except Exception as e:
                yield {"type": "error", "turn": turn, "message": str(e)}
                break

            self._histories[current].append({"role": "assistant", "content": f"{char_name}：{reply}"})
            for other in others:
                self._histories[other].append({"role": "user", "content": f"{char_name}：{reply}"})

            yield {"type": "judging", "turn": turn, "character": char_key, "char_name": char_name}

            result = await judge_async(
                self.client,
                current,
                reply,
                turn,
                scene,
                original_request=original_request,
                recent_history=generation_history,
                correction_applied=correction_applied,
            )
            score = result.get("drift_score", 0)
            is_ooc = result.get("is_ooc", False)

            self._stats["scores"].append(score)
            if is_ooc:
                self._stats["ooc_count"] += 1
                self._pending_corrections[current] = result.get("correction")
                if self._pending_corrections[current]:
                    self._stats["corrections"] += 1
            else:
                self._pending_corrections[current] = None

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
                "generator_provider": self.generator_provider,
                "generator_model": self.model,
                "evaluator_provider": result.get("evaluator_provider", ""),
                "evaluator_model": result.get("evaluator_model", ""),
            }
            self._turn_log.append(turn_data)
            self.state.add_turn(
                Turn(
                    turn_number=turn,
                    character=current,
                    prompt=original_request,
                    response=reply,
                    timestamp=datetime.now().isoformat(),
                )
            )
            yield {"type": "turn", **turn_data}

            drift_record = {
                "session_id": self.session_id,
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
                "generator_provider": self.generator_provider,
                "generator_model": self.model,
                "evaluator_provider": result.get("evaluator_provider", ""),
                "evaluator_model": result.get("evaluator_model", ""),
                "original_request": original_request,
                "correction_applied": correction_applied,
                "timestamp": datetime.now().isoformat(),
            }
            append_drift_record(self.drift_scores_file, drift_record)

            await asyncio.sleep(self.api_delay)
            self._current_idx = (self._current_idx + 1) % len(self.character_ids)

        scores = self._stats["scores"]
        avg_score = sum(scores) / len(scores) if scores else 0
        final_stats = {
            "total_turns": len(scores),
            "ooc_count": self._stats["ooc_count"],
            "corrections": self._stats["corrections"],
            "avg_score": round(avg_score, 2),
            "max_score": max(scores) if scores else 0,
        }

        saved_path: Optional[Path] = None
        if self._turn_log:
            try:
                saved_path = save_history(
                    self.history_dir, self.session_id, scene, self.model, self._turn_log, final_stats
                )
            except Exception as e:
                print(f"[server] 历史记录保存失败: {e}")

        yield {
            "type": "done",
            "session_id": self.session_id,
            "stats": final_stats,
            "history_file": str(saved_path) if saved_path else None,
        }
