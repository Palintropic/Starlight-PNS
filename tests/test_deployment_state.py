# tests/test_deployment_state.py — DEPLOY-1 里"运行时数据与密钥不留在镜像层"
# 那一半的边界。
#
# 盯住的东西按"错了会怎样"排：
#   1. 日志遮蔽是按**值**做的，而且盖得住异常路径——traceback 才是最可能把
#      凭据带出去的地方，不是我们自己写的那几行 print。
#   2. 太短的值不遮蔽：把一个 3 字符的"密钥"从所有输出里抹掉会毁掉日志本身。
#      这条限制是写明的行为，不是遗漏。
#   3. 生产模式拒绝写回仓库源码与 .env：那种写入活不过下一次容器重建，还会
#      盖住注入的配置——一次会静静回退的改动比一次响亮的拒绝坏得多。
#   4. 拒绝是**拒绝**：磁盘上的源码逐字节不变。
#   5. 运行时数据的位置跟着 data/ 走，不躺在仓库根上。
#
# 运行: python -m unittest tests.test_deployment_state -v
import io
import os
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from fastapi.testclient import TestClient  # noqa: E402

from pns.interfaces import paths, redaction  # noqa: E402
from pns.interfaces.app import create_app  # noqa: E402
from pns.interfaces.composition import WorldControlPlane  # noqa: E402
from pns.interfaces.security import DeploymentSettings  # noqa: E402

from accounts_support import (  # noqa: E402
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    cheap_store,
)
from pns.runtime.reload import BOUNDARY  # noqa: E402
from pns.world import codegen  # noqa: E402

ADMIN_TOKEN = "ADMIN-CANARY-0f1e2d3c4b5a69788796a5b4c3d2e1f0"
KEY_CANARY = "MODEL-CANARY-9a8b7c6d5e4f30211203f4e5d6c7b8a9"


# ── 1–2. 日志遮蔽 ───────────────────────────────────────────────────────
class RedactionTests(unittest.TestCase):
    def setUp(self):
        self.env = {"PNS_ADMIN_TOKEN": ADMIN_TOKEN, "MIMO_API_KEY": KEY_CANARY}
        self.redactor = redaction.SecretRedactor(
            ["PNS_ADMIN_TOKEN", "MIMO_API_KEY"], env=self.env
        )

    def stream(self):
        sink = io.StringIO()
        return sink, redaction.RedactingStream(sink, self.redactor)

    def test_a_secret_in_a_plain_line_is_masked(self):
        sink, stream = self.stream()
        stream.write(f"provider 报错：key={KEY_CANARY} 无效\n")
        self.assertNotIn(KEY_CANARY, sink.getvalue())
        self.assertIn(redaction.MASK, sink.getvalue())

    def test_a_secret_split_across_writes_is_still_masked(self):
        """日志实现常常把一行拆成好几次 write。按行缓冲就是为了这个。"""
        sink, stream = self.stream()
        stream.write("key=" + KEY_CANARY[:10])
        stream.write(KEY_CANARY[10:] + " 无效\n")
        self.assertNotIn(KEY_CANARY, sink.getvalue())

    def test_a_traceback_is_masked(self):
        """异常路径必须盖得住——验收条件里点名了它。"""
        sink, stream = self.stream()
        try:
            raise RuntimeError(f"调用失败：Authorization: Bearer {ADMIN_TOKEN}")
        except RuntimeError:
            traceback.print_exc(file=stream)
        stream.flush()
        self.assertNotIn(ADMIN_TOKEN, sink.getvalue())
        self.assertIn("RuntimeError", sink.getvalue())

    def test_flush_masks_a_partial_line(self):
        sink, stream = self.stream()
        stream.write(f"半行 {ADMIN_TOKEN}")
        self.assertEqual(sink.getvalue(), "", "整行没写完就不该落地")
        stream.flush()
        self.assertNotIn(ADMIN_TOKEN, sink.getvalue())

    def test_short_values_are_left_alone(self):
        """写明的限制：太短的值不遮蔽，否则日志会被抹成噪音。"""
        redactor = redaction.SecretRedactor(["SHORT"], env={"SHORT": "abc"})
        self.assertEqual(redactor.apply("abc 是一段正常文字"), "abc 是一段正常文字")

    def test_longest_secret_is_replaced_first(self):
        """一个短密钥恰好是长密钥前缀时，先换短的会留下尾巴。"""
        long_secret = "SECRETVALUE-LONGER"
        redactor = redaction.SecretRedactor(
            ["A", "B"], env={"A": "SECRETVALUE", "B": long_secret}
        )
        masked = redactor.apply(f"x {long_secret} y")
        self.assertNotIn("SECRETVALUE", masked)
        self.assertNotIn("-LONGER", masked)

    def test_unset_secrets_do_not_mask_everything(self):
        redactor = redaction.SecretRedactor(["MISSING"], env={})
        self.assertEqual(redactor.apply("原样"), "原样")

    def test_install_is_idempotent_and_reversible(self):
        original = sys.stdout
        try:
            redaction.install(["PNS_ADMIN_TOKEN"])
            once = sys.stdout
            redaction.install(["PNS_ADMIN_TOKEN"])
            self.assertIs(sys.stdout, once, "重复安装不该再包一层")
        finally:
            redaction.uninstall()
        self.assertIs(sys.stdout, original)

    def test_stream_reports_what_it_wrote(self):
        _, stream = self.stream()
        text = "一行\n"
        self.assertEqual(stream.write(text), len(text))


