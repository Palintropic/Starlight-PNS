# tests/test_quiet_time_events.py — 「记录安静的分钟」开关（WORLD-1 存档增长设计 §3）。
#
# 盯住的东西：
#   1. 默认 record：每个时钟步一条 world.time_advanced（阶段一的行为不变）。
#   2. 拨到 skip：安静的步照样推进时钟，但不写时间事件；有到期、有作息事件的步照写。
#   3. 每拨一次在同一个事务里记一条策略记录，随存档走，重启不会拨回去；
#      值相同的"拨动"不记。切换只影响之后。
#   4. "没记"与"丢了"分得开：record 生效时出现的空档（包括尾巴）让存档加载失败；
#      skip 生效范围里的空档合法。
#   5. 作息在一个没声明成边界的时刻交出了事件：这一步不算安静，整步回滚改记录模式，
#      时钟不卡。
#
# 运行: python -m unittest discover -s tests -p test_quiet_time_events.py
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pns.models.activation import ActivationKind, ScheduledActivation  # noqa: E402
from pns.models.event import EventType  # noqa: E402
from pns.models.session import SessionStateError  # noqa: E402
from pns.models.time_events import (  # noqa: E402
    QuietTime,
    TimeEventPolicy,
    TimeEventPolicyError,
    TimeEventPolicyRecord,
    time_gaps,
)
from pns.runtime.autonomy import coordinator as coordinator_mod  # noqa: E402
from pns.runtime.persistence.archive import ArchiveError  # noqa: E402
from pns.runtime.scheduler import SchedulerError  # noqa: E402
from test_world_lifecycle import WorldTestCase, _adapters  # noqa: E402

T0 = datetime(2026, 8, 22, 23, 50)


def _times(state):
    return state.events.by_type(EventType.WORLD_TIME_ADVANCED)


def _step(world, n=1):
    for _ in range(n):
        world.runtime.advance(1)


class QuietTimeTestCase(WorldTestCase):
    def skip(self, world):
        return world.runtime.set_quiet_time_events(False)

    def reopen(self, world):
        world.close()
        return self.service.restore("nightcord", adapters=_adapters())

    def rewrite_archive(self, mutate):
        payload = self.archive_json()
        mutate(payload)
        self.archive_path().write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ── 1–3. 开关本身 ───────────────────────────────────────────────────────
