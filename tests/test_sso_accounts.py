"""SSO identity authority, concurrency and rollback counterexamples (§4)."""

import concurrent.futures
import sqlite3
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from accounts_support import cheap_store, ADMIN_PASSWORD
from pns.interfaces.accounts import (
    AccountConflict,
    AccountDisabled,
    LastAdminError,
    SsoManagedError,
)

ISSUER = "https://id.example/"
SUB = "12345678-abcd-4000-8000-000000000001"


class SsoAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "accounts.db"
        self.store = cheap_store(self.path)
        self.addCleanup(self.store.close)
        self.admin = self.store.create_human("local-admin", ADMIN_PASSWORD, "admin")

    def link(self, role="admin", **kw):
        return self.store.link_or_sync_sso(ISSUER, SUB, role, **kw)

    def test_link_sync_metadata_and_revision(self):
        p, first = self.link(email="display@example.com", display_name="Display")
        self.assertTrue(first)
        self.assertEqual(p.username, "sso-12345678abcd")
        self.assertEqual(
            self.store.sso_identity(p.principal_id)["email"], "display@example.com"
        )
        p2, first = self.link("observer")
        self.assertFalse(first)
        self.assertEqual(p2.principal_id, p.principal_id)
        self.assertEqual(p2.security_revision, p.security_revision + 1)
        p3, _ = self.link("observer")
        self.assertEqual(p3.security_revision, p2.security_revision)
        actions = [r["action"] for r in self.store.audit_records()]
        self.assertEqual(actions.count("sso.link"), 1)
        self.assertEqual(actions.count("sso.role_sync"), 1)
        self.assertNotIn(SUB, str(self.store.audit_records()))
        self.assertNotIn("display@example.com", str(self.store.audit_records()))

    def test_local_floor_excludes_sso_even_with_rollback_password(self):
        p, _ = self.link()
        self.rollback_sql(
            "UPDATE principals SET password_hash=? WHERE principal_id=?",
            (self.store.hash_password(ADMIN_PASSWORD), p.principal_id),
        )
        self.assertEqual(self.store.enabled_admin_count(), 1)
        for change in ({"enabled": False}, {"role": "observer"}):
            with self.assertRaises(LastAdminError):
                self.store.set_authority(self.admin.principal_id, **change)
        self.store.set_authority(p.principal_id, enabled=False)
        self.store.set_authority(p.principal_id, enabled=True)
        self.assertFalse(self.store.authenticate(p.username, ADMIN_PASSWORD).ok)

    def test_sync_ignores_last_admin_after_rollback(self):
        p, _ = self.link()
        self.rollback_sql(
            "UPDATE principals SET enabled=0 WHERE principal_id=?",
            (self.admin.principal_id,),
        )
        p, _ = self.link("observer")
        self.assertEqual(p.role, "observer")
        self.assertEqual(self.store.enabled_admin_count(), 0)

    def rollback_sql(self, sql, args):
        with sqlite3.connect(self.path) as db:
            db.execute(sql, args)

    def test_local_disable_wins_and_does_not_sync_metadata(self):
        p, _ = self.link(email="old@example.com")
        self.store.set_authority(p.principal_id, enabled=False)
        with self.assertRaises(AccountDisabled):
            self.link("observer", email="new@example.com")
        self.assertEqual(self.store.get(p.principal_id).role, "admin")
        self.assertEqual(
            self.store.sso_identity(p.principal_id)["email"], "old@example.com"
        )

    def test_sso_authority_and_password_are_managed_in_storage(self):
        p, _ = self.link()
        for action in (
            lambda: self.store.set_authority(p.principal_id, role="observer"),
            lambda: self.store.set_password(p.principal_id, ADMIN_PASSWORD),
            lambda: self.store.change_own_password(
                p.principal_id, "bad", ADMIN_PASSWORD
            ),
        ):
            with self.assertRaises(SsoManagedError):
                action()

    def test_collision_rolls_back_both_tables(self):
        self.store.create_human("sso-12345678abcd", ADMIN_PASSWORD, "observer")
        with self.assertRaises(AccountConflict):
            self.link()
        self.assertEqual(self.store.count(), 2)
        self.assertEqual(self.store.enabled_admin_count(), 1)

    def test_two_connections_first_login_serializes(self):
        second = cheap_store(self.path)
        self.addCleanup(second.close)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            futures = [
                pool.submit(s.link_or_sync_sso, ISSUER, SUB, "observer")
                for s in (self.store, second)
            ]
            outcomes = [f.result() for f in futures]
        self.assertEqual(sum(first for _, first in outcomes), 1)
        self.assertEqual(outcomes[0][0].principal_id, outcomes[1][0].principal_id)
        self.assertEqual(self.store.count(), 2)

    def test_offline_cli_uses_the_same_management_and_recovery_rules(self):
        p, _ = self.link()
        script = Path(__file__).resolve().parents[1] / "scripts" / "accounts.py"

        def cli(*args, password=""):
            return subprocess.run(
                [sys.executable, str(script), "--db", str(self.path), *args],
                input=password + "\n",
                text=True,
                capture_output=True,
            )

        for command in (
            ("set-role", "--username", p.username, "--role", "observer"),
            ("reset-password", "--username", p.username),
        ):
            result = cli(*command, password=ADMIN_PASSWORD)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Palintropic", result.stderr)
        self.assertEqual(cli("disable", "--username", p.username).returncode, 0)
        self.assertEqual(cli("enable", "--username", p.username).returncode, 0)
        self.assertNotEqual(
            cli("disable", "--username", self.admin.username).returncode, 0
        )
        self.rollback_sql(
            "UPDATE principals SET enabled=0 WHERE principal_id=?",
            (self.admin.principal_id,),
        )
        result = cli(
            "create-admin", "--username", "recovery-admin", password=ADMIN_PASSWORD
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.store.enabled_admin_count(), 1)

    def test_schema_version_unchanged(self):
        self.link()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(
                db.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0],
                "1",
            )
            self.assertEqual(
                len(db.execute("PRAGMA table_info(principals)").fetchall()), 10
            )
