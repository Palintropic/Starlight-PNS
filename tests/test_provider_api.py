# tests/test_provider_api.py — 面板改模型卡片的接口（CFG-1 §5、卡片补丁 §3 / §9、审查 §五）
#
# 守的线：
#   1. 新建卡片只保存、不启用；id 由服务端生成；新建 token 必填，编辑留空沿用
#      这张卡片已存的；坏请求 400 且什么都没写；响应里没有 token；
#   2. 切换只改 active；编辑生效中的卡片等于一次切换；切到 .env 保留卡片、
#      DELETE 清空卡片；.env 无效时切到它被拒；
#   3. 备用卡片：只过形状校验（http、退役账户都能存着），启用时才拒；它不挡别的
#      切换；删除时只被判分引用的卡片也算在用；
#   4. 有世界开着时**所有**卡片写操作都 409 world_open（新建、编辑备用、删除、切换、
#      清空），文件和快照都不变；保存与恢复世界两个方向交错，一方 409、另一方完整；
#   5. 顺手建账户的两步：卡片不合法一个都不建；账户没发布 → 什么都没变；账户
#      已发布未证实耐久 → 不写卡片、带账户 id，挂到它上面重试能成；账户成功、
#      卡片没写成 → card_failed + 账户 id；卡片写完进了隔离 → 隔离 + 账户 id；
#      并发和退役都被闸门挡在外面；成功后再提交一次不会复用同一个账户；
#   6. 账户退役只看实际引用：备用卡片挂着的账户可以退役，启用这张卡片时拒绝；
#   7. 坏掉的 provider.json 不会被当成空的接着写；
#   8. 密钥出口：未生效卡片里也放合成 key（含短 key），列表、reload 状态、错误、
#      日志里都没有它；
#   9. 隔离：所有写操作和建世界都 503，resolve 解开之后照常；
#  10. 写操作只有管理员能做：operator 403，break-glass bearer 可以。
#
# 运行: python -m unittest tests.test_provider_api -v
import json
import logging
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
from pns.runtime import content_registry, provider_settings, provider_txn  # noqa: E402
from pns.runtime.autonomy.clock_worker import ClockConfig  # noqa: E402
from pns.runtime.autonomy.seeding import ActivationCadence  # noqa: E402
from pns.runtime.provider_settings import ACCOUNTS_FILE, PROVIDER_FILE, ensure_env_account  # noqa: E402
from pns.runtime.provider_switch import ProviderSwitchGate  # noqa: E402
from pns.runtime.reload import ConfigBoundary, SessionSupervisor  # noqa: E402
from tests.autonomy_support import clock_threads, wait_for  # noqa: E402
from tests.test_mvp_generation import FakeProvider  # noqa: E402
from tests.test_provider_settings import GEN_KEY, JUDGE_KEY, _accounts_file  # noqa: E402
from tests.test_provider_txn import _Faults  # noqa: E402

URL = "/api/settings/provider"
CARDS = f"{URL}/cards"
ACTIVE = f"{URL}/active"
ACCOUNTS = "/api/settings/accounts"
ENV_KEY = "sk-env-canary-00112233445566778899"
SPARE_KEY = "sk-spare-canary-99887766554433221100"
SHORT_KEY = "sk-short-7f3e"
LEGACY = {"world_id": "nightcord", "scene": "nightcord", "characters": ["mizuki", "ena"]}
FOLLOW = {"follow": "generation"}
NEW_ACCOUNT = {"id": "nov", "name": "Novita", "kind": "novita", "currency": "USD", "multiplier": "1.1"}
ALL_KEYS = (GEN_KEY, JUDGE_KEY, ENV_KEY, SPARE_KEY, SHORT_KEY)


def _card(name="生成", model="gen-a", token=GEN_KEY, account="mimo-main", api_format="anthropic",
          url="https://gen.example/anthropic", **extra):
    return {"name": name, "account": account, "api_format": api_format, "base_url": url,
            "model": model, "token": token, **extra}


