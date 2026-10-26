# tests/test_provider_api.py — 面板改模型线路的接口（CFG-1 §5、§11.4、§11.6、§11.13）
#
# 守的线：
#   1. 从 env 第一次切到面板 token 必填；之后留空复用本线路已存的；解除跟随时
#      留空拷贝生成线路的；坏请求 400 且什么都没写；响应里没有 token；
#   2. 有世界开着 → 409 world_open 并列出世界，文件和快照都不变；全关时保存，
#      恢复世界后新建的 client 用的是新线路；
#   3. DELETE：.env 那一套无效时 400 且不删文件；有效时删文件、回到 env 派生；
#   4. 保存进行中恢复世界、恢复世界进行中保存，都是一方 409、另一方完整；
#   5. 隔离：PUT / DELETE / 建世界都 503，resolve 解开之后照常；
#   6. 写操作只有管理员能做：operator 403，break-glass bearer 可以。
#
# 运行: python -m unittest tests.test_provider_api -v
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from fastapi.testclient import TestClient  # noqa: E402

from pns.interfaces import provider_api  # noqa: E402
from pns.interfaces.app import create_app  # noqa: E402
from pns.interfaces.composition import AutonomySettings, WorldControlPlane  # noqa: E402
from pns.runtime import content_registry, provider_txn  # noqa: E402
from pns.runtime.autonomy.clock_worker import ClockConfig  # noqa: E402
from pns.runtime.autonomy.seeding import ActivationCadence  # noqa: E402
from pns.runtime.provider_settings import ACCOUNTS_FILE, PROVIDER_FILE  # noqa: E402
from pns.runtime.provider_switch import ProviderSwitchGate  # noqa: E402
from pns.runtime.reload import ConfigBoundary, SessionSupervisor  # noqa: E402
from tests.autonomy_support import clock_threads, wait_for  # noqa: E402
from tests.test_mvp_generation import FakeProvider  # noqa: E402
from tests.test_provider_settings import GEN_KEY, JUDGE_KEY, _accounts_file  # noqa: E402
from tests.test_provider_txn import _Faults  # noqa: E402

URL = "/api/settings/provider"
ENV_KEY = "sk-env-canary-00112233445566778899"
LEGACY = {"world_id": "nightcord", "scene": "nightcord", "characters": ["mizuki", "ena"]}


def _line(model, token, account="mimo-main", api_format="anthropic", url="https://gen.example/anthropic"):
    return {"account": account, "api_format": api_format, "base_url": url, "model": model, "token": token}


def _request(gen_model="gen-a", gen_token=GEN_KEY, judge=None):
    return {
        "generation": _line(gen_model, gen_token),
        "judge": judge if judge is not None else _line(
            "judge-a", JUDGE_KEY, account="ds", api_format="openai", url="https://judge.example/v1"
        ),
    }


