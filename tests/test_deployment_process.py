# tests/test_deployment_process.py — DEPLOY-1 里只有真的起一个进程才证得了的东西。
#
# 这一组把服务器当**进程**来测：真的 uvicorn、真的信号、真的磁盘、真的
# HTTP。它跑在一棵按 `.dockerignore` 放行清单拷出来的文件树上，也就是镜像里
# 会有的那个文件子集——所以"这些文件够不够开机"顺带也被证了一遍。
#
# 那棵树里**没有 .env**，这一点是要害而不是细节：`.env` 的优先级高于进程
# 环境，如果测试跑在真仓库上，开发机上那份带真 API Key 的 .env 会把测试注入
# 的假 provider 顶掉，于是一条本该打向本地假端点的调用会打到真的服务商去。
#
# 盯住的东西按"错了会怎样"排：
#   1. 生产缺必填项时进程**起不来**，而且不留下一个空世界或一个被锁住的存档。
#   2. 重启不等于 Start：重启进程、恢复世界、等若干个 tick 周期，模型假端点
#      的计数必须是 0。同一个假端点在显式 Start 之后必须计到 ≥1——否则这个
#      计数器没有证伪能力，第 2 条就是一句空话。
#   3. SIGTERM 走文档写明的生命周期：有界退出、如实的关闭报告、磁盘上是最后
#      一次成功 checkpoint。
#   4. SIGKILL 之后恢复出来的正好是最后一次成功 checkpoint，不多不少。
#   5. 换一个进程（等价于换一个容器）不丢世界。
#   6. 捕获到的进程输出里没有凭据，异常路径也算。
#   7. 同一个存档目录同时只有一个写者。
#
# 运行: python -m unittest tests.test_deployment_process -v
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

ADMIN_TOKEN = "PROC-ADMIN-CANARY-6d5c4b3a29180716f5e4d3c2b1a09876"
KEY_CANARY = "PROC-MODEL-CANARY-1122334455667788990aabbccddeeff0"

# 镜像里会有的文件子集。跟 .dockerignore 的放行清单一致；对不上时
# tests/test_deployment_package.py 会先红。
IMAGE_TREE = ("pns", "scripts", "packs", "config.yaml")

SCENE = "nightcord"
CHARACTERS = ["mizuki", "ena"]

STARTUP_TIMEOUT = 40.0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ModelStub:
    """一个只会数数的假 provider 端点。

    它是这一组测试里"有没有发生模型调用"的唯一判据：断言不看日志措辞、不看
    内部计数器，只看**有没有请求真的打到一个端点上**。
    """

    def __init__(self):
        self.requests = []
        # 单次响应前的停顿。用来制造"停机撞上一次进行中的调用"这个场面。
        self.delay = 0.0
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                with stub._lock:
                    stub.requests.append((self.path, body))
                if stub.delay:
                    time.sleep(stub.delay)
                payload = json.dumps(
                    {
                        "id": "stub",
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "……嗯。"},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.requests)

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def build_image_tree(destination: Path) -> Path:
    """按镜像的放行清单拷一棵文件树出来。里面**没有** .env。"""
    destination.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.bak", ".DS_Store")
    for name in IMAGE_TREE:
        source = REPO_ROOT / name
        target = destination / name
        if source.is_dir():
            shutil.copytree(source, target, ignore=ignore)
        else:
            shutil.copy2(source, target)
    # 前端只拷构建产物，跟镜像一样。
    dist = REPO_ROOT / "dashboard" / "dist"
    if dist.exists():
        shutil.copytree(dist, destination / "dashboard" / "dist")
    return destination


