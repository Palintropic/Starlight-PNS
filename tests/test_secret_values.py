"""F4：卡片凭据从设置入口到流 / reload 状态出口的故障注入。"""
import io
import os
import subprocess
import sys
import threading
import unittest
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from unittest.mock import patch

from tests import test_provider_api as provider_tests
from pns.interfaces import accord, redaction
from pns.integrations.accord.feed import FeedSession
from pns.runtime import secret_values
from pns.runtime.content_registry import ConfigValidationError

TOKEN = "f4-panel-token-0123456789abcdef"
REPLACEMENT = "f4-replacement-token-fedcba9876543210"


def isolate_values(test):
    for name, value in (
        ("_values", OrderedDict()),
        ("process_redactor", redaction.SecretRedactor(())),
    ):
        guard = patch.object(secret_values, name, value)
        guard.start()
        test.addCleanup(guard.stop)


class SecretValueTests(unittest.TestCase):
    def setUp(self):
        isolate_values(self)

    def test_capacity_evicts_least_recently_registered_value(self):
        values = [f"f4-capacity-{i:04d}" for i in range(secret_values.MAX_SECRET_VALUES + 1)]
        secret_values.register_values(values[:-1])
        # values[0] 再次登记（比如它仍是现役卡片、又读了一次设置），
        # 新值进来时被挤掉的应当是 values[1]，不是还在用的 values[0]。
        secret_values.register_values([values[0], values[-1]])
        snapshot = secret_values.secret_values_snapshot()
        self.assertEqual(len(snapshot), secret_values.MAX_SECRET_VALUES)
        self.assertIn(values[0], snapshot)
        self.assertNotIn(values[1], snapshot)
        self.assertEqual(snapshot, tuple(values[2:-1]) + (values[0], values[-1]))

    def test_active_token_survives_long_rotation_of_other_cards(self):
        active = "f4-active-card-token"
        for i in range(secret_values.MAX_SECRET_VALUES * 2):
            # 模拟每次读设置：现役卡片和一张不断换 token 的卡片一起登记。
            secret_values.register_values([active, f"f4-rotating-{i:04d}"])
        self.assertIn(active, secret_values.secret_values_snapshot())

    def test_short_values_and_longest_match_follow_the_env_rules(self):
        short = "s" * (redaction.MIN_REDACTABLE_CHARS - 1)
        prefix = "x" * redaction.MIN_REDACTABLE_CHARS
        secret_values.register_values([short, prefix + "-long", "  " + prefix + "  "])
        redactor = redaction.SecretRedactor(["KEY"], env={"KEY": prefix})
        self.assertEqual(redactor.apply(short), short)
        self.assertEqual(redactor.apply(prefix + "-long"), redaction.MASK)
        self.assertEqual(redactor.secrets(), [prefix + "-long", prefix])

    def test_snapshots_remain_independent_during_concurrent_registration(self):
        secret_values.register_values([TOKEN])
        redactor = redaction.SecretRedactor(())
        old = redactor.secrets()
        start = threading.Barrier(4)

        def writer(index):
            start.wait(timeout=5)
            for number in range(300):
                secret_values.register_values([f"f4-thread-{index}-{number:04d}"])

        def reader():
            start.wait(timeout=5)
            for _ in range(600):
                values = redactor.secrets()
                self.assertLessEqual(len(values), secret_values.MAX_SECRET_VALUES)
                self.assertEqual(len(values), len(set(values)))
                redactor.apply("ordinary log line")

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(writer, i) for i in range(3)] + [pool.submit(reader)]
            for future in futures:
                future.result(timeout=10)
        self.assertEqual(old, [TOKEN])
        old.clear()
        self.assertTrue(redactor.secrets())

    def test_import_is_inert_and_does_not_load_interfaces(self):
        result = subprocess.run(
            [sys.executable, "-c", """
import sys
before = (sys.stdout, sys.stderr)
from pns.runtime import secret_values
assert (sys.stdout, sys.stderr) == before
assert secret_values.secret_values_snapshot() == ()
assert not any(name.startswith('pns.interfaces') for name in sys.modules)
assert 'pns.runtime.reload' not in sys.modules
assert 'pns.runtime.provider_settings' not in sys.modules
"""], cwd=provider_tests.REPO_ROOT, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


class CardSecretEgressTests(provider_tests.ProviderApiTestBase):
    def setUp(self):
        isolate_values(self)
        flag = patch.dict(os.environ, {accord.ENV_ACCORD_FEED: "1"})
        flag.start()
        self.addCleanup(flag.stop)
        super().setUp()
        # 流先安装、卡片后保存：验证 redactor 每次输出取新的登记快照。
        self.sink = io.StringIO()
        self.stream = redaction.RedactingStream(self.sink, redaction.SecretRedactor(()))

    def assert_masked_failure(self, tag, token=TOKEN):
        output = self.sink.getvalue()
        self.assertNotIn(token, output)
        self.assertIn(redaction.MASK, output)
        self.assertIn(tag, output)
        self.assertIn("RuntimeError: synthetic upstream failure", output)

    def persistent_failure(self, token=TOKEN):
        with redirect_stdout(self.stream), patch.object(
            self.plane, "create", side_effect=RuntimeError(f"synthetic upstream failure {token}")
        ):
            response = self.client.post("/api/persistent-worlds", json=provider_tests.LEGACY)
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json()["detail"]["category"], "internal_error")
        self.assertNotIn(token, response.text)

    def accord_failure(self, token=TOKEN):
        response = self.client.post("/api/persistent-worlds", json=provider_tests.LEGACY)
        self.assertEqual(response.status_code, 201, response.text)
        with redirect_stdout(self.stream), patch.object(
            FeedSession, "poll", side_effect=RuntimeError(f"synthetic upstream failure {token}")
        ) as poll:
            response = self.client.get("/api/accord/worlds/nightcord/events")
        self.assertEqual(response.status_code, 200, response.text)
        poll.assert_called_once()
        self.assertNotIn(token, response.text)

    def test_http_saved_spare_token_is_masked_in_persistent_worlds_failure(self):
        card = self.add(provider_tests._card(token=TOKEN))
        self.assertNotEqual(self.boundary.active().models.generation.card, card)
        self.persistent_failure()
        self.assert_masked_failure("[persistent-worlds]")

    def test_http_saved_spare_token_is_masked_in_accord_failure(self):
        self.add(provider_tests._card(token=TOKEN))
        self.accord_failure()
        self.assert_masked_failure("[accord]")

    def test_replaced_token_still_masks_both_failure_paths(self):
        card = self.add(provider_tests._card(token=TOKEN))
        response = self.client.put(
            f"{provider_tests.CARDS}/{card}", json=provider_tests._card(token=REPLACEMENT)
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(TOKEN, self.file.read_text())
        self.persistent_failure()
        self.assert_masked_failure("[persistent-worlds]")
        self.sink.seek(0)
        self.sink.truncate()
        self.accord_failure()
        self.assert_masked_failure("[accord]")

    def test_deleted_token_stays_masked(self):
        card = self.add(provider_tests._card(token=TOKEN))
        response = self.client.delete(f"{provider_tests.CARDS}/{card}")
        self.assertEqual(response.status_code, 200, response.text)
        self.persistent_failure()
        self.assert_masked_failure("[persistent-worlds]")

    def test_reload_masks_build_exceptions_before_storing_status(self):
        self.add(provider_tests._card(token=TOKEN))
        for exception in (RuntimeError, ConfigValidationError):
            with self.subTest(exception=exception), patch(
                "pns.runtime.reload.build_content_registry",
                side_effect=exception(f"synthetic upstream failure {TOKEN}"),
            ):
                result = self.boundary.reload()
            self.assertEqual(result.status, "failed")
            self.assertNotIn(TOKEN, result.error)
            self.assertIn(redaction.MASK, result.error)
            stored = self.boundary.status()["last_reload"]["error"]
            self.assertEqual(stored, result.error)

    def test_reload_uses_the_stream_redactors_current_env_values_too(self):
        with patch.dict(os.environ, {"F4_ENV_KEY": TOKEN}):
            redactor = redaction.install(["F4_ENV_KEY"], streams=[])
            with patch("pns.runtime.reload.build_content_registry", side_effect=RuntimeError(TOKEN)):
                result = self.boundary.reload()
        self.assertIs(secret_values.process_redactor, redactor)
        self.assertEqual(result.error, "RuntimeError: " + redaction.MASK)

    def test_new_process_reads_all_disk_tokens_without_saving(self):
        self.add(provider_tests._card(token=TOKEN))
        self.add(provider_tests._card(token=REPLACEMENT))
        before = self.file.read_bytes()
        result = subprocess.run(
            [sys.executable, "-c", """
import sys
from pathlib import Path
from pns.runtime.provider_settings import load_accounts, read_document
from pns.runtime.secret_values import SecretRedactor, secret_values_snapshot
assert secret_values_snapshot() == ()
redactor = SecretRedactor(())
directory = Path(sys.argv[1])
document = read_document(directory, load_accounts(directory))
for card in document.cards.values():
    print(redactor.apply('disk exception: ' + card.token.reveal()))
""", str(self.dir)], cwd=provider_tests.REPO_ROOT,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for token in (TOKEN, REPLACEMENT):
            self.assertNotIn(token, result.stdout)
        self.assertEqual(result.stdout.count(redaction.MASK), 2)
        self.assertEqual(self.file.read_bytes(), before)