class ProviderApiTestBase(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.addCleanup(self._restore_env)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.dir = self.tmp / "data" / "settings"
        self.dir.mkdir(parents=True)
        (self.dir / ACCOUNTS_FILE).write_text(json.dumps(_accounts_file()), encoding="utf-8")
        for target, value in ((content_registry, self.dir),):
            p = patch.object(target, "SETTINGS_DIR", value)
            p.start()
            self.addCleanup(p.stop)
        self.supervisor = SessionSupervisor()
        self.boundary = ConfigBoundary(self.supervisor, stop_timeout=1.0)
        # env 派生那一套要有 key：世界才建得起 client，DELETE 才有有效的默认。
        os.environ[self.boundary.active().models.env_key_name] = ENV_KEY
        p = patch.object(provider_api, "BOUNDARY", self.boundary)
        p.start()
        self.addCleanup(p.stop)
        self.built = []
        self.provider = FakeProvider()
        self.before = set(clock_threads())
        self.plane = WorldControlPlane(
            root=self.tmp / "worlds",
            registry_provider=self.boundary.active,
            client_factory=self.factory,
            switch_gate=ProviderSwitchGate(quarantined=lambda: self.boundary.quarantined),
            autonomy=AutonomySettings(
                clock=ClockConfig(rate=3600.0, interval_seconds=0.01, stop_timeout_seconds=2.0),
                cadence=ActivationCadence(), shutdown_timeout_seconds=1.0,
            ),
        )
        self.addCleanup(
            lambda: wait_for(lambda: not (set(clock_threads()) - self.before), what="worker 退出")
        )
        self.addCleanup(self.plane.service.release_all)
        self.client = TestClient(create_app(self.plane))

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)

    def factory(self, api_key, *, settings):
        self.built.append((settings.name, settings.model))
        return self.provider

    @property
    def file(self):
        return self.dir / PROVIDER_FILE

    def stored(self):
        return json.loads(self.file.read_text(encoding="utf-8"))

    def put(self, body):
        return self.client.put(URL, json=body)

    def assert_no_tokens(self, response):
        for key in (GEN_KEY, JUDGE_KEY, ENV_KEY):
            self.assertNotIn(key, response.text)


class SaveTests(ProviderApiTestBase):
    def test_the_first_switch_from_env_needs_tokens(self):
        for body in (_request(gen_token=""), _request(judge=_line("j", "", account="ds"))):
            with self.subTest(body=body["judge"].get("token")):
                response = self.put(body)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["detail"]["category"], "invalid_settings")
                self.assertFalse(self.file.exists())
        self.assertEqual(self.client.get(URL).json()["settings"]["source"], "env")

    def test_a_save_switches_the_snapshot_and_hides_tokens(self):
        response = self.put(_request())
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_no_tokens(response)
        settings = response.json()["settings"]
        self.assertEqual((settings["source"], settings["generator_model"], settings["evaluator_model"]),
                         ("panel", "gen-a", "judge-a"))
        self.assertTrue(settings["generation"]["has_token"])
        self.assertEqual(self.boundary.active().models.generator_model, "gen-a")
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.dir / provider_txn.TXN_FILE).exists())
        self.assertFalse((self.dir / provider_txn.PREV_FILE).exists())
        self.assertEqual(self.stored()["updated_by"], "svc-open-development")
        get = self.client.get(URL)
        self.assert_no_tokens(get)
        self.assertEqual(get.json()["settings"], settings)

    def test_empty_tokens_reuse_what_is_stored(self):
        self.assertEqual(self.put(_request()).status_code, 200)
        response = self.put(_request(gen_model="gen-b", gen_token="",
                                     judge=_line("judge-b", "", account="ds", api_format="openai",
                                                 url="https://judge.example/v1")))
        self.assertEqual(response.status_code, 200, response.text)
        stored = self.stored()
        self.assertEqual((stored["generation"]["token"], stored["judge"]["token"]), (GEN_KEY, JUDGE_KEY))
        self.assertEqual(stored["generation"]["model"], "gen-b")

    def test_unfollowing_with_an_empty_token_copies_the_generation_token(self):
        self.assertEqual(self.put(_request(judge={"follow": "generation"})).status_code, 200)
        self.assertEqual(self.stored()["judge"], {"follow": "generation"})
        self.assertIsNotNone(self.client.get(URL).json()["settings"]["judge"]["follow"])
        response = self.put(_request(gen_token="", judge=_line("judge-c", "", account="ds")))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.stored()["judge"]["token"], GEN_KEY)
        self.assertIsNone(response.json()["settings"]["judge"]["follow"])

    def test_invalid_requests_change_nothing(self):
        self.assertEqual(self.put(_request()).status_code, 200)
        before = self.file.read_bytes()
        snapshot = self.boundary.active()
        for name, body in (
            ("账户不存在", _request(judge=_line("j", JUDGE_KEY, account="nobody"))),
            ("http", {**_request(), "generation": _line("g", GEN_KEY, url="http://gen.example/x")}),
            ("URL 带凭据", {**_request(), "generation": _line("g", GEN_KEY, url=f"https://u:{GEN_KEY}@h/x")}),
            ("跟随别的", _request(judge={"follow": "judge"})),
            ("缺字段", {"generation": {"model": "g"}, "judge": {"follow": "generation"}}),
        ):
            with self.subTest(name):
                response = self.put(body)
                self.assertEqual(response.status_code, 400, response.text)
                self.assert_no_tokens(response)
                self.assertEqual(self.file.read_bytes(), before)
                self.assertIs(self.boundary.active(), snapshot)