class SwitchTests(QuietTimeTestCase):
    def test_the_default_records_every_step(self):
        world = self.created()
        self.assertEqual(
            world.status()["quiet_time_events"],
            {"record": True, "since_sim": None, "since_wall": None, "flips": 0},
        )
        _step(world, 5)
        self.assertEqual(len(_times(world.state)), 5)

    def test_skip_moves_the_clock_without_time_events(self):
        world = self.created()
        _step(world, 2)
        result = self.skip(world)
        self.assertTrue(result["changed"])
        self.assertFalse(result["record"])
        self.assertEqual(result["since_sim"], (T0 + timedelta(minutes=2)).isoformat())
        _step(world, 5)
        self.assertEqual(len(_times(world.state)), 2)
        self.assertEqual(world.state.world_state.clock, T0 + timedelta(minutes=7))
        # 时钟变了就是变了：dirty，会被 checkpoint。
        self.assertTrue(world.status()["dirty"])

    def test_the_switch_survives_a_restart_and_the_gap_is_legal(self):
        world = self.created()
        _step(world, 2)
        self.skip(world)
        _step(world, 5)
        restored = self.reopen(world)
        self.assertEqual(restored.state.world_state.clock, T0 + timedelta(minutes=7))
        report = restored.status()["quiet_time_events"]
        self.assertFalse(report["record"], "重启不会把开关拨回去")
        self.assertEqual(report["flips"], 1)
        _step(restored, 3)
        self.assertEqual(len(_times(restored.state)), 2)

    def test_a_flip_alone_makes_the_world_dirty(self):
        world = self.created()
        self.assertFalse(world.status()["dirty"])
        self.skip(world)
        self.assertTrue(world.status()["dirty"], "只拨了开关也是一笔没存的运维操作")

    def test_a_flip_to_the_same_value_records_nothing(self):
        world = self.created()
        self.assertFalse(world.runtime.set_quiet_time_events(True)["changed"])
        self.assertEqual(world.state.time_events.records, ())
        self.skip(world)
        self.assertFalse(self.skip(world)["changed"])
        self.assertEqual(len(world.state.time_events.records), 1)

    def test_flipping_back_records_again_and_the_history_explains_itself(self):
        world = self.created()
        _step(world, 1)
        self.skip(world)
        _step(world, 4)
        world.runtime.set_quiet_time_events(True)
        _step(world, 3)
        times = _times(world.state)
        self.assertEqual(len(times), 4)
        # 空档正好是 skip 的那四分钟。
        gaps = time_gaps(
            ((e.occurred_at, e.payload["minutes"]) for e in times),
            world.state.world_state.clock,
            world.state.time_events.epoch,
        )
        self.assertEqual(gaps, [(T0 + timedelta(minutes=1), T0 + timedelta(minutes=5))])
        self.assertEqual(
            [r.value for r in world.state.time_events.records],
            [QuietTime.SKIP, QuietTime.RECORD],
        )
        restored = self.reopen(world)
        self.assertEqual(len(_times(restored.state)), 4)

    def test_a_step_with_a_due_is_not_quiet(self):
        world = self.created()
        self.skip(world)
        world.state.scheduler.schedule(
            ScheduledActivation(
                activation_id="wake",
                kind=ActivationKind.CHARACTER_ACTIVATION,
                due_at=T0 + timedelta(minutes=3),
                character_id="mizuki",
            )
        )
        _step(world, 5)
        times = _times(world.state)
        self.assertEqual(len(times), 1)
        # 那一条从它自己的起点写起：之前两分钟没记，空档本身就说明什么都没发生。
        self.assertEqual(times[0].occurred_at, T0 + timedelta(minutes=2))
        self.assertEqual(list(times[0].provenance["due_activations"]), ["wake"])
        self.reopen(world)

    def test_the_policy_rolls_back_with_its_transaction(self):
        world = self.created()
        with self.assertRaises(RuntimeError):
            with world.state.atomic_commit():
                world.state.set_time_events(
                    world.state.time_events.flipped(
                        QuietTime.SKIP, sim=world.state.world_state.clock, wall="w"
                    )
                )
                raise RuntimeError("abort")
        self.assertEqual(world.state.time_events.records, ())

    def test_a_flip_must_be_stamped_with_the_current_clock(self):
        world = self.created()
        with world.state.atomic_commit():
            with self.assertRaises(SessionStateError):
                world.state.set_time_events(
                    world.state.time_events.flipped(
                        QuietTime.SKIP, sim=T0 + timedelta(minutes=5), wall="w"
                    )
                )

    def test_the_public_scheduler_has_no_silent_advance(self):
        # 攻击（全量审查 F2）：runtime.scheduler 是公开的。它看不见作息边界，
        # 让它自己宣布"这一步安静"就能跨过作息变化而不写、不应用。
        world = self.created()
        self.skip(world)
        with self.assertRaises(TypeError):
            world.runtime.scheduler.advance_to(T0 + timedelta(minutes=5), record=False)
        self.assertEqual(world.state.world_state.clock, T0)
        # 公开推进照样记事件。
        world.runtime.scheduler.advance_to(T0 + timedelta(minutes=5))
        self.assertEqual(len(_times(world.state)), 1)

    def test_the_quiet_primitive_still_needs_the_skip_ledger(self):
        # 纵深：即使内部原语被误用，record 生效时也拒绝，不存下读不回来的空档。
        world = self.created()
        with self.assertRaises(SchedulerError):
            world.runtime.scheduler._advance_quietly(T0 + timedelta(minutes=5))
        self.assertEqual(world.state.world_state.clock, T0)
        self.skip(world)
        world.runtime.scheduler._advance_quietly(T0 + timedelta(minutes=5))
        world.runtime.set_quiet_time_events(True)
        with self.assertRaises(SchedulerError):
            world.runtime.scheduler._advance_quietly(T0 + timedelta(minutes=9))
        self.reopen(world)

    def test_the_scheduler_refuses_a_quiet_step_that_has_a_due(self):
        world = self.created()
        self.skip(world)
        scheduler = world.state.scheduler
        scheduler.schedule(
            ScheduledActivation(
                activation_id="wake",
                kind=ActivationKind.CHARACTER_ACTIVATION,
                due_at=T0 + timedelta(minutes=1),
                character_id="mizuki",
            )
        )
        with self.assertRaises(SchedulerError):
            scheduler._advance_quietly(T0 + timedelta(minutes=1))
        self.assertEqual(world.state.world_state.clock, T0)


