# tests/test_world2_runtime.py — WORLD-2 C7：运行期发布、门内检查、稳定序位播种。
#
# 守的线（设计 v3 §4.4、§4.7、§6.1/4/12/13；实施单 §4.7）：
#   1. 发布：入住之后作息导演多了她、旧居民的表是同一个对象；第二天她按自己的作息
#      起床、去吃饭、去上课；旧居民的排期条目一条不动；
#   2. 回滚：图替换、授予、放置、名单、账本、播种、导演加人、名单核对之后分别注入
#      异常，整份状态与导演都回到操作前；
#   3. 门：没在跑的运行时、作息门的五种未齐状态都不能被入住解锁；只缺新人内容时
#      不留下名单 / 排期；名单核对的三项各自能拦下；
#   4. 播种：四次单人入住同分钟 / 跨分钟首个到期错开且确定；第三笔失败不改变第四笔；
#      种子形状、算法、序位、到期被改都拒绝；排期里已有同名条目拒绝；
#   5. 恢复：周期推进过的种子照常打开；缺种子、换节律、离开相位、多一条周期激活拒绝；
#      恢复后重新绑定，门把她当作已采用主体、导演里有她。
#
# 运行: python -m unittest discover -s tests -p test_world2_runtime.py
import copy
import dataclasses
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from pns.models.activation import ActivationKind, ScheduledActivation  # noqa: E402
from pns.models.event import EventType  # noqa: E402
from pns.models.session import SessionState  # noqa: E402
from pns.models.world_state import ActivityKind, WorldState  # noqa: E402
from pns.runtime import event_commit  # noqa: E402
from pns.runtime.autonomy.audit import ScriptedAuditor  # noqa: E402
from pns.runtime.autonomy import coordinator as coordinator_module  # noqa: E402
from pns.runtime.autonomy.coordinator import AutonomousRuntime, AutonomyError  # noqa: E402
from pns.runtime.autonomy.seeding import ActivationCadence, seed_character_activations  # noqa: E402
from pns.runtime.event_commit import EventCommitError  # noqa: E402
from pns.runtime.formal_world import (  # noqa: E402
    ABSENT,
    FormalWorldError,
    check_roster_runnable,
    rhythm_fingerprint,
)
from pns.runtime.persistence.archive import WorldArchive  # noqa: E402
from pns.runtime.persistence.lifecycle import RuntimeAdapters  # noqa: E402
from pns.runtime.reload import BOUNDARY  # noqa: E402
from pns.runtime.rhythm import RhythmDirector, RhythmDirectorError  # noqa: E402
from pns.runtime.world2_replay import WorldReplayError, verify_world2_history  # noqa: E402
from pns.runtime.world2_seed import activation_from_seed, admission_seed  # noqa: E402

from tests.test_formal_world import _with_rhythm  # noqa: E402
from tests.test_world2_content import FOUR  # noqa: E402
from tests.test_world2_events import (  # noqa: E402
    MMJ,
    _admission_payload,
    _event,
    _extension_payload,
    _Injected,
    _old_world_state,
)

REVISION = 1


def _content():
    return dict(BOUNDARY.active().rhythms())


def _rig(night=True):
    """一个在跑的旧世界：四人带开局排期、作息导演只有四人；默认推进到 23:30（宿舍全睡窗口）。"""
    state = _old_world_state()
    content = _content()
    from pns.runtime.scheduler import PersistentScheduler

    scheduler = PersistentScheduler(state)
    with state.atomic_commit():
        seed_character_activations(scheduler, list(FOUR), ActivationCadence())
    runtime = AutonomousRuntime(
        state,
        auditor=ScriptedAuditor(),
        rhythm=RhythmDirector({cid: content[cid] for cid in FOUR}),
    )
    runtime.start()
    if night:
        runtime.advance(270)  # 19:00 → 23:30
    return state, runtime, content


def _extend(state, runtime, content):
    return runtime._commit_world2(
        _event(state.world_state, EventType.WORLD_LOCATIONS_EXTENDED, _extension_payload(state), "w2:ext"),
        content_rhythms=content,
        registry_revision=REVISION,
    )