def _judge_card(model="judge-a", token=JUDGE_KEY, account="ds", **extra):
    return _card(name="判分", model=model, token=token, account=account, api_format="openai",
                 url="https://judge.example/v1", **extra)


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
        # env 派生那一套要有 key：世界才建得起 client，切回 .env 才是有效的。
        os.environ[self.boundary.active().models.env_key_name] = ENV_KEY
        for target in (provider_api,):
            p = patch.object(target, "BOUNDARY", self.boundary)
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
        self.built.append((settings.name, settings.model, settings.base_url, api_key, settings.card))
        return self.provider

    @property
    def file(self):
        return self.dir / PROVIDER_FILE

    def stored(self):
        return json.loads(self.file.read_text(encoding="utf-8"))

    def accounts_on_disk(self):
        return json.loads((self.dir / ACCOUNTS_FILE).read_text(encoding="utf-8"))["accounts"]

    def add(self, body, status=201):
        response = self.client.post(CARDS, json=body)
        self.assertEqual(response.status_code, status, response.text)
        return response.json().get("card")

    def activate(self, generation, judge=FOLLOW, status=200):
        response = self.client.put(ACTIVE, json={"generation": generation, "judge": judge})
        self.assertEqual(response.status_code, status, response.text)
        return response

    def two_cards(self):
        """生成一张、判分一张，并启用（v1 时代"保存两条线路"的等价物）。"""
        gen, judge = self.add(_card()), self.add(_judge_card())
        self.activate(gen, {"card": judge})
        return gen, judge

    def assert_no_tokens(self, response):
        for key in ALL_KEYS:
            self.assertNotIn(key, response.text)

    def assert_unchanged(self, before_bytes, snapshot):
        if before_bytes is None:
            self.assertFalse(self.file.exists())
        else:
            self.assertEqual(self.file.read_bytes(), before_bytes)
        self.assertIs(self.boundary.active(), snapshot)