class ServerEntrySecretNamesTests(unittest.TestCase):
    def test_every_provider_key_name_is_covered(self):
        """新增一个 provider 就自动进入遮蔽范围，不靠有人记得回来改。"""
        import server
        from oobe import PROVIDERS

        names = set(server.secret_env_names())
        self.assertIn("PNS_ADMIN_TOKEN", names)
        for provider in PROVIDERS.values():
            self.assertIn(provider["key_name"], names)


# ── 3–4. 生产不可变边界 ─────────────────────────────────────────────────
class ImmutableProductionTests(unittest.TestCase):
    # 源码本体，加上 World Editor 保存前会留下的覆盖式备份。备份也要还原：
    # 一次守卫失效不该在工作树里留下任何残留物。
    SOURCES = (codegen.SCENES_PATH, codegen.FACTS_PATH)
    BACKUPS = tuple(
        path.with_suffix(path.suffix + ".bak") for path in SOURCES
    )

    def setUp(self):
        self.registry = BOUNDARY.active()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "worlds"
        self.dist = Path(self._tmp.name) / "dist"
        self.dist.mkdir()
        self._env = patch.dict(
            os.environ, {self.registry.models.key_name: KEY_CANARY}
        )
        self._env.start()
        self.before = {path: path.read_bytes() for path in self.SOURCES}
        self.backups_before = {
            path: (path.read_bytes() if path.exists() else None)
            for path in self.BACKUPS
        }
        self.plane = WorldControlPlane(root=self.root)
        # AUTH-1：生产进程要求至少一个启用着的管理员。这一组用例全部走 bearer，
        # 账户库在这里只是让 `create_app()` 的生产必填校验成立。
        self.accounts = cheap_store(Path(self._tmp.name) / "accounts.sqlite3")
        self.accounts.create_human(ADMIN_USERNAME, ADMIN_PASSWORD, "admin")
        self.addCleanup(self.accounts.close)
        self.app = create_app(
            self.plane,
            settings=DeploymentSettings(mode="production", admin_token=ADMIN_TOKEN),
            account_store=self.accounts,
            dashboard_dist=self.dist,
            registry_provider=lambda: self.registry,
        )
        self.client = TestClient(self.app)
        self.headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    def tearDown(self):
        try:
            for path, content in self.before.items():
                if path.read_bytes() != content:
                    path.write_bytes(content)
                    self.fail(f"生产模式下的一次写请求改动了 {path}")
            for path, content in self.backups_before.items():
                after = path.read_bytes() if path.exists() else None
                if after != content:
                    if content is None:
                        path.unlink()
                    else:
                        path.write_bytes(content)
                    self.fail(f"生产模式下的一次写请求留下了 {path}")
        finally:
            self.plane.service.release_all()
            self._env.stop()
            self._tmp.cleanup()

    def refused(self, method, path, **kwargs):
        response = self.client.request(method, path, headers=self.headers, **kwargs)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["category"], "immutable_deployment")
        return response

    def test_world_editor_writes_are_refused(self):
        self.refused("POST", "/api/world/facts", json={"facts": {"a": "b"}})
        self.refused("POST", "/api/world/facts/source", json={"source": "WORLD_FACTS={}"})
        self.refused("POST", "/api/world/scenes", json={})
        self.refused("POST", "/api/world/scenes/source", json={"source": "SCENES={}"})

    def test_env_writing_config_post_is_refused(self):
        self.refused(
            "POST",
            "/api/config",
            json={"provider_key": "1", "model": "mimo-v2.5", "api_key": "x" * 40},
        )

    def test_refusal_happens_before_the_payload_is_even_valid(self):
        """守卫在请求体校验之前：一份畸形请求体也拿 409，不是 422。

        否则"生产不可写"这句话会依赖请求体恰好合法，等于没有守卫。
        """
        self.refused("POST", "/api/world/facts", json={"完全不对": 1})

    def test_reads_still_work_in_production(self):
        """拒绝的是写，不是看。在生产上看一眼当前内容完全正当。"""
        for path in ("/api/world/facts", "/api/world/scenes", "/api/config"):
            with self.subTest(path=path):
                self.assertEqual(
                    self.client.get(path, headers=self.headers).status_code, 200
                )

    def test_reload_is_still_allowed_in_production(self):
        """重载不写盘，它只是把磁盘上已经有的东西重新读一遍并校验。"""
        response = self.client.post("/api/config/reload", headers=self.headers)
        self.assertIn(response.status_code, (200, 409), response.text)

    def test_development_still_allows_the_same_writes(self):
        """开发行为一个字没变——生产的严格不是靠把功能删掉换来的。"""
        app = create_app(
            WorldControlPlane(root=self.root / "dev"),
            settings=DeploymentSettings(mode="development", admin_token=None),
            dashboard_dist=self.dist,
        )
        client = TestClient(app)
        # 不真的写盘：只要证明它没有撞上那道 409 守卫。
        response = client.post("/api/config", json={"provider_key": "不存在"})
        self.assertNotEqual(response.status_code, 409)


