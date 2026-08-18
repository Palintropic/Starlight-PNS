# pns/interfaces/world.py — World Editor API
# 图形化编辑 pns/world/scenes.py / facts.py。写回逻辑（JSON⇄Python源码、备份、
# 校验）都在 pns/world/codegen.py 里，这里只负责路由、reload、报错转换。
import importlib
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import pns.world as world_mod
import pns.world.facts as facts_submod
import pns.world.scenes as scenes_submod
from pns.world import codegen

router = APIRouter(prefix="/api/world", tags=["world"])


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
    return world_mod.SCENES


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
    _reload_world()
    return world_mod.SCENES


@router.get("/scenes/source")
def get_world_scenes_source():
    return {"source": codegen.SCENES_PATH.read_text(encoding="utf-8")}


@router.post("/scenes/source")
def post_world_scenes_source(payload: SourcePayload):
    try:
        codegen.save_scenes_source(payload.source)
    except codegen.CodegenError as e:
        raise HTTPException(400, str(e))
    _reload_world()
    return {"source": codegen.SCENES_PATH.read_text(encoding="utf-8")}


@router.get("/facts")
def get_world_facts():
    return {"facts": world_mod.WORLD_FACTS, "groups": codegen.FACT_GROUPS}


@router.post("/facts")
def post_world_facts(payload: FactsPayload):
    try:
        codegen.save_facts(payload.facts)
    except codegen.CodegenError as e:
        raise HTTPException(400, str(e))
    _reload_world()
    return {"facts": world_mod.WORLD_FACTS, "groups": codegen.FACT_GROUPS}


@router.get("/facts/source")
def get_world_facts_source():
    return {"source": codegen.FACTS_PATH.read_text(encoding="utf-8")}


@router.post("/facts/source")
def post_world_facts_source(payload: SourcePayload):
    try:
        codegen.save_facts_source(payload.source)
    except codegen.CodegenError as e:
        raise HTTPException(400, str(e))
    _reload_world()
    return {"source": codegen.FACTS_PATH.read_text(encoding="utf-8")}
