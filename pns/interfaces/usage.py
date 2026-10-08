# pns/interfaces/usage.py — 用量账本的 HTTP 边界（COST-1）
#
#     GET  /api/usage/summary?days=7
#     POST /api/usage/balance-anchor
#
# 汇总是只读的（read 权限）；设余额锚点是写操作，按 authz 的默认规则要
# operate 权限，和开始 / 停止认知同一道门。
#
# 账本是运维数据：这里交出去的只有计数、token 数、估算花费、封闭枚举值和
# 我们自己的时间戳。provider 的报错原文从来没进过账本，所以这里也不可能有。
#
# 余额永远是"本地估算"：MiMo 没有余额接口，计费还有延迟（官方 FAQ），账本也
# 看不见 SDK 内部重试和超时后其实扣了钱的那些请求。正文里把这些不确定性
# 原样交出去，由界面写明，而不是在这里算成一个看起来很确定的数。
import math
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, StrictFloat, StrictInt
from typing import Union

from pns.runtime.usage_ledger import (
    DEFAULT_BALANCE_WARN_YUAN,
    DEFAULT_FAILURE_ALERT,
    AnchorError,
    load_anchor,
    save_anchor,
    summarize,
    validate_anchor,
)

from .composition import WorldControlPlane

router = APIRouter(prefix="/api/usage", tags=["usage"])

FAILURE_ALERT_ENV = "PNS_PROVIDER_FAILURE_ALERT"
BALANCE_WARN_ENV = "PNS_BALANCE_WARN_YUAN"
MAX_SUMMARY_DAYS = 90


class UsageSettingsError(ValueError):
    pass


@dataclass(frozen=True)
class UsageSettings:
    """两个告警阈值。启动时读一次；看不懂就让进程起不来，绝不悄悄回落。"""

    failure_alert: int = DEFAULT_FAILURE_ALERT
    balance_warn_yuan: float = DEFAULT_BALANCE_WARN_YUAN

    @classmethod
    def from_env(cls, env=None) -> "UsageSettings":
        env = os.environ if env is None else env
        alert_raw = (env.get(FAILURE_ALERT_ENV) or "").strip()
        warn_raw = (env.get(BALANCE_WARN_ENV) or "").strip()
        alert = DEFAULT_FAILURE_ALERT
        warn = DEFAULT_BALANCE_WARN_YUAN
        if alert_raw:
            try:
                alert = int(alert_raw)
            except ValueError as e:
                raise UsageSettingsError(f"{FAILURE_ALERT_ENV} 必须是正整数，实际是 {alert_raw!r}") from e
            if alert < 1:
                raise UsageSettingsError(f"{FAILURE_ALERT_ENV} 必须是正整数，实际是 {alert_raw!r}")
        if warn_raw:
            try:
                warn = float(warn_raw)
            except ValueError as e:
                raise UsageSettingsError(f"{BALANCE_WARN_ENV} 必须是数字，实际是 {warn_raw!r}") from e
            if not math.isfinite(warn):
                raise UsageSettingsError(f"{BALANCE_WARN_ENV} 必须是有限数，实际是 {warn_raw!r}")
        return cls(failure_alert=alert, balance_warn_yuan=warn)


def get_control_plane(request: Request) -> WorldControlPlane:
    plane = getattr(request.app.state, "world_control_plane", None)
    if plane is None:  # pragma: no cover - create_app 总会装上一个
        raise HTTPException(503, {"category": "control_plane_unavailable",
                                  "message": "本进程没有装配持久世界组装边界"})
    return plane


def get_usage_settings(request: Request) -> UsageSettings:
    settings = getattr(request.app.state, "usage_settings", None)
    return settings if isinstance(settings, UsageSettings) else UsageSettings()


class BalanceAnchorRequest(BaseModel):
    # Strict：`"100"`、`true` 都不是余额。
    balance_yuan: Union[StrictInt, StrictFloat]
    at: Optional[datetime] = None


@router.get("/summary")
def usage_summary(
    days: int = Query(7, ge=1, le=MAX_SUMMARY_DAYS),
    plane: WorldControlPlane = Depends(get_control_plane),
    settings: UsageSettings = Depends(get_usage_settings),
) -> Dict[str, Any]:
    """最近 `days` 个北京日的用量、连续失败段与余额估算。"""
    ledger = plane.usage_ledger
    return summarize(
        ledger,
        days=days,
        anchor=load_anchor(ledger.root),
        alert_threshold=settings.failure_alert,
        warn_yuan=settings.balance_warn_yuan,
    )


@router.post("/balance-anchor")
def set_balance_anchor(
    payload: BalanceAnchorRequest,
    plane: WorldControlPlane = Depends(get_control_plane),
) -> Dict[str, Any]:
    """记下"控制台上看到的此刻余额"。之后的估算从这里往下扣。

    填的是余额本身，不是充了多少：欠着 0.58 再充 100，控制台显示 99.42。
    """
    ledger = plane.usage_ledger
    try:
        anchor = validate_anchor(payload.balance_yuan, payload.at, ledger.now())
    except AnchorError as e:
        raise HTTPException(422, {"category": "invalid_anchor", "message": str(e)}) from e
    try:
        save_anchor(ledger.root, anchor)
    except OSError as e:
        raise HTTPException(
            500, {"category": "storage_error", "message": "余额锚点写不进用量目录"}
        ) from e
    return anchor.to_dict()