class CardTests(ProviderApiTestBase):
    def test_a_new_card_is_saved_but_not_enabled(self):
        response = self.client.post(CARDS, json=_card())
        self.assertEqual(response.status_code, 201, response.text)
        self.assert_no_tokens(response)
        card_id = response.json()["card"]
        self.assertRegex(card_id, r"^c-[0-9a-f]{8}$")
        settings = response.json()["settings"]
        # 添加不是换线路：生效的仍是 env。
        self.assertEqual(settings["active"], {"generation": "env", "judge": FOLLOW})
        self.assertEqual((settings["generation"]["card"], settings["judge"]["card"]), ("env", "env"))
        (listed,) = settings["cards"]
        self.assertEqual((listed["id"], listed["model"], listed["has_token"], listed["token_hint"]),
                         (card_id, "gen-a", True, GEN_KEY[-4:]))
        self.assertNotIn("token", listed)
        self.assertTrue(settings["env_card"]["builtin"])
        self.assertEqual(self.stored()["cards"][card_id]["token"], GEN_KEY)
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)

    def test_a_new_card_needs_a_token(self):
        for token in ("", None):
            with self.subTest(token=token):
                response = self.client.post(CARDS, json=_card(token=token))
                self.assertEqual(response.status_code, 400, response.text)
                self.assertFalse(self.file.exists())

    def test_switching_switches_the_snapshot_and_hides_tokens(self):
        gen, judge = self.two_cards()
        response = self.client.get(URL)
        self.assert_no_tokens(response)
        settings = response.json()["settings"]
        self.assertEqual((settings["source"], settings["generator_model"], settings["evaluator_model"]),
                         ("panel", "gen-a", "judge-a"))
        self.assertEqual(settings["active"], {"generation": gen, "judge": {"card": judge}})
        self.assertEqual(self.boundary.active().models.generator_model, "gen-a")
        self.assertFalse((self.dir / provider_txn.TXN_FILE).exists())
        self.assertFalse((self.dir / provider_txn.PREV_FILE).exists())
        self.assertEqual(self.stored()["updated_by"], "svc-open-development")
        # 切换不碰卡片：卡片内容逐字节没变。
        cards = self.stored()["cards"]
        self.activate(judge, FOLLOW)
        self.assertEqual(self.stored()["cards"], cards)
        self.assertEqual(self.boundary.active().models.generation.card, judge)

    def test_an_empty_token_keeps_what_the_card_stored(self):
        gen, _ = self.two_cards()
        response = self.client.put(f"{CARDS}/{gen}", json=_card(model="gen-b", token=""))
        self.assertEqual(response.status_code, 200, response.text)
        stored = self.stored()["cards"][gen]
        self.assertEqual((stored["token"], stored["model"]), (GEN_KEY, "gen-b"))
        # 编辑生效中的卡片 = 一次切换。
        self.assertEqual(self.boundary.active().models.generator_model, "gen-b")

    def test_invalid_requests_change_nothing(self):
        gen, judge = self.two_cards()
        before, snapshot = self.file.read_bytes(), self.boundary.active()
        for name, call in (
            ("账户不存在", lambda: self.client.post(CARDS, json=_card(account="nobody"))),
            ("URL 带凭据", lambda: self.client.post(CARDS, json=_card(url=f"https://u:{GEN_KEY}@h/x"))),
            ("不是 http(s)", lambda: self.client.post(CARDS, json=_card(url="ftp://gen.example/x"))),
            ("缺字段", lambda: self.client.post(CARDS, json={"model": "g", "token": GEN_KEY})),
            ("多余字段", lambda: self.client.post(CARDS, json={**_card(), "id": "c-mine"})),
            ("协议", lambda: self.client.post(CARDS, json=_card(api_format=GEN_KEY))),
            ("判分 thinking", lambda: self.client.post(CARDS, json=_card(judge_thinking="maybe"))),
            ("启用不存在的卡片", lambda: self.client.put(ACTIVE, json={"generation": "c-nope", "judge": FOLLOW})),
            ("跟随还带模型", lambda: self.client.put(ACTIVE, json={"generation": gen, "judge": {**FOLLOW, "model": "x"}})),
            ("跟随别的", lambda: self.client.put(ACTIVE, json={"generation": gen, "judge": {"follow": "judge"}})),
            ("空覆盖模型", lambda: self.client.put(ACTIVE, json={"generation": gen, "judge": {"card": judge, "model": ""}})),
            ("编辑成 http", lambda: self.client.put(f"{CARDS}/{gen}", json=_card(url="http://gen.example/x"))),
        ):
            with self.subTest(name):
                response = call()
                self.assertEqual(response.status_code, 400, response.text)
                self.assert_no_tokens(response)
                self.assert_unchanged(before, snapshot)
        self.assertEqual(self.client.put(f"{CARDS}/c-nope", json=_card()).status_code, 404)
        self.assert_unchanged(before, snapshot)

    def test_a_spare_card_is_only_checked_for_shape(self):
        gen, judge = self.two_cards()
        # 非本机 http：存得下（形状合法），启用时被有效线路校验拦住。
        spare = self.add(_card(name="备用", url="http://spare.example/x", token=SPARE_KEY))
        before, snapshot = self.file.read_bytes(), self.boundary.active()
        response = self.activate(spare, status=400)
        self.assert_no_tokens(response)
        self.assert_unchanged(before, snapshot)
        # 它不挡别的切换。
        self.activate(judge, {"card": gen})
        self.assertEqual(self.boundary.active().models.generation.card, judge)

    def test_judge_models_expand_into_the_clients(self):
        """审查 F3：比较实际 client 收到的模型、地址和 key。"""
        os.environ.update(GENERATOR_MODEL="env-gen", EVALUATOR_MODEL="env-judge")
        self.assertEqual(self.client.post("/api/persistent-worlds", json=LEGACY).status_code, 201)
        self.assertEqual(self.client.post("/api/persistent-worlds/nightcord/close").status_code, 200)
        env_url = self.boundary.active().models.generation.base_url
        gen, judge = self.add(_card()), self.add(_judge_card())
        gen_url, judge_url = "https://gen.example/anthropic", "https://judge.example/v1"
        for name, active, expected in (
            ("env 跟随", ("env", FOLLOW),
             [("env-gen", env_url, ENV_KEY, "env"), ("env-judge", env_url, ENV_KEY, "env")]),
            ("卡片跟随", (gen, FOLLOW),
             [("gen-a", gen_url, GEN_KEY, gen), ("gen-a", gen_url, GEN_KEY, gen)]),
            ("判分指定 env", (gen, {"card": "env"}),
             [("gen-a", gen_url, GEN_KEY, gen), ("env-judge", env_url, ENV_KEY, "env")]),
            ("判分指定卡片", ("env", {"card": judge}),
             [("env-gen", env_url, ENV_KEY, "env"), ("judge-a", judge_url, JUDGE_KEY, judge)]),
            ("覆盖模型", (gen, {"card": gen, "model": "gen-small"}),
             [("gen-a", gen_url, GEN_KEY, gen), ("gen-small", gen_url, GEN_KEY, gen)]),
            ("env 覆盖", ("env", {"card": "env", "model": "env-small"}),
             [("env-gen", env_url, ENV_KEY, "env"), ("env-small", env_url, ENV_KEY, "env")]),
        ):
            with self.subTest(name):
                self.activate(*active)
                self.built.clear()
                self.assertEqual(self.client.post("/api/persistent-worlds/nightcord/restore").status_code, 200)
                self.assertEqual(self.client.post("/api/persistent-worlds/nightcord/close").status_code, 200)
                self.assertEqual(
                    self.built,
                    [("generation", *expected[0]), ("judge", *expected[1])],
                )

    def test_switching_to_env_keeps_cards_and_delete_clears_them(self):
        gen, _ = self.two_cards()
        cards = self.stored()["cards"]
        self.activate("env")
        self.assertEqual(self.stored()["active"], {"generation": "env", "judge": FOLLOW})
        self.assertEqual(self.stored()["cards"], cards)
        self.assertEqual(self.boundary.active().models.generation.card, "env")
        response = self.client.delete(URL)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.file.exists())
        self.assertEqual(response.json()["settings"]["source"], "env")
        self.assertEqual(response.json()["settings"]["cards"], [])
        # 账户目录不受影响。
        self.assertTrue((self.dir / ACCOUNTS_FILE).exists())

    def test_an_invalid_env_refuses_switching_to_it(self):
        gen, _ = self.two_cards()
        before, snapshot = self.file.read_bytes(), self.boundary.active()
        key_name = self.boundary.active().models.env_key_name
        # 协议不合法在派生时就拒；缺 key 派生得出来，只有有效线路校验拦得住。
        for name, change in (("协议不合法", ("API_FORMAT", "bogus")), ("缺 key", (key_name, ""))):
            with self.subTest(name), patch.dict(os.environ, dict([change])):
                for call in (lambda: self.client.put(ACTIVE, json={"generation": "env", "judge": FOLLOW}),
                             lambda: self.client.put(ACTIVE, json={"generation": gen, "judge": {"card": "env"}}),
                             lambda: self.client.delete(URL)):
                    response = call()
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(response.json()["detail"]["category"], "invalid_settings")
                    self.assert_unchanged(before, snapshot)
                # 没选 env 时坏掉的 env 不挡卡片的写操作。
                self.assertEqual(self.client.put(f"{CARDS}/{gen}", json=_card(model="gen-c", token="")).status_code, 200)
                before, snapshot = self.file.read_bytes(), self.boundary.active()

    def test_deleting_cards(self):
        gen, judge = self.two_cards()
        spare = self.add(_card(name="备用", token=SPARE_KEY))
        before, snapshot = self.file.read_bytes(), self.boundary.active()
        for name, card_id in (("生成在用", gen), ("只被判分引用", judge)):
            with self.subTest(name):
                response = self.client.delete(f"{CARDS}/{card_id}")
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["detail"]["category"], "card_in_use")
                self.assert_unchanged(before, snapshot)
        # 判分覆盖模型引用的也算在用。
        self.activate(gen, {"card": spare, "model": "x-small"})
        self.assertEqual(self.client.delete(f"{CARDS}/{spare}").status_code, 409)
        self.activate(gen, {"card": judge})
        self.assertEqual(self.client.delete(f"{CARDS}/env").status_code, 400)
        self.assertEqual(self.client.delete(f"{CARDS}/c-nope").status_code, 404)
        response = self.client.delete(f"{CARDS}/{spare}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(spare, self.stored()["cards"])
        self.assertNotIn(SPARE_KEY, self.file.read_text(encoding="utf-8"))

    def test_deleting_the_last_card_while_on_env_removes_the_file(self):
        card = self.add(_card())
        self.assertEqual(self.client.delete(f"{CARDS}/{card}").status_code, 200)
        self.assertFalse(self.file.exists())

    def test_a_broken_file_is_not_written_over(self):
        self.two_cards()
        snapshot = self.boundary.active()
        for name, text in (("不是 JSON", GEN_KEY + "{"), ("v1", json.dumps({"version": 1})),
                           ("卡片坏了", json.dumps({**self.stored(), "cards": {"c-x": {"token": GEN_KEY}}}))):
            with self.subTest(name):
                self.file.write_text(text, encoding="utf-8")
                for call in (lambda: self.client.post(CARDS, json=_card()),
                             lambda: self.client.put(ACTIVE, json={"generation": "env", "judge": FOLLOW}),
                             lambda: self.client.delete(URL)):
                    response = call()
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertEqual(response.json()["detail"]["category"], "provider_file_broken")
                    self.assert_no_tokens(response)
                    self.assertEqual(self.file.read_text(encoding="utf-8"), text)
                    self.assertIs(self.boundary.active(), snapshot)


class WorldInteractionTests(ProviderApiTestBase):
    def test_an_open_world_refuses_every_card_write(self):
        gen, judge = self.two_cards()
        spare = self.add(_card(name="备用", token=SPARE_KEY))
        self.assertEqual(self.client.post("/api/persistent-worlds", json=LEGACY).status_code, 201)
        before, snapshot = self.file.read_bytes(), self.boundary.active()
        for name, call in (
            ("新建", lambda: self.client.post(CARDS, json=_card())),
            ("新建并建账户", lambda: self.client.post(CARDS, json={**_card(), "create_account": {}})),
            ("编辑备用", lambda: self.client.put(f"{CARDS}/{spare}", json=_card(name="改名", token=""))),
            ("编辑生效", lambda: self.client.put(f"{CARDS}/{gen}", json=_card(model="x", token=""))),
            ("删除备用", lambda: self.client.delete(f"{CARDS}/{spare}")),
            ("切换", lambda: self.client.put(ACTIVE, json={"generation": spare, "judge": FOLLOW})),
            ("只改判分", lambda: self.client.put(ACTIVE, json={"generation": gen, "judge": {"card": spare}})),
            ("清空", lambda: self.client.delete(URL)),
        ):
            with self.subTest(name):
                response = call()
                self.assertEqual(response.status_code, 409, response.text)
                detail = response.json()["detail"]
                self.assertEqual((detail["category"], detail["worlds"]), ("world_open", ["nightcord"]))
                self.assert_unchanged(before, snapshot)
        self.assertFalse([a for a in self.accounts_on_disk() if a.startswith("c-")])
        self.assertIsNotNone(self.plane.service.opened("nightcord"))

    def test_a_restored_world_uses_the_new_card(self):
        self.assertEqual(self.client.post("/api/persistent-worlds", json=LEGACY).status_code, 201)
        self.assertEqual(self.client.post("/api/persistent-worlds/nightcord/close").status_code, 200)
        gen, judge = self.two_cards()
        self.built.clear()
        self.assertEqual(self.client.post("/api/persistent-worlds/nightcord/restore").status_code, 200)
        self.assertEqual([(b[0], b[1], b[4]) for b in self.built],
                         [("generation", "gen-a", gen), ("judge", "judge-a", judge)])

    def test_save_and_restore_interleave_both_ways(self):
        self.assertEqual(self.client.post("/api/persistent-worlds", json=LEGACY).status_code, 201)
        self.assertEqual(self.client.post("/api/persistent-worlds/nightcord/close").status_code, 200)
        gen = self.add(_card())
        reached, release = threading.Event(), threading.Event()

        # 切换停在文件事务里时恢复世界：409，世界没开、切换照常完成。
        real_save = provider_txn.save

        def paused_save(*args, **kwargs):
            reached.set()
            release.wait(10)
            return real_save(*args, **kwargs)

        result = {}
        with patch.object(provider_txn, "save", paused_save):
            worker = threading.Thread(target=lambda: result.setdefault(
                "put", self.client.put(ACTIVE, json={"generation": gen, "judge": FOLLOW})))
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

        # 恢复世界停在建 adapters 时改卡片：409，什么都没写；恢复照常完成。
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
                for call in (lambda: self.client.post(CARDS, json=_card()),
                             lambda: self.client.put(f"{CARDS}/{gen}", json=_card(model="gen-other", token=""))):
                    response = call()
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertEqual(response.json()["detail"]["category"], "provider_switch_in_progress")
                    self.assertEqual(self.file.read_bytes(), before)
            finally:
                release.set()
                worker.join(10)
        self.assertEqual(result["restore"].status_code, 200, result["restore"].text)


class CreateWithAccountTests(ProviderApiTestBase):
    """新建卡片顺手建同 id 的账户（审查 F6）。"""

    def body(self, **account):
        return {**_card(name="DeepSeek"), "create_account": {"kind": "deepseek", **account}}

    def test_the_account_shares_the_card_id(self):
        response = self.client.post(CARDS, json=self.body(currency="USD", multiplier="0.8"))
        self.assertEqual(response.status_code, 201, response.text)
        card_id = response.json()["card"]
        self.assertEqual(response.json()["account"], card_id)
        account = self.accounts_on_disk()[card_id]
        self.assertEqual((account["name"], account["kind"], account["currency"], account["multiplier"]),
                         ("DeepSeek", "deepseek", "USD", "0.8"))
        self.assertEqual(self.stored()["cards"][card_id]["account"], card_id)
        # 成功后再提交一次：新的卡片、新的账户，不复用上一个。
        again = self.client.post(CARDS, json=self.body())
        self.assertEqual(again.status_code, 201, again.text)
        self.assertNotEqual(again.json()["card"], card_id)
        self.assertEqual(len(self.stored()["cards"]), 2)

    def test_an_invalid_card_creates_no_account(self):
        before = self.accounts_on_disk()
        for name, body in (
            ("卡片坏了", {**self.body(), "base_url": f"https://u:{GEN_KEY}@h/x"}),
            ("没有 token", {**self.body(), "token": ""}),
            ("账户坏了", self.body(currency="JPY")),
            ("倍率坏了", self.body(multiplier="0")),
            ("kind 是 env", self.body(kind="env")),
        ):
            with self.subTest(name):
                response = self.client.post(CARDS, json=body)
                self.assertEqual(response.status_code, 400, response.text)
                self.assert_no_tokens(response)
                self.assertEqual(self.accounts_on_disk(), before)
                self.assertFalse(self.file.exists())

    def test_an_account_that_was_not_published_changes_nothing(self):
        before = self.accounts_on_disk()
        with _Faults(0, "not_published").active():
            response = self.client.post(CARDS, json=self.body())
        self.assertEqual((response.status_code, response.json()["detail"]["category"]), (500, "not_written"))
        self.assertEqual(self.accounts_on_disk(), before)
        self.assertFalse(self.file.exists())

    def test_an_account_not_proven_durable_stops_before_the_card(self):
        with _Faults(0, "not_durable").active():
            response = self.client.post(CARDS, json=self.body())
        detail = response.json()["detail"]
        self.assertEqual((response.status_code, detail["category"]), (500, "account_not_durable"))
        account = detail["account"]
        self.assertIn(account, self.accounts_on_disk())
        self.assertFalse(self.file.exists())
        # 重试：明确挂到这个账户上，不再建账户。
        card = self.add(_card(name="DeepSeek", account=account))
        self.assertEqual(self.stored()["cards"][card]["account"], account)
        self.assertEqual(len(self.accounts_on_disk()), 3)

    def test_an_account_whose_card_failed_is_kept_and_named(self):
        faults = _Faults()
        with faults.active():
            self.add(self.body())
        # 账户写盘是第 0 次调用；provider.json 的发布在它之后。
        publish = faults.trace.index(("write", PROVIDER_FILE))
        self.client.delete(URL)
        accounts_before = set(self.accounts_on_disk())
        with _Faults(publish, "not_published").active():
            response = self.client.post(CARDS, json=self.body())
        detail = response.json()["detail"]
        self.assertEqual((response.status_code, detail["category"], detail["cause"]), (500, "card_failed", "not_written"))
        account = detail["account"]
        self.assertEqual(set(self.accounts_on_disk()) - accounts_before, {account})
        self.assertFalse(self.file.exists())
        self.assertNotIn("retired_at", self.accounts_on_disk()[account])

    def test_a_card_that_lands_in_quarantine_names_its_account(self):
        faults = _Faults()
        with faults.active():
            self.add(self.body())
        clear = faults.trace.index(("unlink", provider_txn.TXN_FILE))
        with _Faults(clear, "not_published").active():
            response = self.client.post(CARDS, json=self.body())
        detail = response.json()["detail"]
        self.assertEqual((response.status_code, detail["category"]), (503, "settings_quarantined"))
        self.assertIn(detail["account"], self.accounts_on_disk())
        self.assertTrue(self.boundary.quarantined)

    def test_concurrent_writes_and_retiring_wait_for_the_gate(self):
        reached, release = threading.Event(), threading.Event()
        real_create = provider_settings.create_account

        def paused_create(*args, **kwargs):
            reached.set()
            release.wait(10)
            return real_create(*args, **kwargs)

        result = {}
        with patch.object(provider_api, "create_account", paused_create):
            worker = threading.Thread(target=lambda: result.setdefault("post", self.client.post(CARDS, json=self.body())))
            worker.start()
            try:
                self.assertTrue(reached.wait(10))
                for name, call in (("另一次新建", lambda: self.client.post(CARDS, json=self.body())),
                                   ("退役", lambda: self.client.post(f"{ACCOUNTS}/ds/retire")),
                                   ("建账户", lambda: self.client.post(ACCOUNTS, json=NEW_ACCOUNT))):
                    with self.subTest(name):
                        response = call()
                        self.assertEqual(response.status_code, 409, response.text)
                        self.assertEqual(response.json()["detail"]["category"], "provider_switch_in_progress")
            finally:
                release.set()
                worker.join(10)
        self.assertEqual(result["post"].status_code, 201, result["post"].text)
        self.assertEqual(len(self.stored()["cards"]), 1)
        self.assertNotIn("nov", self.accounts_on_disk())


class AccountApiTests(ProviderApiTestBase):
    def test_create_then_reference_it(self):
        response = self.client.post(ACCOUNTS, json=NEW_ACCOUNT)
        self.assertEqual(response.status_code, 201, response.text)
        created = self.accounts_on_disk()["nov"]
        self.assertEqual((created["kind"], created["currency"], created["balance"]), ("novita", "USD", "manual"))
        self.assertTrue(created["created_at"])
        # 还没落过盘的内建 env 不会被顺手写进文件。
        self.assertNotIn("env", self.accounts_on_disk())
        self.assertIn("nov", self.client.get(ACCOUNTS).json()["accounts"])
        gen = self.add(_card())
        judge = self.add(_judge_card(account="nov"))
        self.activate(gen, {"card": judge})
        self.assertEqual(self.boundary.active().models.judge.provider, "novita")

    def test_bad_creates_write_nothing(self):
        self.assertEqual(self.client.post(ACCOUNTS, json=NEW_ACCOUNT).status_code, 201)
        before = (self.dir / ACCOUNTS_FILE).read_bytes()
        for name, body in (
            ("重复 id", NEW_ACCOUNT),
            ("保留 id", {**NEW_ACCOUNT, "id": "env"}),
            ("kind 是 env", {**NEW_ACCOUNT, "id": "x1", "kind": "env"}),
            ("币种不支持", {**NEW_ACCOUNT, "id": "x2", "currency": "JPY"}),
            ("倍率不是正数", {**NEW_ACCOUNT, "id": "x3", "multiplier": "0"}),
            ("id 非法", {**NEW_ACCOUNT, "id": "带空格 的"}),
        ):
            with self.subTest(name):
                response = self.client.post(ACCOUNTS, json=body)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual((self.dir / ACCOUNTS_FILE).read_bytes(), before)

    def test_only_name_and_multiplier_change(self):
        self.assertEqual(self.client.post(ACCOUNTS, json=NEW_ACCOUNT).status_code, 201)
        created_at = self.accounts_on_disk()["nov"]["created_at"]
        response = self.client.post(f"{ACCOUNTS}/nov", json={"name": "N2", "multiplier": "2"})
        self.assertEqual(response.status_code, 200, response.text)
        for field, value in (("currency", "CNY"), ("kind", "openrouter"), ("created_at", "x")):
            with self.subTest(field):
                response = self.client.post(f"{ACCOUNTS}/nov", json={field: value})
                self.assertEqual(response.status_code, 400, response.text)
        # 带着原值提交不算修改。
        self.assertEqual(self.client.post(f"{ACCOUNTS}/nov", json={"currency": "USD"}).status_code, 200)
        stored = self.accounts_on_disk()["nov"]
        self.assertEqual((stored["name"], stored["multiplier"], stored["currency"], stored["created_at"]),
                         ("N2", "2", "USD", created_at))
        self.assertEqual(self.client.post(f"{ACCOUNTS}/nobody", json={"name": "x"}).status_code, 404)

    def test_retiring_only_counts_cards_in_use(self):
        gen, judge = self.two_cards()
        for account in ("mimo-main", "ds"):
            with self.subTest(in_use=account):
                response = self.client.post(f"{ACCOUNTS}/{account}/retire")
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["detail"]["category"], "account_in_use")
        # 判分改跟随之后，ds 只被备用卡片挂着：可以退役（审查 F8）。
        self.activate(gen, FOLLOW)
        self.assertEqual(self.client.post(f"{ACCOUNTS}/ds/retire").status_code, 200)
        self.assertTrue(self.accounts_on_disk()["ds"]["retired_at"])
        # 备用卡片留着退役账户照样能列出、编辑（账户不变）、删除；启用它被拒。
        listed = {c["id"]: c for c in self.client.get(URL).json()["settings"]["cards"]}
        self.assertEqual(listed[judge]["account"], "ds")
        self.assertEqual(self.client.put(f"{CARDS}/{judge}", json=_judge_card(model="j2", token="")).status_code, 200)
        before, snapshot = self.file.read_bytes(), self.boundary.active()
        for active in ((judge, FOLLOW), (gen, {"card": judge})):
            with self.subTest(activate=active):
                response = self.activate(*active, status=400)
                self.assertIn("退役", response.json()["detail"]["message"])
                self.assert_unchanged(before, snapshot)
        # 新建或改挂到退役账户：拒绝。
        self.assertEqual(self.client.post(CARDS, json=_judge_card()).status_code, 400)
        self.assertEqual(self.client.put(f"{CARDS}/{gen}", json=_card(account="ds", token="")).status_code, 400)
        self.assertEqual(self.client.delete(f"{CARDS}/{judge}").status_code, 200)
        # 退役的不能再退、不能改、id 不复用。
        self.assertEqual(self.client.post(f"{ACCOUNTS}/ds/retire").status_code, 400)
        self.assertEqual(self.client.post(f"{ACCOUNTS}/ds", json={"name": "x"}).status_code, 400)
        self.assertEqual(self.client.post(ACCOUNTS, json={**NEW_ACCOUNT, "id": "ds"}).status_code, 400)

    def test_retiring_sees_the_snapshot_too(self):
        # 没有面板文件时线路来自 env：内建 env 不在文件里 → 404；落过盘之后，它被
        # 生效快照的线路引用（磁盘上没有 provider.json 可看）→ 409。
        self.assertEqual(self.client.post(f"{ACCOUNTS}/env/retire").status_code, 404)
        ensure_env_account(self.dir)
        response = self.client.post(f"{ACCOUNTS}/env/retire")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["category"], "account_in_use")

    def test_retiring_refuses_when_the_card_file_is_broken(self):
        self.two_cards()
        self.file.write_text("{", encoding="utf-8")
        self.assertEqual(self.client.post(ACCOUNTS, json=NEW_ACCOUNT).status_code, 201)
        response = self.client.post(f"{ACCOUNTS}/nov/retire")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertNotIn("retired_at", self.accounts_on_disk()["nov"])

    def test_accounts_survive_clearing_the_cards(self):
        self.assertEqual(self.client.post(ACCOUNTS, json=NEW_ACCOUNT).status_code, 201)
        before = self.accounts_on_disk()
        self.two_cards()
        self.assertEqual(self.client.delete(URL).status_code, 200)
        self.two_cards()
        self.assertEqual(self.accounts_on_disk(), before)

    def test_a_failed_account_write_is_reported_as_such(self):
        for fault, status, category in (("not_published", 500, "not_written"),
                                        ("not_durable", 500, "not_durable")):
            with self.subTest(fault):
                with _Faults(0, fault).active():
                    response = self.client.post(ACCOUNTS, json={**NEW_ACCOUNT, "id": f"x-{fault}"})
                self.assertEqual((response.status_code, response.json()["detail"]["category"]),
                                 (status, category))
        # 没写成的那个不存在；耐久证实不了的那个已经在文件里，身份照样不复用。
        self.assertNotIn("x-not_published", self.accounts_on_disk())
        self.assertIn("x-not_durable", self.accounts_on_disk())
        self.assertEqual(self.client.post(ACCOUNTS, json={**NEW_ACCOUNT, "id": "x-not_durable"}).status_code, 400)


