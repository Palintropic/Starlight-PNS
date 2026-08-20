# pns/interfaces/simulate.py — 实时对话模拟 WebSocket
# 只负责协议消息（accept/receive_json/send_json）；会话编排、角色池、
# round-robin、correction 队列等逻辑都在 pns.runtime.session_runtime 里。
from fastapi import APIRouter, WebSocket

from pns.runtime.session_runtime import SessionRuntime, SessionSetupError

router = APIRouter(tags=["simulate"])


@router.websocket("/ws/run")
async def run_simulation(ws: WebSocket):
    await ws.accept()

    try:
        params = await ws.receive_json()
    except Exception:
        await ws.close(code=1003)
        return

    try:
        runtime = SessionRuntime.create(params)
    except SessionSetupError as e:
        await ws.send_json({"type": "error", "message": str(e)})
        await ws.close()
        return

    # 显式 close()：run() 正常走完会自己注销，但如果 send_json 中途抛异常，
    # 异步生成器的 finally 要等 GC 才跑，会让准入记账里挂着一个死会话。
    try:
        async for msg in runtime.run():
            await ws.send_json(msg)
    finally:
        runtime.close()
