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
from uuid import uuid4

import pns.world as world_mod
import pns.logic.router as router_mod
from pns.logic.simulation import (
    append_drift_record,
    call_character_async,
    judge_async,
    save_history,
)
from pns.models.session import SessionState, Turn
from pns.models.world_state import WorldState
from pns.world.characters import registry as character_registry
from pns.world.scene_compat import SceneMappingError, build_initial_world_state

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
        world_state: WorldState,
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
        self.scene = scene
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
        # 权威世界状态只在 create() 里造一次，运行时不再另存副本：
        # self.world 始终就是 self.state.world_state 那一个对象。
        self.state.attach_world_state(world_state)
        self.state.initialize_runtime(scene["trigger"])

    @property
    def session_id(self) -> str:
        return self.state.session_id

    @property
    def world(self) -> WorldState:
        """本会话唯一的权威 WorldState。"""
        return self.state.world_state

    @classmethod
    def create(
        cls,
        params: dict,
        *,
        history_dir: Path = _DEFAULT_HISTORY_DIR,
        drift_scores_file: Path = _DEFAULT_DRIFT_SCORES_FILE,
    ) -> "SessionRuntime":
        if not isinstance(params, dict):
            raise SessionSetupError("会话参数必须是 JSON 对象")

        scene_id = params.get("scene", world_mod.DEFAULT_SCENE)
        try:
            max_turns = int(params.get("max_turns", 8))
            max_tokens = int(params.get("max_tokens", 1024))
            temperature = float(params.get("temperature", 0.85))
            api_delay = float(params.get("api_delay", 1.0))
        except (TypeError, ValueError) as e:
            raise SessionSetupError("max_turns、max_tokens、temperature 和 api_delay 必须是数字") from e

        if max_turns < 1 or max_tokens < 1 or api_delay < 0:
            raise SessionSetupError("max_turns 和 max_tokens 必须大于 0，api_delay 不能小于 0")

        model = params.get("model") or os.environ.get("GENERATOR_MODEL") or os.environ.get("MODEL", "mimo-v2.5-pro")
        generator_provider = os.environ.get("PROVIDER", "")
        character_ids = params.get("characters") or ["mizuki", "ena"]

        if not isinstance(character_ids, list) or not all(
            isinstance(cid, str) and cid for cid in character_ids
        ):
            raise SessionSetupError("characters 必须是非空角色 ID 组成的数组")
        if len(character_ids) < 2:
            raise SessionSetupError("至少需要2个角色才能开始会话")
        if len(set(character_ids)) != len(character_ids):
            raise SessionSetupError("角色列表不能包含重复角色")

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

        # 遗留 scene 只在这里投影成初始世界状态一次，之后 scene 不再是世界真相。
        try:
            world_state = build_initial_world_state(scene, character_ids)
        except SceneMappingError as e:
            raise SessionSetupError(str(e)) from e

        client = router_mod.create_client(api_key)
        session_id = (
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{scene['id']}_{uuid4().hex[:12]}"
        )

        return cls(
            session_id=session_id,
            scene=scene,
            character_ids=character_ids,
            world_state=world_state,
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
        self.state.start()
        try:
            async for message in self._run():
                yield message
        finally:
            self.state.cancel()

    async def _run(self) -> AsyncIterator[dict]:
        scene = self.scene
        world = self.world

        yield {
            "type": "start",
            "session_id": self.session_id,
            # 遗留 scene 块的形状不变，但 time/location 现在是当前世界状态的
            # 投影，而不是从 scene 字典里直接抄出来的静态文本。
            "scene": {
                "id": scene["id"],
                "label": scene["label"],
                "trigger": scene["trigger"],
                "time": world_mod.render_clock(world.clock),
                "location": world_mod.render_session_location(world),
            },
            "world": world.to_dict(),
            "max_turns": self.max_turns,
            "model": self.model,
        }

        for turn in range(1, self.max_turns + 1):
            current = self.state.current_character
            char_key = current
            meta = character_registry.get_character_metadata(current)
            char_name = meta.get("name", current)
            yield {"type": "generating", "turn": turn, "character": char_key, "char_name": char_name}

            generation_history = list(self.state.history_for(current))
            original_request = next(
                (
                    item.get("content", "")
                    for item in reversed(generation_history)
                    if item.get("role") == "user"
                ),
                scene.get("trigger", ""),
            )
            correction_applied = self.state.correction_for(current)

            try:
                reply = await call_character_async(
                    self.client, current, generation_history, world, self.model,
                    self.max_tokens, self.temperature, correction_applied,
                )
            except character_registry.CharacterNotReadyError as e:
                message = f"角色 '{current}' 尚未准备好：{e.detail}"
                self.state.record_error(message)
                yield {
                    "type": "error", "turn": turn, "character": current,
                    "message": message,
                }
                break
            except Exception as e:
                message = str(e)
                self.state.record_error(message)
                yield {"type": "error", "turn": turn, "message": message}
                break

            yield {"type": "judging", "turn": turn, "character": char_key, "char_name": char_name}

            try:
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
            except Exception as e:
                message = str(e)
                self.state.record_error(message)
                yield {"type": "error", "turn": turn, "message": message}
                break
            score = result.get("drift_score", 0)
            is_ooc = result.get("is_ooc", False)

            completed_turn = Turn(
                turn_number=turn,
                character=char_key,
                prompt=original_request,
                response=reply,
                timestamp=datetime.now().isoformat(),
                char_name=char_name,
                score=score,
                is_ooc=is_ooc,
                confidence=result.get("confidence", 0.0),
                drift_type=result.get("drift_type", ""),
                reason=result.get("reason", ""),
                correction=result.get("correction"),
                correction_applied=correction_applied,
                needs_human_review=result.get("needs_human_review", False),
                dimensions=result.get("dimensions", {}),
                dimensions_complete=result.get("dimensions_complete", False),
                methodology_version=result.get("methodology_version", ""),
                scene_id=result.get("scene_id", ""),
                lore_tag=result.get("lore_tag", ""),
                router_reference_status=result.get("router_reference_status", ""),
                generator_provider=self.generator_provider,
                generator_model=self.model,
                evaluator_provider=result.get("evaluator_provider", ""),
                evaluator_model=result.get("evaluator_model", ""),
            )
            try:
                append_drift_record(
                    self.drift_scores_file,
                    completed_turn.to_drift_record(self.session_id),
                )
            except Exception as e:
                message = f"漂移记录保存失败: {e}"
                self.state.record_error(message)
                yield {"type": "error", "turn": turn, "message": message}
                break

            self.state.record_turn(completed_turn)
            yield {"type": "turn", **completed_turn.to_wire_dict()}

            await asyncio.sleep(self.api_delay)
            self.state.advance_character()

        final_stats = self.state.final_stats()

        saved_path: Optional[Path] = None
        if self.state.turns:
            try:
                saved_path = save_history(
                    self.history_dir,
                    self.session_id,
                    scene,
                    self.model,
                    [turn.to_wire_dict() for turn in self.state.turns],
                    final_stats,
                    world=world,
                )
            except Exception as e:
                self.state.record_error(f"历史记录保存失败: {e}")
                print(f"[server] 历史记录保存失败: {e}")

        self.state.complete()
        yield {
            "type": "done",
            "session_id": self.session_id,
            "stats": final_stats,
            "history_file": str(saved_path) if saved_path else None,
        }