def _admit(state, runtime, content, character_id="airi", payload=None):
    world = state.world_state
    payload = payload if payload is not None else _admission_payload(world, character_id)
    return runtime._commit_world2(
        _event(world, EventType.WORLD_RESIDENT_ADMITTED, payload, f"w2:admit:{character_id}"),
        content_rhythms=content,
        registry_revision=REVISION,
    )


def _old_entries(state):
    return [
        (a.activation_id, a.due_at, a.interval_minutes, dict(a.payload))
        for _, a in state.activations.entries()
        if a.character_id in FOUR
    ]


# ── 1. 发布 ─────────────────────────────────────────────────────────────
class PublishTests(unittest.TestCase):
    def test_she_is_in_the_director_and_lives_by_her_rhythm(self):
        state, runtime, content = _rig()
        old_director = runtime.rhythm
        old_tables = {cid: old_director.rhythm_for(cid) for cid in FOUR}
        _extend(state, runtime, content)
        self.assertIs(runtime.rhythm, old_director)  # 扩展不换导演
        entries = _old_entries(state)
        _admit(state, runtime, content)
        self.assertEqual(_old_entries(state), entries)
        director = runtime.rhythm
        self.assertIsNot(director, old_director)
        self.assertFalse(old_director.has("airi"))
        self.assertIs(director.rhythm_for("airi"), content["airi"])
        for cid in FOUR:
            self.assertIs(director.rhythm_for(cid), old_tables[cid])

        runtime.advance(8 * 60 + 30)  # → 次日 08:00（周一）
        world = state.world_state
        mine = [e for e in state.events.events() if e.actor_id == "airi"]
        self.assertTrue(mine)
        self.assertTrue(all(e.provenance.get("kind") == event_commit.RHYTHM_PROVENANCE_KIND for e in mine))
        expected = content["airi"].segment_at(world.clock)
        self.assertEqual(world.location_of("airi"), expected.location_id)
        self.assertIs(world.activity_of("airi").kind, expected.activity)
        self.assertIsNot(world.activity_of("airi").kind, ActivityKind.RESTING)

    def test_with_resident_refuses_someone_already_there(self):
        director = RhythmDirector({cid: _content()[cid] for cid in FOUR})
        with self.assertRaises(RhythmDirectorError):
            director.with_resident("mizuki", _content()["mizuki"])


# ── 2. 回滚 ─────────────────────────────────────────────────────────────
def _after(target, name):
    """包一层：先照常执行，再抛注入异常。"""
    original = getattr(target, name)

    def wrapped(*args, **kwargs):
        result = original(*args, **kwargs)
        raise _Injected(name)

    return patch.object(target, name, wrapped)


class RollbackTests(unittest.TestCase):
    def assert_no_trace(self, inject, *, admit):
        state, runtime, content = _rig()
        if admit:
            _extend(state, runtime, content)
        before = state.to_dict()
        director = runtime.rhythm
        with inject:
            with self.assertRaises(_Injected):
                if admit:
                    _admit(state, runtime, content)
                else:
                    _extend(state, runtime, content)
        self.assertEqual(state.to_dict(), before)
        self.assertIs(runtime.rhythm, director)
        self.assertTrue(runtime.running)

    def test_after_the_graph_swap(self):
        self.assert_no_trace(_after(WorldState, "_replace_locations"), admit=False)

    def test_after_each_admission_step(self):
        points = {
            "grants": _after(event_commit, "install_grants"),
            "placement": _after(WorldState, "place_character"),
            "activity": _after(WorldState, "set_activity"),
            "roster": _after(SessionState, "_admit_character"),
            "seed": _after(event_commit, "_schedule_seed"),
            "ledger": _after(SessionState, "set_content"),
            "director": _after(RhythmDirector, "with_resident"),
            "roster_check": _after(coordinator_module, "check_roster_runnable"),
        }
        for name, inject in points.items():
            with self.subTest(point=name):
                self.assert_no_trace(inject, admit=True)