# ── 4. 没记 vs 丢了 ─────────────────────────────────────────────────────
class GapValidationTests(QuietTimeTestCase):
    def recorded_world(self):
        world = self.created()
        _step(world, 5)
        world.close()

    def drop_time_event(self, payload, which):
        entries = payload["state"]["events"]["events"]
        times = [i for i, e in enumerate(entries) if e["type"] == "world.time_advanced"]
        del entries[times[which]]
        for index, entry in enumerate(entries):
            entry["sequence"] = index

    def test_a_missing_time_event_under_record_is_a_damaged_archive(self):
        self.recorded_world()
        self.rewrite_archive(lambda p: self.drop_time_event(p, 2))
        with self.assertRaises(ArchiveError) as caught:
            self.service.restore("nightcord", adapters=_adapters())
        self.assertIn("存档缺了事件", str(caught.exception))

    def test_a_missing_last_time_event_is_caught_by_the_tail_check(self):
        self.recorded_world()
        self.rewrite_archive(lambda p: self.drop_time_event(p, -1))
        with self.assertRaises(ArchiveError) as caught:
            self.service.restore("nightcord", adapters=_adapters())
        self.assertIn("存档缺了事件", str(caught.exception))

    def test_deleting_every_time_event_is_caught(self):
        # 全量审查 F3：一条时间事件都不剩时，时钟从起点走到现在也必须有解释。
        self.recorded_world()

        def wipe(payload):
            payload["state"]["events"]["events"] = [
                e for e in payload["state"]["events"]["events"]
                if e["type"] != "world.time_advanced"
            ]
            for index, entry in enumerate(payload["state"]["events"]["events"]):
                entry["sequence"] = index

        self.rewrite_archive(wipe)
        with self.assertRaises(ArchiveError) as caught:
            self.service.restore("nightcord", adapters=_adapters())
        self.assertIn("存档缺了事件", str(caught.exception))

    def test_a_gap_before_the_first_time_event_is_caught(self):
        self.recorded_world()
        self.rewrite_archive(lambda p: self.drop_time_event(p, 0))
        with self.assertRaises(ArchiveError):
            self.service.restore("nightcord", adapters=_adapters())

    def test_the_epoch_cannot_be_dropped_or_moved(self):
        self.recorded_world()
        payload = self.archive_json()
        self.rewrite_archive(lambda p: p["state"].__setitem__("time_events", None))
        with self.assertRaises(ArchiveError) as caught:
            self.service.restore("nightcord", adapters=_adapters())
        self.assertIn("缺少 time_events", str(caught.exception))
        self.archive_path().write_text(json.dumps(payload), encoding="utf-8")

        def later(p):
            p["state"]["time_events"]["epoch"] = "2026-08-22T23:55:00"
            p["state"]["events"]["events"] = [
                e for e in p["state"]["events"]["events"]
                if e["type"] != "world.time_advanced"
            ]
            for index, entry in enumerate(p["state"]["events"]["events"]):
                entry["sequence"] = index

        # 把起点挪到此刻再删光时间事件：这个测试世界既没有开局来源、也没有认知
        # 时间线可以交叉核对，只剩账本自己 —— 多字段伪造，同 R2-F4 一类，能加载。
        # 正式世界与按现实时间走的世界会被拒绝（见 test_formal_world 与下一条）。
        self.rewrite_archive(later)
        self.service.restore("nightcord", adapters=_adapters()).close()

    def test_a_moved_epoch_contradicts_the_cognition_timeline(self):
        from pns.runtime.autonomy.clock_worker import ClockConfig

        world = self.created(clock=ClockConfig(rate=1.0, interval_seconds=60.0))
        world.close()

        def later(p):
            p["state"]["time_events"]["epoch"] = "2026-08-22T23:51:00"

        self.rewrite_archive(later)
        with self.assertRaises(ArchiveError) as caught:
            self.service.restore("nightcord", adapters=_adapters())
        self.assertIn("认知时间线", str(caught.exception))

    def test_a_version_2_clock_world_takes_its_epoch_from_the_cognition_timeline(self):
        # 复审 R2-F4：有独立开局记录的 v2 存档，删掉开头一段时间事件也会被发现。
        from pns.runtime.autonomy.clock_worker import ClockConfig

        world = self.created(clock=ClockConfig(rate=1.0, interval_seconds=60.0))
        _step(world, 3)
        world.close()

        def downgrade_and_drop_first(p):
            p["version"] = 2
            del p["segments"]
            p["state"]["time_events"] = None
            self.drop_time_event(p, 0)

        self.rewrite_archive(downgrade_and_drop_first)
        with self.assertRaises(ArchiveError) as caught:
            self.service.restore("nightcord", adapters=_adapters())
        self.assertIn("存档缺了事件", str(caught.exception))

    def test_a_version_2_world_without_a_launch_record_cannot_see_a_dropped_prefix(self):
        # 兼容上限，写明而不是假装：既没有开局来源、也没有认知时间线的 v2 世界，
        # 删掉开头一段时间事件发现不了。
        self.recorded_world()

        def downgrade_and_drop_first(p):
            p["version"] = 2
            del p["segments"]
            p["state"]["time_events"] = None
            self.drop_time_event(p, 0)

        self.rewrite_archive(downgrade_and_drop_first)
        restored = self.service.restore("nightcord", adapters=_adapters())
        self.assertEqual(restored.state.time_events.epoch, T0 + timedelta(minutes=1))
        restored.close()

    def test_a_version_2_archive_uses_the_legacy_epoch(self):
        self.recorded_world()

        def downgrade(p):
            p["version"] = 2
            del p["segments"]
            p["state"]["time_events"] = None

        self.rewrite_archive(downgrade)
        restored = self.service.restore("nightcord", adapters=_adapters())
        self.assertEqual(restored.state.time_events.epoch, T0)
        restored.close()
        self.assertEqual(self.archive_json()["state"]["time_events"]["epoch"], T0.isoformat())

    def test_a_gap_that_outlives_the_skip_span_is_refused(self):
        # skip 从第 1 分钟到第 3 分钟；第 5 分钟那条被删掉 —— 落在 record 里。
        world = self.created()
        _step(world, 1)
        self.skip(world)
        _step(world, 2)
        world.runtime.set_quiet_time_events(True)
        _step(world, 4)
        world.close()
        self.rewrite_archive(lambda p: self.drop_time_event(p, 2))
        with self.assertRaises(ArchiveError):
            self.service.restore("nightcord", adapters=_adapters())

    def test_a_policy_record_later_than_the_clock_is_refused(self):
        world = self.created()
        self.skip(world)
        world.close()

        def forward(payload):
            payload["state"]["time_events"]["records"][0]["from_sim"] = "2030-01-01T00:00:00"

        self.rewrite_archive(forward)
        with self.assertRaises(ArchiveError):
            self.service.restore("nightcord", adapters=_adapters())