class ServerProcess:
    """一个真的 `python scripts/server.py`。"""

    def __init__(self, tree: Path, log: Path, env: dict):
        self.tree = tree
        self.log = log
        self.env = env
        self.port = int(env["PORT"])
        self.process = None

    def start(self):
        handle = self.log.open("ab")
        self.process = subprocess.Popen(
            [sys.executable, str(self.tree / "scripts" / "server.py")],
            cwd=str(self.tree),
            env=self.env,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        handle.close()
        return self.process

    def wait_ready(self, timeout=STARTUP_TIMEOUT) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(self.url("/readyz"), timeout=1.0) as r:
                    if r.status == 200:
                        return True
            except Exception:
                time.sleep(0.15)
        return False

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def request(self, method: str, path: str, body=None, token=ADMIN_TOKEN):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url(path), data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=30.0) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"null")
            except json.JSONDecodeError:
                return e.code, {"raw": raw.decode("utf-8", "replace")}

    def stop(self, sig=signal.SIGTERM, timeout=60.0) -> int:
        if self.process is None or self.process.poll() is not None:
            return -1 if self.process is None else self.process.returncode
        self.process.send_signal(sig)
        return self.process.wait(timeout=timeout)

    def kill(self):
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=30.0)

    def output(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")


class ProcessTestCase(unittest.TestCase):
    """一棵干净的镜像文件树、一个假 provider 端点、一份独立的存档根。"""

    @classmethod
    def setUpClass(cls):
        cls._tree_dir = tempfile.TemporaryDirectory()
        cls.tree = build_image_tree(Path(cls._tree_dir.name) / "app")
        if not (cls.tree / "dashboard" / "dist").exists():
            raise unittest.SkipTest("Dashboard 未构建，生产模式起不来")

    @classmethod
    def tearDownClass(cls):
        cls._tree_dir.cleanup()

    def setUp(self):
        self.stub = ModelStub()
        self.addCleanup(self.stub.close)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)
        self.root = self.state / "worlds"
        self.log = self.state / "server.log"
        self.servers = []
        self.addCleanup(self.cleanup_servers)

    def cleanup_servers(self):
        for server in self.servers:
            server.kill()

    def env(self, **overrides):
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PYTHONUNBUFFERED": "1",
            "PNS_ENV": "production",
            "PNS_ADMIN_TOKEN": ADMIN_TOKEN,
            "PNS_WORLD_ROOT": str(self.root),
            "PORT": str(free_port()),
            # provider 指向本地假端点。真调用会被数到，而不是打出去。
            "API_FORMAT": "openai",
            "BASE_URL": f"http://127.0.0.1:{self.stub.port}/v1",
            "PNS_API_KEY_NAME": "SMOKE_KEY",
            "SMOKE_KEY": KEY_CANARY,
            "MODEL": "smoke-model",
            "PNS_GRACEFUL_TIMEOUT": "5",
        }
        env.update(overrides)
        return env

    def server(self, **overrides) -> ServerProcess:
        server = ServerProcess(self.tree, self.log, self.env(**overrides))
        self.servers.append(server)
        return server

    def started(self, **overrides) -> ServerProcess:
        server = self.server(**overrides)
        server.start()
        self.assertTrue(
            server.wait_ready(), f"服务器没起来：\n{server.output()[-3000:]}"
        )
        return server

    # ── 便捷 ────────────────────────────────────────────────────────────
    def create_world(self, server, world_id="nightcord"):
        status, body = server.request(
            "POST",
            "/api/persistent-worlds",
            {"world_id": world_id, "scene": SCENE, "characters": CHARACTERS},
        )
        self.assertEqual(status, 201, body)
        return body

    def assert_stopped_cleanly(self, server):
        """SIGTERM 之后的退出码。

        uvicorn 在跑完优雅停机之后会把捕获到的信号重新发给自己，所以进程是
        "被 SIGTERM 结束"（Python 侧 -15，容器侧 128+15=143），不是 0。这是
        POSIX 的正常写法，也是 `docker stop` 期望看到的形状——不是失败。
        关键证据是**停机在那之前跑完了**，所以下面还要看关闭报告。
        """
        code = server.stop()
        self.assertIn(
            code, (0, -int(signal.SIGTERM)), f"停机异常：{server.output()[-2000:]}"
        )
        return code

    def archive(self, world_id="nightcord"):
        return json.loads((self.root / world_id / "world.json").read_text("utf-8"))


