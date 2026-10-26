# tests/test_provider_settings.py — 模型线路、账户与密钥边界（CFG-1 底座）
#
# 守的线：
#   1. Secret 不肯被打印：repr / str / format / f-string / json / pickle / vars /
#      FastAPI 的 jsonable_encoder 都拿不到明文；短 token 连末 4 位都不给；
#   2. env 派生与改动前逐字段一致：GENERATOR_MODEL / EVALUATOR_MODEL 各管各的、
#      判分是独立线路不是跟随、key 用到时才从进程环境取；
#   3. 面板文件存在就以它为准；坏文件、版本不认识、缺字段、http、带凭据的 URL、
#      不存在 / 已退役的账户、空 token 都让这份配置不通过，而错误消息里没有 token；
#   4. 账户目录：没有文件时只有内建 env；env 落盘幂等、不改 created_at；0600；
#   5. 写盘合同三档：rename 前失败目标不变；目录同步失败报"已发布未耐久"；
#   6. 两条线路真的分开走：生成走 A（anthropic）、判分走 B（openai），各自的
#      地址、key、模型、账本协议都对；研究会话也是两个 client；
#   7. 生产启动：面板两条线路 token 齐全时不再要求 env 的旧 key；
#   8. 真 SDK 打两个回环假服务器，经 SessionRuntime.create()/run() 跑完一轮：
#      每台服务器只收到自己那条线路的 key 和模型，协议对调也一样，消息和落盘
#      文件里没有 key；上游报错 / 非 JSON 响应时，key 也不进消息、落盘文件、
#      stdout、stderr 和日志；建 client 失败时，研究会话抛的异常、世界的 HTTP
#      响应、stdout、stderr、日志都只有固定文案；跟随时判分真的判完；
#   9. 世界那一半同理：经 HTTP 建世界、开认知，生成走 A、判分走 B，账本两行协议
#      各自对；上游回显 key 时状态接口、存档、账本、输出里都没有它；两个世界
#      共用一本账同时跑、生成与判分请求重叠时，每次响应的 usage（两台各不相同、
#      每次也不同）恰好记成一行，记在发出那个请求的线路和角色名下。
#
# 运行: python -m unittest tests.test_provider_settings -v
import asyncio
import contextlib
import copy
import io
import json
import logging
import os
import pickle
import re
import stat
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from fastapi.encoders import jsonable_encoder  # noqa: E402

from pns.interfaces.app import _verify_production_config  # noqa: E402
from pns.interfaces.composition import AdaptersUnavailable, WorldControlPlane  # noqa: E402
from pns.interfaces.security import DeploymentSettings  # noqa: E402
from pns.logic.router import DIMENSION_KEYS  # noqa: E402
from pns.models.action import ActionId  # noqa: E402
from pns.runtime import content_registry  # noqa: E402
from pns.runtime import settings_files  # noqa: E402
from pns.runtime import usage_ledger as ul  # noqa: E402
from pns.runtime.autonomy.audit import AuditRequest  # noqa: E402
from pns.runtime.content_registry import ConfigValidationError, build_content_registry  # noqa: E402
from pns.runtime.provider_settings import (  # noqa: E402
    ACCOUNTS_FILE,
    ENV_ACCOUNT,
    LINE_GENERATION,
    LINE_JUDGE,
    PROVIDER_FILE,
    SOURCE_ENV,
    SOURCE_PANEL,
    AccountCatalog,
    EnvSecret,
    Line,
    ModelSettings,
    ProviderSettingsError,
    Secret,
    ensure_env_account,
    env_account,
    load_accounts,
    load_model_settings,
)
from pns.runtime.reload import BOUNDARY, ConfigBoundary, SessionSupervisor  # noqa: E402
from pns.runtime.session_runtime import SessionRuntime, SessionSetupError  # noqa: E402

GEN_KEY = "sk-gen-canary-0123456789abcdef"
JUDGE_KEY = "sk-judge-canary-fedcba9876543210"
ADMIN_TOKEN = "CFG1-ADMIN-CANARY-a1b2c3d4e5f60718293a4b5c6d7e8f90"

VERDICT = json.dumps(
    {
        "drift_score": 1, "confidence": 0.9, "drift_type": "无", "reason": "ok",
        "dimensions": {key: {"score": 1, "reason": "像"} for key in DIMENSION_KEYS},
    },
    ensure_ascii=False,
)


def _panel_file(**overrides):
    payload = {
        "version": 1,
        "generation": {
            "account": "mimo-main", "api_format": "anthropic",
            "base_url": "https://gen.example/anthropic", "model": "gen-model",
            "token": GEN_KEY,
        },
        "judge": {
            "account": "ds", "api_format": "openai",
            "base_url": "https://judge.example/v1", "model": "judge-model",
            "token": JUDGE_KEY,
        },
        "updated_at": "2026-10-25T00:00:00+00:00",
        "updated_by": "admin",
    }
    payload.update(overrides)
    return payload


def _accounts_file(**extra):
    accounts = {
        "mimo-main": {
            "name": "MiMo 主账号", "kind": "mimo", "balance": "manual",
            "currency": "CNY", "multiplier": "1", "created_at": "2026-10-25T00:00:00+00:00",
        },
        "ds": {
            "name": "DeepSeek", "kind": "deepseek", "balance": "manual",
            "currency": "CNY", "multiplier": "1", "created_at": "2026-10-25T00:00:00+00:00",
        },
    }
    accounts.update(extra)
    return {"version": 1, "accounts": accounts}


class _SettingsDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "settings"
        self.dir.mkdir()

    def write(self, name, payload):
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        (self.dir / name).write_text(text, encoding="utf-8")

    def load(self, env=None):
        return load_model_settings(self.dir, env or {}, ooc_threshold=5.0)


# ── 1. Secret ───────────────────────────────────────────────────────────
class SecretTests(unittest.TestCase):
    def test_no_printing_path_reveals_the_value(self):
        secret = Secret(GEN_KEY)
        line = Line(
            name=LINE_GENERATION, account="a", api_format="anthropic",
            base_url="https://x.example", model="m", token=secret,
        )
        for text in (
            repr(secret), str(secret), f"{secret}", "{}".format(secret),
            "%s" % secret, repr(line), str(line),
            json.dumps({"t": secret}, default=str),
        ):
            self.assertNotIn(GEN_KEY, text)
        self.assertEqual(secret.reveal(), GEN_KEY)

    def test_it_cannot_be_serialized_or_introspected(self):
        secret = Secret(GEN_KEY)
        with self.assertRaises(TypeError):
            pickle.dumps(secret)
        with self.assertRaises(TypeError) as caught:
            json.dumps({"t": secret})
        self.assertNotIn(GEN_KEY, str(caught.exception))
        with self.assertRaises(TypeError):
            vars(secret)
        with self.assertRaises(TypeError):
            hash(secret)
        self.assertIs(copy.deepcopy(secret), secret)

    def test_fastapi_encoding_does_not_leak(self):
        try:
            encoded = jsonable_encoder({"t": Secret(GEN_KEY)})
        except Exception as e:  # 拒绝编码也行，只要原文不出来
            self.assertNotIn(GEN_KEY, str(e))
        else:
            self.assertNotIn(GEN_KEY, json.dumps(encoded, default=str))

    def test_hint_only_for_long_tokens(self):
        self.assertEqual(Secret("x" * 15 + "ABCD").hint(), "ABCD")
        self.assertIsNone(Secret("short-ABCD").hint())
        self.assertIsNone(Secret("x" * 12 + "ABC").hint())

    def test_env_secret_reads_the_environment_when_used(self):
        secret = EnvSecret("CFG1_TEST_KEY")
        with patch.dict(os.environ, {"CFG1_TEST_KEY": ""}):
            self.assertFalse(secret)
        with patch.dict(os.environ, {"CFG1_TEST_KEY": GEN_KEY}):
            self.assertEqual(secret.reveal(), GEN_KEY)
            self.assertNotIn(GEN_KEY, repr(secret))