class OutermostTests(unittest.TestCase):
    def test_refused_inside_another_transaction(self):
        # 嵌在外层事务里：外层回滚会撤掉状态，却撤不掉已换上的导演。所以直接拒绝。
        state, runtime, content = _rig()
        _extend(state, runtime, content)
        before, director = state.to_dict(), runtime.rhythm
        with self.assertRaises(_Injected):
            with state.atomic_commit():
                try:
                    _admit(state, runtime, content)
                except AutonomyError as e:
                    self.assertIn("最外层事务", str(e))
                raise _Injected()
        self.assertEqual(state.to_dict(), before)
        self.assertIs(runtime.rhythm, director)


# ── 3. 门 ───────────────────────────────────────────────────────────────
class GateTests(unittest.TestCase):
    def assert_refused(self, state, runtime, content, message, *, character_id="airi"):
        before, director = state.to_dict(), runtime.rhythm
        with self.assertRaisesRegex(AutonomyError, message):
            _admit(state, runtime, content, character_id)
        self.assertEqual(state.to_dict(), before)
        self.assertIs(runtime.rhythm, director)
        self.assertNotIn(character_id, state.characters)
        self.assertFalse(state.activations.has(f"seed.activation:{character_id}"))

    def test_a_runtime_that_is_not_running_is_refused(self):
        state, runtime, content = _rig()
        _extend(state, runtime, content)
        runtime.stop("held")
        self.assert_refused(state, runtime, content, "已经停止")

    def _held(self, reason):
        """在跑的运行时，账本 / 内容处于某种未齐状态。"""
        state, runtime, content = _rig()
        _extend(state, runtime, content)
        registry = BOUNDARY.active()
        changed = _with_rhythm(registry, "mizuki", ActivityKind.COMPOSING).rhythms()["mizuki"]
        fingerprint = rhythm_fingerprint(changed)

        def ledger(change):
            with state.atomic_commit():
                state.set_content(change(state.content))

        if reason in ("pending", "declined", "deferred"):
            content["mizuki"] = changed
            ledger(lambda l: l.offered("rhythm:mizuki", fingerprint, registry_revision=2, wall="w"))
            if reason != "pending":
                conflict = state.content.pending()[0].conflict_id
                ledger(lambda l: l.decided(conflict, reason, wall="w"))
        elif reason == "missing_definition":
            del content["mizuki"]
        elif reason == "adopted_absent":
            ledger(lambda l: l.offered("rhythm:mizuki", ABSENT, registry_revision=2, wall="w"))
            conflict = state.content.pending()[0].conflict_id
            ledger(lambda l: l.decided(conflict, "adopted", wall="w"))
            del content["mizuki"]
        return state, runtime, content

    def test_no_held_state_is_unlocked_by_an_admission(self):
        for reason in ("pending", "declined", "deferred", "missing_definition", "adopted_absent"):
            with self.subTest(reason=reason):
                state, runtime, content = self._held(reason)
                self.assert_refused(state, runtime, content, f"作息门未齐（mizuki:{reason}")

    def test_a_world_without_a_ledger_is_refused_cleanly(self):
        state, runtime, content = _rig()
        event = _event(
            state.world_state, EventType.WORLD_LOCATIONS_EXTENDED, _extension_payload(state), "w2:ext"
        )
        state.content = None
        with self.assertRaisesRegex(AutonomyError, "WORLD-2 操作只用于带内容账本的正式世界"):
            runtime._commit_world2(event, content_rhythms=content, registry_revision=REVISION)

    def test_missing_new_resident_content_leaves_no_roster_or_queue(self):
        state, runtime, content = _rig()
        _extend(state, runtime, content)
        del content["airi"]
        self.assert_refused(state, runtime, content, "没有 'airi' 的作息")

    def test_silently_different_content_is_refused(self):
        # payload 带的是一版作息，本次打开所用的内容里是另一版：不悄悄安装。
        state, runtime, content = _rig()
        _extend(state, runtime, content)
        content["airi"] = _with_rhythm(BOUNDARY.active(), "airi", ActivityKind.COMPOSING).rhythms()["airi"]
        world = state.world_state
        payload = _admission_payload(world)
        before = state.to_dict()
        with self.assertRaisesRegex(AutonomyError, "内容里没有已采用的那一版"):
            runtime._commit_world2(
                _event(world, EventType.WORLD_RESIDENT_ADMITTED, payload, "w2:admit:airi"),
                content_rhythms=content,
                registry_revision=REVISION,
            )
        self.assertEqual(state.to_dict(), before)


