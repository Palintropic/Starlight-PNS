# tests/test_departure_observed.py — FIX-46：离场进旧地点同处者的观察。
#
# 10-12 08:25 雫和爱莉离开食堂，留下的两人收不到"她们走了"，对着空座位说了
# 二十分钟。设计稿 kickoff/FIX_46_DEPARTURE_EXPOSURE_DESIGN.md（v4，ena 三轮审）。
#
# 所有输入都从公开路径构造：作息步 / Agency 提案 / commit_session_event →
# 会话里记下的观察 → 记忆 / 上下文 / 提示词。不手捏 Observation，不手捏 perceived
# （add3451 的教训：手捏的观察让一个在生产上零效果的补丁测试全绿）。
#
# 运行: python -m unittest tests.test_departure_observed -v
import copy
import json
import unittest
from datetime import datetime, timedelta

from grants_support import grant_everything
from pns.models.action import ActionId, LegalAction
from pns.models.event import MOVE_ORIGIN_KEY, Event, EventError, EventScope, EventType
from pns.models.exposure import ExposureReason
from pns.models.location import Connection, Location, LocationGraph, LocationKind
from pns.models.channel import ChannelRegistry
from pns.models.memory import MemoryClass, current_fact_ids, derive_memory_id
from pns.models.observation import DEPARTED_PERCEIVED_TYPE
from pns.models.session import SessionState, SessionStateError
from pns.models.world_state import ActivityKind, Availability, WorldState
from pns.runtime.agency.context import build_agency_context
from pns.runtime.agency.engine import AgencyOutcome
from pns.runtime.autonomy.audit import ScriptedAuditor
from pns.runtime.autonomy.context import build_generation_context
from pns.runtime.autonomy.coordinator import AutonomousRuntime
from pns.runtime.autonomy.prompt import render_situation
from pns.runtime.event_commit import EventCommitError, commit_session_event
from pns.runtime.memory.encoder import MemoryEncoder
from pns.runtime.memory.recall import MemoryRecall
from pns.runtime.rhythm import RhythmDirector
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph
from pns.world.rhythm import DailyRhythm, RhythmSegment, parse_day_minute

from test_agency import _MovePolicy, _due, _rig as _agency_rig  # noqa: E402

CLOCK = datetime(2026, 8, 21, 7, 30)
NAMES = {"ena": "绘名", "mizuki": "瑞希", "kanade": "奏", "mafuyu": "真冬"}


def _world(clock=CLOCK, graph=None):
    world = WorldState(
        clock=clock,
        locations=graph if graph is not None else build_default_location_graph(),
        channels=build_default_channel_registry() if graph is None else ChannelRegistry(),
    )
    grant_everything(world)
    return world


def _session(world):
    state = SessionState(
        session_id="s1",
        scene="gate",
        characters=["mizuki", "ena", "kanade", "mafuyu"],
    )
    state.attach_world_state(world)
    state.initialize_runtime("开场")
    return state


def _move(state, actor, to, event_id, *, scope=EventScope.LOCATION, origin=True):
    """一条跟生产者同形状的移动事件：出发地取 actor 此刻所在。"""
    world = state.world_state
    payload = {MOVE_ORIGIN_KEY: world.location_of(actor)} if origin else {}
    return Event(
        event_id=event_id,
        type=EventType.CHARACTER_LOCATION_CHANGED,
        occurred_at=world.clock,
        scope=scope,
        actor_id=actor,
        location_id=to,
        payload=payload,
    )


def _say(state, actor, text, event_id):
    world = state.world_state
    return Event(
        event_id=event_id,
        type=EventType.DIALOGUE_SPOKEN,
        occurred_at=world.clock,
        scope=EventScope.LOCATION,
        actor_id=actor,
        location_id=world.location_of(actor),
        participants=tuple(world.characters_at(world.location_of(actor))),
        payload={"text": text, "char_name": NAMES.get(actor, actor)},
    )


def _observation(state, observer, event_id):
    return state.observations.find(observer, event_id)