# ── 2. env 派生 ─────────────────────────────────────────────────────────
class EnvDerivationTests(_SettingsDir):
    def test_separate_models_stay_separate_lines(self):
        settings, _ = self.load({"MODEL": "base", "GENERATOR_MODEL": "A", "EVALUATOR_MODEL": "B"})
        self.assertEqual(settings.source, SOURCE_ENV)
        self.assertFalse(settings.judge_follows)
        self.assertEqual((settings.generator_model, settings.evaluator_model), ("A", "B"))
        self.assertEqual(
            (settings.generation.account, settings.judge.account), (ENV_ACCOUNT, ENV_ACCOUNT)
        )

    def test_only_model_feeds_both(self):
        settings, _ = self.load({"MODEL": "only"})
        self.assertEqual((settings.generator_model, settings.evaluator_model), ("only", "only"))
        self.assertFalse(settings.judge_follows)

    def test_defaults_match_the_previous_settings(self):
        settings, _ = self.load({})
        for line in (settings.generation, settings.judge):
            self.assertEqual(line.api_format, "anthropic")
            self.assertEqual(line.base_url, "https://api.xiaomimimo.com/anthropic")
            self.assertEqual(line.model, "mimo-v2.6-pro")
        self.assertEqual(settings.env_key_name, "MIMO_API_KEY")

    def test_every_field_comes_from_the_one_env_set(self):
        env = {
            "API_FORMAT": "openai", "BASE_URL": "https://e.example/v1",
            "PNS_API_KEY_NAME": "CFG1_ENV_KEY", "PROVIDER": "acme", "MODEL": "m",
        }
        settings, _ = self.load(env)
        for line in (settings.generation, settings.judge):
            self.assertEqual(
                (line.api_format, line.base_url, line.provider), ("openai", "https://e.example/v1", "acme")
            )
        with patch.dict(os.environ, {"CFG1_ENV_KEY": GEN_KEY}):
            self.assertEqual(settings.generation.token.reveal(), GEN_KEY)
            self.assertEqual(settings.judge.token.reveal(), GEN_KEY)

    def test_bad_api_format_is_refused(self):
        # 没有面板文件时 env 照旧严格校验（面板存在时不校验，见 PanelFileTests）。
        for env in ({"API_FORMAT": "grpc"}, {"BASE_URL": ""}, {"PNS_API_KEY_NAME": ""}):
            with self.subTest(env=env):
                with self.assertRaises(ProviderSettingsError):
                    self.load(env)


# ── 3. 面板文件 ─────────────────────────────────────────────────────────
class PanelFileTests(_SettingsDir):
    def setUp(self):
        super().setUp()
        self.write(ACCOUNTS_FILE, _accounts_file())

    def test_an_independent_judge_line(self):
        self.write(PROVIDER_FILE, _panel_file())
        settings, catalog = self.load({"MODEL": "ignored"})
        self.assertEqual(settings.source, SOURCE_PANEL)
        self.assertFalse(settings.judge_follows)
        gen, judge = settings.generation, settings.judge
        self.assertEqual((gen.api_format, gen.model, gen.account, gen.host), ("anthropic", "gen-model", "mimo-main", "gen.example"))
        self.assertEqual((judge.api_format, judge.model, judge.account), ("openai", "judge-model", "ds"))
        self.assertEqual((gen.token.reveal(), judge.token.reveal()), (GEN_KEY, JUDGE_KEY))
        self.assertEqual((gen.provider, judge.provider), ("mimo", "deepseek"))
        self.assertIn("ds", catalog.accounts)

    def test_a_following_judge_is_distinguishable_from_an_equal_one(self):
        self.write(PROVIDER_FILE, _panel_file(judge={"follow": "generation"}))
        follows, _ = self.load()
        self.assertTrue(follows.judge_follows)
        self.assertEqual(follows.judge.token.reveal(), GEN_KEY)
        self.assertEqual(follows.judge.name, LINE_JUDGE)
        equal = dict(_panel_file()["generation"])
        self.write(PROVIDER_FILE, _panel_file(judge=equal))
        independent, _ = self.load()
        self.assertFalse(independent.judge_follows)
        self.assertEqual(follows.public_dict()["judge"]["follow"], "generation")
        self.assertIsNone(independent.public_dict()["judge"]["follow"])

    def test_the_public_projection_carries_no_token(self):
        self.write(PROVIDER_FILE, _panel_file())
        settings, _ = self.load()
        text = json.dumps(settings.public_dict(), ensure_ascii=False)
        self.assertNotIn(GEN_KEY, text)
        self.assertNotIn(JUDGE_KEY, text)
        self.assertEqual(settings.public_dict()["generation"]["token_hint"], GEN_KEY[-4:])
        self.assertTrue(settings.public_dict()["judge"]["has_token"])

    def test_broken_files_refuse_without_echoing_the_content(self):
        cases = {
            "not json": GEN_KEY + "{",
            "not an object": json.dumps([GEN_KEY]),
            "unknown version": _panel_file(version=2),
            "missing line": _panel_file(generation=None),
            "missing token": _panel_file(generation={**_panel_file()["generation"], "token": None}),
            "empty token": _panel_file(generation={**_panel_file()["generation"], "token": ""}),
            "empty model": _panel_file(judge={**_panel_file()["judge"], "model": " "}),
            "bad format": _panel_file(judge={**_panel_file()["judge"], "api_format": GEN_KEY}),
            "plain http": _panel_file(judge={**_panel_file()["judge"], "base_url": "http://judge.example/v1"}),
            "credentials in url": _panel_file(
                judge={**_panel_file()["judge"], "base_url": f"https://u:{GEN_KEY}@judge.example/v1"}
            ),
            "no host": _panel_file(judge={**_panel_file()["judge"], "base_url": "https://"}),
            "unknown account": _panel_file(judge={**_panel_file()["judge"], "account": "nobody"}),
            "follow something else": _panel_file(judge={"follow": "judge"}),
        }
        for label, payload in cases.items():
            with self.subTest(label):
                self.write(PROVIDER_FILE, payload)
                with self.assertRaises(ProviderSettingsError) as caught:
                    self.load()
                self.assertNotIn(GEN_KEY, str(caught.exception))
                self.assertNotIn(JUDGE_KEY, str(caught.exception))

    def test_local_http_is_allowed(self):
        for host in ("127.0.0.1", "localhost"):
            with self.subTest(host):
                self.write(
                    PROVIDER_FILE,
                    _panel_file(judge={**_panel_file()["judge"], "base_url": f"http://{host}:8080/v1"}),
                )
                settings, _ = self.load()
                self.assertEqual(settings.judge.host, host)

    def test_a_retired_account_cannot_carry_a_line(self):
        retired = _accounts_file()
        retired["accounts"]["ds"]["retired_at"] = "2026-10-25T01:00:00+00:00"
        self.write(ACCOUNTS_FILE, retired)
        self.write(PROVIDER_FILE, _panel_file())
        with self.assertRaises(ProviderSettingsError):
            self.load()

    def test_an_unparseable_url_is_refused_without_echoing_it(self):
        bad_urls = {
            # U+2100 在 NFKC 下变出 "/"，urlsplit 当场抛，消息里是整段 netloc。
            "nfkc": f"https://u:{GEN_KEY}@bad\u2100.example/v1",
            # urlsplit 和 hostname 都成功，读 port 时才抛，消息里是端口原文。
            "lazy port": f"https://host.example:{GEN_KEY}/v1",
            "port out of range": "https://host.example:99999/v1",
            "bad ipv6": f"https://[{GEN_KEY}/v1",
        }
        for name, bad in bad_urls.items():
            with self.subTest(name):
                self.write(PROVIDER_FILE, _panel_file(generation={**_panel_file()["generation"], "base_url": bad}))
                with self.assertRaises(ProviderSettingsError) as caught:
                    self.load()
                self.assertIsNone(caught.exception.__cause__)
                self.assertNotIn(GEN_KEY, str(caught.exception))
                line = Line(
                    name=LINE_GENERATION, account="a", api_format="anthropic",
                    base_url=bad, model="m", token=Secret(GEN_KEY),
                )
                self.assertEqual(line.host, "")

    def test_a_valid_panel_is_not_vetoed_by_a_broken_env(self):
        self.write(PROVIDER_FILE, _panel_file())
        for env in ({"API_FORMAT": "grpc"}, {"BASE_URL": ""}, {"PNS_API_KEY_NAME": ""}):
            with self.subTest(env=env):
                settings, _ = self.load(env)
                self.assertEqual(settings.source, SOURCE_PANEL)
                self.assertFalse(settings.env_complete)
                self.assertEqual(
                    (settings.generator_model, settings.evaluator_model), ("gen-model", "judge-model")
                )

    def test_the_env_key_name_is_recorded_as_is_beside_a_panel(self):
        self.write(PROVIDER_FILE, _panel_file())
        for env, name, complete in (
            ({"MIMO_API_KEY": "sk-legacy"}, "MIMO_API_KEY", True),
            ({"PNS_API_KEY_NAME": "OTHER_KEY", "OTHER_KEY": "sk-legacy"}, "OTHER_KEY", True),
            ({"PNS_API_KEY_NAME": "OTHER_KEY"}, "OTHER_KEY", False),
            # 空串不偷偷换成默认名：它记录的是"旧配置用不了"，回滚预检要看到原样。
            ({"PNS_API_KEY_NAME": "", "MIMO_API_KEY": "sk-legacy"}, "", False),
        ):
            with self.subTest(env=env):
                with patch.dict(os.environ, env):
                    settings, _ = self.load(env)
                self.assertEqual((settings.env_key_name, settings.env_complete), (name, complete))
                self.assertEqual(
                    (settings.generation.token.reveal(), settings.judge.token.reveal()), (GEN_KEY, JUDGE_KEY)
                )

    def test_panel_tokens_do_not_need_the_env_key(self):
        self.write(PROVIDER_FILE, _panel_file())
        with patch.dict(os.environ, {"MIMO_API_KEY": ""}):
            settings, _ = self.load({"MIMO_API_KEY": ""})
        self.assertTrue(settings.generation.token and settings.judge.token)
        self.assertFalse(settings.env_complete)