# ── 1. 生产必填项缺失 ───────────────────────────────────────────────────
class ProductionStartupTests(ProcessTestCase):
    def assert_refuses_to_start(self, **overrides):
        server = self.server(**overrides)
        process = server.start()
        code = process.wait(timeout=STARTUP_TIMEOUT)
        self.assertNotEqual(code, 0, "缺必填配置的生产进程不该正常退出")
        self.assertFalse(self.root.exists(), "起不来的进程不该留下存档根")
        return server.output()

    def test_missing_admin_token_refuses_to_start(self):
        output = self.assert_refuses_to_start(PNS_ADMIN_TOKEN="")
        self.assertIn("PNS_ADMIN_TOKEN", output)

    def test_short_admin_token_refuses_to_start(self):
        self.assert_refuses_to_start(PNS_ADMIN_TOKEN="short-token")

    def test_missing_model_credential_refuses_to_start(self):
        output = self.assert_refuses_to_start(SMOKE_KEY="")
        self.assertIn("SMOKE_KEY", output)

    def test_a_started_production_server_is_ready_and_guarded(self):
        server = self.started()
        status, _ = server.request("GET", "/api/persistent-worlds", token=None)
        self.assertEqual(status, 401)
        status, body = server.request("GET", "/readyz", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "production")


# ── 2. 重启不等于 Start ─────────────────────────────────────────────────
class RestartDoesNotStartTests(ProcessTestCase):
    def test_restart_and_restore_make_zero_model_calls(self):
        first = self.started()
        self.create_world(first)
        self.assertEqual(first.request("POST", "/api/persistent-worlds/nightcord/checkpoint")[0], 200)
        self.assert_stopped_cleanly(first)

        second = self.started(
            PNS_AUTONOMY_TICK_MINUTES="10", PNS_AUTONOMY_INTERVAL_SECONDS="0.5"
        )
        status, world = second.request("POST", "/api/persistent-worlds/nightcord/restore")
        self.assertEqual(status, 200, world)
        # 若干个 tick 周期都过去了。自动调用如果存在，早该发生了。
        time.sleep(3.0)
        self.assertEqual(
            self.stub.count, 0, f"重启之后自己开始花额度了：{self.stub.requests[:1]}"
        )
        status, world = second.request("GET", "/api/persistent-worlds/nightcord")
        self.assertEqual(status, 200, world)
        self.assertTrue(world["running"], "世界应当是开着的")
        autonomy = world["autonomy"]
        self.assertTrue(
            autonomy is None or autonomy["state"] == "stopped",
            f"恢复之后驱动不该在跑：{autonomy}",
        )

    def test_an_explicit_start_does_reach_the_model(self):
        """上一条的证伪能力全靠这一条：假端点确实数得到调用。"""
        server = self.started(
            PNS_AUTONOMY_TICK_MINUTES="10", PNS_AUTONOMY_INTERVAL_SECONDS="0.5"
        )
        self.create_world(server)
        self.assertEqual(self.stub.count, 0)
        status, body = server.request(
            "POST", "/api/persistent-worlds/nightcord/autonomy/start"
        )
        self.assertEqual(status, 200, body)
        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline and self.stub.count == 0:
            time.sleep(0.25)
        self.assertGreater(
            self.stub.count, 0, f"显式 Start 之后也没有模型调用：{server.output()[-2000:]}"
        )
        self.assertEqual(
            server.request("POST", "/api/persistent-worlds/nightcord/autonomy/stop")[0],
            200,
        )