class SecretEgressTests(ProviderApiTestBase):
    """审查 §五 第 6 条：未生效卡片里也放合成 key，含一把短 key。"""

    def test_spare_card_keys_stay_inside(self):
        gen, judge = self.two_cards()
        spare = self.add(_card(name="备用", token=SPARE_KEY))
        short = self.add(_card(name="短", token=SHORT_KEY))
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("cfg1-test").debug("start")
            responses = [
                self.client.get(URL),
                self.client.get("/api/config/reload"),
                self.client.get("/api/config"),
                self.client.put(f"{CARDS}/{spare}", json=_card(name="备用", url=f"https://u:{SPARE_KEY}@h/x", token="")),
                self.client.put(f"{CARDS}/{short}", json=_card(name="短", api_format=SHORT_KEY, token="")),
                self.client.put(ACTIVE, json={"generation": SPARE_KEY, "judge": FOLLOW}),
                self.client.delete(f"{CARDS}/c-nope"),
                self.client.put(f"{CARDS}/{spare}", json=_card(name="备用", model="m2", token="")),
                self.client.post(f"{URL}/resolve", json={"keep": "current"}),
            ]
        for response in responses:
            with self.subTest(response.request.url.path, status=response.status_code):
                self.assert_no_tokens(response)
        listed = {c["id"]: c for c in responses[0].json()["settings"]["cards"]}
        self.assertEqual(listed[spare]["token_hint"], SPARE_KEY[-4:])
        # 短 key 连末 4 位都不给。
        self.assertEqual((listed[short]["has_token"], listed[short]["token_hint"]), (True, None))
        text = "\n".join(logs.output)
        for key in ALL_KEYS:
            self.assertNotIn(key, text)
        self.assertNotIn(SPARE_KEY, json.dumps(self.boundary.status(), ensure_ascii=False, default=str))
        self.assertEqual(self.stored()["cards"][spare]["token"], SPARE_KEY)