class RegistryBuildTests(_SettingsDir):
    def test_the_registry_reads_the_panel_and_refuses_a_broken_one(self):
        self.write(ACCOUNTS_FILE, _accounts_file())
        self.write(PROVIDER_FILE, _panel_file())
        with patch.object(content_registry, "SETTINGS_DIR", self.dir):
            registry = build_content_registry()
            self.assertEqual(registry.models.source, SOURCE_PANEL)
            self.assertEqual(registry.models.judge.model, "judge-model")
            text = json.dumps(registry.to_dict(), ensure_ascii=False)
            self.assertNotIn(GEN_KEY, text)
            self.assertNotIn(JUDGE_KEY, text)
            self.write(PROVIDER_FILE, _panel_file(version=9))
            with self.assertRaises(ConfigValidationError) as caught:
                build_content_registry()
            self.assertNotIn(GEN_KEY, str(caught.exception))

    def test_a_failed_reload_does_not_publish_the_url(self):
        bad = f"https://u:{GEN_KEY}@bad\u2100.example/v1"
        self.write(ACCOUNTS_FILE, _accounts_file())
        self.write(PROVIDER_FILE, _panel_file(generation={**_panel_file()["generation"], "base_url": bad}))
        boundary = ConfigBoundary(SessionSupervisor())
        with patch.object(content_registry, "SETTINGS_DIR", self.dir):
            result = boundary.reload()
            exposed = json.dumps(
                {"result": result.to_dict(), "status": boundary.status()}, ensure_ascii=False
            )
        self.assertEqual(result.status, "failed")
        self.assertNotIn(GEN_KEY, exposed)

    def test_the_reload_endpoints_do_not_publish_the_url(self):
        from fastapi.testclient import TestClient

        from pns.interfaces.app import create_app

        self.write(ACCOUNTS_FILE, _accounts_file())
        client = TestClient(create_app(WorldControlPlane(root=self.dir.parent / "worlds")))
        for bad in (f"https://u:{GEN_KEY}@bad\u2100.example/v1", f"https://host.example:{GEN_KEY}/v1"):
            with self.subTest(bad=bad.replace(GEN_KEY, "<key>")):
                self.write(PROVIDER_FILE, _panel_file(generation={**_panel_file()["generation"], "base_url": bad}))
                before = BOUNDARY.active()
                with patch.object(content_registry, "SETTINGS_DIR", self.dir):
                    posted = client.post("/api/config/reload")
                status = client.get("/api/config/reload")
                self.assertEqual(posted.status_code, 400, posted.text)
                self.assertIs(BOUNDARY.active(), before)  # 失败的重载不换快照
                for response in (posted, status):
                    self.assertNotIn(GEN_KEY, response.text)

    def test_without_a_panel_file_the_registry_is_env_derived(self):
        with patch.object(content_registry, "SETTINGS_DIR", self.dir):
            registry = build_content_registry()
        self.assertEqual(registry.models.source, SOURCE_ENV)
        self.assertEqual(set(registry.accounts.accounts), {ENV_ACCOUNT})


# ── 4. 账户目录 ─────────────────────────────────────────────────────────
class AccountCatalogTests(_SettingsDir):
    def test_without_a_file_only_the_builtin_env_account_exists(self):
        catalog = load_accounts(self.dir)
        self.assertEqual(set(catalog.accounts), {ENV_ACCOUNT})
        self.assertEqual(catalog.get(ENV_ACCOUNT).created_at, "")

    def test_ensure_env_account_is_idempotent(self):
        first = ensure_env_account(self.dir)
        self.assertTrue(first.created_at)
        path = self.dir / ACCOUNTS_FILE
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        before = path.read_bytes()
        again = ensure_env_account(self.dir)
        self.assertEqual(again, first)
        self.assertEqual(path.read_bytes(), before)

    def test_ensure_env_account_keeps_other_accounts(self):
        self.write(ACCOUNTS_FILE, _accounts_file())
        ensure_env_account(self.dir)
        self.assertEqual(set(load_accounts(self.dir).accounts), {ENV_ACCOUNT, "mimo-main", "ds"})

    def test_broken_catalog_entries_are_refused(self):
        cases = {
            "bad id": _accounts_file(**{"a/b": _accounts_file()["accounts"]["ds"]}),
            "bad kind": _accounts_file(ds={**_accounts_file()["accounts"]["ds"], "kind": "bank"}),
            "bad currency": _accounts_file(ds={**_accounts_file()["accounts"]["ds"], "currency": "JPY"}),
            "bad multiplier": _accounts_file(ds={**_accounts_file()["accounts"]["ds"], "multiplier": "-1"}),
            "float multiplier": _accounts_file(ds={**_accounts_file()["accounts"]["ds"], "multiplier": 1.2}),
            "auto balance": _accounts_file(ds={**_accounts_file()["accounts"]["ds"], "balance": "auto"}),
            "version": {"version": 3, "accounts": {}},
        }
        for label, payload in cases.items():
            with self.subTest(label):
                self.write(ACCOUNTS_FILE, payload)
                with self.assertRaises(ProviderSettingsError):
                    load_accounts(self.dir)


