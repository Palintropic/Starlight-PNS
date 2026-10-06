#!/usr/bin/env python3
"""WORLD-2 运维脚本：地点扩展与居民入住。

只调用服务器的三个 WORLD-2 接口，不读存档文件、不另起 writer：

    POST /api/persistent-worlds/{world}/world2/preflight
    POST /api/persistent-worlds/{world}/world2/operations
    GET  /api/persistent-worlds/{world}/world2/operations/{operation_id}

凭据是 break-glass bearer token，从环境变量 PNS_ADMIN_TOKEN 读，不走命令行参数。

用法（一次只执行一笔；扩展与四人入住由操作员依次执行）：

    # 预检
    admit_residents.py preflight extension
    admit_residents.py preflight admission --character airi --roommates minori,haruka,shizuku --ordinal 0

    # 执行：先预检，把要发出的请求体写进 --record 文件，再发送
    admit_residents.py execute extension --operation-id mmj-ext-1
    admit_residents.py execute admission --operation-id mmj-airi --character airi \\
        --roommates minori,haruka,shizuku --ordinal 0

    # 响应丢了 / 结果不是 durable：用记下来的同一份请求重发（只查询或补存，不重判）
    admit_residents.py execute --request world2-mmj-airi.json

    # 查询（只读，不补存）
    admit_residents.py status mmj-airi

退出码：0 = durable / 预检可行；2 = 被拒或预检不可行；3 = 已提交但不是 durable
（committed / visible_not_durable，先停下来看清楚再决定要不要重发）；1 = 其它错误。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

ENV_TOKEN = "PNS_ADMIN_TOKEN"
# 跟 scripts/healthcheck.py 一样：容器里服务听在 127.0.0.1:$PORT（默认 7860）。
DEFAULT_BASE_URL = f"http://127.0.0.1:{os.environ.get('PORT', '7860')}"
DEFAULT_WORLD = "yoake-mae"

# send(method, path, body) -> (status, json)
Sender = Callable[[str, str, Optional[Dict]], Tuple[int, object]]


def http_sender(base_url: str, token: str) -> Sender:
    def send(method: str, path: str, body: Optional[Dict]) -> Tuple[int, object]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            base_url.rstrip("/") + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"null")
            except ValueError:
                return e.code, {"message": raw.decode("utf-8", "replace")}

    return send


class World2Client:
    def __init__(self, send: Sender, world: str) -> None:
        self._send = send
        self._prefix = f"/api/persistent-worlds/{world}/world2"

    def preflight(self, body: Dict) -> Tuple[int, object]:
        return self._send("POST", f"{self._prefix}/preflight", body)

    def execute(self, body: Dict) -> Tuple[int, object]:
        return self._send("POST", f"{self._prefix}/operations", body)

    def status(self, operation_id: str) -> Tuple[int, object]:
        return self._send("GET", f"{self._prefix}/operations/{operation_id}", None)


def preflight_body(args) -> Dict:
    if args.kind == "extension":
        return {"kind": "extension"}
    return {
        "kind": "admission",
        "character_id": args.character,
        "roommates": _roommates(args.roommates),
        "ordinal": args.ordinal,
    }


def _roommates(text: Optional[str]):
    return [item.strip() for item in (text or "").split(",") if item.strip()]


def execute_body(args, preflight: Dict) -> Dict:
    """由一次可行的预检结果拼出执行请求。只拷预检给出的字段，不自己算任何东西。"""
    if args.kind == "extension":
        return {"kind": "extension", "operation_id": args.operation_id, **preflight["fingerprints"]}
    proposal = preflight["proposal"]
    return {
        "kind": "admission",
        "operation_id": args.operation_id,
        "character_id": proposal["character_id"],
        "location_id": proposal["location_id"],
        "roommates": proposal["roommates"],
        "ordinal": proposal["ordinal"],
        "window": proposal["window"],
        "fingerprints": preflight["fingerprints"],
    }


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _outcome_code(status: int, payload) -> int:
    if status >= 400:
        return 2 if status in (409, 422) else 1
    outcome = payload.get("outcome") if isinstance(payload, dict) else None
    return 0 if outcome == "durable" else 3


def run(argv, *, send: Optional[Sender] = None) -> int:
    parser = argparse.ArgumentParser(description="WORLD-2 地点扩展与居民入住（运维）")
    parser.add_argument("--base-url", default=os.environ.get("PNS_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--world", default=DEFAULT_WORLD)
    sub = parser.add_subparsers(dest="command", required=True)

    def kind_args(p, *, optional=False):
        p.add_argument("kind", choices=("extension", "admission"), nargs="?" if optional else None)
        p.add_argument("--character")
        p.add_argument("--roommates", help="逗号分隔的室友 id")
        p.add_argument("--ordinal", type=int)

    pre = sub.add_parser("preflight", help="只读预检")
    kind_args(pre)
    exe = sub.add_parser("execute", help="执行一笔（先预检，记下请求体再发送）")
    kind_args(exe, optional=True)
    exe.add_argument("--operation-id")
    exe.add_argument("--request", help="重发一份记下来的请求体（JSON 文件），原样发送")
    exe.add_argument("--record", help="把要发出的请求体先写到这里（默认 world2-<operation_id>.json）")
    st = sub.add_parser("status", help="查询一笔（只读，不补存）")
    st.add_argument("operation_id")
    args = parser.parse_args(argv)

    if send is None:
        token = os.environ.get(ENV_TOKEN)
        if not token:
            print(f"需要环境变量 {ENV_TOKEN}（break-glass bearer token）", file=sys.stderr)
            return 1
        send = http_sender(args.base_url, token)
    client = World2Client(send, args.world)

    if args.command == "preflight":
        status, payload = client.preflight(preflight_body(args))
        _print({"http_status": status, **(payload if isinstance(payload, dict) else {"body": payload})})
        if status >= 400:
            return _outcome_code(status, payload)
        return 0 if payload.get("feasible") else 2

    if args.command == "status":
        status, payload = client.status(args.operation_id)
        _print({"http_status": status, **(payload if isinstance(payload, dict) else {"body": payload})})
        return 0 if status < 400 else 1

    if args.request:
        body = json.loads(Path(args.request).read_text(encoding="utf-8"))
    else:
        if not args.kind or not args.operation_id:
            parser.error("execute 需要 kind 与 --operation-id，或者 --request 文件")
        status, preflight = client.preflight(preflight_body(args))
        if status >= 400 or not preflight.get("feasible"):
            _print({"stage": "preflight", "http_status": status, **(preflight if isinstance(preflight, dict) else {})})
            return 2
        body = execute_body(args, preflight)
        record = Path(args.record or f"world2-{args.operation_id}.json")
        # 先落一份请求体：响应丢了也能用同一份请求重发，不必（也不能）再预检一次。
        record.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"请求体已记到 {record}", file=sys.stderr)
    status, payload = client.execute(body)
    _print({"http_status": status, **(payload if isinstance(payload, dict) else {"body": payload})})
    return _outcome_code(status, payload)


def main() -> int:  # pragma: no cover - 命令行入口
    return run(sys.argv[1:])


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
