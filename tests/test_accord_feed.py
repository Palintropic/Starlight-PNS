# tests/test_accord_feed.py — Accord feed（只读快照 + SSE）的不变量。
#
# 盯住的东西按"错了会怎样"排：
#   1. 开关关着时，PNS 跟没有这个模块一模一样：路由不存在，其它路由一条不多一条不少。
#      开关的值看不懂，进程起不来。
#   2. 只用 `read`：observer 能 GET；不登录 401；流活着时账户被停用，流当场结束。
#   3. 连续 200 条台词，一条不漏、顺序对；带 Last-Event-ID 重连从断点续上，不重复。
#   4. 世界关掉推 `world: stopped`；干净关闭再恢复是 `running` 并接着推；恢复丢掉了
#      客户端见过的台词，推 `world: restored` + 新 hello，旧台词不重放——包括恢复后同
#      一个下标上长出一条 **ID 相同、正文不同** 的台词那种情况。
#   5. 流里没有 overview 之外的字段；读取不改世界。
#
# 运行: python -m unittest tests.test_accord_feed -v
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts"), str(REPO_ROOT / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from fastapi.testclient import TestClient  # noqa: E402

from accounts_support import cheap_store  # noqa: E402
from pns.integrations.accord.feed import Cursor, CursorError, _LineChain  # noqa: E402
from pns.interfaces.accord import ENV_ACCORD_FEED, FeedSettings  # noqa: E402
from pns.interfaces.app import create_app  # noqa: E402
from pns.interfaces.composition import WorldControlPlane  # noqa: E402
from pns.interfaces.security import DeploymentConfigError, DeploymentSettings  # noqa: E402
from pns.models.event import Event, EventScope, EventType, new_event_id  # noqa: E402
from pns.runtime.event_commit import commit_session_event  # noqa: E402
from pns.runtime.reload import BOUNDARY  # noqa: E402

SCENE = "nightcord"
CHARACTERS = ["mizuki", "ena"]
CANARY = "CANARY-SECRET-6f3a9c2e-DO-NOT-LEAK"
ADMIN_TOKEN = "accord-test-admin-token-0123456789abcdef"
OBSERVER_PASSWORD = "accord-observer-password-1"

# 流里每种消息允许出现的字段。全部来自 overview 已经透出的东西。
ALLOWED_FIELDS = {
    "hello": {"node", "clock", "state", "residents", "cursor", "at"},
    "line": {"text", "speaker", "source", "kind", "place", "channel", "at"},
    "resident": {"id", "name", "place", "activity", "online", "at"},
    "clock": {"clock", "at"},
    "world": {"state", "at"},
}
RESIDENT_FIELDS = {"id", "name", "place", "activity", "online"}

# 测试用的快节拍：流活不了多久，TestClient 才拿得到完整正文。
FAST = FeedSettings(
    poll_seconds=0.01,
    keepalive_seconds=0.05,
    clock_seconds=0.0,
    recheck_seconds=0.02,
    lifetime_seconds=0.3,
)


class _FakeModelClient:
    def __getattr__(self, name):  # pragma: no cover - 被调用即测试有问题
        raise AssertionError(f"测试不该真的调用模型客户端（.{name}）")


def parse_sse(text):
    """把一段 SSE 正文拆成 [(id, event, data)]；注释和只有 retry 的块跳过。"""
    messages = []
    block = {}
    for line in text.split("\n"):
        if line == "":
            if "event" in block:
                messages.append(
                    (block.get("id"), block["event"], json.loads(block["data"]))
                )
            block = {}
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(": ")
        block[field] = value
    return messages


def stream_scope(client=None, query=b""):
    headers = [(b"host", b"testserver")]
    if client is not None:
        cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
        headers.append((b"cookie", cookie.encode()))
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/accord/worlds/nightcord/events",
        "raw_path": b"/api/accord/worlds/nightcord/events",
        "query_string": query,
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }


async def never_disconnect():
    await asyncio.Event().wait()


def drive_stream(
    app, *, client=None, on_start=None, on_hello=None, on_final=None, query=b"", timeout=5.0,
    expect_final=True,
):
    """不经 TestClient、直接按 ASGI 跑一条流，返回 (写出的正文, 耗时)。

    `on_start` / `on_hello` 在发响应头、写出 hello 的那次 send 里跑，可以卡在里面
    不返回（客户端读得慢、传输层背压）；`on_final` 在收尾那次 send 里跑。不管流
    怎么结束，收尾都得恰好一次——响应头都没发完就被掐断的除外（`expect_final=False`）。
    """
    scope = stream_scope(client, query)
    sent = []

    async def send(message):
        if message["type"] != "http.response.body":
            if on_start is not None:
                await on_start()
            return
        sent.append(message)
        if not message.get("more_body", False):
            if on_final is not None:
                await on_final()
        elif on_hello is not None and b"event: hello\n" in message.get("body", b""):
            await on_hello()

    async def run():
        began = asyncio.get_running_loop().time()
        await asyncio.wait_for(app(scope, never_disconnect, send), timeout=timeout)
        return asyncio.get_running_loop().time() - began

    elapsed = asyncio.run(run())
    finals = [m for m in sent if not m.get("more_body", False)]
    if expect_final:
        assert len(finals) == 1, f"收尾应当恰好一次，实际 {len(finals)} 次"
        assert sent[-1] is finals[0], "收尾之后不该再写"
    else:
        assert not sent, "响应头没发完，不该有正文"
    return "".join(m.get("body", b"").decode() for m in sent), elapsed


