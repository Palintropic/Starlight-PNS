# tests/test_simulate_ws.py — /ws/run 在客户端中途断开时的收尾。
#
# 客户端走掉是正常结束：处理函数要安静返回（不冒 traceback），建好的会话
# 照样注销。用真的 starlette WebSocket 跑，让 ASGI send 抛 OSError，走它
# 自己的 OSError → WebSocketDisconnect 转换，而不是在替身里直接抛。
#
# 盯住的断开路径：
#   1. 读参数时客户端已断开
#   2. 建会话失败、回错误消息时客户端已断开
#   3. 推送台词途中客户端断开（会话要注销）
# 以及反面：runtime 内部抛的 WebSocketDisconnect 不能被当成断开吞掉。
#
# 运行: python -m unittest tests.test_simulate_ws -v
import asyncio
import json
import unittest
from unittest.mock import patch

from starlette.websockets import WebSocket, WebSocketDisconnect

from pns.interfaces import simulate
from pns.runtime.session_runtime import SessionSetupError


class _FakeRuntime:
    def __init__(self, fail_inside=False):
        self.closed = False
        self.fail_inside = fail_inside

    async def run(self):
        for i in range(3):
            if self.fail_inside and i == 1:
                raise WebSocketDisconnect(code=4000)
            yield {"type": "line", "n": i}

    def close(self):
        self.closed = True


def _socket(incoming, sends_before_gone):
    """incoming：客户端依次发来的 ASGI 消息；第 sends_before_gone 条 websocket.send
    起，ASGI send 抛 OSError（像 uvicorn 发现对端已断开那样）。"""
    queue = [{"type": "websocket.connect"}, *incoming]
    sent = []

    async def receive():
        return queue.pop(0) if queue else {"type": "websocket.disconnect", "code": 1006}

    async def send(message):
        if message["type"] in ("websocket.send", "websocket.close"):
            if len([m for m in sent if m["type"] == "websocket.send"]) >= sends_before_gone:
                raise OSError("peer went away")
        sent.append(message)

    scope = {"type": "websocket", "path": "/ws/run", "headers": [], "query_string": b""}
    return WebSocket(scope, receive, send), sent


def _params():
    return {"type": "websocket.receive", "text": json.dumps({})}


class WsRunDisconnectTests(unittest.TestCase):
    def _run(self, ws, **create):
        with patch.object(simulate.SessionRuntime, "create", **create):
            asyncio.run(simulate.run_simulation(ws))

    def test_a_disconnect_before_params_ends_quietly(self):
        ws, _ = _socket([{"type": "websocket.disconnect", "code": 1001}], sends_before_gone=0)
        self._run(ws, return_value=_FakeRuntime())  # 不抛

    def test_a_disconnect_while_reporting_a_setup_error_ends_quietly(self):
        ws, sent = _socket([_params()], sends_before_gone=0)
        self._run(ws, side_effect=SessionSetupError("no such scene"))  # 不抛
        self.assertFalse([m for m in sent if m["type"] == "websocket.send"])

    def test_a_disconnect_mid_run_ends_quietly_and_releases_the_session(self):
        runtime = _FakeRuntime()
        ws, sent = _socket([_params()], sends_before_gone=1)
        self._run(ws, return_value=runtime)  # 不抛
        self.assertEqual(len([m for m in sent if m["type"] == "websocket.send"]), 1)
        self.assertTrue(runtime.closed)

    def test_a_disconnect_raised_inside_the_runtime_is_not_swallowed(self):
        runtime = _FakeRuntime(fail_inside=True)
        ws, _ = _socket([_params()], sends_before_gone=99)
        with self.assertRaises(WebSocketDisconnect):
            self._run(ws, return_value=runtime)
        self.assertTrue(runtime.closed)

    def test_other_send_errors_still_propagate(self):
        runtime = _FakeRuntime()
        ws, _ = _socket([_params()], sends_before_gone=99)

        async def broken(_msg, mode="text"):
            raise ValueError("not a disconnect")

        ws.send_json = broken
        with self.assertRaises(ValueError):
            self._run(ws, return_value=runtime)
        self.assertTrue(runtime.closed)


if __name__ == "__main__":
    unittest.main()
