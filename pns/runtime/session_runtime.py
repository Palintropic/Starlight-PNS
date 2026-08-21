# pns/runtime/session_runtime.py — 会话编排层
# 承接原来焊在 pns.interfaces.simulate 里的角色池校验、round-robin 轮转、
# correction 队列、逐轮统计与归档触发；生成/判分/归档的实际实现仍在
# pns.logic.simulation。run() 产出的字典就是 WS 消息本体，调用方
# （目前是 /ws/run）只负责逐条 send_json，不重塑消息形状。
import asyncio
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Optional
from uuid import uuid4

import pns.logic.router as router_mod
from pns.logic.simulation import (
    append_drift_record,
    call_character_async,
    judge_async,
    save_history,
)
from pns.models.session import SessionState, Turn
from pns.models.world_state import WorldState
from pns.runtime.content_registry import ContentRegistry
from pns.runtime.event_commit import (
    commit_dialogue,
    dialogue_event_for_turn,
    project_turn_message,
)
from pns.runtime.reload import BOUNDARY, SessionAdmissionClosed, SessionSupervisor
from pns.runtime.scheduler import PersistentScheduler
from pns.world.characters import registry as character_registry
from pns.world.context import render_clock, render_session_location
from pns.world.scene_compat import SceneMappingError

# 不从 pns.interfaces.paths 导入：pns.interfaces 包的 __init__ 会连带拉起
# app.py -> config.py -> oobe，这条链只在服务器自己的启动上下文里能跑通。
# session_runtime 需要能在没有 FastAPI/服务器上下文的地方独立导入和测试，
# 所以在这里就地算出同样的仓库根目录，不依赖 interfaces 包。
_ROOT_DIR = Path(__file__).resolve().parent.parent.parent
_DEFAULT_HISTORY_DIR = _ROOT_DIR / "history"
_DEFAULT_DRIFT_SCORES_FILE = _ROOT_DIR / "data" / "drift_scores.jsonl"


class SessionSetupError(Exception):
    """会话参数在开跑前校验失败（角色数量、角色是否存在、API Key 缺失等）。"""


