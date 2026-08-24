# tests/test_accounts.py — AUTH-1 的账户存储边界
#
# 盯住的东西按"错了会怎样"排：
#
#   1. 明文密码不落地。库文件里搜不到它，审计里搜不到它，返回值里也没有。
#      连"尝试过的用户名"都不记——把密码打进用户名框每天都在发生。
#   2. 判重只有一条规则。大小写、全角、非 ASCII 同形字都折叠到同一个键上，
#      所以界面上看起来一样的两个名字不可能同时存在。
#   3. "最后一个管理员"是**写事务里的裁决**，不是先查后写。两个并发的降级
#      不可能都通过；把那道闸弱化掉，同一批用例必须变红。
#   4. 权威一变，security_revision 就前进。会话按它判活，所以停用/改角色/
#      改密码不需要"通知"任何人就已经生效了。
#   5. bootstrap 幂等，且永远不覆盖已有账户。并发启动只会创建一个。
#   6. 库能重开。容器换掉之后账户和审计还在——它是数据，不是进程状态。
#
# 运行: python -m unittest tests.test_accounts -v
import stat
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts"), str(REPO_ROOT / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from pns.interfaces.accounts import (  # noqa: E402
    LOGIN_BAD_PASSWORD,
    LOGIN_DISABLED,
    LOGIN_MALFORMED,
    LOGIN_UNKNOWN,
    MIN_PASSWORD_CHARS,
    ROLE_SCOPES,
    ROLES,
    SCHEMA_VERSION,
    SCOPE_ACCOUNTS,
    SCOPE_OPERATE,
    SCOPE_READ,
    AccountConflict,
    AccountStore,
    AccountStoreError,
    InvalidPassword,
    InvalidUsername,
    LastAdminError,
    canonical_username,
)

from accounts_support import (  # noqa: E402
    ADMIN_PASSWORD,
    ADMIN_PASSWORD_HASH,
    ADMIN_USERNAME,
    CHEAP_HASHER,
    OTHER_PASSWORD,
    cheap_store,
)


class CountingHasher:
    """一层只数数的包装。`PasswordHasher` 的方法是只读属性，改不了。"""

    def __init__(self, inner):
        self.inner = inner
        self.verified = []

    @property
    def memory_cost(self):
        return self.inner.memory_cost

    def hash(self, password):
        return self.inner.hash(password)

    def verify(self, encoded, password):
        self.verified.append(encoded)
        return self.inner.verify(encoded, password)


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "accounts.sqlite3"
        self.store = cheap_store(self.path)
        self.addCleanup(self.store.close)

    def admin(self, username=ADMIN_USERNAME):
        return self.store.create_human(username, ADMIN_PASSWORD, "admin")


# ── 1. 明文与哈希都不外流 ───────────────────────────────────────────────
class NoPlaintextTests(StoreTestCase):
    def test_the_database_file_never_contains_the_password(self):
        self.admin()
        self.store.create_human("ena", OTHER_PASSWORD, "operator")
        # 落盘：SQLite 的 DELETE 日志模式下 commit 之后主库就是权威的那一份。
        blob = self.path.read_bytes()
        self.assertNotIn(ADMIN_PASSWORD.encode(), blob)
        self.assertNotIn(OTHER_PASSWORD.encode(), blob)
        self.assertIn(b"$argon2id$", blob, "存的应该是 Argon2id 哈希")

    def test_the_principal_object_has_no_credential_field(self):
        principal = self.admin()
        self.assertNotIn("password", repr(principal).lower())
        self.assertNotIn("argon2", repr(principal))
        self.assertNotIn("password_hash", principal.public_dict())

    def test_audit_records_never_carry_the_attempted_username(self):
        """把密码打进用户名框每天都在发生。记下来 = 把明文写进磁盘。"""
        self.admin()
        self.store.authenticate(ADMIN_PASSWORD, "whatever-else-11")
        self.store.authenticate("no-such-person", ADMIN_PASSWORD)
        blob = self.path.read_bytes()
        self.assertNotIn(ADMIN_PASSWORD.encode(), blob)
        for record in self.store.audit_records():
            self.assertNotIn("username", record["detail"])
            self.assertNotIn(ADMIN_PASSWORD, str(record))