# ── 1. 10-12 复现：作息带人离开，留下的人看见 ta 走 ────────────────────────
ENA_LEAVES = DailyRhythm(
    character_id="ena",
    segments=(
        RhythmSegment(
            at=parse_day_minute("00:00"),
            activity=ActivityKind.IDLE,
            location_id="mizuki_home",
        ),
        RhythmSegment(
            at=parse_day_minute("08:00"),
            activity=ActivityKind.STUDYING,
            location_id="ena_home_studio",
        ),
    ),
)


class RhythmDepartureTests(unittest.TestCase):
    def setUp(self):
        world = _world()
        for cid in ("mizuki", "ena", "kanade", "mafuyu"):
            world.place_character(cid, "mizuki_home")
        world.set_availability("mafuyu", Availability.ASLEEP)
        self.state = _session(world)
        self.runtime = AutonomousRuntime(
            self.state,
            auditor=ScriptedAuditor(),
            rhythm=RhythmDirector({"ena": ENA_LEAVES}),
        )
        self.runtime.start()
        self.runtime.apply_rhythm()
        # 08:00 到绘名家画室要 25 分钟：07:35 出门，07:47 走到街上。
        self.runtime.advance(20)
        self.move = next(
            e
            for e in self.state.events
            if e.type is EventType.CHARACTER_LOCATION_CHANGED and e.actor_id == "ena"
        )

    def test_the_rhythm_writes_the_origin(self):
        self.assertEqual(self.move.location_id, "city_streets")
        self.assertEqual(dict(self.move.payload), {MOVE_ORIGIN_KEY: "mizuki_home"})

    def test_those_left_behind_see_her_go_and_nothing_more(self):
        for observer in ("mizuki", "kanade"):
            observation = _observation(self.state, observer, self.move.event_id)
            self.assertIsNotNone(observation, observer)
            self.assertIs(observation.reason, ExposureReason.DEPARTED_FROM)
            self.assertEqual(
                dict(observation.perceived),
                {
                    "type": DEPARTED_PERCEIVED_TYPE,
                    "actor_id": "ena",
                    "location_id": "mizuki_home",
                },
            )
            # 去向一个字都没有。
            self.assertNotIn("city_streets", json.dumps(observation.to_dict()))
            self.assertNotIn("ena_home", json.dumps(observation.to_dict()))

    def test_a_sleeper_in_the_room_sees_nothing(self):
        self.assertIsNone(_observation(self.state, "mafuyu", self.move.event_id))

    def test_the_prompt_says_she_left_without_saying_where(self):
        context = build_agency_context(
            self.state.world_state,
            "mizuki",
            _due_for(self.state),
            self.state.observations.for_character("mizuki"),
        )
        self.assertEqual([actor for actor, _ in context.recent_departures], ["ena"])
        text = _situation(context)
        self.assertIn("【刚才离开了这里的】绘名", text)
        self.assertIn("别当成对方还能听见你", text)
        self.assertIn("绘名离开了", text)
        for leak in ("city_streets", "街", "ena_home", "画室"):
            self.assertNotIn(leak, text)

    def test_the_archive_round_trips_and_a_forged_departure_is_refused(self):
        # 观察的逐字段核对挂在记忆核验链上（设计 §3.3：这次不扩大它的调用范围），
        # 所以先让留下的人把这次离场记住，再去篡改那条观察。作息事件本身不进
        # 记忆（协调器只编码 Agency 行动的事件），这里用运行时自己的编码器补一次。
        self.runtime.memory.encode_event(self.move.event_id)
        payload = self.state.to_dict()
        SessionState.from_dict(copy.deepcopy(payload))

        def forged(mutate):
            broken = copy.deepcopy(payload)
            for entry in broken["observations"]["observations"]:
                if (
                    entry["observer_id"] == "mizuki"
                    and entry["source_event_id"] == self.move.event_id
                ):
                    mutate(entry)
            with self.assertRaises(SessionStateError):
                SessionState.from_dict(broken)

        forged(lambda e: e["perceived"].update(location_id="kanade_home"))

        # 观察和它长出的记忆一起改成同一个假地点：记忆与观察自洽，只剩"离场地点
        # 必须是事件的出发地"这一条拦得住。
        both = copy.deepcopy(payload)
        for entry in both["observations"]["observations"]:
            if entry["observer_id"] == "mizuki" and entry["source_event_id"] == self.move.event_id:
                entry["perceived"]["location_id"] = "kanade_home"
        records = both["memory"]["store"]["records"]
        for index, entry in enumerate(records):
            if entry["owner_id"] == "mizuki" and entry["source_event_id"] == self.move.event_id:
                records[index] = json.loads(
                    json.dumps(entry, ensure_ascii=False).replace("mizuki_home", "kanade_home")
                )
        with self.assertRaisesRegex(SessionStateError, "出发地"):
            SessionState.from_dict(both)
        forged(lambda e: e["perceived"].update(destination="city_streets"))
        forged(lambda e: e.update(reason=ExposureReason.SAME_LOCATION.value))