# ── 5. 写盘合同 ─────────────────────────────────────────────────────────
class DurableWriteTests(_SettingsDir):
    def test_a_written_file_is_0600(self):
        path = self.dir / "x.json"
        settings_files.durable_write(path, b"{}")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.read_bytes(), b"{}")

    def test_failure_before_rename_leaves_the_target_and_no_temp_file(self):
        path = self.dir / "x.json"
        path.write_bytes(b"old")
        with patch.object(settings_files.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(settings_files.NotPublished):
                settings_files.durable_write(path, b"new")
        self.assertEqual(path.read_bytes(), b"old")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["x.json"])

    def test_a_directory_sync_failure_is_published_but_not_durable(self):
        path = self.dir / "x.json"
        path.write_bytes(b"old")
        real = settings_files.sync_dir

        def fail_after_rename(directory):
            # 目录链（data/ 里的 settings 目录项）刷成了，rename 之后刷 settings/ 失败。
            if Path(directory) == self.dir:
                raise OSError(5, "EIO")
            return real(directory)

        with patch.object(settings_files, "sync_dir", side_effect=fail_after_rename):
            with self.assertRaises(settings_files.PublishedNotDurable):
                settings_files.durable_write(path, b"new")
        self.assertEqual(path.read_bytes(), b"new")

    def test_new_directories_are_synced_into_their_parents(self):
        path = self.dir / "a" / "b" / "x.json"
        with patch.object(settings_files, "sync_dir", wraps=settings_files.sync_dir) as sync:
            settings_files.durable_write(path, b"{}", anchor=self.dir)
        synced = {Path(call.args[0]) for call in sync.call_args_list}
        self.assertEqual(synced, {self.dir, self.dir / "a", self.dir / "a" / "b"})

    def test_an_unsynced_new_directory_is_not_published(self):
        path = self.dir / "new" / "x.json"
        with patch.object(settings_files, "sync_dir", side_effect=OSError(5, "EIO")):
            with self.assertRaises(settings_files.NotPublished):
                settings_files.durable_write(path, b"{}")
        self.assertFalse(path.exists())

    def test_a_retry_after_a_failed_parent_sync_resyncs_the_chain(self):
        path = self.dir / "a" / "b" / "x.json"
        real = settings_files.sync_dir

        def fail_deepest(directory):
            if Path(directory) == self.dir / "a":  # b 建好了，刷它在 a 里的目录项失败
                raise OSError(5, "EIO")
            return real(directory)

        with patch.object(settings_files, "sync_dir", side_effect=fail_deepest):
            with self.assertRaises(settings_files.NotPublished):
                settings_files.durable_write(path, b"first", anchor=self.dir)
        self.assertTrue(path.parent.is_dir())  # 目录留下来了，但目录项没刷成
        with patch.object(settings_files, "sync_dir", return_value=True) as sync:
            self.assertIs(settings_files.durable_write(path, b"second", anchor=self.dir), True)
        synced = [Path(call.args[0]) for call in sync.call_args_list]
        for directory in (self.dir, self.dir / "a", self.dir / "a" / "b"):
            self.assertIn(directory, synced)

    def test_a_retry_does_not_forget_an_unsupported_parent_sync(self):
        path = self.dir / "new" / "x.json"
        real = settings_files.sync_dir
        with patch.object(
            settings_files, "sync_dir", side_effect=lambda d: False if Path(d) == self.dir else real(d)
        ):
            self.assertIs(settings_files.durable_write(path, b"first"), False)
            self.assertIs(settings_files.durable_write(path, b"second"), False)

    def test_a_concurrent_writer_does_not_ride_on_an_unsynced_directory(self):
        # A 建好目录、卡在刷父目录项上；B 这时看到目录"已存在"，也必须自己刷一遍。
        path = self.dir / "new" / "x.json"
        real = settings_files.sync_dir
        a_waiting, release = threading.Event(), threading.Event()
        synced = []

        def sync(directory):
            directory = Path(directory)
            synced.append((threading.current_thread().name, directory))
            if threading.current_thread().name == "writer-a" and directory == self.dir:
                a_waiting.set()
                release.wait(5)
                raise OSError(5, "EIO")
            return real(directory)

        outcome = {}

        def writer_a():
            try:
                settings_files.durable_write(path, b"a")
            except settings_files.NotPublished:
                outcome["a"] = "not published"

        with patch.object(settings_files, "sync_dir", side_effect=sync):
            thread = threading.Thread(target=writer_a, name="writer-a")
            thread.start()
            self.assertTrue(a_waiting.wait(5))
            self.assertIs(settings_files.durable_write(path, b"b"), True)
            release.set()
            thread.join(5)
        self.assertIn((threading.current_thread().name, self.dir), synced)
        self.assertEqual(outcome, {"a": "not published"})
        self.assertEqual(path.read_bytes(), b"b")

    def test_the_anchor_must_be_an_ancestor(self):
        with self.assertRaises(ValueError):
            settings_files.durable_write(self.dir / "x.json", b"{}", anchor=self.dir / "elsewhere")

    def test_unsupported_parent_sync_is_reported(self):
        path = self.dir / "new" / "x.json"
        real = settings_files.sync_dir
        with patch.object(
            settings_files, "sync_dir", side_effect=lambda d: False if Path(d) == self.dir else real(d)
        ):
            self.assertFalse(settings_files.durable_write(path, b"{}"))

    def test_unlink_has_the_same_three_outcomes(self):
        path = self.dir / "x.json"
        path.write_bytes(b"old")
        with patch.object(Path, "unlink", side_effect=PermissionError("no")):
            with self.assertRaises(settings_files.NotPublished):
                settings_files.durable_unlink(path)
        self.assertTrue(path.exists())
        with patch.object(settings_files, "sync_dir", side_effect=OSError(5, "EIO")):
            with self.assertRaises(settings_files.PublishedNotDurable):
                settings_files.durable_unlink(path)
        self.assertFalse(path.exists())
        settings_files.durable_unlink(path)  # 不存在也算删过


class JudgeThinkingTests(_SettingsDir):
    """判分时 thinking 怎么发由线路说了算（不再只靠嗅探 MiMo 官方地址）。"""

    def setUp(self):
        super().setUp()
        self.write(ACCOUNTS_FILE, _accounts_file())

    def test_the_field_is_optional_checked_and_followed(self):
        self.write(PROVIDER_FILE, _panel_file())
        settings, _ = self.load()
        self.assertEqual((settings.generation.judge_thinking, settings.judge.judge_thinking), ("auto", "auto"))
        generation = {**_panel_file()["generation"], "judge_thinking": "off"}
        self.write(PROVIDER_FILE, _panel_file(generation=generation, judge={"follow": LINE_GENERATION}))
        settings, _ = self.load()
        self.assertEqual(settings.judge.judge_thinking, "off")
        self.assertEqual(settings.public_dict()["judge"]["judge_thinking"], "off")
        self.write(PROVIDER_FILE, _panel_file(generation={**generation, "judge_thinking": GEN_KEY}))
        with self.assertRaises(ProviderSettingsError) as caught:
            self.load()
        self.assertNotIn(GEN_KEY, str(caught.exception))
        self.assertEqual(ModelSettings.from_env({}).judge.judge_thinking, "auto")

    def test_the_judge_request_follows_the_line(self):
        from pns.logic import router

        sent = []
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: sent.append(kw) or SimpleNamespace(
            content=[SimpleNamespace(type="text", text="{}")])))
        relay, mimo = "https://relay.example/anthropic", "https://api.xiaomimimo.com/anthropic"
        for policy, url, disabled in (
            ("auto", mimo, True), ("auto", relay, False),
            ("off", relay, True), ("off", mimo, True),
            ("provider", mimo, False), ("provider", relay, False),
        ):
            with self.subTest(policy=policy, url=url):
                sent.clear()
                line = Line(name=LINE_JUDGE, account="ds", api_format="anthropic", base_url=url,
                            model="m", token=Secret("k"), judge_thinking=policy)
                router._call(client, "m", "sys", "msg", settings=line)
                self.assertEqual("thinking" in sent[0], disabled)
                if disabled:
                    self.assertEqual(sent[0]["thinking"], {"type": "disabled"})

    def test_a_put_carries_it(self):
        from pns.runtime.provider_settings import build_candidate

        request = {
            "generation": {**_panel_file()["generation"], "judge_thinking": "off"},
            "judge": {**_panel_file()["judge"]},
        }
        payload, settings = build_candidate(
            request, None, load_accounts(self.dir), {}, updated_by="t", ooc_threshold=5.0
        )
        self.assertEqual(json.loads(payload)["generation"]["judge_thinking"], "off")
        self.assertNotIn("judge_thinking", json.loads(payload)["judge"])
        self.assertEqual((settings.generation.judge_thinking, settings.judge.judge_thinking), ("off", "auto"))
        with self.assertRaises(ProviderSettingsError):
            build_candidate({**request, "judge": {**request["judge"], "judge_thinking": "maybe"}},
                            None, load_accounts(self.dir), {}, updated_by="t", ooc_threshold=5.0)


# ── 6. 两条线路真的分开走 ───────────────────────────────────────────────
class _Anthropic:
    def __init__(self, calls):
        self.messages = SimpleNamespace(create=self._create)
        self._calls = calls

    def _create(self, **kwargs):
        self._calls.append(("anthropic", kwargs["model"], kwargs["system"]))
        return SimpleNamespace(
            content=[SimpleNamespace(text="今天也在这里哦")],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=100, output_tokens=10,
                cache_read_input_tokens=None, cache_creation_input_tokens=None,
            ),
        )


class _OpenAI:
    def __init__(self, calls):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self._calls = calls

    def _create(self, **kwargs):
        self._calls.append(("openai", kwargs["model"], kwargs["messages"][0]["content"]))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=VERDICT), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=300, completion_tokens=50, prompt_tokens_details=None),
        )