# ── 2. 用户名判重 ───────────────────────────────────────────────────────
class UsernameCanonicalisationTests(StoreTestCase):
    def test_case_and_fullwidth_fold_to_one_key(self):
        for raw in ("Admin", "ADMIN", "ａｄｍｉｎ", "  admin  "):
            with self.subTest(raw=raw):
                self.assertEqual(canonical_username(raw)[1], "admin")

    def test_lookalikes_outside_ascii_are_refused_outright(self):
        """西里尔 'а' 是 alnum，casefold 之后仍是它自己：允许它就等于允许
        一个和 admin 并存、界面上看不出区别的账户。"""
        for raw in ("аdmin", "admın", "ådmin", "ad​min"):
            with self.subTest(raw=raw):
                with self.assertRaises(InvalidUsername):
                    canonical_username(raw)

    def test_shape_rules(self):
        for raw in ("ab", "a" * 65, ".admin", "-admin", "ad min", "ad/min", "", 7):
            with self.subTest(raw=raw):
                with self.assertRaises(InvalidUsername):
                    canonical_username(raw)
        for raw in ("abc", "a.b-c_d", "9lives"):
            with self.subTest(raw=raw):
                canonical_username(raw)

    def test_duplicate_creation_is_a_conflict_across_folded_forms(self):
        self.admin("mizuki")
        for duplicate in ("mizuki", "MIZUKI", "ｍｉｚｕｋｉ"):
            with self.subTest(duplicate=duplicate):
                with self.assertRaises(AccountConflict):
                    self.store.create_human(duplicate, OTHER_PASSWORD, "observer")
        self.assertEqual(len(self.store.list_humans()), 1)


# ── 3. 密码策略 ─────────────────────────────────────────────────────────
class PasswordPolicyTests(StoreTestCase):
    def test_short_and_blank_passwords_are_refused(self):
        for bad in ("a" * (MIN_PASSWORD_CHARS - 1), " " * 20, 7, None):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidPassword):
                    self.store.hash_password(bad)

    def test_passwords_are_not_normalised_or_trimmed(self):
        """规范化一个秘密 = 悄悄改掉它，然后让人在别的客户端上登不进来。"""
        password = "  padded-password-1  "
        self.store.create_human("kanade", password, "observer")
        self.assertTrue(self.store.authenticate("kanade", password).ok)
        self.assertFalse(self.store.authenticate("kanade", password.strip()).ok)


# ── 4. 登录判定 ─────────────────────────────────────────────────────────
class AuthenticateTests(StoreTestCase):
    def test_the_reason_is_recorded_but_never_returned_as_a_difference(self):
        self.admin("mizuki")
        self.store.create_human("ena", OTHER_PASSWORD, "observer")
        self.store.set_authority(
            self.store.find_by_username("ena").principal_id, enabled=False
        )
        cases = [
            (("nobody", ADMIN_PASSWORD), LOGIN_UNKNOWN),
            (("mizuki", "wrong-password-x"), LOGIN_BAD_PASSWORD),
            (("ena", OTHER_PASSWORD), LOGIN_DISABLED),
            ((" ", ADMIN_PASSWORD), LOGIN_MALFORMED),
        ]
        for (username, password), reason in cases:
            with self.subTest(username=username):
                outcome = self.store.authenticate(username, password)
                self.assertIsNone(outcome.principal)
                self.assertEqual(outcome.reason, reason)
        recorded = [
            r["detail"]["reason"]
            for r in self.store.audit_records()
            if r["action"] == "auth.login"
        ]
        for _, reason in cases:
            self.assertIn(reason, recorded)

    def test_every_path_pays_for_one_argon2_verify(self):
        """否则"这个用户名存不存在"可以用秒表问出来。

        判据是"真的调了一次校验"，不是"两次耗时差不多"——后者在 CI 上是一条
        会随机红的用例，前者是这条保证的**机制**。
        """
        counting = CountingHasher(CHEAP_HASHER)
        store = AccountStore(Path(self._tmp.name) / "counted.sqlite3", hasher=counting)
        self.addCleanup(store.close)
        store.create_human("mizuki", ADMIN_PASSWORD, "admin")

        store.authenticate("mizuki", ADMIN_PASSWORD)
        store.authenticate("nobody-at-all", ADMIN_PASSWORD)
        store.authenticate("!!! not a username !!!", ADMIN_PASSWORD)
        self.assertEqual(len(counting.verified), 3, "有一条路径没付 Argon2 的钱")
        # 未知用户和畸形用户名用的是同一份影子哈希，不是空串、也不是每次现算
        # 的新哈希（现算的话耗时形状又不一样了）。
        self.assertEqual(counting.verified[1], counting.verified[2])
        self.assertNotEqual(counting.verified[0], counting.verified[1])

    def test_a_disabled_account_cannot_log_in_even_with_the_right_password(self):
        principal = self.admin("mizuki")
        self.store.create_human("ena", OTHER_PASSWORD, "admin")
        self.store.set_authority(principal.principal_id, enabled=False)
        self.assertFalse(self.store.authenticate("mizuki", ADMIN_PASSWORD).ok)


