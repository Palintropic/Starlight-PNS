# tests/test_formal_world.py — 正式世界「夜明け前」的开局与内容门（WORLD-1 计划 §4.1、§4.3、§5.2）。
#
# 守的线：
#   1. 开局时刻是启动当天（Asia/Tokyo）的 19:00，按世界时区判"当天"；
#   2. 初始状态全部由内容推出：授予、位置、活动、频道在场取自作息表在 19:00 的那一段；
#      不写任何世界事件，也不给角色写开场白；
#   3. 开局来源（身份、时区、开局时刻、创建时刻、内容版本与指纹、操作者）写进
#      origin，不进任何角色的上下文；
#   4. 只能走正式开局入口，不能从遗留场景建，已有存档不覆盖；
#   5. 时间与作息从开局起就在走，跨过 01:00 进 Nightcord，不按 Start 不调模型；
#   6. 内容门：内容包里出现新一版作息，记一条待决、不生效；明确采用之后下次打开才生效；
#      账本随存档往返，篡改被拒。
#
# 运行: python -m unittest discover -s tests -p test_formal_world.py
import dataclasses
import os
import sys
import tempfile
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from fastapi.testclient import TestClient  # noqa: E402

from pns.interfaces.app import create_app  # noqa: E402
from pns.interfaces.composition import AutonomySettings, WorldControlPlane  # noqa: E402
from pns.models.content_ledger import (  # noqa: E402
    ConflictStatus,
    ContentLedger,
    ContentLedgerError,
    content_fingerprint,
)
from pns.models.session import SessionState, SessionStateError  # noqa: E402
from pns.models.world_state import ActivityKind  # noqa: E402
from pns.runtime.autonomy.clock_worker import ClockConfig  # noqa: E402
from pns.runtime.autonomy.seeding import ActivationCadence  # noqa: E402
from pns.runtime.formal_world import (  # noqa: E402
    ABSENT,
    YOAKE_MAE,
    FormalWorldError,
    formal_session_state,
    gated_rhythms,
    rhythm_fingerprint,
    rhythm_subject,
)
from pns.runtime.persistence.lifecycle import _fingerprint  # noqa: E402
from pns.runtime.reload import BOUNDARY  # noqa: E402
from pns.world.context import render_world_context  # noqa: E402
from pns.world.rhythm import DailyRhythm  # noqa: E402

from tests.autonomy_support import clock_threads, wait_for  # noqa: E402
from tests.test_mvp_generation import CANARY, FakeProvider  # noqa: E402

# 2026-09-27 10:00 UTC = 19:00 JST
WALL = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)


def _state(registry=None, wall=WALL, operator="airi"):
    return formal_session_state(
        YOAKE_MAE,
        registry if registry is not None else BOUNDARY.active(),
        session_id="yoake-mae_s1",
        wall=wall,
        operator=operator,
    )


def _with_rhythm(registry, character_id, activity_at_19):
    """一份内容快照的副本，其中某人 19:00 所在那一段的活动换掉了。"""
    content = registry.character(character_id)
    rhythm = content.rhythm
    target = rhythm.segment_at(datetime(2026, 9, 27, 19, 0))
    assert target.activity != activity_at_19, "换成一样的活动就不是新版本"
    segments = tuple(
        dataclasses.replace(segment, activity=activity_at_19)
        if segment is target
        else segment
        for segment in rhythm.segments
    )
    changed = dataclasses.replace(
        content, rhythm=DailyRhythm(character_id=rhythm.character_id, segments=segments)
    )
    characters = dict(registry.characters)
    characters[character_id] = changed
    return dataclasses.replace(
        registry, revision=registry.revision + 1, characters=characters
    )