def _due_for(state):
    from pns.models.activation import ActivationDue, ActivationKind

    return ActivationDue(
        activation_id="look",
        kind=ActivationKind.CHARACTER_ACTIVATION,
        due_at=state.world_state.clock,
        fired_at=state.world_state.clock,
        sequence=0,
        character_id="mizuki",
    )


def _situation(agency_context):
    generation = build_generation_context(
        agency_context, LegalAction(action_id=ActionId.SPEAK_HERE)
    )
    return render_situation(generation, names=NAMES)


# ── 2–4. 曝光边界 ───────────────────────────────────────────────────────
class ExposureBoundaryTests(unittest.TestCase):
    def _two_worlds(self):
        states = []
        for _ in range(2):
            world = _world()
            world.place_character("mizuki", "mizuki_home")
            world.place_character("ena", "mizuki_home")
            world.place_character("kanade", "city_streets")
            states.append(_session(world))
        return states

    def test_destination_observers_get_exactly_what_they_got_before(self):
        with_origin, without = self._two_worlds()
        commit_session_event(with_origin, _move(with_origin, "ena", "city_streets", "m"))
        commit_session_event(without, _move(without, "ena", "city_streets", "m", origin=False))
        self.assertEqual(
            _observation(with_origin, "kanade", "m").to_dict(),
            _observation(without, "kanade", "m").to_dict(),
        )
        # 没有出发地的旧形状：原地的人照旧感知不到离开。
        self.assertIsNone(_observation(without, "mizuki", "m"))
        self.assertIsNotNone(_observation(with_origin, "mizuki", "m"))

    def test_a_private_move_is_not_seen_leaving(self):
        state = self._two_worlds()[0]
        commit_session_event(
            state, _move(state, "ena", "city_streets", "m", scope=EventScope.PRIVATE)
        )
        self.assertIsNone(_observation(state, "mizuki", "m"))
        self.assertIsNone(_observation(state, "kanade", "m"))

    def test_a_forged_origin_is_refused_at_commit(self):
        state = self._two_worlds()[0]
        event = _move(state, "ena", "city_streets", "m")
        forged = Event(**{**_fields(event), "payload": {MOVE_ORIGIN_KEY: "kanade_home"}})
        with self.assertRaises(EventCommitError):
            commit_session_event(state, forged)

    def test_the_payload_takes_only_a_well_formed_origin(self):
        state = self._two_worlds()[0]
        base = _fields(_move(state, "ena", "city_streets", "m"))
        for payload in (
            {MOVE_ORIGIN_KEY: "mizuki_home", "route": ["city_streets"]},
            {MOVE_ORIGIN_KEY: ""},
            {MOVE_ORIGIN_KEY: 3},
            {MOVE_ORIGIN_KEY: "city_streets"},
        ):
            with self.assertRaises(EventError, msg=payload):
                Event(**{**base, "payload": payload})

    def test_departed_is_not_an_event_type(self):
        with self.assertRaises(EventError):
            Event(
                event_id="x",
                type=DEPARTED_PERCEIVED_TYPE,
                occurred_at=CLOCK,
                scope=EventScope.LOCATION,
                actor_id="ena",
                location_id="mizuki_home",
            )