# ── 5. 角色与权限 ───────────────────────────────────────────────────────
class RoleScopeTests(StoreTestCase):
    def test_the_role_table_is_what_the_routes_rely_on(self):
        self.assertEqual(set(ROLE_SCOPES), set(ROLES))
        self.assertEqual(
            ROLE_SCOPES["admin"], {SCOPE_READ, SCOPE_OPERATE, SCOPE_ACCOUNTS}
        )
        self.assertEqual(ROLE_SCOPES["operator"], {SCOPE_READ, SCOPE_OPERATE})
        self.assertEqual(ROLE_SCOPES["observer"], {SCOPE_READ})

    def test_an_unknown_role_never_becomes_an_account(self):
        with self.assertRaises(Exception):
            self.store.create_human("nobody", ADMIN_PASSWORD, "superuser")
        self.assertEqual(self.store.list_humans(), [])


# ── 6. 权威变更与撤销判据 ───────────────────────────────────────────────
class AuthorityTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.first = self.admin("mizuki")
        self.second = self.store.create_human("ena", OTHER_PASSWORD, "admin")

    def test_every_authority_change_advances_the_security_revision(self):
        before = self.store.get(self.second.principal_id).security_revision
        after_role = self.store.set_authority(self.second.principal_id, role="observer")
        self.assertGreater(after_role.security_revision, before)
        after_enabled = self.store.set_authority(
            self.second.principal_id, enabled=False
        )
        self.assertGreater(after_enabled.security_revision, after_role.security_revision)
        after_password = self.store.set_password(
            self.second.principal_id, "another-password-1"
        )
        self.assertGreater(
            after_password.security_revision, after_enabled.security_revision
        )

    def test_the_last_enabled_admin_cannot_be_disabled_or_demoted(self):
        self.store.set_authority(self.second.principal_id, role="observer")
        with self.assertRaises(LastAdminError):
            self.store.set_authority(self.first.principal_id, enabled=False)
        with self.assertRaises(LastAdminError):
            self.store.set_authority(self.first.principal_id, role="operator")
        self.assertTrue(self.store.get(self.first.principal_id).enabled)
        self.assertEqual(self.store.get(self.first.principal_id).role, "admin")

    def test_a_refused_demotion_is_audited(self):
        """审计写在回滚**之后**：写在事务里的话它会跟着被拒绝的改动一起消失。"""
        self.store.set_authority(self.second.principal_id, role="observer")
        with self.assertRaises(LastAdminError):
            self.store.set_authority(self.first.principal_id, enabled=False)
        failures = [
            r
            for r in self.store.audit_records()
            if r["result"] == "failure" and r["action"] == "account.authority"
        ]
        self.assertTrue(failures, "被拒绝的降级没有留下任何痕迹")

    def test_weakening_the_guard_makes_the_invariant_fail(self):
        """把那道闸拿掉，上一条用例必须变红——否则它证明不了任何东西。"""
        self.store.set_authority(self.second.principal_id, role="observer")
        original = AccountStore._enabled_admins
        AccountStore._enabled_admins = lambda self: 99
        try:
            self.store.set_authority(self.first.principal_id, enabled=False)
        finally:
            AccountStore._enabled_admins = original
        self.assertFalse(self.store.get(self.first.principal_id).enabled)
        self.assertEqual(self.store.enabled_admin_count(), 0)

    def test_concurrent_demotions_cannot_both_win(self):
        """两条连接、两个线程、同一个文件：裁决必须发生在写锁之下。"""
        second = cheap_store(self.path)
        self.addCleanup(second.close)
        barrier = threading.Barrier(2)
        results = {}

        def demote(name, store, target):
            barrier.wait()
            try:
                store.set_authority(target, role="observer")
                results[name] = "ok"
            except LastAdminError:
                results[name] = "refused"
            except sqlite3.OperationalError as e:  # pragma: no cover - 锁等待超时
                results[name] = f"locked:{e}"

        threads = [
            threading.Thread(
                target=demote, args=("a", self.store, self.first.principal_id)
            ),
            threading.Thread(
                target=demote, args=("b", second, self.second.principal_id)
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(results.values()), ["ok", "refused"], results)
        self.assertEqual(self.store.enabled_admin_count(), 1)

    def test_changing_your_own_password_needs_the_current_one(self):
        with self.assertRaises(InvalidPassword):
            self.store.change_own_password(
                self.first.principal_id, "not-the-password", "a-new-password-1"
            )
        failures = [
            r
            for r in self.store.audit_records()
            if r["action"] == "auth.password_change" and r["result"] == "failure"
        ]
        self.assertTrue(failures, "一串失败的改密尝试是最像凭据被偷的信号")
        self.store.change_own_password(
            self.first.principal_id, ADMIN_PASSWORD, "a-new-password-1"
        )
        self.assertTrue(
            self.store.authenticate("mizuki", "a-new-password-1").ok
        )
        self.assertFalse(self.store.authenticate("mizuki", ADMIN_PASSWORD).ok)


# ── 7. bootstrap ────────────────────────────────────────────────────────
class BootstrapTests(StoreTestCase):
    def test_bootstrap_creates_the_first_admin_and_then_does_nothing(self):
        first = self.store.bootstrap_admin(ADMIN_USERNAME, ADMIN_PASSWORD_HASH)
        self.assertEqual(first.role, "admin")
        self.assertIsNone(self.store.bootstrap_admin(ADMIN_USERNAME, ADMIN_PASSWORD_HASH))
        self.assertIsNone(self.store.bootstrap_admin("someone-else", ADMIN_PASSWORD_HASH))
        self.assertEqual(len(self.store.list_humans()), 1)
        self.assertTrue(self.store.authenticate(ADMIN_USERNAME, ADMIN_PASSWORD).ok)

    def test_bootstrap_never_resurrects_authority_in_a_non_empty_store(self):
        """库里已经有账户就什么都不做，**哪怕一个管理员都没有**。

        否则 bootstrap 变量就是一条"把 .env 改一行就能重新拿到管理员"的提权
        路径：任何能改环境的人都等于管理员。恢复的正路是那个要文件系统访问的
        离线命令（`create_human`），它对非空库照样有效。
        """
        self.store.create_human("watcher", ADMIN_PASSWORD, "observer")
        self.assertEqual(self.store.enabled_admin_count(), 0)
        self.assertIsNone(self.store.bootstrap_admin("rescue", ADMIN_PASSWORD_HASH))
        self.assertEqual(self.store.enabled_admin_count(), 0)
        self.assertEqual(len(self.store.list_humans()), 1)
        # 离线命令走的是另一条路，它建得出来——那是文档里的恢复办法。
        self.store.create_human("rescue", OTHER_PASSWORD, "admin")
        self.assertEqual(self.store.enabled_admin_count(), 1)

    def test_bootstrap_refuses_anything_that_is_not_an_argon2id_hash(self):
        for bad in ("", "hunter2", "$2b$12$abcdefghijklmnopqrstuv", 7, ADMIN_PASSWORD):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidPassword):
                    self.store.bootstrap_admin(ADMIN_USERNAME, bad)
        # argon2i 是合法的 Argon2 编码，但不是这个板子承诺存的那一种。
        argon2i = ADMIN_PASSWORD_HASH.replace("$argon2id$", "$argon2i$")
        with self.assertRaises(InvalidPassword):
            self.store.bootstrap_admin(ADMIN_USERNAME, argon2i)
        self.assertEqual(self.store.list_humans(), [])

    def test_concurrent_bootstrap_creates_exactly_one_admin(self):
        second = cheap_store(self.path)
        self.addCleanup(second.close)
        barrier = threading.Barrier(2)
        created = []

        def boot(store, username):
            barrier.wait()
            try:
                result = store.bootstrap_admin(username, ADMIN_PASSWORD_HASH)
            except sqlite3.OperationalError:  # pragma: no cover - 锁等待超时
                result = None
            if result is not None:
                created.append(result.principal_id)

        threads = [
            threading.Thread(target=boot, args=(self.store, "first-admin")),
            threading.Thread(target=boot, args=(second, "second-admin")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(created), 1, created)
        self.assertEqual(len(self.store.list_humans()), 1)


# ── 8. 库本身 ───────────────────────────────────────────────────────────
class StoreFileTests(StoreTestCase):
    def test_the_database_file_is_only_readable_by_its_owner(self):
        self.admin()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_accounts_and_audit_survive_reopening(self):
        """容器换掉之后账户和审计还在——它是数据，不是进程状态。"""
        principal = self.admin("mizuki")
        self.store.authenticate("mizuki", ADMIN_PASSWORD)
        self.store.close()
        reopened = cheap_store(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(
            [p.principal_id for p in reopened.list_humans()], [principal.principal_id]
        )
        self.assertTrue(
            any(r["action"] == "auth.login" for r in reopened.audit_records())
        )
        self.assertTrue(reopened.authenticate("mizuki", ADMIN_PASSWORD).ok)

    def test_a_newer_schema_refuses_to_be_opened_by_this_version(self):
        """旧进程按自己的理解去写一个新 schema，比起不起得来危险得多。"""
        self.admin()
        self.store.close()
        # `with sqlite3.connect(...)` 只管事务，**不关连接**——要自己 close。
        db = sqlite3.connect(self.path)
        try:
            db.execute(
                "UPDATE schema_meta SET value=? WHERE key='schema_version'",
                (str(SCHEMA_VERSION + 1),),
            )
            db.commit()
        finally:
            db.close()
        with self.assertRaises(AccountStoreError):
            cheap_store(self.path)

    def test_journal_mode_is_delete_so_one_file_is_the_whole_database(self):
        """账户库跟着数据卷一起备份；WAL 会让"拷走这一个文件"不再等于
        "拷走这个库"。"""
        db = sqlite3.connect(self.path)
        try:
            mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            db.close()
        self.assertEqual(mode.lower(), "delete")

    def test_production_hashing_parameters_are_not_the_cheap_test_ones(self):
        """测试用便宜参数是为了跑得动，不是把默认值调低了。"""
        from argon2 import (
            DEFAULT_MEMORY_COST,
            DEFAULT_PARALLELISM,
            DEFAULT_TIME_COST,
            PasswordHasher,
        )

        default = PasswordHasher()
        self.assertGreaterEqual(DEFAULT_MEMORY_COST, 64 * 1024)
        self.assertGreaterEqual(DEFAULT_TIME_COST, 3)
        self.assertGreaterEqual(DEFAULT_PARALLELISM, 1)
        self.assertGreater(default.memory_cost, CHEAP_HASHER.memory_cost)
        encoded = default.hash("a-real-password-here")
        self.assertTrue(encoded.startswith("$argon2id$"))


if __name__ == "__main__":
    unittest.main()
