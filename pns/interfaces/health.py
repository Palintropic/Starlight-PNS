# pns/interfaces/health.py — 存活与就绪
#
#     GET /healthz  存活：这个进程还能应答吗
#     GET /readyz   就绪：启动配置完成了吗
#
# 两条都是**公开**的，因为容器编排要在没有凭据的情况下问它们。所以它们必须
# 同时满足两件事，而这两件事是本模块唯一的内容：
#
#   1. **不泄露。** 正文里没有密钥、没有 provider 名、没有世界状态、没有路径。
#      能看出来的只有"这是生产还是开发"和"要不要登录"——那两样浏览器本来就
#      要从 /api/auth/session 问到，藏着没有意义。
#   2. **没有权威副作用。** 不调用模型、不推进时间、不重载配置、不获取世界
#      所有权、不建目录。这里只读几个进程启动时就定下来的常量。
#
# 就绪为什么可以这么轻：**配置不可用的生产进程根本起不来**（见
# `create_app` 的生产必填校验）。所以"起来了并且能应答"就是"启动配置完成了"
# 的充分证据，健康检查不需要——也不该——再去磁盘上确认一遍。配置坏掉的表现
# 是连接被拒绝，不是一个回答"我不太好"的 200。
from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz():
    return {"status": "ok"}


@router.get("/readyz")
def readyz(request: Request):
    readiness = getattr(request.app.state, "readiness", None)
    if readiness is None:  # pragma: no cover - create_app 总会装上一个
        return {"status": "ready"}
    return {"status": "ready", **readiness}