class PolicyLedgerTests(unittest.TestCase):
    def rec(self, value, minute, wall="w"):
        return TimeEventPolicyRecord(QuietTime(value), T0 + timedelta(minutes=minute), wall)

    def test_the_first_record_must_turn_skip_on(self):
        with self.assertRaises(TimeEventPolicyError):
            TimeEventPolicy(T0, (self.rec("record", 0),))

    def test_records_alternate_and_never_go_back_in_time(self):
        with self.assertRaises(TimeEventPolicyError):
            TimeEventPolicy(T0, (self.rec("skip", 0), self.rec("skip", 1)))
        with self.assertRaises(TimeEventPolicyError):
            TimeEventPolicy(T0, (self.rec("skip", 5), self.rec("record", 1)))

    def test_coverage_is_the_complement_of_the_record_spans(self):
        policy = TimeEventPolicy(
            T0, (self.rec("skip", 10), self.rec("record", 20), self.rec("skip", 20))
        )
        at = lambda m: T0 + timedelta(minutes=m)  # noqa: E731
        self.assertTrue(policy.covers(at(10), at(30)), "长度为 0 的 record 段不打断 skip")
        self.assertFalse(policy.covers(at(9), at(12)))
        self.assertTrue(policy.covers(at(25), at(10_000)))
        flipped_back = TimeEventPolicy(T0, (self.rec("skip", 10), self.rec("record", 20)))
        self.assertFalse(flipped_back.covers(at(15), at(21)))
        self.assertTrue(flipped_back.covers(at(15), at(20)))

    def test_it_round_trips(self):
        policy = TimeEventPolicy(T0, (self.rec("skip", 3, "2026-09-27T10:00:00+00:00"),))
        self.assertEqual(TimeEventPolicy.from_dict(policy.to_dict()), policy)
        self.assertEqual(
            policy.to_dict()["records"][0]["policy"], "quiet_time_events"
        )

    def test_overlapping_time_events_are_refused(self):
        with self.assertRaises(TimeEventPolicyError):
            time_gaps([(T0, 10), (T0 + timedelta(minutes=5), 1)], T0 + timedelta(minutes=6), T0)


