# pns/interfaces/simulate.py — 实时对话模拟 WebSocket
# 只负责协议消息（accept/receive_json/send_json）；会话编排、角色池、
# round-robin、correction 队列等逻辑都在 pns.runtime.session_runtime 里。
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from pns.runtime.session_runtime import SessionRuntime, SessionSetupError

router = APIRouter(tags=["simulate"])


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

    try:
        runtime = SessionRuntime.create(params)
    except SessionSetupError as e:
        if await _sent(ws.send_json({"type": "error", "message": str(e)})):
            await _sent(ws.close())
        return

    # 显式 close()：run() 正常走完会自己注销，但如果中途返回或抛异常，
    # 异步生成器的 finally 要等 GC 才跑，会让准入记账里挂着一个死会话。
    try:
        async for msg in runtime.run():
            if not await _sent(ws.send_json(msg)):
                return
    finally:
        runtime.close()