def _fields(event):
    return {
        "event_id": event.event_id,
        "type": event.type,
        "occurred_at": event.occurred_at,
        "scope": event.scope,
        "actor_id": event.actor_id,
        "location_id": event.location_id,
        "payload": dict(event.payload),
    }


def _graph(*rooms):
    """rooms: (id, connections, audible_from)。连接是单向的，两头都要写。"""
    return LocationGraph(
        tuple(
            Location(
                location_id=room_id,
                name=room_id,
                kind=LocationKind.ROOM,
                connections=tuple(Connection(to, travel_minutes=m) for to, m in links),
                perception={"audible_from": list(audible)} if audible else {},
            )
            for room_id, links, audible in rooms
        )
    )


class AudibleRangeTests(unittest.TestCase):
    def test_the_next_room_hears_her_leave(self):
        graph = _graph(
            ("hall", (("stage", 1),), ()),
            ("stage", (("hall", 1), ("room", 1)), ("hall",)),
            ("room", (("stage", 1),), ()),
        )
        world = _world(graph=graph)
        world.place_character("ena", "stage")
        world.place_character("mizuki", "hall")
        state = _session(world)
        commit_session_event(state, _move(state, "ena", "room", "m"))
        observation = _observation(state, "mizuki", "m")
        self.assertIs(observation.reason, ExposureReason.DEPARTED_FROM)
        self.assertEqual(observation.perceived["location_id"], "stage")

    def test_hearing_the_destination_wins_over_hearing_the_departure(self):
        # 原地在目的地的可闻范围里：照旧得到到场观察（I1：离场不新增目的地，
        # 但独立满足到场资格的人本来就知道）。
        graph = _graph(
            ("a", (("c", 1),), ()),
            ("c", (("a", 1),), ("a",)),
        )
        world = _world(graph=graph)
        world.place_character("ena", "a")
        world.place_character("mizuki", "a")
        state = _session(world)
        commit_session_event(state, _move(state, "ena", "c", "m"))
        observation = _observation(state, "mizuki", "m")
        self.assertIs(observation.reason, ExposureReason.AUDIBLE_FROM)
        self.assertEqual(observation.perceived["type"], "character.location_changed")


# ── 3b. 同一次规划里零分钟连走多跳：每跳的出发地是上一跳的落点 ─────────────
class ZeroMinuteHopsTests(unittest.TestCase):
    def test_each_hop_departs_from_where_the_last_one_landed(self):
        graph = _graph(
            ("a", (("b", 0),), ()),
            ("b", (("a", 0), ("c", 0)), ()),
            ("c", (("b", 0),), ()),
        )
        world = _world(clock=datetime(2026, 8, 21, 8, 0), graph=graph)
        world.place_character("ena", "a")
        world.place_character("mizuki", "a")
        world.place_character("kanade", "b")
        state = _session(world)
        rhythm = DailyRhythm(
            character_id="ena",
            segments=(
                RhythmSegment(
                    at=parse_day_minute("08:00"),
                    activity=ActivityKind.IDLE,
                    location_id="c",
                ),
            ),
        )
        runtime = AutonomousRuntime(
            state, auditor=ScriptedAuditor(), rhythm=RhythmDirector({"ena": rhythm})
        )
        runtime.start()
        runtime.apply_rhythm()
        moves = [e for e in state.events if e.type is EventType.CHARACTER_LOCATION_CHANGED]
        self.assertEqual(
            [(dict(e.payload)[MOVE_ORIGIN_KEY], e.location_id) for e in moves],
            [("a", "b"), ("b", "c")],
        )
        self.assertEqual(state.world_state.location_of("ena"), "c")
        self.assertIs(
            _observation(state, "mizuki", moves[0].event_id).reason,
            ExposureReason.DEPARTED_FROM,
        )
        self.assertIs(
            _observation(state, "kanade", moves[0].event_id).reason,
            ExposureReason.SAME_LOCATION,
        )
        self.assertIs(
            _observation(state, "kanade", moves[1].event_id).reason,
            ExposureReason.DEPARTED_FROM,
        )


