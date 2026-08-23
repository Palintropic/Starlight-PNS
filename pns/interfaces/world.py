# pns/interfaces/world.py — World Editor API
# 图形化编辑 pns/world/scenes.py / facts.py。写回逻辑（JSON⇄Python源码、备份、
# 校验）都在 pns/world/codegen.py 里，这里只负责路由、重载、报错转换。
#
# scenes.py / facts.py 里的是纯数据字面量，属于 reloadable configuration：写盘
# 之后走 BOUNDARY.reload() 让它生效。以前这里用 importlib.reload 把模块重新
# 执行一遍 —— 那是在跑着的进程里换代码，没有整体校验、没有原子性、失败会留下
# 半初始化的模块，也不会停掉正在读旧配置的会话。P7 之后不再这么做。
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from pns.runtime.reload import BOUNDARY, write_and_reload
from pns.world import codegen

from .security import refuse_in_production

router = APIRouter(prefix="/api/world", tags=["world"])

# 写接口在生产模式下被拒绝：它们改的是镜像层里的源码，那份改动活不过下一次
# 容器重建。读接口不受影响——在生产上看一眼当前内容是完全正当的。
WRITE_GUARD = [Depends(refuse_in_production)]


def _save(paths, write):
    """写盘 + 重载，作为一次事务：没生效就把磁盘退回保存之前的样子。

    否则一份过不了校验的配置会留在磁盘上——运行中的进程靠 last-known-good
    撑着看不出问题，下次重启就起不来了。已有重载在跑时直接 409，不写盘。
    """
    try:
        result = write_and_reload(
            BOUNDARY, paths, write, reason="World Editor 保存"
        )
    except codegen.CodegenError as e:
        raise HTTPException(400, str(e))
    if result.status == "busy":
        raise HTTPException(
            409,
            result.error or "已有一次配置重载正在进行，本次保存未写入任何内容。",
        )
    if result.status == "failed":
        raise HTTPException(
            400,
            f"新配置没通过校验、未生效，磁盘已回滚到保存前的内容，"
            f"仍在使用上一份可用配置：{result.error}",
        )


def _scenes() -> dict:
    return BOUNDARY.active().scenes_snapshot()


def _facts() -> dict:
    return BOUNDARY.active().facts_snapshot()


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


@router.post("/scenes", dependencies=WRITE_GUARD)
def post_world_scenes(scenes: dict[str, Scene]):
    for key, scene in scenes.items():
        if scene.id != key:
            raise HTTPException(400, f"scene key '{key}' 与内部 id '{scene.id}' 不一致")
    payload = {key: scene.model_dump() for key, scene in scenes.items()}
    _save([codegen.SCENES_PATH], lambda: codegen.save_scenes(payload))
    return _scenes()


@router.get("/scenes/source")
def get_world_scenes_source():
    return {"source": codegen.SCENES_PATH.read_text(encoding="utf-8")}


@router.post("/scenes/source", dependencies=WRITE_GUARD)
def post_world_scenes_source(payload: SourcePayload):
    _save([codegen.SCENES_PATH], lambda: codegen.save_scenes_source(payload.source))
    return {"source": codegen.SCENES_PATH.read_text(encoding="utf-8")}


@router.get("/facts")
def get_world_facts():
    return {"facts": _facts(), "groups": codegen.FACT_GROUPS}


@router.post("/facts", dependencies=WRITE_GUARD)
def post_world_facts(payload: FactsPayload):
    _save([codegen.FACTS_PATH], lambda: codegen.save_facts(payload.facts))
    return {"facts": _facts(), "groups": codegen.FACT_GROUPS}


@router.get("/facts/source")
def get_world_facts_source():
    return {"source": codegen.FACTS_PATH.read_text(encoding="utf-8")}


@router.post("/facts/source", dependencies=WRITE_GUARD)
def post_world_facts_source(payload: SourcePayload):
    _save([codegen.FACTS_PATH], lambda: codegen.save_facts_source(payload.source))
    return {"source": codegen.FACTS_PATH.read_text(encoding="utf-8")}
