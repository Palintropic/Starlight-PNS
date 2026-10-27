# tests/test_provider_txn.py — 模型设置保存的文件事务（CFG-1 §4.4、§5.4、§11.5、§11.13、§13 第 2、3 条）
#
# 守的线：
#   1. 写盘失败矩阵：先干净地跑一遍记下每一次写盘调用，再在每一次调用上分别注入
#      "没发布"和"已发布未耐久"。结局按故障落在哪一段来定：恢复材料阶段 → 没写成；
#      发布本身没发布 → 没写成、已发布未耐久 → 已回退；收尾时删 .txn 失败或旧内容
#      写不回 → 隔离；只有 .prev 删不掉 → 照常。没写成 / 已回退时磁盘和快照都是
#      旧的、准入开着、没有 .txn；隔离时准入关着，并且能用 resolve() 恢复；
#   2. 进程在每一次写盘调用前后退出：provider.json 不再是旧内容时 .txn 一定还在
#      （切换已经成功的除外），.txn 在时 .prev 的摘要与记录一致；然后用一个新边界
#      走 resolve() 能回到一致；
#   3. 切换失败（候选构建不过）走回退；切换抛异常时，快照没换 → 回退，换了 → 隔离；
#   4. 不信任读不回来的 .prev；遗留 .txn 时不叠事务；
#   5. resolve()：三种选择、.prev 对不上被拒且什么都不改、所选配置构建不过保持隔离、
#      没有可解的事务；
#   6. 生产启动发现 .txn 拒绝起来；服务器侧脚本能恢复，show 不打印 token。
#
# 运行: python -m unittest tests.test_provider_txn -v
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from pns.runtime import content_registry, provider_txn, settings_files  # noqa: E402
from pns.runtime.provider_settings import (  # noqa: E402
    ACCOUNTS_FILE,
    PROVIDER_FILE,
    SOURCE_ENV,
    SOURCE_PANEL,
)
from pns.runtime.provider_txn import (  # noqa: E402
    OUTCOME_NOT_WRITTEN,
    OUTCOME_OK,
    OUTCOME_QUARANTINED,
    OUTCOME_ROLLED_BACK,
    PREV_FILE,
    TXN_FILE,
)
from pns.runtime.reload import ConfigBoundary, SessionSupervisor  # noqa: E402
from pns.runtime.settings_files import NotPublished, PublishedNotDurable  # noqa: E402
from tests.test_provider_settings import (  # noqa: E402
    ADMIN_TOKEN,
    GEN_KEY,
    JUDGE_KEY,
    _accounts_file,
    _panel_file,
)


def _bytes(payload):
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


_GEN = _panel_file()["generation"]
A = _bytes(_panel_file())
B = _bytes(_panel_file(generation={**_GEN, "model": "gen-model-b"}))
# 账户不存在：文件能写，构建过不了。
BROKEN = _bytes(_panel_file(generation={**_GEN, "account": "nobody"}))

FAULTS = ("not_published", "not_durable")
CRASHES = ("crash_before", "crash_after")


class _Crash(BaseException):
    """模拟进程在这一刻退出：不是 Exception，谁也接不住。"""


class _Faults:
    """在 settings_files 的写盘函数上按调用序号注入故障，并记下每次调用。"""

    def __init__(self, at=None, fault=None):
        self.at, self.fault = at, fault
        self.trace = []
        self._real = {"write": settings_files.durable_write, "unlink": settings_files.durable_unlink}

    def _call(self, kind, path, *args, **kwargs):
        index = len(self.trace)
        self.trace.append((kind, Path(path).name))
        real = self._real[kind]
        if index != self.at:
            return real(path, *args, **kwargs)
        if self.fault == "not_published":
            raise NotPublished("injected")
        if self.fault == "crash_before":
            raise _Crash()
        real(path, *args, **kwargs)
        if self.fault == "not_durable":
            raise PublishedNotDurable("injected")
        raise _Crash()

    @contextlib.contextmanager
    def active(self):
        with patch.object(settings_files, "durable_write", lambda p, *a, **k: self._call("write", p, *a, **k)), \
                patch.object(settings_files, "durable_unlink", lambda p, *a, **k: self._call("unlink", p, *a, **k)):
            yield self