# ── 3c. Agency 的移动：出发地来自世界，存档往返成立；信任边界写清楚 ───────
class AgencyMoveArchiveTests(unittest.TestCase):
    def setUp(self):
        self.state, scheduler, engine = _agency_rig(policy=_MovePolicy("mizuki_home"))
        record = engine.evaluate(_due(scheduler, "a1", minutes=10))
        self.assertIs(record.outcome, AgencyOutcome.ACTED)
        self.event = self.state.events.get(record.event_id)
        self.payload = self.state.to_dict()

    def _entry(self, payload):
        for entry in payload["events"]["events"]:
            if entry["event_id"] == self.event.event_id:
                return entry
        raise AssertionError("没找到那条移动")

    def test_the_origin_is_where_she_stood(self):
        self.assertEqual(dict(self.event.payload), {MOVE_ORIGIN_KEY: "mizuki_home_room"})
        SessionState.from_dict(copy.deepcopy(self.payload))

    def test_malformed_borrowed_origins_are_refused(self):
        for mutate in (
            lambda p: p.update(extra="x"),
            lambda p: p.update({MOVE_ORIGIN_KEY: ""}),
            lambda p: p.update({MOVE_ORIGIN_KEY: 7}),
            lambda p: p.update({MOVE_ORIGIN_KEY: "mizuki_home"}),
        ):
            broken = copy.deepcopy(self.payload)
            mutate(self._entry(broken)["payload"])
            with self.assertRaises((SessionStateError, ValueError)):
                SessionState.from_dict(broken)

    def test_without_a_baseline_a_well_formed_wrong_origin_restores(self):
        # 信任边界（设计 §3.1）：借用只验形状。没有 WORLD-2 基准的会话，恢复
        # 证明不了出发地是真的 —— 这一条恢复成功是预期，不是漏洞，也不是保证。
        broken = copy.deepcopy(self.payload)
        self._entry(broken)["payload"][MOVE_ORIGIN_KEY] = "city_streets"
        SessionState.from_dict(broken)

    def test_an_old_move_without_an_origin_still_restores(self):
        old = copy.deepcopy(self.payload)
        self._entry(old)["payload"].pop(MOVE_ORIGIN_KEY)
        SessionState.from_dict(old)


