# pns/interfaces/world.py — World Editor API
# 图形化编辑 pns/world/scenes.py / facts.py。写回逻辑（JSON⇄Python源码、备份、
# 校验）都在 pns/world/codegen.py 里，这里只负责路由、重载、报错转换。
#
# scenes.py / facts.py 里的是纯数据字面量，属于 reloadable configuration：写盘
# 之后走 BOUNDARY.reload() 让它生效。以前这里用 importlib.reload 把模块重新
# 执行一遍 —— 那是在跑着的进程里换代码，没有整体校验、没有原子性、失败会留下
# 半初始化的模块，也不会停掉正在读旧配置的会话。P7 之后不再这么做。
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pns.runtime.reload import BOUNDARY
from pns.world import codegen

router = APIRouter(prefix="/api/world", tags=["world"])


def _apply_reload():
    """写盘之后让新配置生效；失败时保留上一份可用配置并把原因抛给编辑器。"""
    result = BOUNDARY.reload(reason="World Editor 保存")
    if result.status == "busy":
        raise HTTPException(409, result.error or "已有一次配置重载正在进行")
    if result.status == "failed":
        raise HTTPException(
            400,
            f"已写入磁盘，但新配置没通过校验、未生效，仍在使用上一份可用配置：{result.error}",
        )


def _scenes() -> dict:
    return {k: dict(v) for k, v in BOUNDARY.active().scenes.items()}


def _facts() -> dict:
    return dict(BOUNDARY.active().world_facts)


class Scene(BaseModel):
    id: str
    label: str
    time: str
    location: str
    weather: str
    day_phase: Literal["morning", "afternoon", "evening", "late_night"]
    scene_type: str
    lore_tag: Literal["CANON", "INFERRED", "UNVERIFIED"]
    trigger: str
    gate_triggers: Optional[dict[str, str]] = None
    gate_opening_note: Optional[str] = None
    auto_next: Optional[str] = None
    auto_turns: Optional[int] = None


class FactsPayload(BaseModel):
    facts: dict[str, str]


class SourcePayload(BaseModel):
    source: str


@router.get("/scenes")
def get_world_scenes():
    return _scenes()


@router.post("/scenes")
def post_world_scenes(scenes: dict[str, Scene]):
    for key, scene in scenes.items():
        if scene.id != key:
            raise HTTPException(400, f"scene key '{key}' 与内部 id '{scene.id}' 不一致")
    payload = {key: scene.model_dump() for key, scene in scenes.items()}
    try:
        codegen.save_scenes(payload)
    except codegen.CodegenError as e:
        raise HTTPException(400, str(e))
    _apply_reload()
    return _scenes()


@router.get("/scenes/source")
def get_world_scenes_source():
    return {"source": codegen.SCENES_PATH.read_text(encoding="utf-8")}


@router.post("/scenes/source")
def post_world_scenes_source(payload: SourcePayload):
    try:
        codegen.save_scenes_source(payload.source)
    except codegen.CodegenError as e:
        raise HTTPException(400, str(e))
    _apply_reload()
    return {"source": codegen.SCENES_PATH.read_text(encoding="utf-8")}


@router.get("/facts")
def get_world_facts():
    return {"facts": _facts(), "groups": codegen.FACT_GROUPS}


@router.post("/facts")
def post_world_facts(payload: FactsPayload):
    try:
        codegen.save_facts(payload.facts)
    except codegen.CodegenError as e:
        raise HTTPException(400, str(e))
    _apply_reload()
    return {"facts": _facts(), "groups": codegen.FACT_GROUPS}


@router.get("/facts/source")
def get_world_facts_source():
    return {"source": codegen.FACTS_PATH.read_text(encoding="utf-8")}


@router.post("/facts/source")
def post_world_facts_source(payload: SourcePayload):
    try:
        codegen.save_facts_source(payload.source)
    except codegen.CodegenError as e:
        raise HTTPException(400, str(e))
    _apply_reload()
    return {"source": codegen.FACTS_PATH.read_text(encoding="utf-8")}