class TxnTestBase(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.addCleanup(self._restore_env)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.case = 0
        # env 那一套凭据由测试自己给，不读仓库里的 .env：resolve 选 absent 时会校验
        # env 派生的线路（审查 F5），不能靠本机碰巧有 key 才通过。
        no_env_file = patch.object(content_registry, "ENV_PATH", self.tmp / "no.env")
        no_env_file.start()
        self.addCleanup(no_env_file.stop)
        os.environ["MIMO_API_KEY"] = "sk-txn-test-env-0123456789abcdef"
        os.environ.pop("PNS_API_KEY_NAME", None)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)

    def fresh(self, initial):
        """新的一份 data/settings 和一个新的边界，provider.json 为 initial（None = 不存在）。"""
        self.case += 1
        self.dir = self.tmp / f"case-{self.case}" / "data" / "settings"
        self.dir.mkdir(parents=True)
        (self.dir / ACCOUNTS_FILE).write_bytes(_bytes(_accounts_file()))
        if initial is not None:
            (self.dir / PROVIDER_FILE).write_bytes(initial)
        settings_dir = patch.object(content_registry, "SETTINGS_DIR", self.dir)
        settings_dir.start()
        self.addCleanup(settings_dir.stop)
        self.supervisor = SessionSupervisor()
        self.boundary = ConfigBoundary(self.supervisor, stop_timeout=1.0)
        return self.boundary.active()

    @property
    def target(self):
        return self.dir / PROVIDER_FILE

    @property
    def txn(self):
        return self.dir / TXN_FILE

    @property
    def prev(self):
        return self.dir / PREV_FILE

    def disk(self):
        return self.target.read_bytes() if self.target.exists() else None

    def save(self, candidate, faults=None):
        faults = faults or _Faults()
        with faults.active():
            return provider_txn.save(self.boundary, self.dir, candidate, reason="测试保存")

    def assert_snapshot_is(self, registry, payload):
        """快照来自这份 provider.json（None = 不存在，env 派生）。"""
        if payload is None:
            self.assertEqual(registry.models.source, SOURCE_ENV)
        else:
            self.assertEqual(registry.models.source, SOURCE_PANEL)
            self.assertEqual(registry.models.generator_model, json.loads(payload)["generation"]["model"])

    def assert_unchanged(self, before, initial):
        self.assertIs(self.boundary.active_or_none(), before)
        self.assertEqual(self.disk(), initial)
        self.assertFalse(self.txn.exists())
        self.assertFalse(self.boundary.quarantined)
        self.assertTrue(self.supervisor.accepting)

    def assert_recoverable(self, initial, boundary):
        """隔离 / 崩溃留下的现场能恢复：有 .txn 就回到事务开始前，没有就留下现在的。

        在线隔离传本进程的边界（解除隔离），崩溃传一个新边界（重启后服务器侧恢复）。
        """
        record = json.loads(self.txn.read_bytes()) if self.txn.exists() else None
        if record is not None and record["previous"] != provider_txn.ABSENT:
            self.assertEqual(provider_txn.digest(self.prev.read_bytes()), record["previous"])
        if record is None:
            keep = provider_txn.KEEP_CURRENT  # 删 .txn 未耐久：事务其实已经结束
        elif initial is None:
            keep = provider_txn.KEEP_ABSENT
        else:
            keep = provider_txn.KEEP_PREVIOUS
        outcome = provider_txn.resolve(boundary, self.dir, keep, reason="恢复")
        self.assertEqual(outcome.status, OUTCOME_OK, outcome)
        self.assertFalse(self.txn.exists())
        self.assertFalse(boundary.quarantined)
        self.assertTrue(boundary.supervisor.accepting)
        if record is not None:
            self.assertEqual(self.disk(), initial)
        self.assert_snapshot_is(boundary.active(), self.disk())


