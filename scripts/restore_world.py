#!/usr/bin/env python3
"""Restore one world through the local API and verify it through overview."""

import argparse
import fcntl
import http.client
import json
import math
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


KNOWN_RESTORE_REJECTIONS = {
    (400, "invalid_content"),
    (400, "invalid_world_id"),
    (409, "content_not_adopted"),
    (409, "provider_switch_in_progress"),
    (503, "settings_quarantined"),
    (503, "adapters_unavailable"),
    (503, "control_plane_unavailable"),
    (503, "ownership_unsupported"),
}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_api(opener, base, token, method, path, timeout):
    request = Request(
        base + path, method=method, headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        with exc:
            try:
                payload = json.load(exc)
            except (ValueError, OSError, http.client.HTTPException):
                payload = {}
        return exc.code, payload


def overview_revision(result, world):
    status, payload = result
    if (
        status == 200
        and isinstance(payload, dict)
        and payload.get("world_id") == world
        and type(payload.get("revision")) is int
        and isinstance(payload.get("autonomy"), dict)
        and payload["autonomy"].get("worker_alive") is True
    ):
        return payload["revision"]
    return None


def error_detail(payload):
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return detail if isinstance(detail, dict) else {}


def recover(call, world, marker, timeout):
    overview_path = "/overview?limit=1"
    try:
        status, payload = call("GET", overview_path, 15)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        print(f"{world}: overview unavailable ({type(exc).__name__})", file=sys.stderr)
        return 2 if marker.exists() else 1
    revision = overview_revision((status, payload), world)
    if revision is not None:
        marker.unlink(missing_ok=True)
        print(f"{world}: already open (revision {revision})")
        return 0

    detail = error_detail(payload)
    if status != 409 or detail.get("category") != "world_not_open":
        if marker.exists():
            print(f"{world}: previous restore remains unverified (overview HTTP {status})", file=sys.stderr)
            return 2
        print(f"{world}: cannot establish initial state (HTTP {status})", file=sys.stderr)
        return 1
    if marker.exists():
        print(f"{world}: previous restore is unresolved ({marker})", file=sys.stderr)
        return 2

    # Keep the marker if the client disconnects or exits before confirming the result.
    marker.touch(mode=0o600, exist_ok=False)
    print(f"{world}: submitting one restore request (timeout {timeout:g}s)", flush=True)
    try:
        try:
            status, payload = call("POST", "/restore", timeout)
        except URLError as exc:
            if not isinstance(exc.reason, ConnectionRefusedError):
                raise
            marker.unlink(missing_ok=True)
            print(f"{world}: connection refused before restore was sent", file=sys.stderr)
            return 1
        if status != 200:
            detail = error_detail(payload)
            diagnostic = {key: detail[key] for key in ("category", "message", "subjects") if key in detail}
            print(f"{world}: restore returned HTTP {status} {json.dumps(diagnostic, ensure_ascii=False)}", file=sys.stderr)
            category = detail.get("category")
            known_rejection = isinstance(category, str) and (status, category) in KNOWN_RESTORE_REJECTIONS
            if status in (401, 403, 404, 422) or known_rejection:
                marker.unlink(missing_ok=True)
                return 1
            return 2
        restored = payload.get("revision") if isinstance(payload, dict) else None
        if type(restored) is not int or payload.get("running") is not True:
            print(f"{world}: restore response did not confirm a running world", file=sys.stderr)
            return 2
        revision = overview_revision(call("GET", overview_path, 60), world)
        if revision is None or revision < restored:
            print(f"{world}: restore returned 200, but overview did not verify it", file=sys.stderr)
            return 2
    except (OSError, ValueError, http.client.HTTPException) as exc:
        print(f"{world}: restore result unverified ({type(exc).__name__})", file=sys.stderr)
        return 2

    marker.unlink(missing_ok=True)
    print(f"{world}: restore verified (revision {revision})")
    return 0


def main():
    parser = Parser(description=__doc__)
    parser.add_argument("world")
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    valid_id = re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", args.world)
    if not valid_id or ".." in args.world:
        print("invalid world ID", file=sys.stderr)
        return 1
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        print("--timeout must be positive and finite", file=sys.stderr)
        return 1

    token = os.getenv("PNS_ADMIN_TOKEN")
    if not token:
        print("PNS_ADMIN_TOKEN must be set", file=sys.stderr)
        return 1
    port = int(os.getenv("PORT", "7860"))
    base = f"http://127.0.0.1:{port}/api/persistent-worlds/{args.world}"
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(method, path, timeout):
        return request_api(opener, base, token, method, path, timeout)

    state_dir = Path("/app/data/.restore-operations")
    try:
        state_dir.mkdir(mode=0o700, exist_ok=True)
        with (state_dir / f"{args.world}.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(f"{args.world}: another restore invocation is active", file=sys.stderr)
                return 2
            return recover(call, args.world, state_dir / f"{args.world}.pending", args.timeout)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        print(f"{args.world}: cannot verify restore state ({type(exc).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
