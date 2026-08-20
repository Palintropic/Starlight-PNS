# pns/interfaces/config.py — Setup Wizard 的 provider/API Key 配置 API，
# 以及配置重载边界的后台接口。
#
# 这里不再 importlib.reload 任何模块：让新配置生效的唯一办法是走
# pns.runtime.reload.BOUNDARY.reload()，它会关闸门、停会话、整体重建校验、
# 原子替换，失败则保留上一份可用配置。改 Python 代码属于 cold update，
# 必须停服替换文件再重启，后台接口不提供、也不应该提供这种能力。
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import pns.logic.router as router_mod
from oobe import PROVIDERS, write_env
from pns.runtime.reload import BOUNDARY

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
def get_config():
    registry = BOUNDARY.active()
    models = registry.models
    return {
        "has_key": bool(router_mod._get_api_key(models.key_name)),
        "model": models.model,
        "generator_model": models.generator_model,
        "evaluator_model": models.evaluator_model,
        "api_format": models.api_format,
        "default_scene": registry.default_scene,
        "config_revision": registry.revision,
    }


class ConfigPayload(BaseModel):
    provider_key: str  # 对应 oobe.PROVIDERS 的动态 key
    model: str
    api_key: str
    generator_model: str | None = None
    evaluator_model: str | None = None


@router.post("")
def post_config(payload: ConfigPayload):
    provider = PROVIDERS.get(payload.provider_key)
    if not provider:
        raise HTTPException(400, f"未知的 provider_key: {payload.provider_key}")
    if not payload.model:
        raise HTTPException(400, "model 不能为空")
    if not payload.api_key:
        raise HTTPException(400, "api_key 不能为空")

    generator_model = payload.generator_model or payload.model
    evaluator_model = payload.evaluator_model or payload.model
    write_env(
        provider,
        payload.model,
        payload.api_key,
        generator_model=generator_model,
        evaluator_model=evaluator_model,
    )

    # .env 属于 reloadable configuration：写盘之后走一次完整重载让它生效。
    result = BOUNDARY.reload(reason="provider 配置更新")
    if result.status == "failed":
        raise HTTPException(
            400,
            f"配置已写入 .env，但重新加载失败，仍在使用上一份可用配置：{result.error}",
        )
    return {"configured": True, "reload": result.to_dict()}


@router.get("/providers")
def get_config_providers():
    return {
        k: {"name": v["name"], "models": v["models"]}
        for k, v in PROVIDERS.items()
    }


@router.get("/reload")
def get_reload_status():
    """当前生效的配置快照、准入状态和上一次重载的结果。"""
    return BOUNDARY.status()


@router.post("/reload")
def post_reload():
    """后台「重新加载配置」按钮。

    并发保护在 ConfigBoundary 里：已经有一次重载在跑时返回 status="busy"，
    对应 HTTP 409，不排队也不并发执行第二次。
    """
    result = BOUNDARY.reload()
    if result.status == "busy":
        raise HTTPException(409, result.error or "已有一次配置重载正在进行")
    if result.status == "failed":
        # 400 但服务是好的：旧配置还在生效，新 session 照常能开。
        raise HTTPException(
            400,
            f"配置校验失败，未生效；仍在使用上一份可用配置"
            f"（revision {result.revision}）：{result.error}",
        )
    return result.to_dict()