def _two_line_registry():
    registry = BOUNDARY.active()
    base = registry.models
    generation = Line(
        name=LINE_GENERATION, account="mimo-main", api_format="anthropic",
        base_url="https://gen.example/anthropic", model="gen-model",
        token=Secret(GEN_KEY), provider="mimo",
    )
    judge = Line(
        name=LINE_JUDGE, account="ds", api_format="openai",
        base_url="https://judge.example/v1", model="judge-model",
        token=Secret(JUDGE_KEY), provider="deepseek",
    )
    models = replace(base, generation=generation, judge=judge, source=SOURCE_PANEL)
    return replace(registry, models=models)


class TwoLineWiringTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.registry = _two_line_registry()
        self.calls = []
        self.built = []

    def factory(self, api_key, *, settings):
        self.built.append((settings.name, api_key, settings.base_url, settings.api_format))
        return _OpenAI(self.calls) if settings.api_format == "openai" else _Anthropic(self.calls)

    def test_world_generation_and_judge_use_their_own_lines(self):
        ledger = ul.UsageLedger(
            self.tmp / "usage", clock=lambda: datetime(2026, 10, 25, 4, tzinfo=timezone.utc)
        )
        plane = WorldControlPlane(
            root=self.tmp / "worlds", client_factory=self.factory, usage_ledger=ledger
        )
        adapters = plane.build_adapters(self.registry, world_id="w")
        self.assertEqual(
            self.built,
            [
                (LINE_GENERATION, GEN_KEY, "https://gen.example/anthropic", "anthropic"),
                (LINE_JUDGE, JUDGE_KEY, "https://judge.example/v1", "openai"),
            ],
        )
        state = plane.new_session_state(
            world_id="w", scene_id=self.registry.default_scene,
            character_ids=["kanade"], registry=self.registry,
        )
        generate = adapters.policy_factory(state)._generator._call
        generate("kanade", self.registry.scene(None), [{"role": "user", "content": "在吗"}])
        verdict = adapters.auditor._judge(
            AuditRequest(
                character_id="mafuyu", proposal_id="p1", payload={"text": "嗯。"},
                action_id=ActionId.SPEAK_HERE,
                now=datetime(2026, 10, 25, 4, tzinfo=timezone.utc),
            )
        )
        self.assertTrue(verdict["dimensions_complete"])
        self.assertEqual(verdict["evaluator_model"], "judge-model")
        self.assertEqual(verdict["evaluator_provider"], "deepseek")
        self.assertEqual(
            [(protocol, model) for protocol, model, _ in self.calls],
            [("anthropic", "gen-model"), ("openai", "judge-model")],
        )
        self.assertIn("监督者Router", self.calls[1][2])
        records, corrupt = ledger.read()
        self.assertEqual(corrupt, 0)
        gen, judge = records
        self.assertEqual((gen.path, gen.protocol, gen.model), ("generation", "anthropic", "gen-model"))
        self.assertEqual((judge.path, judge.protocol, judge.model), ("judge", "openai", "judge-model"))
        self.assertEqual(judge.usage, ul.Usage(300, 0, 0, 50))
        auditor = adapters.auditor
        self.assertEqual(
            (auditor._evaluator_provider, auditor._generator_provider), ("deepseek", "mimo")
        )

    def test_the_cache_warmer_only_ever_sees_the_generation_line(self):
        from pns.interfaces.composition import AutonomySettings

        plane = WorldControlPlane(
            root=self.tmp / "worlds", client_factory=self.factory,
            autonomy=replace(AutonomySettings.from_env(), warm_window_seconds=60, warm_interval_seconds=30),
        )
        adapters = plane.build_adapters(self.registry, world_id="w")
        self.assertIsNotNone(adapters.warmer)
        # 生成线路是 anthropic、判分线路是 openai：续命的协议必须是生成那条的。
        self.assertEqual(adapters.warmer._protocol, "anthropic")

    def test_research_sessions_build_one_client_per_line(self):
        with patch("pns.runtime.session_runtime.router_mod.create_client", side_effect=self.factory):
            runtime = SessionRuntime.create(
                {"characters": ["mizuki", "ena"], "max_turns": 1, "api_delay": 0},
                registry=self.registry,
                supervisor=SessionSupervisor(),
                history_dir=self.tmp / "history",
                drift_scores_file=self.tmp / "drift.jsonl",
            )
        self.assertEqual([b[0] for b in self.built], [LINE_GENERATION, LINE_JUDGE])
        self.assertIsInstance(runtime.client, _Anthropic)
        self.assertIsInstance(runtime.judge_client, _OpenAI)
        self.assertEqual(runtime.model, "gen-model")
        self.assertEqual(runtime.generator_provider, "mimo")


# ── 7. 生产启动 ─────────────────────────────────────────────────────────
class ProductionStartupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        (self.tmp / "dist").mkdir()

    def start(self, registry):
        # 只跑模型凭据这一关；管理员账户那一关有自己的测试（test_auth_api）。
        _verify_production_config(
            DeploymentSettings(mode="production", admin_token=ADMIN_TOKEN),
            self.tmp / "dist",
            lambda: registry,
        )

    def test_panel_tokens_alone_are_enough(self):
        registry = _two_line_registry()
        catalog = AccountCatalog(accounts={
            ENV_ACCOUNT: env_account(),
            "mimo-main": replace(env_account(), account_id="mimo-main", kind="mimo"),
            "ds": replace(env_account(), account_id="ds", kind="deepseek"),
        })
        with patch.dict(os.environ, {registry.models.env_key_name: ""}):
            self.start(replace(registry, accounts=catalog))

    def test_a_missing_judge_token_refuses_to_start(self):
        from pns.interfaces.security import DeploymentConfigError

        registry = _two_line_registry()
        catalog = AccountCatalog(accounts={
            "mimo-main": replace(env_account(), account_id="mimo-main"),
            "ds": replace(env_account(), account_id="ds"),
        })
        models = replace(registry.models, judge=replace(registry.models.judge, token=Secret("")))
        with self.assertRaises(DeploymentConfigError) as caught:
            self.start(replace(registry, models=models, accounts=catalog))
        self.assertIn("判分线路", str(caught.exception))
        self.assertNotIn(GEN_KEY, str(caught.exception))


# ── 8. 真 SDK 走回环 ────────────────────────────────────────────────────
class _InFlight:
    """几台假服务器共用：记下每个请求进来时，别的服务器上有没有请求还没回。

    给了 hold，那台服务器的第一个请求会停住，直到别的服务器也有请求在路上（最多
    等 10 秒）：两个世界同时起步时步调一致，不这么做，生成和判分可能很久都碰不上头。
    """

    def __init__(self, delay, *, hold=None):
        self.delay = delay
        self._hold = hold
        self._held = False
        self._partner = threading.Event()
        self._lock = threading.Lock()
        self._current = {}
        self.overlaps = set()  # (进来的服务器, 正在处理中的那台)

    @contextlib.contextmanager
    def request(self, name):
        with self._lock:
            busy = {other for other, n in self._current.items() if n}
            self.overlaps.update((name, other) for other in busy)
            self._current[name] = self._current.get(name, 0) + 1
            if (name == self._hold and busy - {name}) or (name != self._hold and self._hold in busy):
                self._partner.set()
            first = name == self._hold and not self._held
            self._held = self._held or first
        try:
            if first:
                self._partner.wait(10.0)
            threading.Event().wait(self.delay)
            yield
        finally:
            with self._lock:
                self._current[name] -= 1