# ── 8. 记忆：同一个事实只有最新那条算现在知道的 ─────────────────────────
class CurrentFactTests(unittest.TestCase):
    def setUp(self):
        world = _world()
        world.place_character("mizuki", "mizuki_home")
        world.place_character("ena", "ena_home")
        self.state = _session(world)
        self.encoder = MemoryEncoder(self.state)

    def _tick(self):
        self.state.world_state.advance_time(1)

    def _facts(self, state=None):
        state = state or self.state
        result = MemoryRecall(state).recall_for("mizuki", classes=(MemoryClass.SEMANTIC,))
        return [dict(r.content) for r in result.records]

    def _current(self, state=None):
        state = state or self.state
        current = current_fact_ids(
            state.memories.for_owner("mizuki"), state.observations.position_of
        )
        return current[("mizuki", "location:ena")]

    def test_leaving_replaces_being_here(self):
        self.encoder.commit_and_encode(_move(self.state, "ena", "mizuki_home", "m1"))
        self._tick()
        self.encoder.commit_and_encode(_move(self.state, "ena", "city_streets", "m2"))
        facts = [f for f in self._facts() if f["fact"] == "location:ena"]
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["value"], "mizuki_home")
        self.assertEqual(facts[0]["stance"], "away")
        self._tick()
        self.encoder.commit_and_encode(_move(self.state, "ena", "mizuki_home", "m3"))
        facts = [f for f in self._facts() if f["fact"] == "location:ena"]
        self.assertEqual(len(facts), 1)
        self.assertNotIn("stance", facts[0])

    def test_two_of_my_own_moves_recall_only_where_i_am_now(self):
        # 修复前的双位置召回（ena 实跑："瑞希 在 瑞希家 / 瑞希 在 街道"）。
        self.encoder.commit_and_encode(_move(self.state, "mizuki", "city_streets", "s1"))
        self._tick()
        self.encoder.commit_and_encode(_move(self.state, "mizuki", "ena_home", "s2"))
        mine = [f for f in self._facts() if f["fact"] == "location:mizuki"]
        self.assertEqual([f["value"] for f in mine], ["ena_home"])

    def test_late_out_of_order_encoding_keeps_the_newer_fact(self):
        commit_session_event(self.state, _move(self.state, "mizuki", "city_streets", "old"))
        commit_session_event(self.state, _move(self.state, "mizuki", "ena_home", "new"))
        self.encoder.encode_event("new")
        self.encoder.encode_event("old")
        restored = SessionState.from_dict(copy.deepcopy(self.state.to_dict()))
        for state in (self.state, restored):
            mine = [f for f in self._facts(state) if f["fact"] == "location:mizuki"]
            self.assertEqual([f["value"] for f in mine], ["ena_home"])

    def test_swapping_same_minute_memories_in_the_archive_changes_nothing(self):
        self.encoder.commit_and_encode(_move(self.state, "mizuki", "city_streets", "first"))
        self.encoder.commit_and_encode(_move(self.state, "mizuki", "ena_home", "second"))
        payload = self.state.to_dict()
        records = payload["memory"]["store"]["records"]
        self.assertGreater(len(records), 1)
        records.reverse()
        for index, entry in enumerate(records):
            entry["sequence"] = index
        restored = SessionState.from_dict(payload)
        mine = [f for f in self._facts(restored) if f["fact"] == "location:mizuki"]
        self.assertEqual([f["value"] for f in mine], ["ena_home"])

    def test_a_reordered_observation_log_is_refused(self):
        self.encoder.commit_and_encode(_move(self.state, "mizuki", "city_streets", "first"))
        self.encoder.commit_and_encode(_move(self.state, "mizuki", "ena_home", "second"))
        payload = self.state.to_dict()
        payload["observations"]["observations"].reverse()
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(payload)

    def test_a_b_a_encoded_out_of_order_still_ends_at_the_second_a(self):
        # ena 的 v3 复核：A₂ 不能被当成重复跳过，否则最新的证据就只剩 B。
        self.state.world_state.place_character("ena", "city_streets")
        commit_session_event(self.state, _move(self.state, "mizuki", "city_streets", "a1"))
        commit_session_event(self.state, _move(self.state, "mizuki", "ena_home", "b"))
        commit_session_event(self.state, _move(self.state, "mizuki", "city_streets", "a2"))
        for event_id in ("a1", "a2", "b"):
            self.encoder.encode_event(event_id)
        restored = SessionState.from_dict(copy.deepcopy(self.state.to_dict()))
        for state in (self.state, restored):
            current = current_fact_ids(
                state.memories.for_owner("mizuki"), state.observations.position_of
            )
            self.assertEqual(
                current[("mizuki", "location:mizuki")],
                derive_memory_id("mizuki", "a2", MemoryClass.SEMANTIC),
            )

    def test_the_same_observation_encoded_twice_is_still_idempotent(self):
        self.encoder.commit_and_encode(_move(self.state, "ena", "mizuki_home", "m1"))
        before = len(self.state.memories)
        self.encoder.encode_event("m1")
        self.assertEqual(len(self.state.memories), before)


