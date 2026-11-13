"""AUTH-2 public API counterexamples, using real RSA JWTs and fake OIDC endpoints."""

import base64
import concurrent.futures
import hashlib
import io
import json
import logging
import os
import re
import sys
import threading
import time
import unittest
from dataclasses import replace
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPSHandler, build_opener
from urllib.response import addinfourl

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from accounts_support import ADMIN_PASSWORD
from test_auth_api import ApiTestCase, ADMIN_TOKEN
from pns.interfaces import sso
from pns.interfaces.app import create_app, _verify_production_accounts
from pns.interfaces.redaction import SsoAccessLogFilter, SecretRedactor
from pns.interfaces.security import (
    DeploymentConfigError,
    DeploymentSettings,
    SsoSettings,
)

ISSUER = "https://id.example/"
SITE = "https://pns.example"
SUB = "12345678-abcd-4000-8000-000000000001"
SSO = SsoSettings(
    ISSUER, "pns+client", "fake/secret+with spaces", SITE + "/api/auth/sso/callback"
)
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
DOC = dict(
    issuer=ISSUER,
    authorization_endpoint=ISSUER + "auth",
    token_endpoint=ISSUER + "token",
    jwks_uri=ISSUER + "jwks",
    end_session_endpoint=ISSUER + "logout",
    code_challenge_methods_supported=["plain", "S256"],
)


def jwk(key=KEY, kid="k1"):
    return {
        **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())),
        "kid": kid,
        "alg": "RS256",
        "use": "sig",
    }