class LaunchClockTests(unittest.TestCase):
    def test_the_launch_day_is_the_tokyo_day(self):
        cases = {
            WALL: datetime(2026, 9, 27, 19, 0),  # 东京 19:00
            datetime(2026, 9, 27, 14, 59, tzinfo=timezone.utc): datetime(2026, 9, 27, 19, 0),  # 东京 23:59
            datetime(2026, 9, 27, 15, 0, tzinfo=timezone.utc): datetime(2026, 9, 28, 19, 0),  # 东京次日 00:00
            datetime(2026, 9, 26, 23, 0, tzinfo=timezone.utc): datetime(2026, 9, 27, 19, 0),  # UTC 前一天
        }
        for wall, expected in cases.items():
            with self.subTest(wall=wall):
                self.assertEqual(YOAKE_MAE.launch_clock(wall), expected)

    def test_a_naive_wall_time_is_refused(self):
        with self.assertRaises(FormalWorldError):
            YOAKE_MAE.launch_clock(datetime(2026, 9, 27, 10, 0))


class InitialStateTests(unittest.TestCase):
    def test_every_resident_starts_where_the_rhythm_puts_them_at_19(self):
        registry = BOUNDARY.active()
        state = _state(registry)
        world = state.world_state
        self.assertEqual(world.clock, datetime(2026, 9, 27, 19, 0))
        for character_id in YOAKE_MAE.residents:
            with self.subTest(character_id=character_id):
                segment = registry.rhythm(character_id).segment_at(world.clock)
                self.assertEqual(world.location_of(character_id), segment.location_id)
                self.assertIs(world.activity_of(character_id).kind, segment.activity)
                self.assertEqual(world.activity_of(character_id).since, world.clock)
        # 计划 §5.2 的清单：瑞希在家、绘名在夜间定时制；19:00 谁都不在 Nightcord。
        self.assertEqual(world.location_of("mizuki"), "mizuki_home_room")
        self.assertEqual(world.location_of("ena"), "kamiyama_high")
        self.assertEqual(world.channels_for("mizuki"), [])
        self.assertEqual(world.channels_for("ena"), [])

    def test_a_launch_inside_a_channel_segment_starts_in_the_channel(self):
        # 开局那一段若声明了频道，开局就在频道里（例如在 25 時里开局）。
        spec = dataclasses.replace(YOAKE_MAE, start=time(1, 30))
        state = formal_session_state(spec, BOUNDARY.active(), session_id="s", wall=WALL)
        world = state.world_state
        self.assertEqual(set(world.channel_participants("nightcord")), {"mizuki", "ena"})

    def test_nothing_happened_before_19(self):
        state = _state()
        self.assertEqual(len(state.events), 0, "开局不写世界事件")
        self.assertEqual(state.histories, {}, "不给角色写一句没发生过的开场白")
        self.assertEqual(len(state.memories), 0)

    def test_the_origin_is_recorded_and_stays_out_of_resident_context(self):
        registry = BOUNDARY.active()
        state = _state(registry, operator="airi-operator")
        origin = state.world_state.metadata["origin"]
        self.assertEqual(origin["kind"], "formal_bootstrap")
        self.assertEqual(origin["world_id"], "yoake-mae")
        self.assertEqual(origin["display_name"], "夜明け前")
        self.assertEqual(origin["timezone"], "Asia/Tokyo")
        self.assertEqual(origin["start"], "2026-09-27T19:00:00")
        self.assertEqual(origin["created_at_utc"], WALL.isoformat())
        self.assertEqual(origin["operator"], "airi-operator")
        self.assertEqual(origin["content"]["revision"], registry.revision)
        self.assertEqual(
            origin["content"]["rhythms"]["mizuki"],
            rhythm_fingerprint(registry.rhythm("mizuki")),
        )
        for character_id in YOAKE_MAE.residents:
            text = render_world_context(state.world_state, character_id)
            for leak in ("formal_bootstrap", "airi-operator", "Asia/Tokyo", origin["content"]["rhythms"]["mizuki"]):
                self.assertNotIn(leak, text)

    def test_the_content_ledger_adopts_the_launch_rhythms(self):
        registry = BOUNDARY.active()
        ledger = _state(registry).content
        for character_id in YOAKE_MAE.residents:
            self.assertTrue(
                ledger.accepts(
                    rhythm_subject(character_id),
                    rhythm_fingerprint(registry.rhythm(character_id)),
                )
            )
        self.assertEqual(ledger.conflicts, ())

    def test_it_survives_an_archive_round_trip(self):
        state = _state()
        restored = SessionState.from_dict(state.to_dict())
        self.assertEqual(restored.content, state.content)
        self.assertEqual(restored.world_state.metadata, state.world_state.metadata)

    def test_a_resident_without_content_cannot_launch(self):
        registry = BOUNDARY.active()
        spec = dataclasses.replace(YOAKE_MAE, residents=("mizuki", "nobody"))
        with self.assertRaises(FormalWorldError):
            formal_session_state(spec, registry, session_id="s", wall=WALL)
        no_rhythm = dataclasses.replace(
            registry,
            characters={
                **dict(registry.characters),
                "ena": dataclasses.replace(registry.character("ena"), rhythm=None),
            },
        )
        with self.assertRaises(FormalWorldError):
            _state(no_rhythm)