class QuarantineApiTests(ProviderApiTestBase):
    def quarantine(self):
        faults = _Faults()
        with faults.active():
            self.gen, self.judge = self.two_cards()
        index = faults.trace.index(("unlink", provider_txn.TXN_FILE))
        with _Faults(index, "not_published").active():
            response = self.client.put(f"{CARDS}/{self.gen}", json=_card(model="gen-q", token=""))
        self.assertEqual(response.status_code, 503, response.text)
        self.assertTrue(self.boundary.quarantined)

    def test_everything_waits_for_resolve(self):
        self.quarantine()
        for name, call in (
            ("新建", lambda: self.client.post(CARDS, json=_card())),
            ("编辑", lambda: self.client.put(f"{CARDS}/{self.gen}", json=_card(token=""))),
            ("删除", lambda: self.client.delete(f"{CARDS}/{self.judge}")),
            ("切换", lambda: self.client.put(ACTIVE, json={"generation": "env", "judge": FOLLOW})),
            ("清空", lambda: self.client.delete(URL)),
            ("建世界", lambda: self.client.post("/api/persistent-worlds", json=LEGACY)),
            ("建账户", lambda: self.client.post(ACCOUNTS, json=NEW_ACCOUNT)),
            ("退役账户", lambda: self.client.post(f"{ACCOUNTS}/ds/retire")),
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
            ("POST", CARDS, _card()),
            ("PUT", f"{CARDS}/c-00000000", _card()),
            ("DELETE", f"{CARDS}/c-00000000", None),
            ("PUT", ACTIVE, {"generation": "env", "judge": FOLLOW}),
            ("DELETE", URL, None),
            ("POST", f"{URL}/resolve", {"keep": "current"}),
            ("POST", ACCOUNTS, NEW_ACCOUNT),
            ("POST", f"{ACCOUNTS}/ds", {"name": "x"}),
            ("POST", f"{ACCOUNTS}/ds/retire", None),
        ):
            with self.subTest(f"{method} {path}"):
                self.assertEqual(operator.request(method, path, json=body).status_code, 403)
        self.assertEqual(operator.get(URL).status_code, 200)
        self.assertEqual(operator.get(ACCOUNTS).status_code, 200)
        self.assertEqual(self.client.post(CARDS, json=_card(), headers=self.bearer).status_code, 201)


if __name__ == "__main__":
    unittest.main()