class FakeIdP(HTTPSHandler):
    """Keep urllib's actual response/redirect processing, replacing only the wire."""

    def __init__(self):
        super().__init__()
        self.doc = dict(DOC)
        self.keys = [jwk()]
        self.requests = []
        self.nonces = {}
        self.overrides = {}
        self.drop = []
        self.signing_key = KEY
        self.algorithm = "RS256"
        self.kid = "k1"
        self.redirect = None
        self.down = False
        self.token_body = None

    def https_open(self, req):
        self.requests.append(req)
        if self.down:
            raise URLError("unavailable")
        headers = Message()
        status = 200
        if self.redirect == req.full_url:
            status = 302
            headers["Location"] = "http://attacker.invalid/collect"
            body = {}
        elif req.full_url == ISSUER + ".well-known/openid-configuration":
            body = self.doc
        elif req.full_url == DOC["jwks_uri"]:
            body = {"keys": self.keys}
        elif req.full_url == DOC["token_endpoint"]:
            data = parse_qs(req.data.decode())
            verifier = data["code_verifier"][0]
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            nonce = self.nonces.get(challenge)
            if nonce is None:
                raise AssertionError("PKCE mismatch")
            now = int(time.time())
            claims = dict(
                iss=ISSUER,
                aud=SSO.client_id,
                sub=SUB,
                exp=now + 300,
                iat=now,
                nonce=nonce,
                roles=["pns:observer"],
                email="private@example.com",
                name="Display",
            )
            claims.update(self.overrides)
            for field in self.drop:
                claims.pop(field, None)
            key = self.signing_key
            if self.algorithm == "none":
                key = None
            elif self.algorithm == "HS256":
                # DER public key as HMAC material: classic algorithm confusion.
                from cryptography.hazmat.primitives import serialization

                key = KEY.public_key().public_bytes(
                    serialization.Encoding.DER,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            body = (
                self.token_body
                if self.token_body is not None
                else {
                    "id_token": self.sign(claims, key),
                    "access_token": "discard-me",
                }
            )
        else:
            raise AssertionError("unexpected network request: " + req.full_url)
        response = addinfourl(
            io.BytesIO(json.dumps(body).encode()), headers, req.full_url, status
        )
        response.msg = "OK" if status == 200 else "Found"
        return response

    def sign(self, claims, key):
        if self.algorithm != "HS256":
            return jwt.encode(
                claims, key, algorithm=self.algorithm, headers={"kid": self.kid}
            )
        # The library correctly refuses public-key HMAC signing; model an attacker
        # constructing it directly, rather than weakening the library in the test.
        import hmac

        def b64(data):
            return base64.urlsafe_b64encode(data).rstrip(b"=")

        material = (
            b64(json.dumps({"alg": "HS256", "kid": self.kid}).encode())
            + b"."
            + b64(json.dumps(claims).encode())
        )
        return (
            material + b"." + b64(hmac.new(key, material, hashlib.sha256).digest())
        ).decode()


class SsoApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.settings = replace(self.settings, sso=SSO, cookie_secure=True)
        self.app = create_app(
            self.plane,
            settings=self.settings,
            account_store=self.accounts,
            registry_provider=lambda: self.registry,
        )
        self.client = TestClient(self.app, base_url=SITE)
        self.service = self.app.state.sso
        self.idp = FakeIdP()
        self.service.provider.opener = build_opener(sso.NoRedirect(), self.idp)

    def start(self, client=None, next="/"):
        client = client or self.client
        response = client.get(
            "/api/auth/sso/login", params={"next": next}, follow_redirects=False
        )
        self.assertEqual(response.status_code, 302)
        q = {
            k: v[0]
            for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()
        }
        if "code_challenge" in q:
            self.idp.nonces[q["code_challenge"]] = q["nonce"]
        return response, q

    def callback(self, q, client=None, **params):
        return (client or self.client).get(
            "/api/auth/sso/callback",
            params={"state": q["state"], "code": "fake-code", **params},
            follow_redirects=False,
        )

    def claim_url(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return re.search(r'href="([^"]+)"', response.text)[1]

    def finish(self, response, client=None):
        return (client or self.client).get(
            self.claim_url(response), follow_redirects=False
        )

    def sign_in(self, **kw):
        _, q = self.start(**kw)
        return self.finish(self.callback(q, kw.get("client")), kw.get("client"))

    def assert_error(self, response, category):
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/?sso_error=" + category)
        self.assertNotIn("pns_session=", response.headers.get("set-cookie", ""))

    def session(self, client=None):
        return (client or self.client).get("/api/auth/session").json()

    def local_login(self, client=None):
        client = client or self.client
        res = client.post(
            "/api/auth/login",
            json={"username": self.admin.username, "password": ADMIN_PASSWORD},
        )
        self.assertEqual(res.status_code, 200)
        return client.cookies.get("pns_session")

    def saved_session(self, sid):
        return self.client.get(
            "/api/auth/session", headers={"cookie": "pns_session=" + sid}
        ).json()

    def test_complete_cookie_jar_retains_binding_until_finish(self):
        response, q = self.start(next="/#world")
        self.assertIn("SameSite=lax", response.headers["set-cookie"])
        self.assertIn("Secure", response.headers["set-cookie"])
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("Path=/api/auth/sso/", response.headers["set-cookie"])
        cb = self.callback(q)
        self.assertNotIn("set-cookie", cb.headers)
        self.assertFalse(self.session()["authenticated"])
        self.assertEqual(len(self.accounts.list_humans()), 3)
        self.assertEqual(cb.headers["cache-control"], "no-store")
        self.assertEqual(cb.headers["referrer-policy"], "no-referrer")
        self.assertIn('<meta http-equiv="refresh"', cb.text)
        self.assertNotIn("<script", cb.text)
        done = self.finish(cb)
        self.assertEqual(done.headers["location"], "/#world")
        self.assertEqual(self.session()["principal"]["via"], "session")
        self.assertEqual(self.session()["principal"]["login"], "sso")
        self.assertEqual(self.session()["principal"]["role"], "observer")
        self.assertFalse(
            any(c.name.startswith(sso.BINDING_PREFIX) for c in self.client.cookies.jar)
        )
        cookie = done.headers["set-cookie"]
        self.assertIn("Max-Age=3600", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertNotIn("private@example.com", cookie)
        self.assertNotIn("discard-me", cookie)
        record = self.app.state.admin_auth.sessions.get(
            self.client.cookies.get("pns_session")
        )
        self.assertEqual(record.ttl, 3600)
        self.assertNotIn(record.id_token, repr(record))
        req = next(r for r in self.idp.requests if r.data)
        self.assertEqual(
            base64.b64decode(req.get_header("Authorization").split()[1]).decode(),
            "pns%2Bclient:fake%2Fsecret%2Bwith+spaces",
        )
        data = parse_qs(req.data.decode())
        self.assertEqual(data["redirect_uri"], [SSO.redirect_uri])
        self.assertEqual(data["grant_type"], ["authorization_code"])
        self.assertNotIn("client_secret", data)
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertEqual(q["scope"], "openid email profile")

    def test_finish_revokes_old_sid_and_keeps_new(self):
        sid = self.local_login()
        self.sign_in()
        self.assertFalse(self.saved_session(sid)["authenticated"])
        self.assertTrue(self.session()["authenticated"])

    def test_callback_and_failed_finish_leave_old_sid_alive(self):
        sid = self.local_login()
        _, q = self.start()
        cb = self.callback(q)
        self.assertTrue(self.saved_session(sid)["authenticated"])
        self.client.cookies.delete(sso.binding_name(q["state"]))
        self.assert_error(self.finish(cb), "state_invalid")
        self.assertTrue(self.saved_session(sid)["authenticated"])
        self.assertEqual(len(self.accounts.list_humans()), 3)

    def test_role_sync_revokes_other_devices_by_revision(self):
        self.idp.overrides = {"roles": ["pns:admin"], "iat": int(time.time()) - 2}
        self.sign_in()
        old = self.client.cookies.get("pns_session")
        self.idp.overrides = {"roles": ["pns:observer"]}
        self.sign_in()
        self.assertFalse(self.saved_session(old)["authenticated"])
        self.assertEqual(self.session()["principal"]["role"], "observer")

    def test_alias_redirect_revokes_alias_sid_before_any_network(self):
        alias = TestClient(self.app, base_url="https://192.168.3.115")
        sid = self.local_login(alias)
        response, _ = self.start(alias, next="/#world")
        self.assertEqual(
            response.headers["location"], SITE + "/api/auth/sso/login?next=%2F%23world"
        )
        self.assertNotIn(sso.BINDING_PREFIX, response.headers.get("set-cookie", ""))
        self.assertFalse(self.app.state.admin_auth.sessions.valid(sid))
        self.assertEqual(self.idp.requests, [])
        self.assertEqual(self.service.pending._rows, {})

    def test_canonical_host_behind_http_proxy_does_not_redirect_loop(self):
        proxied = TestClient(self.app, base_url="http://pns.example")
        response, q = self.start(proxied)
        self.assertIn("state", q)
        self.assertTrue(
            response.headers["location"].startswith(DOC["authorization_endpoint"])
        )

    def test_forged_host_redirect_target_comes_from_configuration(self):
        response = self.client.get(
            "/api/auth/sso/login?next=//evil.example",
            headers={"host": "evil.example"},
            follow_redirects=False,
        )
        self.assertEqual(
            response.headers["location"], SITE + "/api/auth/sso/login?next=%2F"
        )

    def test_state_one_time_and_callback_failure_cleans_cookie(self):
        _, q = self.start()
        bad = self.callback(q, error="access_denied")
        self.assert_error(bad, "idp_denied")
        self.assertIn("Max-Age=0", bad.headers["set-cookie"])
        self.assertIn("Path=/api/auth/sso/", bad.headers["set-cookie"])
        self.assert_error(self.callback(q), "state_invalid")
        self.assertEqual(len(self.idp.requests), 1)  # discovery only

    def test_state_binding_mismatch_consumes_state(self):
        _, q = self.start()
        attacker = TestClient(self.app, base_url=SITE)
        self.assert_error(self.callback(q, attacker), "state_invalid")
        self.assert_error(self.callback(q), "state_invalid")

    def test_state_replay_with_original_binding_is_refused(self):
        _, q = self.start()
        self.assertEqual(self.callback(q).status_code, 200)
        self.assert_error(self.callback(q), "state_invalid")

    def test_pending_expiry(self):
        _, q = self.start()
        self.service.pending.clock = lambda: time.monotonic() + 601
        self.assert_error(self.callback(q), "state_invalid")

    def test_ticket_replay_and_expiry_have_no_login_success_audit(self):
        _, q = self.start()
        cb = self.callback(q)
        url = self.claim_url(cb)
        self.service.tickets.clock = lambda: time.monotonic() + 31
        self.assert_error(self.client.get(url, follow_redirects=False), "state_invalid")
        self.assert_error(self.client.get(url, follow_redirects=False), "state_invalid")
        records = self.accounts.audit_records()
        self.assertFalse(any(r["action"] == "sso.login" for r in records))
        failures = [r for r in records if r["action"] == "sso.login_failed"]
        self.assertEqual(
            failures[0]["detail"], {"stage": "finish", "category": "state_invalid"}
        )
        self.assertEqual(len(self.accounts.list_humans()), 3)

    def test_success_ticket_cannot_be_claimed_twice(self):
        _, q = self.start()
        cb = self.callback(q)
        binding = self.client.cookies.get(sso.binding_name(q["state"]))
        self.finish(cb)
        sid = self.client.cookies.get("pns_session")
        replay = self.client.get(
            self.claim_url(cb),
            headers={
                "cookie": sso.binding_name(q["state"])
                + "="
                + binding
                + "; pns_session="
                + sid,
            },
            follow_redirects=False,
        )
        self.assert_error(replay, "state_invalid")
        self.assertTrue(self.saved_session(sid)["authenticated"])
        records = [
            r for r in self.accounts.audit_records() if r["action"] == "sso.login"
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["detail"], {"role": "observer", "first_login": True}
        )
        self.assertNotIn(SUB, str(self.accounts.audit_records()))
        self.assertNotIn("private@example.com", str(self.accounts.audit_records()))

    def test_disabled_account_finish_keeps_old_local_session(self):
        self.sign_in()
        pid = self.session()["principal"]["principal_id"]
        self.accounts.set_authority(pid, enabled=False)
        sid = self.local_login()
        _, q = self.start()
        self.assert_error(self.finish(self.callback(q)), "account_disabled")
        self.assertTrue(self.saved_session(sid)["authenticated"])

    def test_name_conflict_does_not_link_local_account(self):
        self.accounts.create_human("sso-12345678abcd", ADMIN_PASSWORD, "admin")
        _, q = self.start()
        self.assert_error(self.finish(self.callback(q)), "account_conflict")
        self.assertEqual(len(self.accounts.list_humans()), 4)

    def test_sso_password_endpoints_and_account_metadata(self):
        self.idp.overrides = {"roles": ["pns:admin"]}
        self.sign_in()
        pid = self.session()["principal"]["principal_id"]
        body = self.client.get("/api/accounts").json()
        me = next(p for p in body["users"] if p["principal_id"] == pid)
        self.assertEqual(me["sso"]["display_name"], "Display")
        self.assertNotIn("subject", me["sso"])
        for path, payload in [
            (
                "/api/auth/password",
                {
                    "current_password": ADMIN_PASSWORD,
                    "new_password": "different-password",
                },
            ),
            (f"/api/accounts/{pid}/role", {"role": "observer"}),
            (f"/api/accounts/{pid}/password", {"password": ADMIN_PASSWORD}),
        ]:
            res = self.client.post(path, json=payload)
            self.assertEqual(res.status_code, 409, res.text)
            self.assertEqual(res.json()["detail"]["category"], "sso_managed")

    def test_local_login_source_and_change_password_remain_usable(self):
        self.local_login()
        self.assertEqual(self.session()["principal"]["via"], "session")
        self.assertEqual(self.session()["principal"]["login"], "password")
        res = self.client.post(
            "/api/auth/password",
            json={
                "current_password": ADMIN_PASSWORD,
                "new_password": "new-local-password",
            },
        )
        self.assertEqual(res.status_code, 200, res.text)

    def test_sso_session_expires_at_its_own_ttl(self):
        self.sign_in()
        sessions = self.app.state.admin_auth.sessions
        sessions._clock = lambda: time.monotonic() + 3601
        self.assertFalse(self.session()["authenticated"])

    def test_sso_logout_returns_idp_url_after_local_revocation(self):
        self.sign_in()
        sid = self.client.cookies.get("pns_session")
        res = self.client.post("/api/auth/logout")
        q = parse_qs(urlsplit(res.json()["sso_logout_url"]).query)
        self.assertEqual(q["post_logout_redirect_uri"], [SITE + "/"])
        self.assertEqual(q["id_token_hint"][0].count("."), 2)
        self.assertFalse(self.saved_session(sid)["authenticated"])

    def test_logout_discovery_failure_still_revokes_and_reports_partial_logout(self):
        self.sign_in()
        self.idp.down = True
        self.service.provider._discovery_until = 0
        res = self.client.post("/api/auth/logout")
        self.assertIsNone(res.json()["sso_logout_url"])
        self.assertFalse(res.json()["sso_signed_out"])
        self.assertFalse(self.session()["authenticated"])

    def test_blocked_idp_does_not_starve_local_login(self):
        import asyncio
        import httpx
        import anyio

        entered, release = threading.Event(), threading.Event()
        opener = self.service.provider.opener

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise URLError("test timed out")
            return opener.open(*args, **kwargs)

        async def scenario():
            # All 48 requests share the same real default 40-token AnyIO limiter.
            self.assertEqual(
                anyio.to_thread.current_default_thread_limiter().total_tokens, 40
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url=SITE,
                follow_redirects=False,
            ) as client:
                tasks = [
                    asyncio.create_task(client.get("/api/auth/sso/login"))
                    for _ in range(48)
                ]
                try:

                    async def wait_for_entry():
                        while not entered.is_set():
                            await asyncio.sleep(0.001)

                    await asyncio.wait_for(wait_for_entry(), 1)
                    response = await asyncio.wait_for(
                        client.post(
                            "/api/auth/login",
                            json={
                                "username": self.admin.username,
                                "password": ADMIN_PASSWORD,
                            },
                        ),
                        1,
                    )
                    self.assertEqual(response.status_code, 200)
                    # Every other miss must complete while the first fetch is blocked.
                    done, pending = await asyncio.wait(tasks, timeout=1)
                    self.assertEqual(len(done), 47)
                    for task in done:
                        self.assert_error(task.result(), "sso_busy")
                finally:
                    release.set()
                    responses = await asyncio.gather(*tasks)
                self.assertEqual(
                    sum("code_challenge=" in r.headers["location"] for r in responses),
                    1,
                )
                recovered = await client.get("/api/auth/sso/login")
                self.assertIn("code_challenge=", recovered.headers["location"])

        with patch.object(self.service.provider, "opener") as mock:
            mock.open.side_effect = blocked
            asyncio.run(scenario())

    def test_shared_gate_rejects_all_entries_without_consuming_flow(self):
        _, q = self.start()
        ticket_response = self.callback(q)
        url = self.claim_url(ticket_response)
        for _ in range(sso.SSO_CONCURRENCY):
            self.assertTrue(self.service.slots.acquire(blocking=False))
        try:
            for path in (
                "/api/auth/sso/login",
                "/api/auth/sso/callback?state=made-up",
                url,
            ):
                with concurrent.futures.ThreadPoolExecutor(1) as pool:
                    response = pool.submit(
                        self.client.get, path, follow_redirects=False
                    ).result(timeout=1)
                self.assert_error(response, "sso_busy")
        finally:
            for _ in range(sso.SSO_CONCURRENCY):
                self.service.slots.release()
        self.assertEqual(self.finish(ticket_response).headers["location"], "/")
        self.assertTrue(self.session()["authenticated"])

    def test_discovery_and_jwks_failure_backoff_expires(self):
        provider = self.service.provider
        for kind in ("discovery", "jwks"):
            with self.subTest(kind=kind):
                now = [100.0]
                provider.clock = lambda: now[0]
                provider._retry_after = {"discovery": 0, "jwks": 0}
                provider._discovery_until = provider._jwks_until = 0
                self.idp.down = False
                if kind == "jwks":
                    provider.discovery()
                fetch = (
                    provider.discovery
                    if kind == "discovery"
                    else lambda: provider._signing_key("k1")
                )
                self.idp.down = True
                before = len(self.idp.requests)
                for _ in range(3):
                    with self.assertRaises(sso.SsoError):
                        fetch()
                self.assertEqual(len(self.idp.requests), before + 1)
                self.idp.down = False
                now[0] += sso.FETCH_BACKOFF
                fetch()
                self.assertEqual(len(self.idp.requests), before + 2)

    def test_unmatched_requests_do_not_write_audit(self):
        before = self.accounts.audit_records(limit=1000)
        for _ in range(100):
            for path in ("callback?state=made-up", "finish?ticket=made-up"):
                self.assert_error(
                    self.client.get("/api/auth/sso/" + path, follow_redirects=False),
                    "state_invalid",
                )
        self.assertEqual(self.accounts.audit_records(limit=1000), before)
        self.assertEqual(
            self.service.unmatched_failures, {"callback": 100, "finish": 100}
        )

    def test_failure_audit_budget_bounds_churn_and_recovers_with_one_summary(self):
        clock = [self.service._audit_updated]
        self.service.audit_clock = lambda: clock[0]
        before = len(self.accounts.audit_records(limit=2000))
        for _ in range(1000):
            _, q = self.start()
            self.assert_error(self.client.get(
                "/api/auth/sso/callback",
                params={"state": q["state"], "error": "access_denied"},
                follow_redirects=False,
            ), "idp_denied")
        self.assertEqual(len(self.accounts.audit_records(limit=2000)) - before,
                         sso.FAILURE_AUDIT_CAPACITY)
        self.assertEqual(self.service.suppressed_failures[("callback", "idp_denied")], 980)
        # Success is independent of the exhausted failure budget.
        self.idp.overrides = {"iat": int(time.time()) - 2}
        self.sign_in()
        self.idp.overrides = {"roles": ["pns:admin"]}
        self.sign_in()
        actions = [r["action"] for r in self.accounts.audit_records(limit=2000)]
        self.assertIn("sso.login", actions)
        self.assertIn("sso.link", actions)
        self.assertIn("sso.role_sync", actions)
        clock[0] += 60
        for _ in range(2):
            _, q = self.start()
            other = TestClient(self.app, base_url=SITE)
            self.assert_error(self.callback(q, other), "state_invalid")
        rows = self.accounts.audit_records(limit=2000)
        summaries = [r for r in rows if r["action"] == "sso.login_failed_suppressed"]
        self.assertEqual([r["detail"] for r in summaries], [{"count": 980}])
        self.assertEqual(sum(r["action"] == "sso.login_failed" for r in rows), 22)

    def test_failure_budget_is_shared_by_concurrent_callback_and_finish(self):
        self.service.audit_clock = lambda: self.service._audit_updated
        requests = []
        for i in range(32):
            client = TestClient(self.app, base_url=SITE)
            _, q = self.start(client)
            if i % 2:
                cb = self.callback(q, client)
                url = re.search(r'href="([^"]+)"', cb.text).group(1)
            else:
                url = "/api/auth/sso/callback?state=" + q["state"]
            client.cookies.clear()
            requests.append((client, url))
        def fail(item):
            client, url = item
            return client.get(url, follow_redirects=False)
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            responses = list(pool.map(fail, requests))
        for response in responses:
            self.assert_error(response, "state_invalid")
        rows = self.accounts.audit_records(limit=1000)
        self.assertEqual(sum(r["action"] == "sso.login_failed" for r in rows), 20)
        self.assertEqual(sum(self.service.suppressed_failures.values()), 12)

    def test_equal_iat_same_role_can_login_twice(self):
        self.idp.overrides = {"iat": int(time.time()) - 1}
        self.sign_in()
        old = self.client.cookies.get("pns_session")
        self.sign_in()
        self.assertTrue(self.session()["authenticated"])
        self.assertNotEqual(self.client.cookies.get("pns_session"), old)

    def test_future_iat_does_not_poison_next_normal_login(self):
        now = int(time.time())
        for iat in (now + 59, now, now + 59, now):
            self.idp.overrides = {"iat": iat}
            self.assertEqual(self.sign_in().headers["location"], "/")
            self.assertTrue(self.session()["authenticated"])

    def test_matched_wrong_binding_still_writes_failure_audit(self):
        _, q = self.start()
        other = TestClient(self.app, base_url=SITE)
        self.assert_error(self.callback(q, other), "state_invalid")
        failures = [
            r
            for r in self.accounts.audit_records()
            if r["action"] == "sso.login_failed"
        ]
        self.assertEqual(len(failures), 1)

    def test_new_ticket_then_old_ticket_preserves_role_and_new_session(self):
        self._claim_role_tickets(new_first=True)

    def test_old_ticket_then_new_ticket_downgrades_role(self):
        self._claim_role_tickets(new_first=False)

    def test_equal_iat_old_role_is_rejected_and_session_preserved(self):
        self._claim_role_tickets(new_first=True, same_iat=True)

    def _claim_role_tickets(self, *, new_first, same_iat=False):
        now = int(time.time()) - 10
        tickets = []
        for i, role in enumerate(("admin", "observer")):
            self.idp.overrides = {"iat": now + (0 if same_iat else i), "roles": ["pns:" + role]}
            _, q = self.start()
            tickets.append(self.callback(q))
        first, second = (1, 0) if new_first else (0, 1)
        self.assertEqual(self.finish(tickets[first]).headers["location"], "/")
        sid = self.client.cookies.get("pns_session")
        response = self.finish(tickets[second])
        if new_first:
            self.assert_error(response, "sso_stale")
            self.assertTrue(self.saved_session(sid)["authenticated"])
            self.assertEqual(self.client.cookies.get("pns_session"), sid)
        else:
            self.assertEqual(response.headers["location"], "/")
            self.assertFalse(self.saved_session(sid)["authenticated"])
        self.assertEqual(self.session()["principal"]["role"], "observer")

    def test_capacity_refuses_new_flow_and_preserves_oldest(self):
        _, q = self.start()
        for _ in range(255):
            self.start(TestClient(self.app, base_url=SITE))
        self.assert_error(
            self.client.get("/api/auth/sso/login", follow_redirects=False), "sso_busy"
        )
        self.assertEqual(self.finish(self.callback(q)).headers["location"], "/")
        self.local_login()  # local auth remains available while full

    def test_first_two_tabs_are_really_concurrent_and_both_bindings_work(self):
        clients = [TestClient(self.app, base_url=SITE) for _ in range(2)]
        barrier = threading.Barrier(2)
        put = self.service.pending.put

        def overlapping(key, pending):
            barrier.wait(timeout=5)
            put(key, pending)

        with patch.object(self.service.pending, "put", overlapping):
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                results = list(pool.map(self.start, clients))
        # A browser accepts both independent Set-Cookie responses.
        for client in clients:
            self.client.cookies.update(client.cookies)
        self.assertEqual(len({sso.binding_name(q["state"]) for _, q in results}), 2)
        for _, q in results:
            self.assertEqual(self.finish(self.callback(q)).status_code, 302)
            self.assertTrue(self.session()["authenticated"])

    def test_eight_visible_cookies_are_cleaned_but_success_callback_preserves_new(self):
        for i in range(8):
            self.client.cookies.set(
                "pns_sso_old" + str(i),
                "binding",
                domain="pns.example",
                path=sso.FLOW_PATH,
            )
        response, q = self.start()
        self.assertEqual(
            sum("Max-Age=0" in h for h in response.headers.get_list("set-cookie")), 8
        )
        self.assertEqual(self.finish(self.callback(q)).status_code, 302)

    def test_seven_visible_cookies_can_temporarily_exceed_threshold(self):
        clients = [TestClient(self.app, base_url=SITE) for _ in range(2)]
        for client in clients:
            for i in range(7):
                client.cookies.set(
                    "pns_sso_old" + str(i),
                    "binding",
                    domain="pns.example",
                    path=sso.FLOW_PATH,
                )
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(self.start, clients))
        for response, _ in responses:
            self.assertEqual(len(response.headers.get_list("set-cookie")), 1)
        for client in clients:
            self.client.cookies.update(client.cookies)
        self.assertEqual(
            sum(c.name.startswith("pns_sso_") for c in self.client.cookies.jar), 9
        )

    def test_bad_claims_each_refuse_without_creating_an_account(self):
        cases = [
            ("iss", ISSUER.rstrip("/")),
            ("aud", "elsewhere"),
            ("aud", [SSO.client_id, "other"]),
            ("azp", "other"),
            ("exp", int(time.time()) - 1),
            ("iat", int(time.time()) + 120),
            ("nonce", "wrong"),
            ("sub", ""),
            ("exp", "9999999999999"),
            ("iat", True),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.idp.overrides = {field: value}
                _, q = self.start()
                self.assert_error(self.callback(q), "token_invalid")
                self.assertFalse(self.session()["authenticated"])
        self.assertEqual(len(self.accounts.list_humans()), 3)

    def test_required_claims_missing(self):
        for field in ("iss", "aud", "exp", "iat", "sub", "nonce"):
            with self.subTest(field=field):
                self.idp.drop = [field]
                _, q = self.start()
                self.assert_error(self.callback(q), "token_invalid")

    def test_multiple_audiences_with_correct_azp_are_allowed(self):
        self.idp.overrides = {"aud": [SSO.client_id, "other"], "azp": SSO.client_id}
        self.sign_in()
        self.assertTrue(self.session()["authenticated"])

    def test_roles_fail_closed_and_filter_other_apps(self):
        for roles, category in [
            (None, "no_role"),
            ([], "no_role"),
            ("pns:admin", "no_role"),
            (["other:admin"], "no_role"),
            (["pns:superadmin"], "unknown_role"),
            (["pns:observer", "pns:admin"], "ambiguous_role"),
            (["pns:observer", "pns:observer"], "ambiguous_role"),
        ]:
            with self.subTest(roles=roles):
                self.idp.overrides = {"roles": roles}
                _, q = self.start()
                self.assert_error(self.callback(q), category)
        self.idp.drop = ["roles"]
        _, q = self.start()
        self.assert_error(self.callback(q), "no_role")
        self.idp.drop = []
        for i, role in enumerate(("admin", "operator", "observer")):
            self.idp.overrides = {"roles": ["other:admin", "pns:" + role], "iat": int(time.time()) - 3 + i}
            self.sign_in()
            self.assertEqual(self.session()["principal"]["role"], role)

    def test_bad_signature_none_and_hmac_public_key_are_refused(self):
        for alg, key in [("none", KEY), ("HS256", KEY), ("RS256", OTHER_KEY)]:
            with self.subTest(alg=alg):
                self.idp.algorithm, self.idp.signing_key = alg, key
                _, q = self.start()
                self.assert_error(self.callback(q), "token_invalid")

    def test_unknown_kid_refreshes_only_once_and_new_key_is_accepted(self):
        self.sign_in()
        self.idp.kid, self.idp.signing_key = "k2", OTHER_KEY
        self.idp.keys = [jwk(OTHER_KEY, "k2")]
        self.sign_in()
        self.assertTrue(self.session()["authenticated"])
        self.idp.kid = "absent"
        before = sum(r.full_url == DOC["jwks_uri"] for r in self.idp.requests)
        _, q = self.start()
        self.assert_error(self.callback(q), "token_invalid")
        after = sum(r.full_url == DOC["jwks_uri"] for r in self.idp.requests)
        self.assertEqual(after - before, 1)

    def test_same_kid_replacement_expires_with_discovery(self):
        self.sign_in()
        self.idp.keys = [jwk(OTHER_KEY)]
        self.service.provider.clock = lambda: time.monotonic() + 3601
        _, q = self.start()
        self.assert_error(self.callback(q), "token_invalid")

    def test_discovery_issuer_and_endpoint_trust_boundary(self):
        for field, value in [
            ("issuer", ISSUER.rstrip("/")),
            ("token_endpoint", "https://evil.example/token"),
            ("authorization_endpoint", "http://id.example/auth"),
            ("jwks_uri", "https://evil.example/jwks"),
            ("end_session_endpoint", "https://user:pass@id.example/logout"),
        ]:
            with self.subTest(field=field):
                self.service.provider._discovery_until = 0
                self.service.provider._retry_after["discovery"] = 0
                self.idp.doc = {**DOC, field: value}
                self.assert_error(
                    self.client.get("/api/auth/sso/login", follow_redirects=False),
                    "idp_unavailable",
                )
        self.idp.doc = dict(DOC)
        self.service.provider._retry_after["discovery"] = 0
        _, q = self.start()
        self.assertIn("state", q)  # failed discovery never cached

    def test_all_oidc_redirects_are_refused_before_following(self):
        for endpoint in (
            ISSUER + ".well-known/openid-configuration",
            DOC["token_endpoint"],
            DOC["jwks_uri"],
        ):
            with self.subTest(endpoint=endpoint):
                self.service.provider._discovery_until = 0
                self.idp.redirect = None
                self.service.provider._retry_after = {"discovery": 0, "jwks": 0}
                _, q = self.start()
                self.idp.redirect = endpoint
                if endpoint.endswith("openid-configuration"):
                    self.service.provider._discovery_until = 0
                    response = self.client.get(
                        "/api/auth/sso/login", follow_redirects=False
                    )
                else:
                    response = self.callback(q)
                self.assert_error(response, "idp_unavailable")
                self.assertFalse(
                    any("attacker.invalid" in r.full_url for r in self.idp.requests)
                )

    def test_token_endpoint_failure_or_missing_token_does_not_issue_session(self):
        _, q = self.start()
        self.idp.down = True
        self.assert_error(self.callback(q), "idp_unavailable")
        self.idp.down = False
        self.idp.token_body = {"access_token": "not-an-identity"}
        _, q = self.start()
        self.assert_error(self.callback(q), "token_invalid")


class ConfigAndLogTests(unittest.TestCase):
    def env(self):
        return dict(
            PNS_SSO_ISSUER=SSO.issuer,
            PNS_SSO_CLIENT_ID=SSO.client_id,
            PNS_SSO_CLIENT_SECRET=SSO.client_secret,
            PNS_SSO_REDIRECT_URI=SSO.redirect_uri,
        )

    def test_disabled_default_and_configured_default_ttl(self):
        self.assertIsNone(DeploymentSettings.from_env({}).sso)
        self.assertEqual(
            DeploymentSettings.from_env(self.env()).sso.session_minutes, 60
        )
        self.assertNotIn(
            SSO.client_secret, repr(DeploymentSettings.from_env(self.env()))
        )

    def test_partial_empty_invalid_minutes_and_insecure_urls_refuse_start(self):
        cases = [{"PNS_SSO_SESSION_MINUTES": "60"}, {"PNS_SSO_ISSUER": ""}]
        cases += [
            {**self.env(), name: value}
            for name, value in (
                ("PNS_SSO_SESSION_MINUTES", "0"),
                ("PNS_SSO_SESSION_MINUTES", "721"),
                ("PNS_SSO_SESSION_MINUTES", "nan"),
                ("PNS_SSO_ISSUER", "http://id.example/"),
                ("PNS_SSO_REDIRECT_URI", "http://192.168.3.115/api/auth/sso/callback"),
                ("PNS_SSO_REDIRECT_URI", "https://user:pass@pns.example/callback"),
            )
        ]
        for env in cases:
            with self.subTest(env=env), self.assertRaises(DeploymentConfigError):
                DeploymentSettings.from_env(env)

    def test_loopback_http_only_in_development(self):
        env = {
            **self.env(),
            "PNS_SSO_ISSUER": "http://localhost:4000/",
            "PNS_SSO_REDIRECT_URI": "http://127.0.0.1:8000/api/auth/sso/callback",
        }
        self.assertIsNotNone(DeploymentSettings.from_env(env).sso)
        env.update(PNS_ENV="production", PNS_ADMIN_TOKEN=ADMIN_TOKEN)
        with self.assertRaises(DeploymentConfigError):
            DeploymentSettings.from_env(env)

    def test_uvicorn_access_filter_omits_dynamic_queries_only_for_sso(self):
        filter = SsoAccessLogFilter()
        for target in (
            "/api/auth/sso/callback?code=CODE-CANARY&state=STATE-CANARY",
            "/api/auth/sso/finish?ticket=TICKET-CANARY",
            "/api/auth/%73so/finish?ticket=TICKET-CANARY",
            "/api/worlds?page=2",
        ):
            record = logging.LogRecord(
                "uvicorn.access",
                logging.INFO,
                "",
                1,
                '%s - "%s %s HTTP/%s" %d',
                ("127.0.0.1", "GET", target, "1.1", 200),
                None,
            )
            self.assertTrue(filter.filter(record))
            text = record.getMessage()
            if "/api/worlds" in target:
                self.assertIn("?page=2", text)
            else:
                for canary in ("CODE-CANARY", "STATE-CANARY", "TICKET-CANARY"):
                    self.assertNotIn(canary, text)

    def test_secret_is_in_shared_process_redaction_list(self):
        from process_setup import secret_env_names

        names = secret_env_names()
        self.assertIn("PNS_SSO_CLIENT_SECRET", names)
        self.assertNotIn(
            SSO.client_secret,
            SecretRedactor(names, env=self.env()).apply(SSO.client_secret),
        )

    def test_next_validation(self):
        for bad in (
            "//evil.example",
            "/\\evil.example",
            "https://evil.example",
            "/\nnext",
            "/\x85next",
            "/" + ("x" * 512),
        ):
            self.assertEqual(sso.safe_next(bad), "/")
        self.assertEqual(sso.safe_next("/#world"), "/#world")


class StartupCompatibilityTests(ApiTestCase):
    def test_disabled_sso_routes_are_public_but_absent_and_local_login_unchanged(self):
        for path in ("login", "callback", "finish"):
            self.assertEqual(self.client.get("/api/auth/sso/" + path).status_code, 404)
        self.assertFalse(self.client.get("/api/auth/session").json()["sso_enabled"])
        response = self.client.post(
            "/api/auth/login",
            json={"username": self.admin.username, "password": ADMIN_PASSWORD},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["principal"]["via"], "session")
        self.assertEqual(response.json()["principal"]["login"], "password")
        self.assertIn("Max-Age=43200", response.headers["set-cookie"])
        self.assertNotIn("sso_logout_url", self.client.post("/api/auth/logout").json())

    def test_sso_cannot_start_with_an_open_development_auth_gate(self):
        from accounts_support import cheap_store

        empty = cheap_store(":memory:")
        self.addCleanup(empty.close)
        settings = DeploymentSettings(mode="development", admin_token=None, sso=SSO)
        with self.assertRaisesRegex(DeploymentConfigError, "SSO.*鉴权门"):
            create_app(
                self.plane,
                settings=settings,
                account_store=empty,
                registry_provider=lambda: self.registry,
            )

    def test_production_requires_local_password_admin_even_with_sso_admin(self):
        self.accounts.link_or_sync_sso(ISSUER, SUB, "admin", issued_at=100)
        settings = replace(self.settings, mode="production", sso=SSO)
        _verify_production_accounts(settings, self.accounts)
        # Rollback could disable it; the next new-image startup must fail.
        import sqlite3

        with sqlite3.connect(self.accounts.path) as db:
            db.execute(
                "UPDATE principals SET enabled=0 WHERE principal_id=?",
                (self.admin.principal_id,),
            )
        with self.assertRaisesRegex(DeploymentConfigError, "非空库不会重新 bootstrap"):
            _verify_production_accounts(settings, self.accounts)

    def test_sso_startup_does_not_contact_idp(self):
        with patch.object(
            sso.Provider, "_json", side_effect=AssertionError("startup network")
        ):
            app = create_app(
                self.plane,
                settings=replace(self.settings, sso=SSO),
                account_store=self.accounts,
                registry_provider=lambda: self.registry,
            )
        self.assertIsNotNone(app.state.sso)


class RealUvicornLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_h11_protocol_access_log_never_contains_sso_query(self):
        import asyncio
        from uvicorn.config import Config
        from uvicorn.protocols.http.h11_impl import H11Protocol
        from uvicorn.server import ServerState

        class Transport(asyncio.Transport):
            closed = False

            def get_extra_info(self, name, default=None):
                return {
                    "sockname": ("127.0.0.1", 7860),
                    "peername": ("127.0.0.1", 12345),
                }.get(name, default)

            def is_closing(self):
                return self.closed

            def close(self):
                self.closed = True

            def write(self, data):
                pass

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger("uvicorn.access")
        prior = logger.level
        filter = SsoAccessLogFilter()
        logger.addHandler(handler)
        logger.addFilter(filter)
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.removeFilter, filter)
        self.addCleanup(logger.setLevel, prior)
        for target in (
            "/api/auth/sso/callback?code=CODE-CANARY&state=STATE-CANARY",
            "/api/auth/sso/finish?ticket=TICKET-CANARY",
            "/api/worlds?page=2",
        ):
            config = Config(app, log_config=None, lifespan="off")
            state = ServerState()
            protocol = H11Protocol(config, state, {})
            transport = Transport()
            protocol.connection_made(transport)
            protocol.data_received(
                f"GET {target} HTTP/1.1\r\nHost: pns.example\r\n\r\n".encode()
            )
            await asyncio.gather(*state.tasks)
            protocol.connection_lost(None)
        text = output.getvalue()
        self.assertIn("/api/auth/sso/callback", text)
        self.assertIn("/api/auth/sso/finish", text)
        self.assertIn("/api/worlds?page=2", text)
        for secret in ("CODE-CANARY", "STATE-CANARY", "TICKET-CANARY"):
            self.assertNotIn(secret, text)
