# tests/test_env_migration.py — `env` 账户落盘与旧单锚点迁移（CFG-1 §4.3、§11.14、§13 第 4 条）
#
# 守的线：
#   1. 第一次：env 落盘（created_at 有值）、旧锚点成了 env 的锚点（带 env 的
#      created_at 和币种）、标记写上、旧锚点文件还在；别的账户不受影响；
#   2. 重复跑幂等：env 的 created_at 不变、锚点文件一个字节都不变；
#   3. 操作员后来改过的 env 锚点不会被再次迁移覆盖；迁移前就有的 env 锚点也不覆盖；
#   4. 没有旧锚点：标记照写，没有 env 锚点；
#   5. 在"env 落盘之后、锚点写之前"退出：下次接着迁移，env 身份不重建；
#   6. 锚点文件读不出来：拒绝，什么都不写。
#
# 运行: python -m unittest tests.test_env_migration -v
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pns.runtime import env_migration, settings_files  # noqa: E402
from pns.runtime.env_migration import ANCHORS_FILE, MIGRATED_KEY, AnchorsUnreadable, migrate_env_account  # noqa: E402
from pns.runtime.provider_settings import ACCOUNTS_FILE, load_accounts  # noqa: E402
from pns.runtime.usage_ledger import ANCHOR_FILE, BalanceAnchor, save_anchor  # noqa: E402
from tests.test_provider_settings import _accounts_file  # noqa: E402

OLD = BalanceAnchor(123.45, datetime(2026, 10, 20, 3, tzinfo=timezone.utc))


class _Crash(BaseException):
    pass


class EnvMigrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = Path(tmp.name) / "data" / "settings"
        self.usage = Path(tmp.name) / "data" / "usage"
        self.settings.mkdir(parents=True)
        (self.settings / ACCOUNTS_FILE).write_text(json.dumps(_accounts_file()), encoding="utf-8")
        save_anchor(self.usage, OLD)

    def migrate(self):
        return migrate_env_account(self.settings, self.usage)

    def anchors_bytes(self):
        return (self.usage / ANCHORS_FILE).read_bytes()

    def env(self):
        return load_accounts(self.settings).get("env")

    def test_first_run(self):
        payload = self.migrate()
        env = self.env()
        self.assertTrue(env.created_at)
        self.assertEqual(payload[MIGRATED_KEY], True)
        self.assertEqual(payload["anchors"]["env"], {
            "balance": 123.45, "currency": "CNY", "at": OLD.to_dict()["at"],
            "account_created_at": env.created_at,
        })
        self.assertEqual(json.loads(self.anchors_bytes()), payload)
        self.assertTrue((self.usage / ANCHOR_FILE).exists())
        self.assertEqual(set(load_accounts(self.settings).accounts), {"env", "mimo-main", "ds"})

    def test_running_again_changes_nothing(self):
        self.migrate()
        created_at, before = self.env().created_at, self.anchors_bytes()
        save_anchor(self.usage, BalanceAnchor(1.0, OLD.at))  # 旧镜像回滚期间又写过旧锚点
        # created_at 只精确到秒：第二次启动换一个时刻，重建身份才看得出来。
        with patch("pns.runtime.provider_settings._now", return_value="2030-01-01T00:00:00+00:00"):
            self.migrate()
        self.assertEqual(self.env().created_at, created_at)
        self.assertEqual(self.anchors_bytes(), before)

    def test_an_env_anchor_is_never_overwritten(self):
        self.migrate()
        payload = json.loads(self.anchors_bytes())
        payload["anchors"]["env"]["balance"] = 99.0  # 操作员后来填的
        (self.usage / ANCHORS_FILE).write_text(json.dumps(payload), encoding="utf-8")
        self.migrate()
        self.assertEqual(json.loads(self.anchors_bytes())["anchors"]["env"]["balance"], 99.0)

        # 迁移之前就已经有 env 锚点、但还没有标记：保留它，补上标记。
        (self.usage / ANCHORS_FILE).write_text(json.dumps(
            {"version": 1, "anchors": {"env": {"balance": 7.0}}}), encoding="utf-8")
        payload = self.migrate()
        self.assertEqual((payload["anchors"]["env"], payload[MIGRATED_KEY]), ({"balance": 7.0}, True))

    def test_without_an_old_anchor(self):
        (self.usage / ANCHOR_FILE).unlink()
        payload = self.migrate()
        self.assertEqual((payload["anchors"], payload[MIGRATED_KEY]), ({}, True))
        # 之后旧锚点文件又出现了（回滚到旧镜像时它写的）：已经迁移过，不再搬进来。
        save_anchor(self.usage, OLD)
        self.assertEqual(self.migrate()["anchors"], {})

    def test_dying_between_the_account_and_the_anchor(self):
        real = settings_files.durable_write

        def crash_on_anchors(path, payload, **kwargs):
            if Path(path).name == ANCHORS_FILE:
                raise _Crash()
            return real(path, payload, **kwargs)

        with patch.object(settings_files, "durable_write", crash_on_anchors), self.assertRaises(_Crash):
            self.migrate()
        created_at = self.env().created_at
        self.assertTrue(created_at)
        self.assertFalse((self.usage / ANCHORS_FILE).exists())
        payload = self.migrate()
        self.assertEqual(self.env().created_at, created_at)
        self.assertEqual(payload["anchors"]["env"]["account_created_at"], created_at)

    def test_an_unreadable_anchors_file_is_left_alone(self):
        for body in ("not json", json.dumps({"version": 2, "anchors": {}}), json.dumps([])):
            with self.subTest(body=body):
                (self.usage / ANCHORS_FILE).write_text(body, encoding="utf-8")
                with self.assertRaises(AnchorsUnreadable):
                    self.migrate()
                self.assertEqual((self.usage / ANCHORS_FILE).read_text(encoding="utf-8"), body)


if __name__ == "__main__":
    unittest.main()