class _LockedAccounts:
    """另开一个连接把账户库独占锁住，模拟账户库被别的写入占着。"""

    def __init__(self, path):
        self._db = sqlite3.connect(path, isolation_level=None)

    def lock(self):
        self._db.execute("BEGIN EXCLUSIVE")

    def release(self):
        if self._db.in_transaction:
            self._db.execute("COMMIT")

    def close(self):
        self.release()
        self._db.close()


def lines_of(messages):
    return [data["text"] for _, event, data in messages if event == "line"]


class FeedTestCase(unittest.TestCase):
    """每个用例一个独立的存档根；开关开着。"""

    flag = "1"

    def setUp(self):
        self.registry = BOUNDARY.active()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "worlds"
        env = {self.registry.models.env_key_name: "test-key-not-a-real-one"}
        if self.flag is not None:
            env[ENV_ACCORD_FEED] = self.flag
        self._env = patch.dict(os.environ, env)
        self._env.start()
        self.addCleanup(self._env.stop)
        if self.flag is None:
            os.environ.pop(ENV_ACCORD_FEED, None)
        self.plane = WorldControlPlane(
            root=self.root, client_factory=lambda *a, **k: _FakeModelClient()
        )
        self.addCleanup(self.plane.service.release_all)
        self.app = self.build_app()
        if self.flag is not None:
            self.app.state.accord_feed_settings = FAST
        self.client = TestClient(self.app)

    def build_app(self):
        return create_app(self.plane)

    # ── 便捷 ────────────────────────────────────────────────────────────
    def open_world(self, world_id="nightcord"):
        response = self.client.post(
            "/api/persistent-worlds",
            json={"world_id": world_id, "scene": SCENE, "characters": CHARACTERS},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return self.plane.service.opened(world_id)

    def world(self, world_id="nightcord"):
        return self.plane.service.opened(world_id)

    def say(self, text, *, speaker="mizuki", event_id=None, world=None):
        """在说话人所在的地点提交一句台词，在场名单照实给。返回它的下标。"""
        world = world if world is not None else self.world()
        ws = world.state.world_state
        location_id = ws.location_of(speaker)
        event = Event(
            event_id=event_id or new_event_id("line"),
            type=EventType.DIALOGUE_SPOKEN,
            occurred_at=ws.clock,
            scope=EventScope.LOCATION,
            actor_id=speaker,
            participants=tuple(ws.characters_at(location_id)),
            location_id=location_id,
            payload={"text": text, "char_name": speaker},
        )
        commit_session_event(world.state, event)
        return world.state.events.sequence_of(event.event_id)

    def post(self, text, *, speaker="mizuki", channel_id="nightcord"):
        world = self.world()
        ws = world.state.world_state
        online = [cid for cid in ws.known_characters() if channel_id in ws.channels_for(cid)]
        event = Event(
            event_id=new_event_id("msg"),
            type=EventType.MESSAGE_SENT,
            occurred_at=ws.clock,
            scope=EventScope.CHANNEL,
            actor_id=speaker,
            participants=tuple(online),
            location_id=None,
            channel_id=channel_id,
            payload={"text": text},
        )
        commit_session_event(world.state, event)

    def stream(self, world_id="nightcord", *, client=None, headers=None, **params):
        client = client if client is not None else self.client
        response = client.get(
            f"/api/accord/worlds/{world_id}/events", headers=headers, params=params
        )
        return response

    def messages(self, world_id="nightcord", **kwargs):
        response = self.stream(world_id, **kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        return parse_sse(response.text)

    def hello(self, world_id="nightcord", client=None):
        client = client if client is not None else self.client
        return client.get(f"/api/accord/worlds/{world_id}/hello")

    def feed(self):
        return self.app.state.accord_feed


# ── 1. 开关 ─────────────────────────────────────────────────────────────
class FlagOffTests(FeedTestCase):
    flag = None

    def test_the_routes_do_not_exist_when_the_flag_is_off(self):
        self.open_world()
        for path in (
            "/api/accord/worlds",
            "/api/accord/worlds/nightcord/hello",
            "/api/accord/worlds/nightcord/events",
        ):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertFalse(hasattr(self.app.state, "accord_feed"))
        overview = self.client.get("/api/persistent-worlds/nightcord/overview")
        self.assertEqual(overview.status_code, 200, overview.text)

    def test_off_means_exactly_the_routes_there_were_before(self):
        def routes(app):
            return {
                (path, tuple(sorted(method.upper() for method in operations)))
                for path, operations in app.openapi()["paths"].items()
            }

        off = routes(self.app)
        with patch.dict(os.environ, {ENV_ACCORD_FEED: "1"}):
            on = routes(create_app(WorldControlPlane(root=self.tmp / "other")))
        added = on - off
        self.assertEqual(off - on, set())
        self.assertTrue(added)
        for path, methods in added:
            self.assertTrue(path.startswith("/api/accord/"), path)
            self.assertEqual(methods, ("GET",), path)

    def test_explicit_off_values_are_off(self):
        for value in ("0", "false", "off", ""):
            with patch.dict(os.environ, {ENV_ACCORD_FEED: value}):
                app = create_app(WorldControlPlane(root=self.tmp / "other"))
                self.assertFalse(hasattr(app.state, "accord_feed"), value)

    def test_an_unreadable_flag_refuses_to_start(self):
        with patch.dict(os.environ, {ENV_ACCORD_FEED: "maybe"}):
            with self.assertRaises(DeploymentConfigError):
                create_app(WorldControlPlane(root=self.tmp / "other"))


# ── 快照与列表 ──────────────────────────────────────────────────────────
class HelloTests(FeedTestCase):
    def test_an_open_world_says_who_is_where(self):
        self.open_world()
        changed = self.client.post(
            "/api/persistent-worlds/nightcord/activity",
            json={"character_id": "mizuki", "activity": "editing_video"},
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        response = self.hello()
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(
            body["node"],
            {"id": "pns:nightcord", "name": "nightcord", "protocol": 1, "kind": "world"},
        )
        self.assertEqual(body["state"], "running")
        self.assertEqual(body["clock"], self.world().state.world_state.clock.isoformat())
        residents = {item["id"]: item for item in body["residents"]}
        self.assertEqual(set(residents), set(CHARACTERS))
        mizuki = residents["mizuki"]
        self.assertEqual(set(mizuki), RESIDENT_FIELDS)
        self.assertEqual(mizuki["name"], self.registry.character_name("mizuki"))
        self.assertEqual(mizuki["activity"], "editing_video")
        ws = self.world().state.world_state
        names = {loc.location_id: loc.name for loc in ws.locations}
        channels = {ch.channel_id: ch.name for ch in ws.channels}
        self.assertEqual(mizuki["place"], names[ws.location_of("mizuki")])
        self.assertEqual(
            mizuki["online"], [channels[cid] for cid in ws.channels_for("mizuki")]
        )
        self.assertTrue(mizuki["online"])
        Cursor.parse(body["cursor"])
        self.assertEqual(set(body), ALLOWED_FIELDS["hello"])

    def test_a_closed_world_is_stopped_without_residents_or_cursor(self):
        self.open_world()
        self.assertEqual(
            self.client.post("/api/persistent-worlds/nightcord/close").status_code, 200
        )
        body = self.hello().json()
        self.assertEqual(body["state"], "stopped")
        self.assertEqual(body["residents"], [])
        self.assertIsNone(body["cursor"])
        self.assertIsNotNone(body["clock"])

    def test_unknown_and_invalid_worlds(self):
        missing = self.hello("never-made")
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertEqual(missing.json()["detail"]["category"], "archive_not_found")
        self.assertEqual(self.stream("never-made").status_code, 404)
        invalid = self.hello("CON")
        self.assertEqual(invalid.status_code, 400, invalid.text)
        self.assertEqual(invalid.json()["detail"]["category"], "invalid_world_id")

    def test_the_world_list_is_id_name_state(self):
        self.assertEqual(self.client.get("/api/accord/worlds").json(), [])
        self.open_world()
        self.open_world("second")
        self.client.post("/api/persistent-worlds/second/close")
        listed = self.client.get("/api/accord/worlds").json()
        self.assertEqual(
            listed,
            [
                {"id": "nightcord", "name": "nightcord", "state": "running"},
                {"id": "second", "name": "second", "state": "stopped"},
            ],
        )

    def test_reading_the_feed_does_not_touch_the_world(self):
        self.open_world()
        self.say("先存一句")
        self.client.post("/api/persistent-worlds/nightcord/checkpoint")
        before = self.client.get("/api/persistent-worlds/nightcord").json()
        self.hello()
        self.messages()
        self.client.get("/api/accord/worlds")
        after = self.client.get("/api/persistent-worlds/nightcord").json()
        self.assertEqual(before["revision"], after["revision"])
        self.assertEqual(before["dirty"], after["dirty"])
        self.assertFalse(after["dirty"])


# ── 3. 事件流：顺序、不漏、续传 ──────────────────────────────────────────
class StreamTests(FeedTestCase):
    def test_the_stream_is_sse_and_starts_with_hello(self):
        self.open_world()
        response = self.stream()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertTrue(response.text.startswith("retry: "))
        messages = parse_sse(response.text)
        self.assertEqual(messages[0][1], "hello")
        self.assertEqual(messages[0][0], messages[0][2]["cursor"])
        self.assertIn(": ping", response.text)

    def test_two_hundred_lines_arrive_in_order_while_the_stream_is_open(self):
        self.open_world()
        self.app.state.accord_feed_settings = FeedSettings(
            poll_seconds=0.01, keepalive_seconds=1.0, clock_seconds=60.0,
            recheck_seconds=1.0, lifetime_seconds=2.0,
        )
        expected = [f"第 {i} 句" for i in range(200)]
        started = threading.Event()

        def talk():
            started.wait(5)
            time.sleep(0.1)
            for text in expected:
                self.say(text)
                time.sleep(0.002)

        thread = threading.Thread(target=talk)
        thread.start()
        started.set()
        messages = self.messages()
        thread.join()
        self.assertEqual(messages[0][1], "hello")
        self.assertEqual(lines_of(messages), expected)
        ids = [Cursor.parse(mid).seq for mid, event, _ in messages if event == "line"]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), 200)

    def test_last_event_id_resumes_exactly_after_the_break(self):
        self.open_world()
        start = self.hello().json()["cursor"]
        expected = [f"第 {i} 句" for i in range(200)]
        for text in expected:
            self.say(text)
        first = self.messages(after=start)
        lines = [(mid, data["text"]) for mid, event, data in first if event == "line"]
        self.assertEqual([text for _, text in lines], expected)

        broke_at = lines[99][0]
        resumed = self.messages(headers={"Last-Event-ID": broke_at})
        self.assertEqual(resumed[0][1], "hello")
        # hello 的 id 是客户端自己的位置：断在补发途中也不跳句。
        self.assertEqual(resumed[0][0], broke_at)
        self.assertEqual(lines_of(resumed), expected[100:])

        # Last-Event-ID 优先于 ?after=（浏览器重连时只会改头，不会改 URL）。
        both = self.messages(after=start, headers={"Last-Event-ID": broke_at})
        self.assertEqual(lines_of(both), expected[100:])

        # 已经送完的位置重连：一句都不重复。
        final = [mid for mid, event, _ in first if event == "line"][-1]
        self.assertEqual(lines_of(self.messages(headers={"Last-Event-ID": final})), [])

    def test_channel_messages_and_location_lines_say_where(self):
        self.open_world()
        start = self.hello().json()["cursor"]
        self.say("在房间里说")
        self.post("在 Nightcord 里说")
        lines = [data for _, event, data in self.messages(after=start) if event == "line"]
        self.assertEqual([line["kind"] for line in lines], ["dialogue", "message"])
        ws = self.world().state.world_state
        places = {loc.location_id: loc.name for loc in ws.locations}
        channels = {ch.channel_id: ch.name for ch in ws.channels}
        self.assertEqual(lines[0]["place"], places[ws.location_of("mizuki")])
        self.assertNotIn("channel", lines[0])
        self.assertEqual(lines[1]["channel"], channels["nightcord"])
        self.assertNotIn("place", lines[1])
        for line in lines:
            self.assertEqual(line["speaker"], "mizuki")
            self.assertEqual(line["source"], "world")

    def test_bad_cursors_are_refused_before_the_stream_starts(self):
        self.open_world()
        for bad in ("12", "abc.def", "1.zzzzzzzzzzzzzzzz", "-2.0000000000000000"):
            response = self.stream(after=bad)
            self.assertEqual(response.status_code, 400, bad)
            self.assertEqual(response.json()["detail"]["category"], "invalid_cursor")
        self.assertEqual(self.stream(after="1." + "0" * 80).status_code, 422)

    def test_the_http_stream_carries_no_internal_structure(self):
        self.open_world()
        start = self.hello().json()["cursor"]
        self.say("一句台词")
        text = self.stream(after=start).text
        for leak in ("archive_path", "provenance", "session_id", "participants", str(self.root)):
            self.assertNotIn(leak, text)
        for _, event, data in parse_sse(text):
            self.assertLessEqual(set(data), ALLOWED_FIELDS[event], event)


# ── 4. 世界开关与恢复（直接驱动会话，节拍完全可控）───────────────────────
class SessionTests(FeedTestCase):
    def subscribe(self, after=None, **kwargs):
        return self.feed().subscribe("nightcord", after, **kwargs)

    def test_a_closed_world_is_stopped_and_a_clean_restore_carries_on(self):
        self.open_world()
        session = self.subscribe()
        self.assertEqual([m.event for m in session.poll()], ["hello"])
        self.say("关之前")
        self.assertEqual([m.data["text"] for m in session.poll() if m.event == "line"], ["关之前"])

        self.client.post("/api/persistent-worlds/nightcord/close")
        stopped = session.poll()
        self.assertEqual([(m.event, m.data["state"]) for m in stopped], [("world", "stopped")])
        self.assertEqual(session.poll(), [])

        self.client.post("/api/persistent-worlds/nightcord/restore")
        self.say("恢复之后")
        resumed = session.poll()
        self.assertEqual(resumed[0].event, "world")
        self.assertEqual(resumed[0].data["state"], "running")
        self.assertNotIn("restored", [m.data.get("state") for m in resumed])
        self.assertEqual([m.data["text"] for m in resumed if m.event == "line"], ["恢复之后"])

    def test_a_restore_that_lost_lines_says_restored_and_does_not_replay(self):
        self.open_world()
        self.say("存下来的")
        self.client.post("/api/persistent-worlds/nightcord/checkpoint")
        session = self.subscribe()
        session.poll()
        self.say("没存下来的")
        self.assertEqual(
            [m.data["text"] for m in session.poll() if m.event == "line"], ["没存下来的"]
        )
        # 不存就放手：恢复只能回到上一次 checkpoint。
        self.plane.service.release_all()
        self.assertEqual([m.data["state"] for m in session.poll()], ["stopped"])
        self.plane.restore("nightcord")
        self.say("新的一句")
        after = session.poll()
        self.assertEqual([m.event for m in after], ["world", "hello"])
        self.assertEqual(after[0].data["state"], "restored")
        self.assertEqual(after[1].data["state"], "running")
        self.assertEqual(lines_of([(m.id, m.event, m.data) for m in after]), [])
        # 恢复之后的新台词从新 hello 的位置往后推，旧的一句不重放。
        self.say("再一句")
        self.assertEqual([m.data["text"] for m in session.poll() if m.event == "line"], ["再一句"])

    def test_the_same_event_id_at_the_same_seq_with_new_text_is_still_restored(self):
        world = self.open_world()
        self.client.post("/api/persistent-worlds/nightcord/checkpoint")
        session = self.subscribe()
        session.poll()
        line_id = f"{world.state.session_id}:t1:dialogue"
        seq = self.say("原来那句", event_id=line_id)
        seen = [m for m in session.poll() if m.event == "line"]
        self.assertEqual([m.data["text"] for m in seen], ["原来那句"])
        cursor = Cursor.parse(seen[-1].id)

        self.plane.service.release_all()
        session.poll()
        self.plane.restore("nightcord")
        # 前提：恢复后同一个下标上长出一条同 ID 的台词。只比 ID 的游标会被骗过去。
        self.assertEqual(self.say("换了一句", event_id=line_id), seq)
        after = session.poll()
        self.assertEqual(after[0].data, {"state": "restored", "at": after[0].data["at"]})
        self.assertNotIn("原来那句", json.dumps([m.data for m in after], ensure_ascii=False))

        # 带着旧位置重新连上的客户端也一样：先 restored，再 hello，不补发。
        fresh = self.subscribe(cursor).poll()
        self.assertEqual([m.event for m in fresh], ["world", "hello"])
        self.assertEqual(fresh[0].data["state"], "restored")

    def test_a_stale_cursor_at_connect_is_restored_then_hello(self):
        self.open_world()
        self.say("一句")
        forged = Cursor(0, "0" * 16)
        messages = self.subscribe(forged).poll()
        self.assertEqual([m.event for m in messages], ["world", "hello"])
        beyond = self.subscribe(Cursor(5000, "0" * 16)).poll()
        self.assertEqual([m.event for m in beyond], ["world", "hello"])

    def test_connecting_while_stopped_then_the_world_opens(self):
        self.open_world()
        start = self.feed().hello("nightcord")["cursor"]
        self.say("关之前说的")
        self.client.post("/api/persistent-worlds/nightcord/close")

        # 带着位置等：世界开起来以后补发它没见过的那句。
        waiting = self.subscribe(Cursor.parse(start))
        hello = waiting.poll()
        self.assertEqual([(m.event, m.data["state"]) for m in hello], [("hello", "stopped")])
        self.assertEqual(hello[0].id, start)
        # 不带位置等：开起来以后给一份新快照。
        blank = self.subscribe()
        blank.poll()

        self.client.post("/api/persistent-worlds/nightcord/restore")
        resumed = waiting.poll()
        self.assertEqual(resumed[0].data["state"], "running")
        self.assertEqual([m.data["text"] for m in resumed if m.event == "line"], ["关之前说的"])
        self.assertEqual(
            {m.data["id"] for m in resumed if m.event == "resident"}, set(CHARACTERS)
        )
        fresh = blank.poll()
        self.assertEqual([m.event for m in fresh], ["world", "hello"])
        self.assertEqual(fresh[0].data["state"], "running")

    def test_resident_changes_and_a_throttled_clock(self):
        world = self.open_world()
        now = [100.0]
        session = self.subscribe(clock_interval=60.0, now=lambda: now[0])
        session.poll()
        self.client.post(
            "/api/persistent-worlds/nightcord/activity",
            json={"character_id": "ena", "activity": "drawing"},
        )
        changed = session.poll()
        self.assertEqual([(m.event, m.data["id"]) for m in changed], [("resident", "ena")])
        self.assertEqual(changed[0].data["activity"], "drawing")
        self.assertEqual(session.poll(), [])

        # 让读到的时钟往前走一分钟，世界本身不动。
        feed = self.feed()
        read = feed._read
        later = (world.state.world_state.clock + timedelta(minutes=1)).isoformat()
        with patch.object(
            feed, "_read", lambda w, **kw: replace(read(w, **kw), clock=later)
        ):
            now[0] += 10
            self.assertEqual(session.poll(), [], "一分钟里最多一条时钟")
            now[0] += 60
            ticked = session.poll()
            self.assertEqual(session.poll(), [], "时钟没再变就不推")
        self.assertEqual([m.event for m in ticked], ["clock"])
        self.assertEqual(ticked[0].data, {"clock": later, "at": later})

    def test_a_busy_world_skips_a_round_instead_of_failing(self):
        from pns.models.session import TransactionBoundaryError

        world = self.open_world()
        session = self.subscribe()
        session.poll()
        self.say("等一下")

        def refuse(*args, **kwargs):
            raise TransactionBoundaryError("有一次提交一直占着它")

        with patch.object(type(world.state), "snapshot_boundary", refuse):
            self.assertEqual(session.poll(), [])
        self.assertEqual([m.data["text"] for m in session.poll() if m.event == "line"], ["等一下"])

    def test_a_first_read_that_times_out_still_starts_with_hello(self):
        from pns.models.session import TransactionBoundaryError

        self.open_world()
        cursor = Cursor.parse(self.feed().hello("nightcord")["cursor"])
        self.say("断线时说的")
        session = self.subscribe(cursor)

        def refuse(*args, **kwargs):
            raise TransactionBoundaryError("有一次提交一直占着它")

        with patch.object(type(self.world().state), "snapshot_boundary", refuse):
            self.assertEqual(session.poll(), [])
        resumed = session.poll()
        self.assertEqual(resumed[0].event, "hello", "第一次没读成，下一轮也得先打招呼")
        self.assertEqual(resumed[0].id, cursor.encode())
        self.assertEqual([m.data["text"] for m in resumed if m.event == "line"], ["断线时说的"])
        self.assertNotIn("hello", [m.event for m in session.poll()])

    def test_a_first_read_fenced_then_closed_is_a_stopped_hello(self):
        from pns.models.session import SessionFencedError

        self.open_world()
        session = self.subscribe()

        def fenced(*args, **kwargs):
            raise SessionFencedError("读到一半被关上了")

        with patch.object(type(self.world().state), "snapshot_boundary", fenced):
            self.assertEqual(session.poll(), [])
        self.client.post("/api/persistent-worlds/nightcord/close")
        self.assertEqual(
            [(m.event, m.data["state"]) for m in session.poll()], [("hello", "stopped")]
        )

    def test_a_line_that_moved_to_another_seq_is_restored(self):
        world = self.open_world()
        ws = world.state.world_state
        location = ws.location_of("mizuki")
        line = Event(
            event_id=f"{world.state.session_id}:t1:dialogue",
            type=EventType.DIALOGUE_SPOKEN,
            occurred_at=ws.clock,
            scope=EventScope.LOCATION,
            actor_id="mizuki",
            participants=tuple(ws.characters_at(location)),
            location_id=location,
            payload={"text": "同一句"},
        )
        first = Event(
            event_id="moved:activity1",
            type=EventType.CHARACTER_ACTIVITY_CHANGED,
            occurred_at=ws.clock,
            scope=EventScope.PRIVATE,
            actor_id="ena",
            payload={"activity": "drawing"},
        )
        last = replace(first, event_id="moved:activity2", payload={"activity": "resting"})
        session = self.subscribe(clock_interval=0)
        session.poll()
        for event in (line, first, last):
            commit_session_event(world.state, event)
        world.runtime.advance(1)
        session.poll()
        cursor = session.cursor
        before = world.state.events.sequence_of(line.event_id)
        tail = world.state.events.latest()

        self.plane.service.release_all()
        self.plane.restore("nightcord")
        world = self.world()
        for event in (first, line, last):
            commit_session_event(world.state, event)
        world.runtime.advance(1)
        # 前提：同一句台词挪了一个下标，末尾那条事件跟原来一模一样。
        self.assertNotEqual(world.state.events.sequence_of(line.event_id), before)
        self.assertEqual(world.state.events.latest(), tail)
        fresh = self.subscribe(cursor).poll()
        self.assertEqual([m.event for m in fresh], ["world", "hello"])
        self.assertEqual(fresh[0].data["state"], "restored")

    def test_a_read_that_outlives_the_stream_is_not_written(self):
        world = self.open_world()
        cursor = self.feed().hello("nightcord")["cursor"]
        self.app.state.accord_feed_settings = replace(FAST, recheck_seconds=60.0, lifetime_seconds=0.04)
        entered, release = threading.Event(), threading.Event()

        def writer():
            # 占着提交闸门，让流的第一次读一直等；放手前说一句。
            with world.state.atomic_commit():
                entered.set()
                release.wait(2)
                self.say("读完时寿命已经到了")

        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(entered.wait(1))

        async def no_op():
            pass

        threading.Timer(0.12, release.set).start()
        try:
            # 写出不等待：读完之后要是不再查一次，这一批在取消生效前就写出去了。
            body, _ = drive_stream(self.app, query=f"after={cursor}".encode(), on_final=no_op)
        finally:
            release.set()
            thread.join(2)
        self.assertNotIn("读完时寿命已经到了", body)

    def test_a_stream_cut_off_mid_read_is_ended_exactly_once(self):
        world = self.open_world()
        self.app.state.accord_feed_settings = replace(FAST, recheck_seconds=60.0, lifetime_seconds=0.04)
        entered, release = threading.Event(), threading.Event()

        def writer():
            with world.state.atomic_commit():
                entered.set()
                release.wait(2)

        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(entered.wait(1))

        async def yield_once():
            await asyncio.sleep(0)

        threading.Timer(0.12, release.set).start()
        try:
            drive_stream(self.app, on_final=yield_once)
        finally:
            release.set()
            thread.join(2)

    def test_every_kind_of_message_stays_inside_the_overview(self):
        self.open_world()
        self.client.post("/api/persistent-worlds/nightcord/checkpoint")
        session = self.subscribe(clock_interval=0.0)
        produced = list(session.poll())
        # 改活动会当场存盘，所以放在台词前面：台词留在最后一次 checkpoint 之后。
        self.client.post(
            "/api/persistent-worlds/nightcord/activity",
            json={"character_id": "ena", "activity": "drawing"},
        )
        self.say("说一句")
        self.post("发一条")
        produced += session.poll()
        self.plane.service.release_all()
        produced += session.poll()
        self.plane.restore("nightcord")
        produced += session.poll()
        kinds = {m.event for m in produced}
        self.assertLessEqual({"hello", "line", "resident", "world"}, kinds)
        for message in produced:
            self.assertLessEqual(set(message.data), ALLOWED_FIELDS[message.event], message)
            if message.event == "resident":
                self.assertEqual(set(message.data), ALLOWED_FIELDS["resident"])
            if message.event == "hello":
                for resident in message.data["residents"]:
                    self.assertEqual(set(resident), RESIDENT_FIELDS)
        self.assertEqual(
            {m.data["state"] for m in produced if m.event == "world"}, {"stopped", "restored"}
        )

    def test_two_subscriptions_extending_out_of_order_share_one_chain(self):
        # 两条流各自读，读得早的那份（更短）晚一步去扩展台词链：不许报错，也不许
        # 把链缩回去——游标要跟按顺序扩展出来的一模一样。
        world = self.open_world()
        self.say("一")
        short = world.state.events.events()
        self.say("二")
        self.say("三")
        long = world.state.events.events()
        out_of_order, in_order = _LineChain(), _LineChain()
        out_of_order.extend(long)
        out_of_order.extend(short)
        in_order.extend(long)
        for seq in range(len(long)):
            self.assertEqual(out_of_order.cursor_at(long, seq), in_order.cursor_at(long, seq))
        self.say("四")
        latest = world.state.events.events()
        out_of_order.extend(latest)
        in_order.extend(latest)
        self.assertEqual(
            out_of_order.cursor_at(latest, len(latest) - 1),
            in_order.cursor_at(latest, len(latest) - 1),
        )

    def test_cursor_round_trip_and_refusals(self):
        cursor = Cursor(42, "0123456789abcdef")
        self.assertEqual(Cursor.parse(cursor.encode()), cursor)
        for bad in (None, 3, "", "42", "42.0123", "042.0123456789abcdef", "4 2.0123456789abcdef"):
            with self.assertRaises(CursorError, msg=repr(bad)):
                Cursor.parse(bad)


# ── 2. 权限：只用 read ───────────────────────────────────────────────────
class AccessTests(FeedTestCase):
    def build_app(self):
        self.accounts = cheap_store(self.tmp / "accounts.sqlite3")
        self.addCleanup(self.accounts.close)
        self.observer = self.accounts.create_human(
            "accord-desk", OBSERVER_PASSWORD, "observer"
        )
        return create_app(
            self.plane,
            settings=DeploymentSettings(mode="development", admin_token=ADMIN_TOKEN),
            account_store=self.accounts,
            registry_provider=lambda: self.registry,
        )

    def admin(self):
        return {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    def observer_client(self):
        client = TestClient(self.app)
        response = client.post(
            "/api/auth/login",
            json={"username": "accord-desk", "password": OBSERVER_PASSWORD},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def test_an_observer_can_read_and_anonymous_cannot(self):
        created = self.client.post(
            "/api/persistent-worlds",
            json={"world_id": "nightcord", "scene": SCENE, "characters": CHARACTERS},
            headers=self.admin(),
        )
        self.assertEqual(created.status_code, 201, created.text)
        observer = self.observer_client()
        self.assertEqual(observer.get("/api/accord/worlds").status_code, 200)
        self.assertEqual(self.hello(client=observer).status_code, 200)
        streamed = self.stream(client=observer)
        self.assertEqual(streamed.status_code, 200, streamed.text)
        self.assertEqual(parse_sse(streamed.text)[0][1], "hello")
        for path in (
            "/api/accord/worlds",
            "/api/accord/worlds/nightcord/hello",
            "/api/accord/worlds/nightcord/events",
        ):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        # observer 能读，但守卫照旧按方法挡写：没有一条 POST 因为挂在 accord 下就放行。
        self.assertEqual(observer.post("/api/accord/worlds").status_code, 403)

    def test_a_foreign_origin_gets_what_the_overview_gets(self):
        self.client.post(
            "/api/persistent-worlds",
            json={"world_id": "nightcord", "scene": SCENE, "characters": CHARACTERS},
            headers=self.admin(),
        )
        observer = self.observer_client()
        foreign = {"Origin": "https://elsewhere.example"}
        overview = observer.get(
            "/api/persistent-worlds/nightcord/overview", headers=foreign
        ).status_code
        hello = observer.get("/api/accord/worlds/nightcord/hello", headers=foreign).status_code
        self.assertEqual(hello, overview)

    def test_disabling_the_account_ends_a_live_stream(self):
        self.client.post(
            "/api/persistent-worlds",
            json={"world_id": "nightcord", "scene": SCENE, "characters": CHARACTERS},
            headers=self.admin(),
        )
        self.app.state.accord_feed_settings = FeedSettings(
            poll_seconds=0.01, keepalive_seconds=0.05, clock_seconds=60.0,
            recheck_seconds=0.02, lifetime_seconds=10.0,
        )
        observer = self.observer_client()
        timer = threading.Timer(
            0.3,
            lambda: self.accounts.set_authority(self.observer.principal_id, enabled=False),
        )
        began = time.monotonic()
        timer.start()
        response = self.stream(client=observer)
        elapsed = time.monotonic() - began
        timer.join()
        self.assertEqual(response.status_code, 200)
        self.assertLess(elapsed, 5.0, "停用账户之后流应当当场结束，而不是活到寿命上限")
        self.assertEqual(self.hello(client=observer).status_code, 401)


    def _observer_on_open_world(self, **settings):
        created = self.client.post(
            "/api/persistent-worlds",
            json={"world_id": "nightcord", "scene": SCENE, "characters": CHARACTERS},
            headers=self.admin(),
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.app.state.accord_feed_settings = replace(FAST, **settings)
        return self.observer_client()

    def test_a_stalled_send_does_not_outlive_a_revoked_account(self):
        observer = self._observer_on_open_world(recheck_seconds=0.05, lifetime_seconds=30.0)

        async def stall():
            self.accounts.set_authority(self.observer.principal_id, enabled=False)
            self.say("停用以后才说的")
            await asyncio.sleep(3.0)

        body, elapsed = drive_stream(self.app, client=observer, on_hello=stall)
        self.assertNotIn("停用以后才说的", body)
        self.assertLess(elapsed, 2.0, "复核不该等卡住的那次写出返回")
        self.assertEqual(self.hello(client=observer).status_code, 401)

    def test_a_stalled_send_does_not_outlive_the_lifetime(self):
        observer = self._observer_on_open_world(recheck_seconds=60.0, lifetime_seconds=0.1)

        async def stall():
            await asyncio.sleep(0.2)
            self.say("寿命到了以后才说的")

        body, elapsed = drive_stream(self.app, client=observer, on_hello=stall)
        self.assertNotIn("寿命到了以后才说的", body)
        self.assertLess(elapsed, 2.0)

    def test_a_stalled_response_start_does_not_outlive_the_lifetime(self):
        observer = self._observer_on_open_world(recheck_seconds=60.0, lifetime_seconds=0.05)

        async def stall():
            await asyncio.sleep(1.0)

        _, elapsed = drive_stream(
            self.app, client=observer, on_start=stall, expect_final=False
        )
        self.assertLess(elapsed, 0.5, "响应头那次 send 卡住时看门狗也要在")

    def test_an_account_revoked_while_the_stream_queued_gets_nothing(self):
        from pns.integrations.accord.feed import AccordFeed

        observer = self._observer_on_open_world(recheck_seconds=30.0, lifetime_seconds=30.0)
        subscribe = AccordFeed.subscribe

        def revoke_then_subscribe(feed, *args, **kwargs):
            # 守卫已经放行、流还没开始：排队期间账户被停用。
            self.accounts.set_authority(self.observer.principal_id, enabled=False)
            return subscribe(feed, *args, **kwargs)

        with patch.object(AccordFeed, "subscribe", revoke_then_subscribe):
            body, elapsed = drive_stream(self.app, client=observer)
        self.assertNotIn("event: hello", body)
        self.assertLess(elapsed, 2.0, "第一次写之前就该认出来，不等下一个复核周期")

    def test_rechecks_stuck_on_the_account_store_are_capped(self):
        from pns.interfaces.accord import RecheckSlots

        observer = self._observer_on_open_world(
            recheck_seconds=0.05, recheck_timeout_seconds=0.1, lifetime_seconds=30.0
        )
        self.app.state.accord_rechecks = RecheckSlots(2)
        locked = _LockedAccounts(self.accounts.path)
        self.addCleanup(locked.close)
        streams = 5

        def in_recheck():
            count = 0
            for frame in sys._current_frames().values():
                while frame is not None:
                    if frame.f_code.co_name == "_still_allowed":
                        count += 1
                        break
                    frame = frame.f_back
            return count

        async def run():
            hellos = 0

            async def send(message):
                nonlocal hellos
                if b"event: hello\n" in message.get("body", b""):
                    hellos += 1
                    if hellos == streams:
                        locked.lock()

            scope = stream_scope(observer)
            await asyncio.wait_for(
                asyncio.gather(*(self.app(scope, never_disconnect, send) for _ in range(streams))),
                timeout=5,
            )

        try:
            asyncio.run(run())
            self.assertLessEqual(in_recheck(), 2, "卡在账户库里的复核线程不该比名额多")
        finally:
            locked.release()
        deadline = time.monotonic() + 5
        while in_recheck() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(in_recheck(), 0)

    def test_a_locked_account_store_does_not_stretch_the_revocation_window(self):
        observer = self._observer_on_open_world(
            recheck_seconds=0.2, recheck_timeout_seconds=5.0, lifetime_seconds=30.0
        )
        locked = _LockedAccounts(self.accounts.path)
        self.addCleanup(locked.close)

        async def revoke_then_lock():
            self.accounts.set_authority(self.observer.principal_id, enabled=False)
            locked.lock()
            loop = asyncio.get_running_loop()
            # 到点该重认了，重认卡在账户库上：这段时间里说的话不能写出去。
            loop.call_later(0.35, self.say, "复核卡住时说的")
            loop.call_later(0.7, locked.release)

        body, elapsed = drive_stream(self.app, client=observer, on_hello=revoke_then_lock)
        self.assertNotIn("复核卡住时说的", body)
        self.assertLess(elapsed, 2.0)
        self.assertEqual(self.hello(client=observer).status_code, 401)

    def test_a_recheck_that_never_answers_ends_the_stream(self):
        observer = self._observer_on_open_world(
            recheck_seconds=0.05, recheck_timeout_seconds=0.2, lifetime_seconds=30.0
        )
        locked = _LockedAccounts(self.accounts.path)
        self.addCleanup(locked.close)

        async def lock():
            locked.lock()

        try:
            body, elapsed = drive_stream(self.app, client=observer, on_hello=lock)
        finally:
            locked.release()
        self.assertLess(elapsed, 1.0, "认不出来就按认不出来处理，不等账户库")
        self.assertIn("event: hello", body)

    def test_a_locked_account_store_does_not_hold_off_the_lifetime(self):
        observer = self._observer_on_open_world(
            recheck_seconds=0.02, recheck_timeout_seconds=5.0, lifetime_seconds=0.1
        )
        locked = _LockedAccounts(self.accounts.path)
        self.addCleanup(locked.close)

        async def lock_and_stall():
            locked.lock()
            await asyncio.sleep(3.0)

        try:
            body, elapsed = drive_stream(self.app, client=observer, on_hello=lock_and_stall)
        finally:
            locked.release()
        self.assertLess(elapsed, 0.5, "寿命不跟着身份复核一起卡住")

if __name__ == "__main__":
    unittest.main()
