"""Deterministic concurrency and legacy URL regressions for SEC-1."""
import json
import threading
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from test_auth_api import ApiTestCase, OPERATOR_PASSWORD
from fastapi.testclient import TestClient
from pns.interfaces.security import LoginThrottle
from pns.runtime import provider_settings as ps


class PasswordRaceTests(ApiTestCase):
    def _race(self, mutation):
        client = self.as_operator()
        entered, release = threading.Event(), threading.Event()
        original = self.accounts._verify

        def verified(*args):
            result = original(*args)
            entered.set()
            if not release.wait(10):
                raise AssertionError("password barrier timed out")
            return result

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(self.accounts, '_verify', verified):
            pending = pool.submit(client.post, '/api/auth/password', json={
                'current_password': OPERATOR_PASSWORD, 'new_password': 'self-new-password-01'})
            try:
                self.assertTrue(entered.wait(10))
                mutation()
                revision = self.accounts.get(self.operator.principal_id).security_revision
            finally:
                release.set()
            response = pending.result(10)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.accounts.get(self.operator.principal_id).security_revision, revision)
        failures = [r for r in self.accounts.audit_records()
                    if r['action'] == 'auth.password_change' and r['result'] == 'failure']
        self.assertEqual(len(failures), 1)
        self.assertIn(response.json()['detail']['category'], str(failures[0]))
        return response

    def test_admin_reset_wins_over_verified_password_change(self):
        response = self._race(lambda: self.accounts.set_password(
            self.operator.principal_id, 'admin-reset-password-01'))
        self.assertEqual(response.json()['detail']['category'], 'stale_credential')
        self.assertTrue(self.accounts.authenticate('operator-one', 'admin-reset-password-01').ok)
        self.assertFalse(self.accounts.authenticate('operator-one', 'self-new-password-01').ok)

    def test_disable_rejects_verified_password_change(self):
        self._race(lambda: self.accounts.set_authority(self.operator.principal_id, enabled=False))
        self.accounts.set_authority(self.operator.principal_id, enabled=True)
        self.assertTrue(self.accounts.authenticate('operator-one', OPERATOR_PASSWORD).ok)

    def test_sso_link_rejects_verified_password_change(self):
        def link():
            with self.accounts._lock:
                self.accounts._db.execute(
                    'INSERT INTO sso_identities(principal_id,issuer,subject,linked_at,last_login_at) VALUES(?,?,?,?,?)',
                    (self.operator.principal_id, 'https://idp.example', 'subject', 'now', 'now'))
        self._race(link)

    def test_normal_password_change_revokes_old_sessions(self):
        client, other = self.as_operator(), self.as_operator()
        response = client.post('/api/auth/password', json={
            'current_password': OPERATOR_PASSWORD, 'new_password': 'self-new-password-01'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(other.get('/api/auth/session').json()['authenticated'])
        self.assertTrue(self.accounts.authenticate('operator-one', 'self-new-password-01').ok)


class ConcurrentLoginTests(ApiTestCase):
    def _concurrent(self, count, password, admitted):
        barrier = threading.Barrier(admitted + 1)
        release = threading.Event()
        original = self.accounts.authenticate
        calls = []
        def authenticate(*args):
            calls.append(args)
            barrier.wait(10)
            if not release.wait(10):
                raise AssertionError('login barrier timed out')
            return original(*args)
        def login():
            with TestClient(self.app) as client:
                return client.post('/api/auth/login', json={
                    'username': 'operator-one', 'password': password}).status_code
        with patch.object(self.accounts, 'authenticate', authenticate), ThreadPoolExecutor(max_workers=count) as pool:
            pending = [pool.submit(login) for _ in range(count)]
            try:
                barrier.wait(10)
                # Rejected requests finish while admitted requests remain at the barrier.
                if count > admitted:
                    from concurrent.futures import wait, FIRST_COMPLETED
                    done, _ = wait(pending, timeout=10, return_when=FIRST_COMPLETED)
                    self.assertTrue(done)
                    self.assertTrue(all(p.result() == 429 for p in done))
                    # Wait until every non-admitted request has completed.
                    import time
                    deadline = time.monotonic() + 10
                    while sum(p.done() for p in pending) < count - admitted and time.monotonic() < deadline:
                        threading.Event().wait(.01)
                    self.assertEqual(sum(p.done() for p in pending), count - admitted)
            finally:
                release.set()
            statuses = [p.result(10) for p in pending]
        self.assertEqual(len(calls), admitted)
        return statuses

    def test_sixteen_wrong_logins_respect_reservations(self):
        statuses = self._concurrent(16, 'wrong-password', 10)
        self.assertEqual(statuses.count(401), 10)
        self.assertEqual(statuses.count(429), 6)

    def test_three_correct_concurrent_logins_succeed(self):
        self.assertEqual(self._concurrent(3, OPERATOR_PASSWORD, 3), [200] * 3)

    def test_authenticate_exception_releases_reservation(self):
        with patch.object(self.accounts, 'authenticate', side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):
                self.client.post('/api/auth/login', json={'username': 'operator-one', 'password': OPERATOR_PASSWORD})
        throttle = self.app.state.admin_auth.throttle
        self.assertEqual(throttle._inflight, {})
        self.as_operator()

    def test_global_reservations_and_capacity_are_bounded(self):
        throttle = LoginThrottle(max_tracked_keys=2, global_max_failures=3)
        self.assertTrue(throttle.reserve('a'))
        self.assertTrue(throttle.reserve('b'))
        self.assertFalse(throttle.reserve('c'))
        self.assertTrue(throttle.reserve('a'))
        self.assertFalse(throttle.reserve('b'))
        throttle.finish('a', success=False)
        throttle.finish('a', success=True)
        throttle.finish('b', success=None)
        self.assertEqual(throttle._inflight, {})
        self.assertEqual(len(throttle._global), 1)
        self.assertNotIn('a', throttle._buckets)


class ProviderUrlTests(ApiTestCase):
    def test_card_and_env_reject_query_and_fragment(self):
        for suffix in ('?api_key=secret', '#secret', '?', '#'):
            url = 'https://provider.example/v1' + suffix
            with self.subTest(suffix=suffix):
                with self.assertRaisesRegex(ps.ProviderSettingsError, '密钥请放到 token 字段'):
                    ps.put_card(ps.EMPTY_DOCUMENT, 'test', dict(name='test', account='env',
                        api_format='openai', base_url=url, model='test', token='test'), ps.builtin_catalog(), create=True)
                with self.assertRaisesRegex(ps.ProviderSettingsError, '密钥请放到 token 字段'):
                    ps.ModelSettings.from_env({'BASE_URL': url})
                self.assertEqual(ps.display_url(url), ps.URL_PLACEHOLDER)

    def test_legacy_active_card_loads_masked_but_cannot_publish(self):
        catalog = ps.builtin_catalog()
        doc = ps.put_card(ps.EMPTY_DOCUMENT, 'test', dict(name='test', account='env',
            api_format='openai', base_url='https://provider.example/v1', model='test', token='test'), catalog, create=True)
        doc = ps.set_active(doc, {'generation': 'test', 'judge': {'follow': 'generation'}})
        raw = doc.to_file(updated_by='test')
        for suffix in ('?api_key=secret', '#secret'):
            raw['cards']['test']['base_url'] = 'https://provider.example/v1' + suffix
            (self.tmp / ps.PROVIDER_FILE).write_text(json.dumps(raw))
            settings, loaded_catalog = ps.load_model_settings(self.tmp, {}, ooc_threshold=5)
            self.assertEqual(settings.generation.base_url, raw['cards']['test']['base_url'])
            self.assertEqual(settings.public_dict()['generation']['base_url'], ps.URL_PLACEHOLDER)
            self.assertEqual(settings.public_dict()['cards'][0]['base_url'], ps.URL_PLACEHOLDER)
            from pns.interfaces.app import _verify_production_config
            from pns.interfaces.security import DeploymentSettings
            _verify_production_config(
                DeploymentSettings(mode='production', admin_token='test-admin-token-' * 3), self.tmp,
                registry_provider=lambda: SimpleNamespace(models=settings, accounts=loaded_catalog))
            from pns.interfaces.provider_api import _check
            from fastapi import HTTPException
            with self.assertRaises(HTTPException) as error:
                _check(settings.document, settings.document, loaded_catalog)
            self.assertEqual(error.exception.status_code, 400)
            with self.assertRaisesRegex(ps.ProviderSettingsError, '密钥请放到 token 字段'):
                ps.validate_for_publish(settings, loaded_catalog, {})

    def test_normal_url_display_is_unchanged(self):
        url = 'https://provider.example/v1'
        self.assertEqual(ps.display_url(url), url)