class SessionRefusedError(SessionSetupError):
    """配置正在重载，准入闸门关着，这次不开新会话。"""


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
        registry: ContentRegistry,
        supervisor: Optional[SessionSupervisor] = None,
        history_dir: Path = _DEFAULT_HISTORY_DIR,
        drift_scores_file: Path = _DEFAULT_DRIFT_SCORES_FILE,
    ):
        # 本次会话锁定的配置快照。整个生命周期只认这一份 —— 中途重载换掉全局
        # 引用，也影响不到已经开跑的会话。
        self.registry = registry
        self.supervisor = supervisor
        self._stop_reason: Optional[str] = None
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
        # 本会话私有的排期与时间推进服务。构造时它就把自己绑到 SessionState 上，
        # 之后 self.scheduler 读的就是那一份 —— 跟 world 一样，运行时不另存副本。
        # 排期队列和到期投递箱归 SessionState 所有，所以会话存档天然带着它们。
        #
        # 研究会话的 round robin **不**调用它，这是刻意的：确定性轮转的可复现性
        # 依赖"一局里时间不动、轮次顺序只由角色列表决定"，而"到点了角色要不要
        # 真的行动"本来就是 P9 的判断，不是调度器的。调度器决定的是时间什么时候
        # 往前走、什么变得可以发生。
        PersistentScheduler(self.state)

    @property
    def session_id(self) -> str:
        return self.state.session_id

    @property
    def world(self) -> WorldState:
        """本会话唯一的权威 WorldState。"""
        return self.state.world_state

    @property
    def scheduler(self) -> PersistentScheduler:
        """本会话唯一的权威调度器。"""
        return self.state.scheduler

    @property
    def stop_reason(self) -> Optional[str]:
        return self._stop_reason

    def request_stop(self, reason: str) -> None:
        """请求停止本会话。由配置重载边界调用。

        信号只是打个标记：会话在下一个轮次开始前观察到它就收尾。不当场掐断，
        是为了不撕开 P5 的提交边界 —— 已经开始的一轮要么整轮提交、要么整轮
        不算数，不会留下半条事件。
        """
        if self._stop_reason is None:
            self._stop_reason = reason

    def close(self) -> None:
        """从准入记账里注销本会话。"""
        if self.supervisor is not None:
            self.supervisor.release(self.session_id)

    @classmethod
    def create(
        cls,
        params: dict,
        *,
        registry: Optional[ContentRegistry] = None,
        supervisor: Optional[SessionSupervisor] = None,
        history_dir: Path = _DEFAULT_HISTORY_DIR,
        drift_scores_file: Path = _DEFAULT_DRIFT_SCORES_FILE,
    ) -> "SessionRuntime":
        if not isinstance(params, dict):
            raise SessionSetupError("会话参数必须是 JSON 对象")

        # from_boundary 记住这份快照是不是从全局边界取的：只有这种情况才需要
        # 在登记之后回头确认它还是当前生效的那一份（见下面的竞态说明）。
        from_boundary = registry is None
        registry = registry if registry is not None else BOUNDARY.active()
        supervisor = supervisor if supervisor is not None else BOUNDARY.supervisor

        scene_id = params.get("scene", registry.default_scene)
        try:
            max_turns = int(params.get("max_turns", 8))
            max_tokens = int(params.get("max_tokens", 1024))
            temperature = float(params.get("temperature", 0.85))
            api_delay = float(params.get("api_delay", 1.0))
        except (TypeError, ValueError) as e:
            raise SessionSetupError("max_turns、max_tokens、temperature 和 api_delay 必须是数字") from e

        if max_turns < 1 or max_tokens < 1 or api_delay < 0:
            raise SessionSetupError("max_turns 和 max_tokens 必须大于 0，api_delay 不能小于 0")

        model = params.get("model") or registry.models.generator_model
        generator_provider = registry.models.provider
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
            if not registry.has_character(cid):
                raise SessionSetupError(
                    f"角色 '{cid}' 不在当前角色包（{registry.pack_name}）中"
                )

        scene = registry.scene(scene_id)
        api_key = router_mod._get_api_key(registry.models.key_name)
        if not api_key:
            raise SessionSetupError("找不到 API Key，请刷新页面完成配置向导，或运行 python oobe.py")

        # 遗留 scene 只在这里投影成初始世界状态一次，之后 scene 不再是世界真相。
        # 配置只喂初始状态，喂完就没有回头路 —— 没有任何通道让重载去改一个
        # 已经存在的 WorldState。
        try:
            world_state = registry.new_world_state(scene, character_ids)
        except SceneMappingError as e:
            raise SessionSetupError(str(e)) from e

        client = router_mod.create_client(api_key, settings=registry.models)
        session_id = (
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{scene['id']}_{uuid4().hex[:12]}"
        )

        runtime = cls(
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
            registry=registry,
            supervisor=supervisor,
            history_dir=history_dir,
            drift_scores_file=drift_scores_file,
        )

        # 登记是准入的唯一权威判断点：闸门关着就在这里被拒，转成 SessionSetupError
        # 交给传输层（/ws/run 会把它变成一条 error 消息）。
        try:
            supervisor.admit(runtime.session_id, runtime)
        except SessionAdmissionClosed as e:
            raise SessionRefusedError(str(e)) from e

        # 竞态：如果这次 create 在"抓快照"和"登记"之间整段错过了一次重载
        # （闸门关→停止现有会话→切换→闸门开，全发生在这两步之间），那这个会话
        # 既没被 stop_all 停到，又抱着上一份配置跑起来了。登记成功之后回头对一次
        # 引用就能堵死这个窗口：对不上说明配置在我们眼皮底下换过了，拒绝重来。
        if from_boundary and BOUNDARY.active_or_none() is not registry:
            supervisor.release(runtime.session_id)
            raise SessionRefusedError("配置刚刚完成重新加载，请重新开始会话。")
        return runtime

    async def run(self) -> AsyncIterator[dict]:
        self.state.start()
        try:
            async for message in self._run():
                yield message
        finally:
            self.state.cancel()
            self.close()

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
                "time": render_clock(world.clock),
                "location": render_session_location(world),
            },
            "world": world.to_dict(),
            "max_turns": self.max_turns,
            "model": self.model,
        }

        for turn in range(1, self.max_turns + 1):
            if self._stop_reason is not None:
                yield {
                    "type": "stopped",
                    "session_id": self.session_id,
                    "turn": turn,
                    "reason": self._stop_reason,
                }
                break

            current = self.state.current_character
            char_key = current
            char_name = self.registry.character_name(current)
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
                    registry=self.registry,
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
                    registry=self.registry,
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

            # 到这里这次输出才算被接受：生成成功、判分成功、审计落盘成功。
            # 只有被接受的结果进世界历史；上面任何一步失败的候选输出都已经
            # break 掉了，不会走到这里。
            try:
                dialogue_event = dialogue_event_for_turn(
                    world, self.state.events, self.session_id, completed_turn
                )
                committed = commit_dialogue(self.state, completed_turn, dialogue_event)
            except Exception as e:
                message = f"事件提交失败: {e}"
                self.state.record_error(message)
                yield {"type": "error", "turn": turn, "message": message}
                break

            yield project_turn_message(committed, completed_turn)

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
