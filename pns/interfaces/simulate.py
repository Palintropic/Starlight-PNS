# pns/interfaces/simulate.py — 实时对话模拟 WebSocket
# 只负责协议消息（accept/receive_json/send_json）；会话编排、角色池、
# round-robin、correction 队列等逻辑都在 pns.runtime.session_runtime 里。
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from pns.runtime.session_runtime import SessionRuntime, SessionSetupError

from .authz import required_scope
from .recheck import IdentityGate, RecheckSlots

router = APIRouter(tags=["simulate"])

# 1008 = policy violation：身份不再成立时关连接用的状态码。
_CLOSE_UNAUTHORIZED = 1008
_UNAUTHORIZED_TEXT = "登录已失效或权限已变更，请重新登录。"


async def _sent(action) -> bool:
    """发一条消息；客户端已经走了就返回 False。

    只包住发送这一下：断开是正常结束，不该冒成 traceback；但 runtime 内部
    抛出的任何东西（哪怕恰好也是 WebSocketDisconnect）都不经过这里，照样上抛。
    """
    try:
        await action
        return True
    except WebSocketDisconnect:
        return False


def _identity_gate(ws: WebSocket) -> Optional[IdentityGate]:
    """这条连接的身份复核。中间件只在握手时认过一次，之后都靠它。"""
    app = ws.scope.get("app")
    auth = getattr(app.state, "admin_auth", None) if app is not None else None
    if auth is None:
        return None
    slots = getattr(app.state, "identity_rechecks", None)
    if slots is None:
        slots = app.state.identity_rechecks = RecheckSlots()
    return IdentityGate(
        auth, ws.scope, required_scope("/ws/run", "GET", "websocket"), slots
    )


@router.websocket("/ws/run")
async def run_simulation(ws: WebSocket):
    # 握手阶段 starlette 不把 OSError 转成 WebSocketDisconnect，客户端在 accept
    # 之前就走了的话只能在这里认出来。
    try:
        await ws.accept()
    except (OSError, WebSocketDisconnect):
        return

    try:
        params = await ws.receive_json()
    except WebSocketDisconnect:
        return
    except Exception:
        await _sent(ws.close(code=1003))
        return

    # 客户端可以握手以后等很久才发参数，握手时的身份到这里已经不算数了。
    gate = _identity_gate(ws)
    if gate is not None and not await gate():
        if await _sent(ws.send_json({"type": "error", "message": _UNAUTHORIZED_TEXT})):
            await _sent(ws.close(code=_CLOSE_UNAUTHORIZED))
        return

    try:
        runtime = SessionRuntime.create(params, authorize=gate)
    except SessionSetupError as e:
        if await _sent(ws.send_json({"type": "error", "message": str(e)})):
            await _sent(ws.close())
        return

    # 显式 close()：run() 正常走完会自己注销，但如果中途返回或抛异常，
    # 异步生成器的 finally 要等 GC 才跑，会让准入记账里挂着一个死会话。
    revoked = False
    gone = False
    try:
        async for msg in runtime.run():
            # 身份失效后只把那条 stopped 发出去。run() 还要走完，已提交的轮次要
            # 存进历史，但收尾内容不再给这条连接。
            if revoked:
                continue
            # gate 在产出 stopped 之前就已经拒绝，所以这里先看：stopped 发送时
            # 客户端刚好断开，也要走完收尾。普通断连不能这样，run() 还没停，
            # 走下去会继续调用模型。
            revoked = gate is not None and gate.denied
            if not await _sent(ws.send_json(msg)):
                if not revoked:
                    return
                gone = True
    finally:
        runtime.close()
    if revoked and not gone:
        await _sent(ws.close(code=_CLOSE_UNAUTHORIZED))