SCENARIOS = (
    # 名字, 原来的 provider.json, 候选, 干净跑一遍的结局
    ("PUT 覆盖", A, B, OUTCOME_OK),
    ("PUT 新建", None, B, OUTCOME_OK),
    ("DELETE", A, None, OUTCOME_OK),
    ("PUT 构建不过", A, BROKEN, OUTCOME_ROLLED_BACK),
)


class FaultMatrixTests(TxnTestBase):
    def clean_trace(self, initial, candidate, expected):
        self.fresh(initial)
        faults = _Faults()
        outcome = self.save(candidate, faults)
        self.assertEqual(outcome.status, expected)
        return faults.trace

    def expected(self, trace, index, fault, clean):
        kind, name = trace[index]
        publish = next(i for i, (_, n) in enumerate(trace) if n == PROVIDER_FILE)
        if index < publish:
            return OUTCOME_NOT_WRITTEN
        if index == publish:
            return OUTCOME_NOT_WRITTEN if fault == "not_published" else OUTCOME_ROLLED_BACK
        if name == PREV_FILE:
            return clean
        return OUTCOME_QUARANTINED  # 删 .txn 失败，或回退时旧内容写不回

    def test_every_write_failing_either_way(self):
        for name, initial, candidate, clean in SCENARIOS:
            trace = self.clean_trace(initial, candidate, clean)
            for index in range(len(trace)):
                for fault in FAULTS:
                    with self.subTest(name, at=trace[index], index=index, fault=fault):
                        before = self.fresh(initial)
                        outcome = self.save(candidate, _Faults(index, fault))
                        want = self.expected(trace, index, fault, clean)
                        self.assertEqual(outcome.status, want)
                        if want == OUTCOME_OK:
                            self.assertIsNot(self.boundary.active_or_none(), before)
                            self.assertEqual(self.disk(), candidate)
                            self.assertFalse(self.txn.exists())
                            self.assertTrue(self.supervisor.accepting)
                            self.assert_snapshot_is(self.boundary.active(), candidate)
                        elif want in (OUTCOME_NOT_WRITTEN, OUTCOME_ROLLED_BACK):
                            self.assert_unchanged(before, initial)
                        else:
                            self.assertTrue(self.boundary.quarantined)
                            self.assertFalse(self.supervisor.accepting)
                            self.assert_recoverable(initial, self.boundary)
                        # 只有 .prev 那一次删除失败时允许留下 .prev。
                        if self.prev.exists() and not self.boundary.quarantined:
                            self.assertEqual(trace[index], ("unlink", PREV_FILE))

    def test_the_process_dying_around_every_write(self):
        for name, initial, candidate, clean in SCENARIOS:
            trace = self.clean_trace(initial, candidate, clean)
            for index in range(len(trace)):
                for crash in CRASHES:
                    with self.subTest(name, at=trace[index], index=index, crash=crash):
                        before = self.fresh(initial)
                        with self.assertRaises(_Crash):
                            self.save(candidate, _Faults(index, crash))
                        swapped = self.boundary.active_or_none() is not before
                        if self.disk() != initial and not swapped:
                            self.assertTrue(self.txn.exists(), "provider.json 变了却没有 .txn")
                        self.assertEqual(provider_txn.pending(self.dir), self.txn.exists())
                        if self.txn.exists():
                            self.assert_recoverable(initial, ConfigBoundary(SessionSupervisor(), stop_timeout=1.0))
                        else:
                            self.assertIn(self.disk(), (initial, candidate))