class WorldInteractionTests(ProviderApiTestBase):
    def test_an_open_world_refuses_the_save(self):
        self.assertEqual(self.client.post("/api/persistent-worlds", json=LEGACY).status_code, 201)
        snapshot = self.boundary.active()
        for call in (lambda: self.put(_request()), lambda: self.client.delete(URL)):
            response = call()
            self.assertEqual(response.status_code, 409, response.text)
            detail = response.json()["detail"]
            self.assertEqual((detail["category"], detail["worlds"]), ("world_open", ["nightcord"]))
            self.assertFalse(self.file.exists())
            self.assertIs(self.boundary.active(), snapshot)
        self.assertIsNotNone(self.plane.service.opened("nightcord"))

    def test_a_restored_world_uses_the_new_line(self):
        self.assertEqual(self.client.post("/api/persistent-worlds", json=LEGACY).status_code, 201)
        self.assertEqual(self.client.post("/api/persistent-worlds/nightcord/close").status_code, 200)
        self.assertEqual(self.put(_request(gen_model="gen-new")).status_code, 200)
        self.built.clear()
        self.assertEqual(self.client.post("/api/persistent-worlds/nightcord/restore").status_code, 200)
        self.assertEqual(self.built, [("generation", "gen-new"), ("judge", "judge-a")])

    def test_save_and_restore_interleave_both_ways(self):
        self.assertEqual(self.client.post("/api/persistent-worlds", json=LEGACY).status_code, 201)
        self.assertEqual(self.client.post("/api/persistent-worlds/nightcord/close").status_code, 200)
        reached, release = threading.Event(), threading.Event()

        # 保存停在文件事务里时恢复世界：409，世界没开、保存照常完成。
        real_save = provider_txn.save

        def paused_save(*args, **kwargs):
            reached.set()
            release.wait(10)
            return real_save(*args, **kwargs)

        result = {}
        with patch.object(provider_txn, "save", paused_save):
            worker = threading.Thread(target=lambda: result.setdefault("put", self.put(_request())))
            worker.start()
            try:
                self.assertTrue(reached.wait(10))
                response = self.client.post("/api/persistent-worlds/nightcord/restore")
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["detail"]["category"], "provider_switch_in_progress")
                self.assertIsNone(self.plane.service.opened("nightcord"))
            finally:
                release.set()
                worker.join(10)
        self.assertEqual(result["put"].status_code, 200, result["put"].text)

        # 恢复世界停在建 adapters 时保存：409，什么都没写；恢复照常完成。
        reached.clear()
        release.clear()
        before = self.file.read_bytes()
        real_factory = self.factory

        def paused_factory(api_key, *, settings):
            reached.set()
            release.wait(10)
            return real_factory(api_key, settings=settings)

        with patch.object(self.plane, "_client_factory", paused_factory):
            worker = threading.Thread(
                target=lambda: result.setdefault("restore", self.client.post("/api/persistent-worlds/nightcord/restore"))
            )
            worker.start()
            try:
                self.assertTrue(reached.wait(10))
                response = self.put(_request(gen_model="gen-other"))
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["detail"]["category"], "provider_switch_in_progress")
                self.assertEqual(self.file.read_bytes(), before)
            finally:
                release.set()
                worker.join(10)
        self.assertEqual(result["restore"].status_code, 200, result["restore"].text)


