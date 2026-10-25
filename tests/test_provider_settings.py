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
#   7. 生产启动：面板两条线路 token 齐全时不再要求 env 的旧 key。
#
# 运行: python -m unittest tests.test_provider_settings -v
import copy
import json
import os
import pickle
import stat
import sys
import tempfile
import unittest
from dataclasses import replace
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
from pns.interfaces.composition import WorldControlPlane  # noqa: E402
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
from pns.runtime.session_runtime import SessionRuntime  # noqa: E402

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
        with self.assertRaises(ProviderSettingsError):
            self.load({"API_FORMAT": "grpc"})


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
        # U+2100 在 NFKC 下变出 "/"，urlsplit 的 ValueError 会带上整段 netloc。
        bad = f"https://u:{GEN_KEY}@bad\u2100.example/v1"
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
        with patch.object(settings_files, "sync_dir", side_effect=OSError(5, "EIO")):
            with self.assertRaises(settings_files.PublishedNotDurable):
                settings_files.durable_write(path, b"new")
        self.assertEqual(path.read_bytes(), b"new")

    def test_new_directories_are_synced_into_their_parents(self):
        path = self.dir / "a" / "b" / "x.json"
        with patch.object(settings_files, "sync_dir", wraps=settings_files.sync_dir) as sync:
            settings_files.durable_write(path, b"{}")
        synced = {Path(call.args[0]) for call in sync.call_args_list}
        self.assertEqual(synced, {self.dir, self.dir / "a", self.dir / "a" / "b"})

    def test_an_unsynced_new_directory_is_not_published(self):
        path = self.dir / "new" / "x.json"
        with patch.object(settings_files, "sync_dir", side_effect=OSError(5, "EIO")):
            with self.assertRaises(settings_files.NotPublished):
                settings_files.durable_write(path, b"{}")
        self.assertFalse(path.exists())

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


if __name__ == "__main__":
    unittest.main()