class SwapAndMaterialTests(TxnTestBase):
    def test_a_swap_that_raises_before_swapping_rolls_back(self):
        before = self.fresh(A)
        with patch.object(ConfigBoundary, "_swap_locked", side_effect=RuntimeError("x")):
            self.assertEqual(self.save(B).status, OUTCOME_ROLLED_BACK)
        self.assert_unchanged(before, A)

    def test_a_swap_that_raises_after_swapping_quarantines(self):
        self.fresh(A)
        real = ConfigBoundary._swap_locked

        def swap_then_raise(boundary, reason):
            real(boundary, reason)
            raise RuntimeError("拿到快照之后炸了")

        with patch.object(ConfigBoundary, "_swap_locked", swap_then_raise):
            self.assertEqual(self.save(B).status, OUTCOME_QUARANTINED)
        self.assertTrue(self.boundary.quarantined)
        self.assertTrue(self.txn.exists())

    def test_a_prev_that_does_not_read_back_is_not_trusted(self):
        before = self.fresh(A)
        self.prev.write_bytes(b"stale")
        real = settings_files.durable_write

        def lying_write(path, payload, **kwargs):
            if Path(path).name == PREV_FILE:
                return True  # 说写了，其实没写
            return real(path, payload, **kwargs)

        with patch.object(settings_files, "durable_write", lying_write):
            self.assertEqual(provider_txn.save(self.boundary, self.dir, B, reason="t").status, OUTCOME_NOT_WRITTEN)
        self.assert_unchanged(before, A)

    def test_a_leftover_txn_is_never_stacked_on(self):
        before = self.fresh(A)
        self.txn.write_bytes(b"{}")
        self.assertEqual(self.save(B).status, OUTCOME_QUARANTINED)
        self.assertIs(self.boundary.active_or_none(), before)
        self.assertEqual(self.disk(), A)
        self.assertEqual(self.txn.read_bytes(), b"{}")


class ResolveTests(TxnTestBase):
    def quarantined_after_put(self, initial):
        """PUT 走到"删 .txn 失败"：快照已是 B、磁盘是 B、.txn 和 .prev 都在、进程隔离。"""
        self.fresh(initial)
        faults = _Faults()
        self.save(B, faults)  # 干净跑一遍拿 trace
        index = faults.trace.index(("unlink", TXN_FILE))
        self.fresh(initial)
        self.assertEqual(self.save(B, _Faults(index, "not_published")).status, OUTCOME_QUARANTINED)

    def resolve(self, keep):
        return provider_txn.resolve(self.boundary, self.dir, keep, reason="解除隔离")

    def test_each_choice_ends_consistent(self):
        for keep, initial, want in (
            (provider_txn.KEEP_PREVIOUS, A, A),
            (provider_txn.KEEP_CURRENT, A, B),
            (provider_txn.KEEP_ABSENT, A, None),
            (provider_txn.KEEP_ABSENT, None, None),
        ):
            with self.subTest(keep, initial=initial is not None):
                self.quarantined_after_put(initial)
                self.assertEqual(self.resolve(keep).status, OUTCOME_OK)
                self.assertEqual(self.disk(), want)
                self.assertFalse(self.txn.exists())
                self.assertFalse(self.prev.exists())
                self.assertFalse(self.boundary.quarantined)
                self.assertTrue(self.supervisor.accepting)
                self.assert_snapshot_is(self.boundary.active(), want)

    def test_a_prev_that_does_not_match_is_refused_and_changes_nothing(self):
        for damage in ("改了", "删了", "原来不存在"):
            with self.subTest(damage):
                self.quarantined_after_put(None if damage == "原来不存在" else A)
                if damage == "改了":
                    self.prev.write_bytes(b"tampered")
                elif damage == "删了":
                    self.prev.unlink()
                snapshot = self.boundary.active_or_none()
                with self.assertRaises(provider_txn.ResolveRefused):
                    self.resolve(provider_txn.KEEP_PREVIOUS)
                self.assertEqual(self.disk(), B)
                self.assertTrue(self.txn.exists())
                self.assertTrue(self.boundary.quarantined)
                self.assertFalse(self.supervisor.accepting)
                self.assertIs(self.boundary.active_or_none(), snapshot)

    def test_a_choice_that_does_not_build_stays_quarantined(self):
        self.quarantined_after_put(A)
        self.target.write_bytes(BROKEN)
        outcome = self.resolve(provider_txn.KEEP_CURRENT)
        self.assertEqual(outcome.status, OUTCOME_QUARANTINED)
        self.assertTrue(self.txn.exists())
        self.assertTrue(self.boundary.quarantined)
        self.assertFalse(self.supervisor.accepting)
        # 换一个能用的选择就能解开。
        self.assertEqual(self.resolve(provider_txn.KEEP_PREVIOUS).status, OUTCOME_OK)
        self.assertEqual(self.disk(), A)

    def test_a_choice_whose_lines_are_unusable_stays_quarantined(self):
        # 审查 F5：选"恢复为不存在"时 env 里没有 key。构建照样成功（开发环境允许缺
        # key），但线路不可用，不能清掉事务、解除隔离。
        self.quarantined_after_put(A)
        key_name = self.boundary.active().models.env_key_name
        os.environ.pop(key_name, None)
        outcome = self.resolve(provider_txn.KEEP_ABSENT)
        self.assertEqual(outcome.status, OUTCOME_QUARANTINED)
        self.assertTrue(self.txn.exists())
        self.assertTrue(self.boundary.quarantined)
        self.assertFalse(self.supervisor.accepting)
        # 补上 key 再选一次就能解开。
        os.environ[key_name] = "sk-resolve-test-0123456789abcdef"
        self.assertEqual(self.resolve(provider_txn.KEEP_ABSENT).status, OUTCOME_OK)
        self.assertFalse(self.txn.exists())
        self.assertFalse(self.boundary.quarantined)

    def test_a_cleanup_failure_while_resolving_stays_quarantined(self):
        self.quarantined_after_put(A)
        with _Faults(1, "not_published").active():  # 0 = 写回 .prev 的内容，1 = 删 .txn
            outcome = self.resolve(provider_txn.KEEP_PREVIOUS)
        self.assertEqual(outcome.status, OUTCOME_QUARANTINED)
        self.assertTrue(self.boundary.quarantined)
        self.assertTrue(self.txn.exists())

    def test_nothing_to_resolve(self):
        self.fresh(A)
        with self.assertRaises(provider_txn.NothingToResolve):
            self.resolve(provider_txn.KEEP_CURRENT)
        self.assertTrue(self.supervisor.accepting)