# ── 5. 作息越界：回滚改记录 ─────────────────────────────────────────────
class NotQuietAfterAllTests(QuietTimeTestCase):
    def test_an_undeclared_rhythm_transition_falls_back_to_recording(self):
        world = self.created()
        self.skip(world)
        runtime = world.runtime
        real = runtime._apply_rhythm_locked
        calls = []

        def surprise():
            calls.append(len(world.state.events))
            # 第一次（安静地走）假装作息交出了事件；重走时照常。
            return ({"event_id": "surprise"},) if len(calls) == 1 else real()

        with patch.object(runtime, "_apply_rhythm_locked", side_effect=surprise):
            _step(world, 1)
        self.assertEqual(len(calls), 2, "安静的一步被整步回滚，再按记录模式重走")
        self.assertEqual(len(_times(world.state)), 1)
        self.assertEqual(world.state.world_state.clock, T0 + timedelta(minutes=1))
        self.reopen(world)

    def test_without_skip_there_is_no_second_attempt(self):
        world = self.created()
        runtime = world.runtime
        with patch.object(
            coordinator_mod.AutonomousRuntime,
            "_clock_step_once",
            autospec=True,
            side_effect=coordinator_mod.AutonomousRuntime._clock_step_once,
        ) as spy:
            _step(world, 1)
        self.assertEqual([c.kwargs["quiet"] for c in spy.call_args_list], [False])
        self.assertIs(runtime, world.runtime)


# ── 正式世界：作息边界照写，安静的分钟不写 ──────────────────────────────
from tests.test_formal_world import PlaneTestCase  # noqa: E402


