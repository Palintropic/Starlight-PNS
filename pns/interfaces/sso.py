"""AUTH-2: OIDC transport and one-time browser-bound login/claim flows.

Only this module contacts the IdP. All routes are synchronous so network and
account I/O run in FastAPI's worker pool, including the no-await session switch.
"""

from __future__ import annotations

import base64
import hashlib
import html
from http.client import HTTPException as HttpTransportError
import json
import math
import logging
from functools import wraps
import secrets
import threading
import time
from dataclasses import dataclass
from hmac import compare_digest
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request as UrlRequest, build_opener

import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .accounts import AccountConflict, AccountDisabled, ROLES
from .auth import _auth, _clear_session_cookie, _set_session_cookie
from .security import DeploymentConfigError, SsoSettings, sso_url_origin

router = APIRouter(prefix="/api/auth/sso", tags=["auth"])
FLOW_PATH = "/api/auth/sso/"
BINDING_PREFIX = "pns_sso_"
CACHE_TTL = 3600
PENDING_TTL = 600
TICKET_TTL = 30
HTTP_TIMEOUT = 5
SSO_CONCURRENCY = 4
FETCH_BACKOFF = 30
FAILURE_AUDIT_CAPACITY = 20
FAILURE_AUDIT_REFILL_PER_MINUTE = 20
FAILURE_CATEGORIES = (
    "state_invalid", "idp_denied", "idp_unavailable", "token_invalid",
    "sso_busy", "no_role", "unknown_role", "ambiguous_role",
    "account_disabled", "account_conflict", "sso_stale",
)
COUNTER_MAX = 2**63 - 1
logger = logging.getLogger(__name__)


class SsoError(Exception):
    def __init__(self, category: str):
        self.category = category
        super().__init__(category)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def safe_next(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value[1:2] in ("/", "\\")
        or len(value) > 512
        or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)
    ):
        return "/"
    return value


def binding_name(state: str) -> str:
    return BINDING_PREFIX + hashlib.sha256(state.encode()).hexdigest()[:16]


def _same_secret(actual: Optional[str], expected: str) -> bool:
    return isinstance(actual, str) and compare_digest(
        actual.encode(), expected.encode()
    )


def _query_url(endpoint: str, values: dict) -> str:
    return endpoint + ("&" if urlsplit(endpoint).query else "?") + urlencode(values)


