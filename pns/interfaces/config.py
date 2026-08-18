# pns/interfaces/config.py — Setup Wizard 用的 provider/API Key 配置 API
import importlib
import os

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import pns.logic.router as router_mod
import pns.world as world_mod
from oobe import PROVIDERS, write_env

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
def get_config():
    key = router_mod._get_api_key()
    return {
        "has_key": bool(key),
        "model": os.environ.get("MODEL", "mimo-v2.5-pro"),
        "api_format": router_mod.API_FORMAT,
        "default_scene": world_mod.DEFAULT_SCENE,
    }


class ConfigPayload(BaseModel):
    provider_key: str  # 对应 oobe.PROVIDERS 的动态 key
    model: str
    api_key: str


@router.post("")
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
    # （跟 world.py 里 _reload_world() reload 世界模块是同一套路）。
    load_dotenv(override=True)
    importlib.reload(router_mod)

    return {"configured": True}


@router.get("/providers")
def get_config_providers():
    return {
        k: {"name": v["name"], "models": v["models"]}
        for k, v in PROVIDERS.items()
    }