class ContentLedgerTests(unittest.TestCase):
    A, B, C = (content_fingerprint(x) for x in ("a", "b", "c"))

    def _ledger(self):
        return ContentLedger((("rhythm:mizuki", self.A),))

    def test_a_new_version_is_pending_and_not_accepted(self):
        ledger = self._ledger().offered("rhythm:mizuki", self.B, registry_revision=2, wall="w")
        (conflict,) = ledger.pending()
        self.assertEqual(conflict.status, ConflictStatus.PENDING)
        self.assertFalse(ledger.accepts("rhythm:mizuki", self.B))
        self.assertTrue(ledger.accepts("rhythm:mizuki", self.A))
        # 同一版再出现一次不重复记。
        self.assertIs(
            ledger.offered("rhythm:mizuki", self.B, registry_revision=3, wall="w2"), ledger
        )

    def test_only_an_adoption_changes_the_adopted_version(self):
        ledger = self._ledger().offered("rhythm:mizuki", self.B, registry_revision=2, wall="w")
        conflict_id = ledger.pending()[0].conflict_id
        declined = ledger.decided(conflict_id, "declined", wall="d")
        self.assertTrue(declined.accepts("rhythm:mizuki", self.A))
        adopted = ledger.decided(conflict_id, "adopted", wall="d")
        self.assertTrue(adopted.accepts("rhythm:mizuki", self.B))
        with self.assertRaises(ContentLedgerError):
            adopted.decided(conflict_id, "declined", wall="d2")

    def test_tampering_is_refused(self):
        ledger = (
            self._ledger()
            .offered("rhythm:mizuki", self.B, registry_revision=2, wall="w")
        )
        ledger = ledger.decided(ledger.pending()[0].conflict_id, "adopted", wall="d")
        cases = {
            "adopted rolled back": lambda p: p["adopted"].update({"rhythm:mizuki": self.A}),
            "adopted swapped": lambda p: p["adopted"].update({"rhythm:mizuki": self.C}),
            "unknown subject": lambda p: p["conflicts"][0].update(subject="rhythm:akito"),
            "decided without a time": lambda p: p["conflicts"][0].update(decided_at_wall=None),
            "unknown status": lambda p: p["conflicts"][0].update(status="maybe"),
            "duplicate": lambda p: p["conflicts"].append(dict(p["conflicts"][0])),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                payload = ledger.to_dict()
                mutate(payload)
                with self.assertRaises(ContentLedgerError):
                    ContentLedger.from_dict(payload)

    def test_the_ledger_rolls_back_and_counts_as_a_change(self):
        state = _state()
        before = state.content
        with self.assertRaises(RuntimeError):
            with state.atomic_commit():
                state.set_content(
                    before.offered("rhythm:mizuki", self.B, registry_revision=9, wall="w")
                )
                raise RuntimeError("组装失败")
        self.assertIs(state.content, before)
        fingerprint = _fingerprint(state)
        with state.atomic_commit():
            state.set_content(
                before.offered("rhythm:mizuki", self.B, registry_revision=9, wall="w")
            )
        self.assertNotEqual(_fingerprint(state), fingerprint, "只记了一条冲突，世界也算变过")

    def test_the_session_refuses_to_forget_a_record(self):
        state = _state()
        with state.atomic_commit():
            state.set_content(
                state.content.offered(
                    "rhythm:mizuki", self.B, registry_revision=9, wall="w"
                )
            )
        with self.assertRaises(SessionStateError):
            with state.atomic_commit():
                state.set_content(ContentLedger(state.content.adopted))
        with self.assertRaises(SessionStateError):
            state.set_content(state.content)  # 事务外


class PlaneTestCase(unittest.TestCase):
    rate = 3600.0
    registry_override = None

    def setUp(self):
        self.registry = BOUNDARY.active()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "worlds"
        env = patch.dict(os.environ, {self.registry.models.key_name: CANARY})
        env.start()
        self.addCleanup(env.stop)
        self.provider = FakeProvider()
        self.before = set(clock_threads())
        self.plane = self.make_plane()
        self.addCleanup(
            lambda: wait_for(lambda: not (set(clock_threads()) - self.before), what="worker 退出")
        )
        self.addCleanup(self.plane.service.release_all)
        self.client = TestClient(create_app(self.plane))

    def make_plane(self, registry_provider=None):
        return WorldControlPlane(
            root=self.root,
            client_factory=lambda *a, **k: self.provider,
            registry_provider=registry_provider,
            autonomy=AutonomySettings(
                clock=ClockConfig(rate=self.rate, interval_seconds=0.01, stop_timeout_seconds=2.0),
                cadence=ActivationCadence(),
                shutdown_timeout_seconds=1.0,
            ),
        )

    def world(self):
        return self.plane.service.opened("yoake-mae")


class BootstrapApiTests(PlaneTestCase):
    rate = 1.0

    def test_bootstrap_opens_the_formal_world(self):
        response = self.client.post("/api/persistent-worlds/yoake-mae/bootstrap")
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["clock"][11:16], "19:00")
        self.assertEqual(body["revision"], 1)
        self.assertEqual(body["autonomy"]["cognition_causes"], ["not_started"])
        self.assertTrue(body["autonomy"]["worker_alive"])
        state = self.world().state
        self.assertEqual(state.world_state.metadata["origin"]["kind"], "formal_bootstrap")
        self.assertEqual(state.anchor.sim_epoch, state.world_state.clock)

    def test_it_never_overwrites_an_existing_archive(self):
        self.assertEqual(
            self.client.post("/api/persistent-worlds/yoake-mae/bootstrap").status_code, 201
        )
        self.client.post("/api/persistent-worlds/yoake-mae/close")
        again = self.client.post("/api/persistent-worlds/yoake-mae/bootstrap")
        self.assertEqual(again.status_code, 409, again.text)
        self.assertEqual(again.json()["detail"]["category"], "archive_already_exists")

    def test_the_formal_world_cannot_come_from_a_legacy_scene(self):
        response = self.client.post(
            "/api/persistent-worlds",
            json={"world_id": "yoake-mae", "scene": "nightcord", "characters": ["mizuki", "ena"]},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse((self.root / "yoake-mae").exists())

    def test_an_undefined_formal_world_is_refused(self):
        response = self.client.post("/api/persistent-worlds/some-world/bootstrap")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse((self.root / "some-world").exists())


class TwentyFiveOClockTests(PlaneTestCase):
    def test_the_day_runs_into_nightcord_without_start_or_model_calls(self):
        self.plane.create_formal("yoake-mae")
        world = self.world()
        wait_for(
            lambda: world.state.world_state.activity_of("mizuki").kind
            is ActivityKind.EDITING_VIDEO,
            what="21:00 瑞希开始做 MV",
        )
        wait_for(
            lambda: set(world.state.world_state.channel_participants("nightcord"))
            == {"mizuki", "ena"},
            timeout=15.0,
            what="25 時两人都在 Nightcord",
        )
        launch = datetime.fromisoformat(world.state.world_state.metadata["origin"]["start"])
        self.assertGreaterEqual(
            world.state.world_state.clock, launch + timedelta(hours=6), "跨过了零点到 01:00"
        )
        self.assertEqual(self.provider.generations, [], "没按 Start，不调模型")
        # 绘名从学校回家、再去工作室，都是走过去的：没有一跳是瞬移。
        graph = world.state.world_state.locations
        position = "kamiyama_high"
        for event in world.state.events.events():
            if event.actor_id == "ena" and event.type.value == "character.location_changed":
                self.assertIsNotNone(
                    graph.travel_minutes(position, event.location_id),
                    f"{position} → {event.location_id} 是瞬移",
                )
                position = event.location_id


class ContentGateTests(PlaneTestCase):
    rate = 1.0

    def test_a_new_rhythm_waits_for_an_explicit_adoption(self):
        self.plane.create_formal("yoake-mae")
        self.plane.close("yoake-mae")

        changed = _with_rhythm(self.registry, "mizuki", ActivityKind.DRAWING)
        plane = self.make_plane(registry_provider=lambda: changed)
        self.addCleanup(plane.service.release_all)
        plane.restore("yoake-mae")
        world = plane.service.opened("yoake-mae")
        (conflict,) = world.state.content.pending()
        self.assertEqual(conflict.subject, "rhythm:mizuki")
        self.assertEqual(conflict.registry_revision, changed.revision)
        # 新版不生效：瑞希不受作息驱动，绘名照常。
        self.assertEqual(world.runtime.rhythm.characters(), ("ena",))

        # 再打开一次不重复记。
        plane.close("yoake-mae")
        plane.restore("yoake-mae")
        world = plane.service.opened("yoake-mae")
        self.assertEqual(len(world.state.content.conflicts), 1)

        # 项目所有者明确采用：下次打开才生效。
        plane.decide_content_conflict("yoake-mae", conflict.conflict_id, "adopted")
        self.assertEqual(world.runtime.rhythm.characters(), ("ena",), "本次打开不变")
        plane.close("yoake-mae")
        plane.restore("yoake-mae")
        world = plane.service.opened("yoake-mae")
        self.assertEqual(world.runtime.rhythm.characters(), ("ena", "mizuki"))
        self.assertEqual(world.state.content.pending(), ())
        self.assertTrue(
            world.state.content.accepts(
                "rhythm:mizuki", rhythm_fingerprint(changed.rhythm("mizuki"))
            )
        )

    def test_a_removed_rhythm_is_a_conflict_not_a_silent_change(self):
        state = _state()
        rhythms = self.registry.rhythms()
        rhythms.pop("mizuki")
        with state.atomic_commit():
            accepted = gated_rhythms(state, rhythms, registry_revision=2, wall="w")
        self.assertEqual(sorted(accepted), ["ena"])
        (conflict,) = state.content.pending()
        self.assertEqual(conflict.subject, "rhythm:mizuki")
        self.assertEqual(conflict.offered_fingerprint, ABSENT)

    def test_a_declined_version_stays_out(self):
        self.plane.create_formal("yoake-mae")
        self.plane.close("yoake-mae")
        changed = _with_rhythm(self.registry, "ena", ActivityKind.RESTING)
        plane = self.make_plane(registry_provider=lambda: changed)
        self.addCleanup(plane.service.release_all)
        plane.restore("yoake-mae")
        conflict = plane.service.opened("yoake-mae").state.content.pending()[0]
        plane.decide_content_conflict("yoake-mae", conflict.conflict_id, "declined")
        plane.close("yoake-mae")
        plane.restore("yoake-mae")
        world = plane.service.opened("yoake-mae")
        self.assertEqual(world.runtime.rhythm.characters(), ("mizuki",))
        self.assertEqual(world.state.content.pending(), ())


if __name__ == "__main__":
    unittest.main()