class StartupAndScriptTests(TxnTestBase):
    def leave_a_transaction(self):
        self.fresh(A)
        faults = _Faults()
        self.save(B, faults)
        self.fresh(A)
        index = faults.trace.index(("write", PROVIDER_FILE))
        with self.assertRaises(_Crash):
            self.save(B, _Faults(index, "crash_after"))
        self.assertTrue(self.txn.exists())

    def test_production_refuses_to_start_with_a_transaction_on_disk(self):
        from pns.interfaces.app import _verify_production_config
        from pns.interfaces.security import DeploymentConfigError, DeploymentSettings

        self.leave_a_transaction()
        (self.tmp / "dist").mkdir()
        with self.assertRaises(DeploymentConfigError) as caught:
            _verify_production_config(
                DeploymentSettings(mode="production", admin_token=ADMIN_TOKEN),
                self.tmp / "dist",
                lambda: self.fail("有 .txn 时不该去构建配置"),
            )
        self.assertEqual(str(caught.exception), provider_txn.STARTUP_REFUSAL)

    def test_the_server_side_script_resolves_and_never_prints_tokens(self):
        import provider_resolve

        self.leave_a_transaction()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(provider_resolve.main(["show"]), 0)
            self.assertEqual(provider_resolve.main(["resolve", "--keep", "previous"]), 0)
            self.assertEqual(provider_resolve.main(["resolve", "--keep", "previous"]), 2)
        self.assertEqual(self.disk(), A)
        self.assertFalse(self.txn.exists())
        text = out.getvalue() + err.getvalue()
        self.assertIn(provider_txn.digest(A), text)
        self.assertNotIn(GEN_KEY, text)
        self.assertNotIn(JUDGE_KEY, text)


if __name__ == "__main__":
    unittest.main()