class Provider:
    def __init__(
        self,
        settings: SsoSettings,
        *,
        development: bool = False,
        opener=None,
        clock=time.monotonic,
    ):
        self.settings = settings
        self.development = development
        # build_opener supplies the normal TLS/HTTP handlers, replacing only its
        # redirect handler. A 3xx raises HTTPError before another request is sent.
        self.opener = opener if opener is not None else build_opener(NoRedirect())
        self.clock = clock
        self._lock = threading.RLock()
        self._discovery = None
        self._discovery_until = 0.0
        self._jwks = None
        self._jwks_until = 0.0
        self._fetching = set()
        self._retry_after = {"discovery": 0.0, "jwks": 0.0}

    def _json(self, url: str, *, data=None, headers=None) -> dict:
        request = UrlRequest(
            url, data=data, headers={"Accept": "application/json", **(headers or {})}
        )
        try:
            with self.opener.open(request, timeout=HTTP_TIMEOUT) as response:
                if response.status != 200:
                    raise SsoError("idp_unavailable")
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise SsoError("idp_unavailable")
                body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError
            return body
        except (HTTPError, URLError, OSError, HttpTransportError, ValueError):
            raise SsoError("idp_unavailable") from None

    def _cached_fetch(self, kind, fetch, *, force=False):
        # Reserve one fetch under the lock, then release it before any I/O.
        with self._lock:
            cached = getattr(self, "_" + kind)
            if (
                not force
                and cached is not None
                and self.clock() < getattr(self, "_" + kind + "_until")
            ):
                return cached
            if kind in self._fetching:
                raise SsoError("sso_busy")
            if self.clock() < self._retry_after[kind]:
                raise SsoError("idp_unavailable")
            self._fetching.add(kind)
            started = self.clock()
        try:
            value = fetch()
            with self._lock:
                setattr(self, "_" + kind, value)
                until = started + CACHE_TTL
                if kind == "jwks":
                    until = min(until, self._discovery_until)
                else:
                    self._jwks, self._jwks_until = None, 0.0
                setattr(self, "_" + kind + "_until", until)
            return value
        except SsoError:
            with self._lock:
                self._retry_after[kind] = self.clock() + FETCH_BACKOFF
            raise
        finally:
            with self._lock:
                self._fetching.remove(kind)

    def discovery(self) -> dict:
        def fetch():
            doc = self._json(
                self.settings.issuer.rstrip("/") + "/.well-known/openid-configuration"
            )
            if doc.get("issuer") != self.settings.issuer:
                raise SsoError("idp_unavailable")
            try:
                origin = sso_url_origin(
                    self.settings.issuer, development=self.development
                )
                for key in (
                    "authorization_endpoint",
                    "token_endpoint",
                    "jwks_uri",
                    "end_session_endpoint",
                ):
                    endpoint = doc.get(key)
                    if (
                        not isinstance(endpoint, str)
                        or sso_url_origin(endpoint, development=self.development)
                        != origin
                    ):
                        raise SsoError("idp_unavailable")
            except DeploymentConfigError:
                raise SsoError("idp_unavailable") from None
            return doc

        return self._cached_fetch("discovery", fetch)

    def _signing_key(self, kid: str):
        doc = self.discovery()

        def fetch():
            jwks = self._json(doc["jwks_uri"])
            if not isinstance(jwks.get("keys"), list):
                raise SsoError("token_invalid")
            return jwks["keys"]

        keys = self._cached_fetch("jwks", fetch)
        for attempt in range(2):
            matches = [
                key for key in keys if isinstance(key, dict) and key.get("kid") == kid
            ]
            if matches:
                if len(matches) != 1:
                    raise SsoError("token_invalid")
                key = matches[0]
                if (
                    key.get("kty") != "RSA"
                    or key.get("alg", "RS256") != "RS256"
                    or key.get("use", "sig") != "sig"
                    or ("key_ops" in key and "verify" not in key["key_ops"])
                ):
                    raise SsoError("token_invalid")
                try:
                    return jwt.PyJWK.from_dict(key, algorithm="RS256").key
                except (jwt.PyJWTError, ValueError, TypeError, KeyError):
                    raise SsoError("token_invalid") from None
            if attempt == 0:
                keys = self._cached_fetch("jwks", fetch, force=True)
        raise SsoError("token_invalid")

    def verify(self, token: str, nonce: str) -> dict:
        try:
            header = jwt.get_unverified_header(token)
            if (
                header.get("alg") != "RS256"
                or not isinstance(header.get("kid"), str)
                or not header["kid"]
            ):
                raise SsoError("token_invalid")
            key = self._signing_key(header["kid"])
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.settings.client_id,
                issuer=self.settings.issuer,
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub"],
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
            now = time.time()
            for name in ("exp", "iat"):
                value = claims[name]
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise SsoError("token_invalid")
            if claims["exp"] <= now or claims["iat"] > now + 60:
                raise SsoError("token_invalid")
            if not _same_secret(claims.get("nonce"), nonce):
                raise SsoError("token_invalid")
            multi = isinstance(claims["aud"], list) and len(claims["aud"]) > 1
            if (multi or "azp" in claims) and claims.get(
                "azp"
            ) != self.settings.client_id:
                raise SsoError("token_invalid")
            if not isinstance(claims["sub"], str) or not claims["sub"]:
                raise SsoError("token_invalid")
            return claims
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise SsoError("token_invalid") from None

    def exchange(self, code: str, verifier: str, nonce: str) -> tuple[dict, str]:
        doc = self.discovery()
        basic = base64.b64encode(
            (
                quote_plus(self.settings.client_id)
                + ":"
                + quote_plus(self.settings.client_secret)
            ).encode()
        ).decode("ascii")
        body = self._json(
            doc["token_endpoint"],
            data=urlencode(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.settings.redirect_uri,
                    "code_verifier": verifier,
                }
            ).encode(),
            headers={
                "Authorization": "Basic " + basic,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        token = body.get("id_token")
        if not isinstance(token, str) or not token:
            raise SsoError("token_invalid")
        return self.verify(token, nonce), token

    def logout_url(self, id_token: str) -> str:
        redirect = urlsplit(self.settings.redirect_uri)
        return _query_url(
            self.discovery()["end_session_endpoint"],
            {
                "id_token_hint": id_token,
                "post_logout_redirect_uri": f"{redirect.scheme}://{redirect.netloc}/",
            },
        )


def role_from(claims: dict) -> str:
    roles = claims.get("roles")
    roles = (
        [role for role in roles if isinstance(role, str) and role.startswith("pns:")]
        if isinstance(roles, list)
        else []
    )
    if not roles:
        raise SsoError("no_role")
    if len(roles) > 1:
        raise SsoError("ambiguous_role")
    role = roles[0][4:]
    if role not in ROLES:
        raise SsoError("unknown_role")
    return role


@dataclass(frozen=True, repr=False)
class Pending:
    nonce: str
    verifier: str
    next: str
    binding: str
    cookie_name: str
    expires_at: float


@dataclass(frozen=True, repr=False)
class Ticket:
    issuer: str
    subject: str
    role: str
    issued_at: float
    email: Optional[str]
    name: Optional[str]
    id_token: str
    next: str
    binding: str
    cookie_name: str
    expires_at: float


class OneTimeTable:
    def __init__(self, *, limit=None, clock=time.monotonic):
        self.clock = clock
        self.limit = limit
        self._rows = {}
        self._lock = threading.Lock()

    def put(self, key, record):
        with self._lock:
            now = self.clock()
            self._rows = {k: v for k, v in self._rows.items() if v.expires_at > now}
            if self.limit is not None and len(self._rows) >= self.limit:
                raise SsoError("sso_busy")
            self._rows[key] = record

    def take(self, key):
        with self._lock:
            return self._rows.pop(key, None)


class SsoLogin:
    def __init__(self, settings: SsoSettings, *, development=False):
        self.settings = settings
        self.provider = Provider(settings, development=development)
        self.pending = OneTimeTable(limit=256)
        self.tickets = OneTimeTable()
        self.slots = threading.BoundedSemaphore(SSO_CONCURRENCY)
        self._counter_lock = threading.Lock()
        self.unmatched_failures = {"callback": 0, "finish": 0}
        self.suppressed_failures = {
            (stage, category): 0
            for stage in ("callback", "finish") for category in FAILURE_CATEGORIES
        }
        self.audit_clock = time.monotonic
        self._audit_updated = self.audit_clock()
        self._audit_tokens = float(FAILURE_AUDIT_CAPACITY)
        self._suppressed_pending = 0

    def audit_failure(self, accounts, stage, category):
        # Serialize budget decisions and summary emission across both endpoints.
        with self._counter_lock:
            now = self.audit_clock()
            self._audit_tokens = min(
                FAILURE_AUDIT_CAPACITY,
                self._audit_tokens + max(0, now - self._audit_updated)
                * FAILURE_AUDIT_REFILL_PER_MINUTE / 60,
            )
            self._audit_updated = now
            if self._audit_tokens < 1:
                key = (stage, category)
                count = min(self.suppressed_failures[key] + 1, COUNTER_MAX)
                self.suppressed_failures[key] = count
                self._suppressed_pending = min(self._suppressed_pending + 1, COUNTER_MAX)
                if count & (count - 1) == 0:
                    logger.warning("SSO suppressed %s %s count=%d", stage, category, count)
                return
            self._audit_tokens -= 1
            if self._suppressed_pending:
                accounts.record_event(
                    "sso.login_failed_suppressed", "failure",
                    detail={"count": self._suppressed_pending},
                )
                self._suppressed_pending = 0
            accounts.record_event(
                "sso.login_failed", "failure", detail={"stage": stage, "category": category}
            )

    def count_unmatched(self, stage):
        # Fixed keys and saturating integers; logarithmic logs contain no input.
        with self._counter_lock:
            count = min(self.unmatched_failures[stage] + 1, 2**63 - 1)
            self.unmatched_failures[stage] = count
        if count & (count - 1) == 0:
            logger.warning("SSO unmatched %s state_invalid count=%d", stage, count)


def _service(request: Request) -> SsoLogin:
    service = getattr(request.app.state, "sso", None)
    if service is None:
        raise HTTPException(404, "Not Found")
    return service


def _private(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _clear_binding(response, auth, cookie_name):
    if cookie_name:
        response.delete_cookie(
            cookie_name,
            path=FLOW_PATH,
            httponly=True,
            secure=auth.settings.cookie_secure,
            samesite="lax",
        )


def _failure(request, category, stage=None, cookie_name=None, *, matched=True):
    auth = _auth(request)
    if stage is not None and not matched:
        _service(request).count_unmatched(stage)
    if stage is not None and matched and auth.accounts is not None:
        _service(request).audit_failure(auth.accounts, stage, category)
    response = _private(RedirectResponse("/?sso_error=" + category, status_code=302))
    _clear_binding(response, auth, cookie_name)
    return response


def _bounded_entry(func):
    @wraps(func)
    def bounded(request: Request):
        service = _service(request)
        if not service.slots.acquire(blocking=False):
            return _failure(request, "sso_busy")
        try:
            return func(request)
        finally:
            service.slots.release()

    return bounded


@router.get("/login")
@_bounded_entry
def login(request: Request):
    service = _service(request)
    auth = _auth(request)
    next_path = safe_next(request.query_params.get("next"))
    canonical = urlsplit(service.settings.redirect_uri)
    expected = sso_url_origin(
        service.settings.redirect_uri, development=not auth.settings.production
    )
    try:
        actual = sso_url_origin(
            f"{canonical.scheme}://{request.url.netloc}",
            development=not auth.settings.production,
        )
    except DeploymentConfigError:
        actual = None
    # Compare host:port; reverse proxies need not report the public scheme.
    if actual is None or actual[1:] != expected[1:]:
        response = _private(
            RedirectResponse(
                f"{canonical.scheme}://{canonical.netloc}{FLOW_PATH}login?"
                + urlencode({"next": next_path}),
                status_code=302,
            )
        )
        auth.sessions.revoke(auth.session_id(request.scope))
        _clear_session_cookie(response, auth)
        return response
    try:
        doc = service.provider.discovery()
        state = secrets.token_urlsafe(32)
        pending = Pending(
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(32),
            next_path,
            secrets.token_urlsafe(32),
            binding_name(state),
            service.pending.clock() + PENDING_TTL,
        )
        service.pending.put(state, pending)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(pending.verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        response = _private(
            RedirectResponse(
                _query_url(
                    doc["authorization_endpoint"],
                    {
                        "response_type": "code",
                        "client_id": service.settings.client_id,
                        "redirect_uri": service.settings.redirect_uri,
                        "scope": "openid email profile",
                        "state": state,
                        "nonce": pending.nonce,
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                    },
                ),
                status_code=302,
            )
        )
        visible = [name for name in request.cookies if name.startswith(BINDING_PREFIX)]
        if len(visible) >= 8:
            for name in visible:
                _clear_binding(response, auth, name)
        response.set_cookie(
            pending.cookie_name,
            pending.binding,
            path=FLOW_PATH,
            max_age=PENDING_TTL,
            httponly=True,
            secure=auth.settings.cookie_secure,
            samesite="lax",
        )
        return response
    except SsoError as exc:
        return _failure(request, exc.category)


@router.get("/callback")
@_bounded_entry
def callback(request: Request):
    service = _service(request)
    state = request.query_params.get("state", "")
    cookie_name = binding_name(state) if state else None
    pending = service.pending.take(state)
    try:
        if (
            pending is None
            or pending.expires_at <= service.pending.clock()
            or not _same_secret(
                request.cookies.get(pending.cookie_name), pending.binding
            )
        ):
            raise SsoError("state_invalid")
        if "error" in request.query_params:
            raise SsoError("idp_denied")
        code = request.query_params.get("code")
        if not code:
            raise SsoError("token_invalid")
        claims, token = service.provider.exchange(code, pending.verifier, pending.nonce)
        role = role_from(claims)
        ticket = secrets.token_urlsafe(32)
        service.tickets.put(
            ticket,
            Ticket(
                service.settings.issuer,
                claims["sub"],
                role,
                claims["iat"],
                claims.get("email") if isinstance(claims.get("email"), str) else None,
                claims.get("name") if isinstance(claims.get("name"), str) else None,
                token,
                pending.next,
                pending.binding,
                pending.cookie_name,
                service.tickets.clock() + TICKET_TTL,
            ),
        )
        url = html.escape(
            FLOW_PATH + "finish?" + urlencode({"ticket": ticket}), quote=True
        )
        # Keep the Lax binding for finish. Never set/replace pns_session here.
        return _private(
            HTMLResponse(
                f'<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                f'<meta http-equiv="refresh" content="0;url={url}"><title>继续登录</title>'
                f'<a href="{url}">继续</a></html>'
            )
        )
    except SsoError as exc:
        return _failure(
            request, exc.category, "callback", cookie_name, matched=pending is not None
        )


@router.get("/finish")
@_bounded_entry
def finish(request: Request):
    service = _service(request)
    auth = _auth(request)
    ticket = service.tickets.take(request.query_params.get("ticket", ""))
    try:
        if (
            ticket is None
            or ticket.expires_at <= service.tickets.clock()
            or not _same_secret(request.cookies.get(ticket.cookie_name), ticket.binding)
        ):
            raise SsoError("state_invalid")
        if auth.accounts is None:  # create_app ensures the store exists
            raise SsoError("idp_unavailable")
        account, first = auth.accounts.link_or_sync_sso(
            ticket.issuer,
            ticket.subject,
            ticket.role,
            issued_at=ticket.issued_at,
            email=ticket.email,
            display_name=ticket.name,
        )
        ttl = service.settings.session_minutes * 60
        sid = auth.sessions.issue(
            account.principal_id,
            account.security_revision,
            login="sso",
            id_token=ticket.id_token,
            ttl=ttl,
        )
        auth.sessions.revoke(auth.session_id(request.scope))
        auth.accounts.record_event(
            "sso.login",
            "success",
            actor=account.principal_id,
            target=account.principal_id,
            detail={"role": account.role, "first_login": first},
        )
        response = _private(RedirectResponse(ticket.next, status_code=302))
        _set_session_cookie(response, auth, sid, ttl=ttl)
        _clear_binding(response, auth, ticket.cookie_name)
        return response
    except (SsoError, AccountConflict, AccountDisabled) as exc:
        return _failure(
            request,
            exc.category,
            "finish",
            ticket.cookie_name if ticket else None,
            matched=ticket is not None,
        )