class DeleteTests(ProviderApiTestBase):
    def test_an_invalid_env_default_keeps_the_file(self):
        self.assertEqual(self.put(_request()).status_code, 200)
        before = self.file.read_bytes()
        key_name = self.boundary.active().models.env_key_name
        # 协议不合法在派生时就拒；缺 key 派生得出来、构建也过得去（开发环境允许），
        # 只有有效线路校验拦得住。
        for name, change in (("协议不合法", ("API_FORMAT", "bogus")), ("缺 key", (key_name, ""))):
            with self.subTest(name), patch.dict(os.environ, dict([change])):
                response = self.client.delete(URL)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["detail"]["category"], "env_default_invalid")
                self.assertEqual(self.file.read_bytes(), before)

    def test_delete_returns_to_env(self):
        self.assertEqual(self.put(_request()).status_code, 200)
        response = self.client.delete(URL)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.file.exists())
        self.assertEqual(response.json()["settings"]["source"], "env")
        self.assertEqual(self.boundary.active().models.source, "env")
        # 账户目录不受影响（§4.3）。
        self.assertTrue((self.dir / ACCOUNTS_FILE).exists())


class QuarantineApiTests(ProviderApiTestBase):
    def quarantine(self):
        faults = _Faults()
        with faults.active():
            self.assertEqual(self.put(_request()).status_code, 200)
        index = faults.trace.index(("unlink", provider_txn.TXN_FILE))
        with _Faults(index, "not_published").active():
            response = self.put(_request(gen_model="gen-q"))
        self.assertEqual(response.status_code, 503, response.text)
        self.assertTrue(self.boundary.quarantined)

    def test_everything_waits_for_resolve(self):
        self.quarantine()
        for name, call in (
            ("PUT", lambda: self.put(_request())),
            ("DELETE", lambda: self.client.delete(URL)),
            ("建世界", lambda: self.client.post("/api/persistent-worlds", json=LEGACY)),
        ):
            with self.subTest(name):
                response = call()
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json()["detail"]["category"], "settings_quarantined")
        self.assertTrue(self.client.get(URL).json()["quarantined"])
        self.assertEqual(self.client.post(f"{URL}/resolve", json={"keep": "nonsense"}).status_code, 422)
        response = self.client.post(f"{URL}/resolve", json={"keep": "previous"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["quarantined"])
        self.assertEqual(response.json()["settings"]["generator_model"], "gen-a")
        self.assertEqual(self.client.post("/api/persistent-worlds", json=LEGACY).status_code, 201)

    def test_resolve_without_anything_to_resolve(self):
        response = self.client.post(f"{URL}/resolve", json={"keep": "current"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["category"], "nothing_to_resolve")


from tests.test_auth_api import ApiTestCase  # noqa: E402


class AdminOnlyTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        tmp = Path(self._tmp.name) / "data" / "settings"
        tmp.mkdir(parents=True)
        (tmp / ACCOUNTS_FILE).write_text(json.dumps(_accounts_file()), encoding="utf-8")
        for target, attr, value in (
            (content_registry, "SETTINGS_DIR", tmp),
            (provider_api, "BOUNDARY", ConfigBoundary(SessionSupervisor(), stop_timeout=1.0)),
        ):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def test_only_admins_write(self):
        operator = self.as_operator()
        for method, path, body in (
            ("PUT", URL, _request()),
            ("DELETE", URL, None),
            ("POST", f"{URL}/resolve", {"keep": "current"}),
        ):
            with self.subTest(f"{method} {path}"):
                self.assertEqual(operator.request(method, path, json=body).status_code, 403)
        self.assertEqual(operator.get(URL).status_code, 200)
        self.assertEqual(self.client.put(URL, json=_request(), headers=self.bearer).status_code, 200)


if __name__ == "__main__":
    unittest.main()
