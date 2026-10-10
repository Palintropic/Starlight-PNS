# tests/test_simulate_ws.py — /ws/run 在客户端中途断开时的收尾。
#
# 客户端关页面后，下一次 send_json 会抛 WebSocketDisconnect。这是正常结束：
# 处理函数要安静返回（不冒 traceback），并且照样注销会话。
#
# 运行: python -m unittest tests.test_simulate_ws -v
import asyncio
import unittest
from unittest.mock import patch

from fastapi import WebSocketDisconnect

from pns.interfaces import simulate


class _FakeRuntime:
    def __init__(self):
        self.closed = False

    async def run(self):
        for i in range(3):
            yield {"type": "line", "n": i}

    def close(self):
        self.closed = True


class _DisconnectingWebSocket:
    """第 n 次 send_json 时像真客户端已断开那样抛 WebSocketDisconnect。"""

    def __init__(self, fail_on: int):
        self.fail_on = fail_on
        self.sent = []

    async def accept(self):
        pass

    async def receive_json(self):
        return {}

    async def send_json(self, msg):
        if len(self.sent) == self.fail_on:
            raise WebSocketDisconnect(code=1006)
        self.sent.append(msg)

    async def close(self, code=1000):
        pass


class WsRunDisconnectTests(unittest.TestCase):
    def _run(self, ws, runtime):
        with patch.object(simulate.SessionRuntime, "create", return_value=runtime):
            asyncio.run(simulate.run_simulation(ws))

    def test_a_client_disconnect_ends_quietly_and_releases_the_session(self):
        runtime = _FakeRuntime()
        ws = _DisconnectingWebSocket(fail_on=1)
        self._run(ws, runtime)  # 不抛
        self.assertEqual(len(ws.sent), 1)
        self.assertTrue(runtime.closed)

    def test_other_send_errors_still_propagate(self):
        runtime = _FakeRuntime()

        class _Broken(_DisconnectingWebSocket):
            async def send_json(self, msg):
                raise ValueError("not a disconnect")

        with self.assertRaises(ValueError):
            self._run(_Broken(fail_on=0), runtime)
        self.assertTrue(runtime.closed)


if __name__ == "__main__":
    unittest.main()