def _fake_provider(reply, *, status=200, raw=None, usage_base=None, inflight=None, name=None):
    """一台只认 POST 的假 provider：按路径回 anthropic 或 openai 格式，记下认证头和模型。

    给了 raw 就原样回这段字节（配 status），用来模拟上游报错和坏响应。reply 可以是
    函数（收请求体、回文本），一台服务器同时扮生成和判分时用。

    给了 usage_base，每个请求回的 usage 都不一样（第 n 个是 usage_base + n），归一后
    两种协议都是 Usage(usage_base + n, 0, 7, n + 1)；server.issued 按序记下
    (这个 usage, 请求体文本)。
    给了 inflight，请求在里面停 inflight.delay 秒，用来制造重叠。
    """
    seen = []
    issued = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            with inflight.request(name) if inflight is not None else contextlib.nullcontext():
                self._answer()

        def _answer(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            key = self.headers.get("x-api-key") or (
                self.headers.get("authorization") or ""
            ).removeprefix("Bearer ")
            with lock:
                seen.append((self.path, key, body.get("model")))
                n = len(issued)
                if usage_base is not None:
                    issued.append((ul.Usage(usage_base + n, 0, 7, n + 1), json.dumps(body, ensure_ascii=False)))
            text = reply(body) if callable(reply) else reply
            if raw is not None:
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if self.path.endswith("/messages"):
                out = {
                    "id": "m", "type": "message", "role": "assistant", "model": body["model"],
                    "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                    "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 2},
                }
                if usage_base is not None:
                    out["usage"] = {
                        "input_tokens": usage_base + n, "output_tokens": n + 1,
                        "cache_read_input_tokens": 7, "cache_creation_input_tokens": None,
                    }
            else:
                out = {
                    "id": "c", "object": "chat.completion", "created": 0, "model": body["model"],
                    "choices": [{
                        "index": 0, "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                }
                if usage_base is not None:
                    # openai 的 prompt_tokens 含缓存命中，归一时减掉。
                    out["usage"] = {
                        "prompt_tokens": usage_base + n + 7, "completion_tokens": n + 1,
                        "total_tokens": usage_base + 2 * n + 8,
                        "prompt_tokens_details": {"cached_tokens": 7},
                    }
            data = json.dumps(out, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.issued = issued
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


class RealSdkTwoLineTests(_SettingsDir):
    def setUp(self):
        super().setUp()
        self.write(ACCOUNTS_FILE, _accounts_file())

    def _url(self, server, api_format):
        suffix = "/anthropic" if api_format == "anthropic" else "/v1"
        return f"http://127.0.0.1:{server.server_port}{suffix}"

    def _run_session(self, gen_format, judge_format, *, gen_raw=None, judge_raw=None, status=200,
                     follow=False):
        def gen_reply(body):
            # 跟随时判分请求也打到这台：认出 Router 的提示词就回判分 JSON。
            if follow and "Router" in json.dumps(body, ensure_ascii=False):
                return VERDICT
            return "「……嗯，今天也画完了。」"

        gen_server, gen_seen = _fake_provider(gen_reply, status=status, raw=gen_raw)
        judge_server, judge_seen = _fake_provider(VERDICT, status=status, raw=judge_raw)
        for server in (gen_server, judge_server):
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        panel = _panel_file()
        self.write(PROVIDER_FILE, _panel_file(
            generation={**panel["generation"], "api_format": gen_format,
                        "base_url": self._url(gen_server, gen_format)},
            judge={"follow": LINE_GENERATION} if follow else {
                **panel["judge"], "api_format": judge_format,
                "base_url": self._url(judge_server, judge_format)},
        ))
        out = self.dir.parent / f"out-{gen_format}-{judge_format}-{follow}"
        with patch.object(content_registry, "SETTINGS_DIR", self.dir):
            registry = build_content_registry()
        runtime = SessionRuntime.create(
            {"max_turns": 1, "api_delay": 0, "characters": ["mizuki", "ena"]},
            registry=registry, supervisor=SessionSupervisor(),
            history_dir=out / "history", drift_scores_file=out / "drift.jsonl",
        )

        async def consume():
            return [message async for message in runtime.run()]

        messages = asyncio.run(consume())
        return messages, gen_seen, judge_seen, out

    def test_each_provider_only_sees_its_own_line(self):
        for gen_format, judge_format in (("anthropic", "openai"), ("openai", "anthropic"),
                                         ("anthropic", "anthropic")):
            with self.subTest(generation=gen_format, judge=judge_format):
                messages, gen_seen, judge_seen, out = self._run_session(gen_format, judge_format)
                self.assertNotIn("error", [m.get("type") for m in messages])
                self.assertTrue(gen_seen and judge_seen)
                self.assertEqual({(key, model) for _, key, model in gen_seen}, {(GEN_KEY, "gen-model")})
                self.assertEqual({(key, model) for _, key, model in judge_seen}, {(JUDGE_KEY, "judge-model")})
                text = json.dumps(messages, ensure_ascii=False, default=str) + "".join(
                    path.read_text(errors="ignore") for path in out.rglob("*") if path.is_file()
                )
                self.assertNotIn(GEN_KEY, text)
                self.assertNotIn(JUDGE_KEY, text)

    def test_a_following_judge_goes_to_the_generation_provider(self):
        for gen_format in ("anthropic", "openai"):
            with self.subTest(generation=gen_format):
                messages, gen_seen, judge_seen, _ = self._run_session(gen_format, None, follow=True)
                self.assertNotIn("error", [m.get("type") for m in messages])
                self.assertEqual(judge_seen, [])
                # 一次生成 + 一次判分，都打到生成那台，用生成的 key 和模型。
                self.assertEqual(len(gen_seen), 2)
                self.assertEqual({(key, model) for _, key, model in gen_seen}, {(GEN_KEY, "gen-model")})
                turns = [m for m in messages if m.get("type") == "turn"]
                self.assertEqual(len(turns), 1)
                # 判分真的判完了，不是解析失败后的降级结果。
                self.assertTrue(turns[0]["dimensions_complete"])
                self.assertNotIn(turns[0]["drift_type"], ("解析失败", "error"))

    def test_upstream_failures_carrying_the_key_stay_inside(self):
        echo = json.dumps({"error": {"type": "invalid_request_error",
                                     "message": f"bad key {GEN_KEY} / {JUDGE_KEY}"}}).encode()
        not_json = f"<html>upstream {GEN_KEY} {JUDGE_KEY}</html>".encode()
        # 合法 JSON、形状不对：两种协议都认不出正文在哪。
        bad_shape = json.dumps({"id": "x", "type": "message", "content": f"{GEN_KEY} {JUDGE_KEY}",
                                "choices": [], "note": f"{GEN_KEY} {JUDGE_KEY}"}).encode()
        cases = (
            ("生成 400 回显", {"gen_raw": echo, "status": 400}),
            ("生成 401 回显", {"gen_raw": echo, "status": 401}),
            ("生成非 JSON", {"gen_raw": not_json}),
            ("生成结构错", {"gen_raw": bad_shape}),
            ("判分 400 回显", {"judge_raw": echo, "status": 400}),
            ("判分非 JSON", {"judge_raw": not_json}),
            ("判分结构错", {"judge_raw": bad_shape}),
        )
        for gen_format, judge_format in (("anthropic", "openai"), ("openai", "anthropic")):
            for name, kwargs in cases:
                with self.subTest(name, generation=gen_format, judge=judge_format):
                    stdout, stderr, logs = io.StringIO(), io.StringIO(), io.StringIO()
                    handler = logging.StreamHandler(logs)
                    logging.getLogger().addHandler(handler)
                    self.addCleanup(logging.getLogger().removeHandler, handler)
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        messages, gen_seen, judge_seen, out = self._run_session(
                            gen_format, judge_format, **kwargs
                        )
                    # 先确认真的走进了失败路径，不然"没漏"什么也说明不了。
                    types = [m.get("type") for m in messages]
                    if "gen_raw" in kwargs:
                        self.assertTrue(gen_seen)
                        self.assertIn("error", types)
                        self.assertNotIn("turn", types)
                    else:
                        self.assertTrue(judge_seen)
                        turns = [m for m in messages if m.get("type") == "turn"]
                        self.assertEqual(len(turns), 1)
                        self.assertIn(turns[0]["drift_type"], ("error", "解析失败"))
                        self.assertFalse(turns[0]["dimensions_complete"])
                    text = json.dumps(messages, ensure_ascii=False, default=str) + "".join(
                        path.read_text(errors="ignore") for path in out.rglob("*") if path.is_file()
                    ) + stdout.getvalue() + stderr.getvalue() + logs.getvalue()
                    self.assertNotIn(GEN_KEY, text)
                    self.assertNotIn(JUDGE_KEY, text)

    def test_client_construction_failures_use_fixed_text(self):
        import anthropic
        import openai

        self.write(PROVIDER_FILE, _panel_file())  # 生成 anthropic、判分 openai
        with patch.object(content_registry, "SETTINGS_DIR", self.dir):
            registry = build_content_registry()
        failures = (
            ("第一个 client", anthropic, "Anthropic", RuntimeError(GEN_KEY)),
            ("第二个 client", openai, "OpenAI", type(JUDGE_KEY, (RuntimeError,), {})("rejected")),
        )
        from fastapi.testclient import TestClient

        from pns.interfaces.app import create_app

        for name, module, attr, error in failures:
            with self.subTest(name):
                stdout, stderr, logs = io.StringIO(), io.StringIO(), io.StringIO()
                handler = logging.StreamHandler(logs)
                logging.getLogger().addHandler(handler)
                self.addCleanup(logging.getLogger().removeHandler, handler)
                plane = WorldControlPlane(
                    root=self.dir.parent / f"worlds-{attr}", registry_provider=lambda: registry,
                    usage_ledger=ul.UsageLedger(self.dir.parent / f"usage-{attr}"),
                )
                client = TestClient(create_app(plane))
                with patch.object(module, attr, side_effect=error), \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    # 研究会话：create() 抛固定文案。
                    with self.assertRaises(SessionSetupError) as caught:
                        SessionRuntime.create(
                            {"max_turns": 1, "characters": ["mizuki", "ena"]},
                            registry=registry, supervisor=SessionSupervisor(),
                            history_dir=self.dir.parent / "h", drift_scores_file=self.dir.parent / "d.jsonl",
                        )
                    # 世界：建世界、开认知时建 client，HTTP 响应同样只有固定文案。
                    responses = [
                        client.post("/api/persistent-worlds", json={
                            "world_id": "nightcord", "scene": "nightcord", "characters": ["mizuki", "ena"],
                        }),
                        client.post("/api/persistent-worlds/nightcord/autonomy/start"),
                        client.get("/api/persistent-worlds/nightcord"),
                    ]
                    # 原始异常留在 __cause__ 里给服务器侧调试（上面扫过的出口都没打印它）。
                    with self.assertRaises(AdaptersUnavailable) as world_caught:
                        plane.build_adapters(registry, world_id="w")
                    plane.service.release_all()
                for raised in (caught.exception, world_caught.exception):
                    self.assertIs(raised.__cause__, error)
                    self.assertTrue(raised.__suppress_context__)
                    self.assertNotIn(GEN_KEY, str(raised) + type(raised).__name__)
                    self.assertNotIn(JUDGE_KEY, str(raised) + type(raised).__name__)
                self.assertTrue(any(r.status_code >= 400 for r in responses), [r.status_code for r in responses])
                text = (
                    str(caught.exception) + type(caught.exception).__name__
                    + "".join(r.text for r in responses)
                    + stdout.getvalue() + stderr.getvalue() + logs.getvalue()
                )
                self.assertNotIn(GEN_KEY, text)
                self.assertNotIn(JUDGE_KEY, text)


# ── 9. 世界：真 SDK 走回环 ──────────────────────────────────────────────
class WorldRealSdkTwoLineTests(_SettingsDir):
    _url = RealSdkTwoLineTests._url

    def setUp(self):
        super().setUp()
        self.write(ACCOUNTS_FILE, _accounts_file())
        from tests.autonomy_support import clock_threads

        self.clock_threads = clock_threads
        self.before = set(clock_threads())

    def _run_world(self, gen_format, judge_format, *, gen_raw=None, judge_raw=None, status=200,
                   follow=False, warm=False, before_stop=None):
        from fastapi.testclient import TestClient

        from pns.interfaces.app import create_app
        from pns.interfaces.composition import AutonomySettings
        from pns.runtime.autonomy.clock_worker import ClockConfig
        from pns.runtime.autonomy.seeding import ActivationCadence
        from tests.autonomy_support import wait_for

        def gen_reply(body):
            if follow and "Router" in json.dumps(body, ensure_ascii=False):
                return VERDICT
            return "「……嗯，今天也画完了。」"

        gen_server, gen_seen = _fake_provider(gen_reply, status=status, raw=gen_raw)
        judge_server, judge_seen = _fake_provider(VERDICT, status=status, raw=judge_raw)
        for server in (gen_server, judge_server):
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        panel = _panel_file()
        self.write(PROVIDER_FILE, _panel_file(
            generation={**panel["generation"], "api_format": gen_format,
                        "base_url": self._url(gen_server, gen_format)},
            judge={"follow": LINE_GENERATION} if follow else {
                **panel["judge"], "api_format": judge_format,
                "base_url": self._url(judge_server, judge_format)},
        ))
        with patch.object(content_registry, "SETTINGS_DIR", self.dir):
            registry = build_content_registry()
        out = self.dir.parent / (
            f"world-{gen_format}-{judge_format}-{status}-{bool(gen_raw)}-{bool(judge_raw)}-{follow}"
        )
        plane = WorldControlPlane(
            root=out / "worlds", registry_provider=lambda: registry,
            usage_ledger=ul.UsageLedger(out / "usage"),
            autonomy=AutonomySettings(
                clock=ClockConfig(rate=3000.0, interval_seconds=0.01, stop_timeout_seconds=2.0),
                cadence=ActivationCadence(), shutdown_timeout_seconds=1.0,
                reply_delay_minutes=None,
                **({"warm_window_seconds": 600.0, "warm_interval_seconds": 30.0} if warm else {}),
            ),
        )
        client = TestClient(create_app(plane))
        try:
            created = client.post("/api/persistent-worlds", json={
                "world_id": "nightcord", "scene": "nightcord", "characters": ["mizuki", "ena"],
            })
            self.assertEqual(created.status_code, 201, created.text)
            client.post("/api/persistent-worlds/nightcord/autonomy/start")
            if follow:
                from pns.models.event import EventType

                # 判分通过的台词才会作为 message.sent 进世界；判分解析失败会被审计拦下。
                wait_for(
                    lambda: plane.service.opened("nightcord").state.events.by_type(EventType.MESSAGE_SENT),
                    timeout=15.0, what="跟随判分通过、说上一句",
                )
            elif gen_raw is None:
                wait_for(lambda: judge_seen, timeout=15.0, what="至少判过一次分")
            else:
                wait_for(lambda: gen_seen, timeout=15.0, what="至少试过一次生成")
            client.post("/api/persistent-worlds/nightcord/autonomy/stop")
            if before_stop is not None:
                before_stop(plane, gen_seen, judge_seen)
            body = client.get("/api/persistent-worlds/nightcord").text
        finally:
            plane.service.release_all()
            wait_for(lambda: not (set(self.clock_threads()) - self.before), what="worker 退出")
        rows = [
            json.loads(line)
            for path in sorted((out / "usage").rglob("*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        return body, rows, gen_seen, judge_seen, out

    def test_cache_warming_only_ever_reaches_the_generation_provider(self):
        # 续命的最小间隔是 30 秒现实时间：认知停下之后把续命的时钟拨到窗口内、
        # 间隔之后，手动续一次。请求仍然经真 SDK 打到回环服务器上。
        for gen_format, judge_format in (("anthropic", "openai"), ("openai", "anthropic")):
            with self.subTest(generation=gen_format, judge=judge_format):
                seen_before = {}

                def warm(plane, gen_seen, judge_seen):
                    from tests.autonomy_support import wait_for

                    warmer = plane.service.opened("nightcord").clock_worker._warmer
                    self.assertIsNotNone(warmer)

                    def booked(path=None):
                        records, _ = plane.usage_ledger.read()
                        return [r for r in records if path is None or r.path == path]

                    # Stop 不等进行中的调用：等每个已到服务器的请求都记了账（没有请求
                    # 还在路上）再记基数。
                    wait_for(lambda: len(booked()) == len(gen_seen) + len(judge_seen),
                             timeout=10.0, what="进行中的调用都记完账")
                    seen_before.update(gen=len(gen_seen), judge=len(judge_seen))
                    real_now = warmer._now
                    warmer._now = lambda: real_now() + 60.0
                    # 时钟 worker 自己也会续：谁抢到都行，只续得了一次（发出前就记下发送时刻）。
                    warmer.warm_once(timeout=5.0)
                    wait_for(lambda: booked("warm"), timeout=10.0, what="续命记账")

                _, rows, gen_seen, judge_seen, _ = self._run_world(
                    gen_format, judge_format, warm=True, before_stop=warm
                )
                # 续命那一次只打到生成那台，用生成的 key 和模型；判分那台一个新请求都没有。
                self.assertEqual(len(gen_seen), seen_before["gen"] + 1)
                self.assertEqual(len(judge_seen), seen_before["judge"])
                self.assertEqual(gen_seen[-1][1:], (GEN_KEY, "gen-model"))
                warmed = [row for row in rows if row["path"] == "warm"]
                self.assertEqual(len(warmed), 1, rows)
                self.assertEqual((warmed[0]["protocol"], warmed[0]["model"], warmed[0]["failure"]),
                                 (gen_format, "gen-model", None))

    def test_world_lines_reach_their_own_providers(self):
        for gen_format, judge_format in (("anthropic", "openai"), ("openai", "anthropic")):
            with self.subTest(generation=gen_format, judge=judge_format):
                body, rows, gen_seen, judge_seen, _ = self._run_world(gen_format, judge_format)
                self.assertEqual({(key, model) for _, key, model in gen_seen}, {(GEN_KEY, "gen-model")})
                self.assertEqual({(key, model) for _, key, model in judge_seen}, {(JUDGE_KEY, "judge-model")})
                protocols = {(row["path"], row["protocol"], row["model"]) for row in rows}
                self.assertIn(("generation", gen_format, "gen-model"), protocols)
                self.assertIn(("judge", judge_format, "judge-model"), protocols)
                self.assertNotIn(GEN_KEY, body)
                self.assertNotIn(JUDGE_KEY, body)

    def test_world_upstream_failures_carrying_the_key_stay_inside(self):
        echo = json.dumps({"error": {"type": "invalid_request_error",
                                     "message": f"bad key {GEN_KEY} / {JUDGE_KEY}"}}).encode()
        for name, kwargs in (("生成 401 回显", {"gen_raw": echo, "status": 401}),
                             ("判分 400 回显", {"judge_raw": echo, "status": 400})):
            with self.subTest(name):
                stdout, stderr, logs = io.StringIO(), io.StringIO(), io.StringIO()
                handler = logging.StreamHandler(logs)
                logging.getLogger().addHandler(handler)
                self.addCleanup(logging.getLogger().removeHandler, handler)
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    body, rows, gen_seen, judge_seen, out = self._run_world("anthropic", "openai", **kwargs)
                # 先确认真的走进了失败路径：目标服务器被打到，账本里那条线有失败行。
                failed_path = "generation" if "gen_raw" in kwargs else "judge"
                self.assertTrue(gen_seen if failed_path == "generation" else judge_seen)
                self.assertTrue(any(row["path"] == failed_path and row["failure"] for row in rows), rows)
                text = body + json.dumps(rows, ensure_ascii=False) + "".join(
                    path.read_text(errors="ignore") for path in out.rglob("*") if path.is_file()
                ) + stdout.getvalue() + stderr.getvalue() + logs.getvalue()
                self.assertNotIn(GEN_KEY, text)
                self.assertNotIn(JUDGE_KEY, text)

    def test_overlapping_worlds_book_each_response_on_its_own_row(self):
        from fastapi.testclient import TestClient

        from pns.interfaces.app import create_app
        from pns.interfaces.composition import AutonomySettings
        from pns.runtime.autonomy.clock_worker import ClockConfig
        from pns.runtime.autonomy.seeding import ActivationCadence
        from tests.autonomy_support import wait_for

        worlds = {"nightcord": ["mizuki", "ena"], "nightcord-b": ["kanade", "mafuyu"]}
        for gen_format, judge_format in (("anthropic", "openai"), ("openai", "anthropic")):
            with self.subTest(generation=gen_format, judge=judge_format):
                # 两台服务器回的 usage 互不相同、每次也不同：任何一行串到别的响应、
                # 别的线路或按错协议读，数值都对不上。
                inflight = _InFlight(delay=0.02, hold="judge")
                gen_server, gen_seen = _fake_provider(
                    "「……嗯，今天也画完了。」", usage_base=10_000, inflight=inflight, name="generation")
                judge_server, judge_seen = _fake_provider(
                    VERDICT, usage_base=20_000, inflight=inflight, name="judge")
                for server in (gen_server, judge_server):
                    self.addCleanup(server.server_close)
                    self.addCleanup(server.shutdown)
                panel = _panel_file()
                self.write(PROVIDER_FILE, _panel_file(
                    generation={**panel["generation"], "api_format": gen_format,
                                "base_url": self._url(gen_server, gen_format)},
                    judge={**panel["judge"], "api_format": judge_format,
                           "base_url": self._url(judge_server, judge_format)},
                ))
                with patch.object(content_registry, "SETTINGS_DIR", self.dir):
                    registry = build_content_registry()
                out = self.dir.parent / f"overlap-{gen_format}-{judge_format}"
                ledger = ul.UsageLedger(out / "usage")
                plane = WorldControlPlane(
                    root=out / "worlds", registry_provider=lambda: registry, usage_ledger=ledger,
                    autonomy=AutonomySettings(
                        clock=ClockConfig(rate=3000.0, interval_seconds=0.01, stop_timeout_seconds=2.0),
                        cadence=ActivationCadence(), shutdown_timeout_seconds=1.0,
                        reply_delay_minutes=None,
                    ),
                )
                client = TestClient(create_app(plane))

                def booked():
                    records, _ = ledger.read()
                    return {(r.world_id, r.path) for r in records}

                try:
                    for world_id, characters in worlds.items():
                        created = client.post("/api/persistent-worlds", json={
                            "world_id": world_id, "scene": "nightcord", "characters": characters,
                        })
                        self.assertEqual(created.status_code, 201, created.text)
                    for world_id in worlds:
                        client.post(f"/api/persistent-worlds/{world_id}/autonomy/start")
                    # 两个世界的生成和判分都进了账，并且有过一个生成请求和一个判分请求
                    # 同时在路上（同一个世界里两者是串行的，所以碰头的一定来自两个世界）。
                    wait_for(
                        lambda: inflight.overlaps & {("generation", "judge"), ("judge", "generation")}
                        and booked() >= {(w, p) for w in worlds for p in ("generation", "judge")},
                        timeout=20.0, what="两个世界都生成、判分过，且生成与判分请求重叠",
                    )
                    for world_id in worlds:
                        client.post(f"/api/persistent-worlds/{world_id}/autonomy/stop")
                finally:
                    plane.service.release_all()
                    wait_for(lambda: not (set(self.clock_threads()) - self.before), what="worker 退出")

                records, corrupt = ledger.read()
                self.assertEqual(corrupt, 0)
                # 请求是替谁发的，从提示词里固定的那一处读：生成看配套的角色宪法文件名，
                # 判分看评分参考的标题。
                speaker = {
                    "generation": re.compile(r"配套文件：`(\w+)_constitution\.md`"),
                    "judge": re.compile(r"【角色专属评分参考：(\w+)】"),
                }
                lines = {
                    "generation": (gen_format, "gen-model", gen_server.issued, gen_seen, GEN_KEY),
                    "judge": (judge_format, "judge-model", judge_server.issued, judge_seen, JUDGE_KEY),
                }
                for path, (protocol, model, issued, seen, key) in lines.items():
                    rows = [r for r in records if r.path == path]
                    self.assertEqual({(k, m) for _, k, m in seen}, {(key, model)})
                    sent = []
                    for usage, body in issued:
                        found = set(speaker[path].findall(body))
                        self.assertEqual(len(found), 1, found)
                        sent.append((usage, found.pop()))
                    # 服务器回出去的每个 usage 恰好对应一行账，记在发这个请求的角色名下：
                    # 不丢、不重、不串线、不串角色。
                    self.assertEqual(
                        sorted(((r.usage, r.character_id) for r in rows), key=repr),
                        sorted(sent, key=repr),
                    )
                    for row in rows:
                        self.assertEqual(
                            (row.protocol, row.model, row.transport, row.failure),
                            (protocol, model, ul.TRANSPORT_RESPONSE, None), row,
                        )
                        self.assertIn(row.character_id, worlds[row.world_id], row)
                self.assertEqual(len(records), sum(len(line[2]) for line in lines.values()))

    def test_a_following_judge_in_the_world_goes_to_the_generation_provider(self):
        for gen_format in ("anthropic", "openai"):
            with self.subTest(generation=gen_format):
                # _run_world 等到 message.sent 才返回：判分真的判完并通过了。
                _, rows, gen_seen, judge_seen, _ = self._run_world(gen_format, None, follow=True)
                self.assertEqual(judge_seen, [])
                self.assertEqual({(key, model) for _, key, model in gen_seen}, {(GEN_KEY, "gen-model")})
                judged = [row for row in rows if row["path"] == "judge"]
                self.assertTrue(judged)
                # 判分按生成线路的协议、模型记账，而且是成功的那种。
                self.assertTrue(all(
                    (row["protocol"], row["model"], row["failure"]) == (gen_format, "gen-model", None)
                    for row in judged
                ), judged)


if __name__ == "__main__":
    unittest.main()