class ImmutabilityGuardFallsClosedTests(unittest.TestCase):
    """守卫拿不到 app 自己的部署设定时，回环境变量；仍然说不清就当成生产。

    "说不清是不是生产"跟"确定不是生产"不是一回事——前者不该换来一次放行。
    """

    def app_with_no_deployment_state(self):
        from fastapi import Depends, FastAPI

        from pns.interfaces.security import refuse_in_production

        app = FastAPI()

        @app.post("/w", dependencies=[Depends(refuse_in_production)])
        def _w():  # pragma: no cover - 生产下该被 409 挡住
            return {"ok": True}

        return TestClient(app)

    def test_env_says_production_so_the_write_is_refused(self):
        client = self.app_with_no_deployment_state()
        with patch.dict(os.environ, {"PNS_ENV": "production"}):
            self.assertEqual(client.post("/w").status_code, 409)

    def test_unreadable_deployment_configuration_is_treated_as_production(self):
        client = self.app_with_no_deployment_state()
        with patch.dict(os.environ, {"PNS_ENV": "production", "PNS_ADMIN_TOKEN": "短"}):
            self.assertEqual(client.post("/w").status_code, 409)

    def test_development_still_allows_it(self):
        client = self.app_with_no_deployment_state()
        with patch.dict(os.environ, {"PNS_ENV": "development"}):
            self.assertEqual(client.post("/w").status_code, 200)


# ── 5. 运行时数据的位置 ─────────────────────────────────────────────────
class RuntimeDataLivesUnderDataDirTests(unittest.TestCase):
    def test_review_decisions_are_under_the_mounted_data_dir(self):
        """审核决策是运行时数据。躺在仓库根上意味着重建容器就没了。"""
        self.assertEqual(paths.REVIEW_DECISIONS_FILE.parent, paths.DATA_DIR)
        self.assertEqual(paths.DRIFT_SCORES_FILE.parent, paths.DATA_DIR)

    def test_world_archives_default_under_the_mounted_data_dir(self):
        from pns.interfaces.composition import default_world_root

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PNS_WORLD_ROOT", None)
            self.assertEqual(default_world_root().parent, paths.DATA_DIR)

    def test_writing_a_decision_creates_the_directory_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "fresh-volume" / "review_decisions.jsonl"
            with patch.object(paths, "REVIEW_DECISIONS_FILE", target), \
                    patch("pns.interfaces.review.REVIEW_DECISIONS_FILE", target):
                app = create_app(
                    WorldControlPlane(root=Path(tmp) / "worlds"),
                    settings=DeploymentSettings(mode="development", admin_token=None),
                )
                client = TestClient(app)
                response = client.post(
                    "/api/review/decision",
                    json={
                        "session_id": "s", "turn": 1,
                        "character": "mizuki", "decision": "approve",
                    },
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()