class RosterCheckTests(unittest.TestCase):
    """check_roster_runnable 的三项各自拦得下（门内核每个 roster 成员）。"""

    def setUp(self):
        self.state, self.runtime, self.content = _rig()
        _extend(self.state, self.runtime, self.content)
        _admit(self.state, self.runtime, self.content)

    def test_passes_for_the_admitted_world(self):
        check_roster_runnable(self.state, self.content, self.runtime.rhythm)

    def test_a_roster_member_without_a_ledger_entry(self):
        self.state.characters.append("minori")
        with self.assertRaisesRegex(FormalWorldError, "账本里没有"):
            check_roster_runnable(self.state, self.content, self.runtime.rhythm)

    def test_a_roster_member_without_content(self):
        content = dict(self.content)
        del content["airi"]
        with self.assertRaisesRegex(FormalWorldError, "内容里没有"):
            check_roster_runnable(self.state, content, self.runtime.rhythm)

    def test_a_roster_member_without_a_director_entry(self):
        director = RhythmDirector({cid: self.content[cid] for cid in FOUR})
        with self.assertRaisesRegex(FormalWorldError, "作息导演里没有"):
            check_roster_runnable(self.state, self.content, director)


# ── 4. 播种 ─────────────────────────────────────────────────────────────
class SeedingTests(unittest.TestCase):
    def test_four_admissions_in_one_minute_are_staggered(self):
        state, runtime, content = _rig()
        _extend(state, runtime, content)
        admitted_at = state.world_state.clock
        for cid in MMJ:
            _admit(state, runtime, content, cid)
        dues = [state.activations.get(f"seed.activation:{cid}").due_at for cid in MMJ]
        self.assertEqual(dues, [admitted_at + timedelta(minutes=5 + 5 * i) for i in range(4)])
        self.assertEqual(state.characters[-4:], list(MMJ))

    def test_across_minutes_and_a_failed_third(self):
        state, runtime, content = _rig()
        _extend(state, runtime, content)
        _admit(state, runtime, content, "airi")
        runtime.advance(1)
        _admit(state, runtime, content, "minori")
        runtime.advance(1)
        broken = _admission_payload(state.world_state, "haruka")
        broken["fingerprints"]["rhythm"] = "0" * 64
        with self.assertRaises(EventCommitError):
            _admit(state, runtime, content, "haruka", payload=broken)
        runtime.advance(1)
        at = state.world_state.clock
        _admit(state, runtime, content, "shizuku")
        self.assertEqual(
            state.activations.get("seed.activation:shizuku").due_at,
            at + timedelta(minutes=5 + 5 * 3),
        )
        self.assertNotIn("haruka", state.characters)

    def test_the_first_due_is_strictly_after_admission(self):
        state, runtime, content = _rig()
        seed = admission_seed(
            "airi",
            ordinal=0,
            admitted_at=state.world_state.clock,
            first_delay_minutes=1,
            stagger_minutes=1,
            interval_minutes=15,
        )
        activation = activation_from_seed(seed, character_id="airi", admitted_at=state.world_state.clock)
        self.assertGreater(activation.due_at, state.world_state.clock)

    def test_malformed_seeds_are_refused_at_commit(self):
        def due(s):
            s["due_at"] = (state.world_state.clock + timedelta(minutes=6)).isoformat()

        mutations = {
            "due": due,
            "algorithm": lambda s: s.update(algorithm="pns.admission_seed/0"),
            "ordinal": lambda s: s.update(ordinal=-1),
            "owner": lambda s: s.update(activation_id="seed.activation:minori"),
            "keys": lambda s: s.pop("cue"),
            "interval": lambda s: s.update(interval_minutes=0),
            "cue": lambda s: s.update(cue=""),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                state, runtime, content = _rig()
                _extend(state, runtime, content)
                payload = _admission_payload(state.world_state)
                mutate(payload["seed"])
                before = state.to_dict()
                with self.assertRaisesRegex(EventCommitError, "排期种子不成立"):
                    _admit(state, runtime, content, payload=payload)
                self.assertEqual(state.to_dict(), before)

    def test_an_existing_entry_with_her_seed_id_is_refused(self):
        state, runtime, content = _rig()
        _extend(state, runtime, content)
        forged = ScheduledActivation(
            activation_id="seed.activation:airi",
            kind=ActivationKind.CHARACTER_ACTIVATION,
            due_at=state.world_state.clock + timedelta(minutes=30),
            character_id="mizuki",
            interval_minutes=15,
        )
        with state.atomic_commit():
            state.activations._append(forged)
        with self.assertRaisesRegex(EventCommitError, "排期排不进去"):
            _admit(state, runtime, content)
        self.assertNotIn("airi", state.characters)


# ── 5. 恢复 ─────────────────────────────────────────────────────────────
class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.state, self.runtime, self.content = _rig()
        _extend(self.state, self.runtime, self.content)
        _admit(self.state, self.runtime, self.content)
        self.runtime.advance(95)  # 种子已经按周期推过几次
        self.payload = self.state.to_dict()

    def test_an_advanced_seed_restores(self):
        seeded = self.state.activations.get("seed.activation:airi")
        self.assertGreater(seeded.due_at, self.state.world_state.clock - timedelta(minutes=15))
        verify_world2_history(SessionState.from_dict(copy.deepcopy(self.payload)))

    def tampered(self, mutate, message):
        state = SessionState.from_dict(copy.deepcopy(self.payload))
        mutate(state)
        with self.assertRaisesRegex(WorldReplayError, message):
            verify_world2_history(state)

    def test_missing_seed(self):
        self.tampered(lambda s: s.activations._remove("seed.activation:airi"), "没有她的种子排期")

    def _replace(self, state, **fields):
        current = state.activations.get("seed.activation:airi")
        state.activations._reschedule(dataclasses.replace(current, **fields))

    def test_changed_interval(self):
        self.tampered(lambda s: self._replace(s, interval_minutes=30), "与入住记录不符")

    def test_off_phase(self):
        def mutate(s):
            current = s.activations.get("seed.activation:airi")
            self._replace(s, due_at=current.due_at + timedelta(minutes=1))

        self.tampered(mutate, "相位")

    def test_a_second_periodic_activation_for_her(self):
        def mutate(s):
            s.activations._append(
                ScheduledActivation(
                    activation_id="seed.activation:airi:twin",
                    kind=ActivationKind.CHARACTER_ACTIVATION,
                    due_at=s.world_state.clock + timedelta(minutes=3),
                    character_id="airi",
                    interval_minutes=15,
                )
            )

        self.tampered(mutate, "双播")

    def test_rebinding_after_restore_drives_her(self):
        restored = WorldArchive.from_state_payload(
            "yoake-mae", copy.deepcopy(self.payload), revision=1
        ).restore_state()
        adapters = RuntimeAdapters(
            auditor=ScriptedAuditor(),
            rhythm=RhythmDirector(_content()),
            content_revision=REVISION,
        )
        runtime, gate = adapters.bind_gated(restored)
        self.assertTrue(gate.complete)
        self.assertIn("airi", [s.character_id for s in gate.subjects])
        self.assertTrue(runtime.rhythm.has("airi"))
        self.assertFalse(runtime.rhythm.has("minori"))


if __name__ == "__main__":
    unittest.main()