# ── 3–5. 停机、崩溃与换容器 ─────────────────────────────────────────────
class ShutdownAndRecoveryTests(ProcessTestCase):
    def test_sigterm_closes_cleanly_and_says_so(self):
        server = self.started()
        self.create_world(server)
        self.assert_stopped_cleanly(server)
        output = server.output()
        self.assertIn("已干净关闭", output)
        self.assertNotIn("**没有**干净关闭", output)
        record = json.loads((self.root / "nightcord" / "OWNER.lock").read_text("utf-8"))
        self.assertEqual(record["state"], "released")

    def test_a_replaced_process_restores_the_committed_state(self):
        """换一个进程等价于换一个容器：存档在挂载点上，不在进程里。"""
        first = self.started()
        self.create_world(first)
        status, body = first.request(
            "POST",
            "/api/persistent-worlds/nightcord/activity",
            {"character_id": "mizuki", "activity": "drawing"},
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body["changed"])
        revision = body["world"]["revision"]
        self.assert_stopped_cleanly(first)

        second = self.started()
        status, world = second.request("POST", "/api/persistent-worlds/nightcord/restore")
        self.assertEqual(status, 200, world)
        self.assertGreaterEqual(world["revision"], revision)
        self.assertEqual(world["clock"], body["world"]["clock"])

    def test_sigkill_recovers_to_the_last_successful_checkpoint(self):
        first = self.started()
        self.create_world(first)
        status, body = first.request(
            "POST",
            "/api/persistent-worlds/nightcord/activity",
            {"character_id": "mizuki", "activity": "composing"},
        )
        self.assertEqual(status, 200, body)
        durable = self.archive()["revision"]
        first.process.send_signal(signal.SIGKILL)
        first.process.wait(timeout=30.0)

        second = self.started()
        status, world = second.request("POST", "/api/persistent-worlds/nightcord/restore")
        self.assertEqual(status, 200, world)
        self.assertEqual(
            world["revision"], durable, "恢复出来的不是最后一次成功 checkpoint"
        )
        # 上一个拥有者是被强杀的，所以锁记录里必须留着这件事，而不是
        # 一句"上一个是干净走的"。
        self.assertIsNotNone(world["recovered_from"])

    def test_sigterm_during_an_in_flight_tick_still_settles_the_boundary(self):
        """停机撞上一次进行中的有界操作。

        假 provider 故意很慢，所以 SIGTERM 到达时驱动多半正卡在一次模型调用上。
        这时要成立的是三件事：进程仍然在有界时间内退出；关闭报告如实说明结果；
        磁盘上是一份读得回来的存档。**不**允许的是在边界落定之前就宣布停了。
        """
        self.stub.delay = 4.0
        server = self.started(
            PNS_AUTONOMY_TICK_MINUTES="10",
            PNS_AUTONOMY_INTERVAL_SECONDS="0.2",
            PNS_GRACEFUL_TIMEOUT="5",
        )
        self.create_world(server)
        self.assertEqual(
            server.request("POST", "/api/persistent-worlds/nightcord/autonomy/start")[0],
            200,
        )
        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline and self.stub.count == 0:
            time.sleep(0.2)
        self.assertGreater(self.stub.count, 0, "没能让一次 tick 真的开始")

        started = time.monotonic()
        self.assert_stopped_cleanly(server)
        elapsed = time.monotonic() - started
        # Compose 给的 stop_grace_period 是 90s。停机预算必须明显小于它，
        # 否则容器会在最后一次 checkpoint 完成之前被 SIGKILL。
        self.assertLess(elapsed, 60.0, f"停机花了 {elapsed:.1f}s，超出预算")

        output = server.output()
        self.assertRegex(output, r"世界 'nightcord' (已干净关闭|\*\*没有\*\*干净关闭)")
        # 磁盘上留下的必须是一份读得回来的存档。
        self.assertGreaterEqual(self.archive()["revision"], 1)

    def test_a_second_writer_against_the_same_directory_fails_loudly(self):
        first = self.started()
        self.create_world(first)
        second = self.started()
        status, body = second.request("POST", "/api/persistent-worlds/nightcord/restore")
        self.assertEqual(status, 409, body)
        self.assertEqual(body["detail"]["category"], "world_already_open")
        # 第一个写者一点没被动到。
        status, world = first.request("GET", "/api/persistent-worlds/nightcord")
        self.assertEqual(status, 200, world)
        self.assertTrue(world["owned"])


# ── 6. 日志里没有凭据 ───────────────────────────────────────────────────
class NoSecretInProcessOutputTests(ProcessTestCase):
    def test_captured_output_never_contains_a_credential(self):
        server = self.started()
        self.create_world(server)
        # 正常路径 + 几条错误路径：坏凭据、不存在的世界、非法请求体、
        # 一次会打到假 provider 上的真生成。
        server.request("GET", "/api/persistent-worlds", token="wrong-token")
        server.request("GET", "/api/persistent-worlds/does-not-exist")
        server.request("POST", "/api/persistent-worlds", {"world_id": "!!bad!!"})
        server.request("POST", "/api/persistent-worlds/nightcord/autonomy/start")
        time.sleep(2.0)
        server.request("POST", "/api/persistent-worlds/nightcord/autonomy/stop")
        self.assert_stopped_cleanly(server)
        output = server.output()
        self.assertNotIn(ADMIN_TOKEN, output)
        self.assertNotIn(KEY_CANARY, output)
        # 日志本身还得有用：不是靠把所有输出都抹掉换来的。
        self.assertIn("nightcord", output)


if __name__ == "__main__":
    unittest.main()