# ── 9. 提示词：回来的不点名、过了一小时不点名、被裁掉也点名 ───────────────
class DepartureCueTests(unittest.TestCase):
    def setUp(self):
        world = _world()
        for cid in ("mizuki", "ena", "kanade"):
            world.place_character(cid, "mizuki_home")
        self.state = _session(world)
        commit_session_event(self.state, _move(self.state, "ena", "city_streets", "left"))

    def _context(self, **kwargs):
        return build_agency_context(
            self.state.world_state,
            "mizuki",
            _due_for(self.state),
            self.state.observations.for_character("mizuki"),
            **kwargs,
        )

    def test_a_departure_squeezed_out_of_the_window_is_still_named(self):
        for index in range(5):
            self.state.world_state.advance_time(1)
            commit_session_event(
                self.state, _say(self.state, "kanade", f"第{index}句", f"k{index}")
            )
        context = self._context(max_observations=2)
        self.assertTrue(context.observations_truncated)
        self.assertNotIn("left", [o.source_event_id for o in context.observations])
        self.assertIn("【刚才离开了这里的】绘名", _situation(context))

    def test_someone_who_came_back_is_not_named(self):
        self.state.world_state.advance_time(5)
        commit_session_event(self.state, _move(self.state, "ena", "mizuki_home", "back"))
        self.assertEqual(self._context().recent_departures, ())
        self.assertNotIn("刚才离开了这里的", _situation(self._context()))

    def test_someone_last_seen_arriving_is_not_named_even_if_gone(self):
        # 看见她回来了，之后她私下走开（私密移动，谁也看不见）：没有看见她走，
        # 就不说"刚才离开了"。
        self.state.world_state.advance_time(5)
        commit_session_event(self.state, _move(self.state, "ena", "mizuki_home", "back"))
        self.state.world_state.advance_time(1)
        commit_session_event(
            self.state,
            _move(self.state, "ena", "city_streets", "quiet", scope=EventScope.PRIVATE),
        )
        self.assertNotIn("ena", self.state.world_state.characters_at("mizuki_home"))
        self.assertEqual(self._context().recent_departures, ())

    def test_after_an_hour_she_is_no_longer_named(self):
        self.state.world_state.advance_time(60)
        self.assertEqual([a for a, _ in self._context().recent_departures], ["ena"])
        self.state.world_state.advance_time(1)
        self.assertEqual(self._context().recent_departures, ())

    def test_leaving_somewhere_else_is_not_named_here(self):
        commit_session_event(self.state, _move(self.state, "mizuki", "city_streets", "me"))
        # 瑞希自己到了街上：绘名是从家里走的，不是从这里。
        self.assertEqual(self._context().recent_departures, ())



# ── 3c（续）有 WORLD-2 基准时，出发地的真假由历史重放兜住 ─────────────────
class BaselineReplayTests(unittest.TestCase):
    def test_a_wrong_origin_in_the_replayed_suffix_is_refused(self):
        from pns.runtime.world2_replay import WorldReplayError, verify_world2_history
        from test_world2_runtime import _admit, _extend, _rig as _world2_rig

        state, runtime, content = _world2_rig()
        _extend(state, runtime, content)
        _admit(state, runtime, content)
        runtime.advance(600)  # 23:30 → 09:30：早上出门上学，作息带人走
        events = list(state.events)
        baseline = next(
            i for i, e in enumerate(events) if e.type is EventType.WORLD_LOCATIONS_EXTENDED
        )
        lone = next(
            e
            for e in events[baseline + 1 :]
            if e.type is EventType.CHARACTER_LOCATION_CHANGED
            and MOVE_ORIGIN_KEY in e.payload
            and not [
                o for o in state.observations.for_event(e.event_id)
                if o.reason is ExposureReason.DEPARTED_FROM
            ]
        )
        payload = state.to_dict()
        verify_world2_history(SessionState.from_dict(copy.deepcopy(payload)))
        for entry in payload["events"]["events"]:
            if entry["event_id"] == lone.event_id:
                wrong = next(
                    i for i in state.world_state.locations.ids()
                    if i not in (lone.payload[MOVE_ORIGIN_KEY], lone.location_id)
                )
                entry["payload"][MOVE_ORIGIN_KEY] = wrong
        restored = SessionState.from_dict(payload)
        with self.assertRaises(WorldReplayError):
            verify_world2_history(restored)


if __name__ == "__main__":
    unittest.main()