class FormalWorldTests(PlaneTestCase):
    rate = 1.0

    def test_a_skipping_evening_writes_only_its_boundaries(self):
        response = self.client.post("/api/persistent-worlds/yoake-mae/bootstrap")
        self.assertEqual(response.status_code, 201, response.text)
        flip = self.client.post(
            "/api/persistent-worlds/yoake-mae/quiet-time-events", json={"record": False}
        )
        self.assertEqual(flip.status_code, 200, flip.text)
        body = flip.json()
        self.assertFalse(body["quiet_time_events"]["record"])
        self.assertEqual(body["quiet_time_events"]["since_sim"], body["clock"])
        # 拨动立即存盘。
        self.assertFalse(body["dirty"])
        world = self.world()
        before = len(_times(world.state))
        start = world.state.world_state.clock
        # 一分钟一分钟地推过 01:00（进 Nightcord）——像 1:1 的时钟 worker 那样。
        for _ in range(7 * 60):
            world.runtime.advance(1)
        recorded = _times(world.state)[before:]
        self.assertLess(len(recorded), 7 * 60 // 4, "绝大多数分钟是安静的")
        # 每一条时间事件都有它不安静的理由：这一步有到期，或者作息在它推到的
        # 那一刻交出了事件。反过来，每一次作息事件之前都有一条时间事件推到那一刻。
        others = {
            e.occurred_at
            for e in world.state.events.events()
            if e.type is not EventType.WORLD_TIME_ADVANCED and e.occurred_at >= start
        }
        reached_at = set()
        for event in recorded:
            reached = event.occurred_at + timedelta(minutes=event.payload["minutes"])
            reached_at.add(reached)
            self.assertTrue(
                event.provenance["due_activations"] or reached in others,
                f"{reached} 那一步什么都没发生，却记了时间事件",
            )
        self.assertTrue(others, "作息边界照走")
        self.assertLessEqual(others, reached_at, "作息事件的那一刻必须有时间事件走到")
        self.assertEqual(
            set(world.state.world_state.channel_participants("nightcord")), {"mizuki", "ena"}
        )
        close = self.client.post("/api/persistent-worlds/yoake-mae/close")
        self.assertEqual(close.status_code, 200, close.text)
        restored = self.client.post("/api/persistent-worlds/yoake-mae/restore")
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertFalse(restored.json()["quiet_time_events"]["record"])

    def test_the_public_scheduler_cannot_skip_the_21_oclock_boundary(self):
        # 全量审查 F2 的反例：skip 下公开 scheduler 从 19:00 一步静默推到 21:00。
        self.client.post("/api/persistent-worlds/yoake-mae/bootstrap")
        self.client.post(
            "/api/persistent-worlds/yoake-mae/quiet-time-events", json={"record": False}
        )
        world = self.world()
        start = world.state.world_state.clock
        with self.assertRaises(TypeError):
            world.runtime.scheduler.advance_to(start + timedelta(hours=2), record=False)
        self.assertEqual(world.state.world_state.clock, start)
        # 复审 R2-F2：记事件的公开推进同样会跳过作息，挂着作息的世界一律拒绝。
        for push in (
            lambda: world.runtime.scheduler.advance_to(start + timedelta(hours=2)),
            lambda: world.runtime.scheduler.advance_by(120),
            lambda: world.runtime.scheduler.advance_to_next_due(),
            lambda: world.runtime.scheduler._advance_quietly(start + timedelta(hours=2)),
        ):
            with self.assertRaises(SchedulerError):
                push()
        self.assertEqual(world.state.world_state.clock, start)
        self.assertEqual(
            world.state.world_state.activity_of("mizuki").kind.value, "idle"
        )
        # 正规推进过 21:00：作息照走、边界那一步照写。
        for _ in range(2 * 60 + 1):
            world.runtime.advance(1)
        self.assertEqual(
            world.state.world_state.activity_of("mizuki").kind.value, "editing_video"
        )
        reached = {
            e.occurred_at + timedelta(minutes=e.payload["minutes"])
            for e in _times(world.state)
        }
        self.assertIn(start.replace(hour=21, minute=0), reached)

    def _forged_time_event(self, state, minutes):
        from pns.models.event import Event, EventScope

        return Event(
            event_id="forged-clock",
            type=EventType.WORLD_TIME_ADVANCED,
            occurred_at=state.world_state.clock,
            scope=EventScope.PUBLIC,
            payload={"minutes": minutes},
        )

    def test_only_the_scheduler_moves_a_persistent_clock(self):
        # 第三方反向测试 B-F1/F2/F3：从调度器以外推时钟，一律拒绝、时钟不动。
        from pns.runtime.event_commit import commit_session_event

        self.client.post("/api/persistent-worlds/yoake-mae/bootstrap")
        world = self.world()
        start = world.state.world_state.clock
        attempts = {
            "commit_session_event": lambda: commit_session_event(
                world.state, self._forged_time_event(world.state, 121)
            ),
            "commit_external_event": lambda: world.runtime.commit_external_event(
                self._forged_time_event(world.state, 121)
            ),
            "WorldState.advance_time": lambda: world.state.world_state.advance_time(2),
        }
        for record in (True, False):
            self.client.post(
                "/api/persistent-worlds/yoake-mae/quiet-time-events", json={"record": record}
            )
            for label, attempt in attempts.items():
                with self.subTest(record=record, path=label):
                    with self.assertRaises(Exception):
                        attempt()
                    self.assertEqual(world.state.world_state.clock, start)
        # 存档照样读得回来。
        self.assertEqual(self.client.post("/api/persistent-worlds/yoake-mae/close").status_code, 200)
        self.assertEqual(self.client.post("/api/persistent-worlds/yoake-mae/restore").status_code, 200)

    def test_a_restore_callback_cannot_forge_a_time_event(self):
        import dataclasses

        from pns.runtime.event_commit import commit_session_event

        self.client.post("/api/persistent-worlds/yoake-mae/bootstrap")
        self.client.post("/api/persistent-worlds/yoake-mae/close")
        outcomes = []
        real = self.plane.build_adapters

        def wrapped(*args, **kwargs):
            adapters = real(*args, **kwargs)
            inner = adapters.policy_factory

            def policy_factory(state):
                try:
                    with state.atomic_commit():
                        commit_session_event(state, self._forged_time_event(state, 8))
                    outcomes.append("forged")
                except Exception:
                    outcomes.append("refused")
                return inner(state)

            return dataclasses.replace(adapters, policy_factory=policy_factory)

        self.plane.build_adapters = wrapped
        self.plane.restore("yoake-mae")
        self.assertEqual(outcomes, ["refused"])
        self.assertEqual(self.world().state.world_state.clock.minute, 0)

    def test_a_dropped_due_step_inside_a_skip_span_is_caught(self):
        # 第三方反向测试 B-F4：skip 范围里删掉一条带到期的时间事件，不能被当成"没记"。
        self.client.post("/api/persistent-worlds/yoake-mae/bootstrap")
        self.client.post(
            "/api/persistent-worlds/yoake-mae/quiet-time-events", json={"record": False}
        )
        world = self.world()
        for _ in range(20):
            world.runtime.advance(1)
        self.client.post("/api/persistent-worlds/yoake-mae/close")
        path = self.root / "yoake-mae" / "world.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = payload["state"]["events"]["events"]
        index = next(
            i for i, e in enumerate(entries)
            if e["type"] == "world.time_advanced" and e["provenance"]["due_activations"]
        )
        del entries[index]
        for position, entry in enumerate(entries):
            entry["sequence"] = position
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        restored = self.client.post("/api/persistent-worlds/yoake-mae/restore")
        self.assertEqual(restored.status_code, 422, restored.text)
        self.assertIn("存档缺了事件", restored.json()["detail"]["message"])

    def test_time_event_ids_are_their_history_positions(self):
        # B-F4 的卡死：id 按"已有几条时间事件"编号，少一条就撞号。改为按序号。
        self.client.post("/api/persistent-worlds/yoake-mae/bootstrap")
        world = self.world()
        for _ in range(3):
            world.runtime.advance(1)
        for position, event in enumerate(world.state.events.events()):
            if event.type is EventType.WORLD_TIME_ADVANCED:
                self.assertTrue(event.event_id.endswith(f":clock:{position}"))

    def test_the_switch_needs_an_open_world_and_a_boolean(self):
        self.client.post("/api/persistent-worlds/yoake-mae/bootstrap")
        bad = self.client.post(
            "/api/persistent-worlds/yoake-mae/quiet-time-events", json={"record": "no"}
        )
        self.assertEqual(bad.status_code, 422)
        self.client.post("/api/persistent-worlds/yoake-mae/close")
        closed = self.client.post(
            "/api/persistent-worlds/yoake-mae/quiet-time-events", json={"record": False}
        )
        self.assertEqual(closed.status_code, 409, closed.text)
        self.assertEqual(closed.json()["detail"]["category"], "world_not_open")

    def test_a_world_whose_runtime_stopped_refuses_with_409_not_500(self):
        self.client.post("/api/persistent-worlds/yoake-mae/bootstrap")
        world = self.world()
        with patch.object(
            type(world.runtime), "set_quiet_time_events",
            side_effect=coordinator_mod.AutonomyError("自主运行时已经停止"),
        ):
            refused = self.client.post(
                "/api/persistent-worlds/yoake-mae/quiet-time-events", json={"record": False}
            )
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["detail"]["category"], "lifecycle_refused")


if __name__ == "__main__":
    unittest.main()
