# tests/test_world2_ledger.py — WORLD-2 C6：账本入住采用与重放基准。
#
# 守的线（设计 v3 §4.5、§6.7；实施单 §4.6）：
#   1. 账本：入住采用与记录 / 决定共用一条连续序号；内容项只能经入住采用新增；
#      重放时入住采用的内容项在那一刻之前不存在；没入住过的账本存档形状不变；
#   2. 提交：世界上第一笔 WORLD-2 操作必须是带基准的扩展，基准逐字就是那一刻的世界；
#      之后谁都不带基准；入住在同一事务里写入住采用，随事务回滚；
#   3. 恢复：从基准加后缀重放推出名单、图、授予、位置、活动、频道，与快照逐项比；
#      账本的入住采用与入住事件一一对应；设计 §6.7 的每种篡改都拒绝打开，
#      而且撞上的是目标检查（断言错误信息）；正常的后续移动 / 活动 / 作息采用照常打开。
#
# 运行: python -m unittest discover -s tests -p test_world2_ledger.py
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

from pns.models.content_ledger import (  # noqa: E402
    ContentAdmission,
    ContentLedger,
    ContentLedgerError,
)
from pns.models.event import Event, EventScope, EventType  # noqa: E402
from pns.models.session import SessionState  # noqa: E402
from pns.runtime.event_commit import (  # noqa: E402
    EventCommitError,
    commit_authority_event,
    commit_session_event,
)
from pns.runtime.persistence.archive import ArchiveError, WorldArchive  # noqa: E402
from pns.runtime.reload import BOUNDARY  # noqa: E402
from pns.runtime.world2_baseline import build_baseline  # noqa: E402
from pns.runtime.world2_replay import WorldReplayError, verify_world2_history  # noqa: E402
from pns.world.locations import build_default_location_graph  # noqa: E402

from tests.test_world2_content import FOUR, NEW  # noqa: E402
from tests.test_world2_events import (  # noqa: E402
    _Injected,
    _admission_payload,
    _event,
    _extension_payload,
    _old_world_state,
)

A, B, C = "a" * 64, "b" * 64, "c" * 64


# ── 1. 账本 ─────────────────────────────────────────────────────────────
class LedgerAdmissionTests(unittest.TestCase):
    genesis = {"rhythm:mizuki": A}

    def base(self):
        return ContentLedger((("rhythm:mizuki", A),))

    def test_admission_takes_the_next_seq_and_survives_later_operations(self):
        ledger = self.base().offered("rhythm:mizuki", B, registry_revision=1, wall="w")
        ledger = ledger.admitted("rhythm:airi", C, operation_id="op-airi")
        self.assertEqual(ledger.admissions, (ContentAdmission("rhythm:airi", C, 1, "op-airi"),))
        self.assertEqual(ledger.adopted_fingerprint("rhythm:airi"), C)
        ledger = ledger.decided(ledger.pending()[0].conflict_id, "adopted", wall="w")
        ledger = ledger.offered("rhythm:airi", A, registry_revision=2, wall="w")
        self.assertEqual(len(ledger.admissions), 1)
        self.assertEqual(ledger.next_seq, 4)
        ledger.check_genesis(self.genesis)
        self.assertEqual(ContentLedger.from_dict(ledger.to_dict()), ledger)

    def test_a_ledger_without_admissions_keeps_its_saved_shape(self):
        ledger = self.base().offered("rhythm:mizuki", B, registry_revision=1, wall="w")
        self.assertEqual(set(ledger.to_dict()), {"adopted", "conflicts", "next_seq"})

    def test_an_empty_admissions_list_in_a_save_is_refused(self):
        payload = self.base().to_dict()
        payload["admissions"] = []
        with self.assertRaises(ContentLedgerError):
            ContentLedger.from_dict(payload)

    def test_cannot_admit_an_existing_subject(self):
        with self.assertRaisesRegex(ContentLedgerError, "已经在账本里"):
            self.base().admitted("rhythm:mizuki", B, operation_id="op")

    def test_successor_may_add_a_subject_only_by_admission(self):
        before = self.base()
        before.admitted("rhythm:airi", C, operation_id="op").check_successor_of(before)
        smuggled = ContentLedger((("rhythm:mizuki", A), ("rhythm:airi", C)), (), 0)
        with self.assertRaisesRegex(ContentLedgerError, "新增只能经入住采用"):
            smuggled.check_successor_of(before)

    def test_successor_cannot_drop_or_rewrite_an_admission(self):
        before = self.base().admitted("rhythm:airi", C, operation_id="op")
        rewritten = ContentLedger(
            before.adopted, (), 1, (ContentAdmission("rhythm:airi", C, 0, "other"),)
        )
        with self.assertRaisesRegex(ContentLedgerError, "只能追加"):
            rewritten.check_successor_of(before)

    def test_successor_admission_cannot_reuse_a_seq(self):
        before = self.base().offered("rhythm:mizuki", B, registry_revision=1, wall="w")
        # 一份内部自洽、却把新入住塞进旧序号的账本。
        reused = ContentLedger(
            (("rhythm:mizuki", A), ("rhythm:airi", C)),
            (dataclasses.replace(before.conflicts[0], offered_seq=1),),
            2,
            (ContentAdmission("rhythm:airi", C, 0, "op"),),
        )
        with self.assertRaisesRegex(ContentLedgerError, "已经用过的序号"):
            reused.check_successor_of(before)

    def test_genesis_refuses_an_admitted_subject_that_was_in_the_origin(self):
        ledger = self.base().admitted("rhythm:airi", C, operation_id="op")
        with self.assertRaisesRegex(ContentLedgerError, "那一刻已经存在"):
            ledger.check_genesis({"rhythm:mizuki": A, "rhythm:airi": C})

    def test_genesis_refuses_a_subject_that_is_neither_origin_nor_admitted(self):
        ledger = self.base().admitted("rhythm:airi", C, operation_id="op")
        with self.assertRaisesRegex(ContentLedgerError, "开局来源加入住采用不一致"):
            ledger.check_genesis({})

    def test_genesis_refuses_a_conflict_on_a_subject_before_its_admission(self):
        # 入住采用的序号被搬到针对它的冲突记录之后：重放到那条记录时内容项还不存在。
        ledger = self.base().admitted("rhythm:airi", C, operation_id="op")
        ledger = ledger.offered("rhythm:airi", A, registry_revision=1, wall="w")
        swapped = ContentLedger(
            ledger.adopted,
            (dataclasses.replace(ledger.conflicts[0], offered_seq=0),),
            2,
            (dataclasses.replace(ledger.admissions[0], seq=1),),
        )
        with self.assertRaisesRegex(ContentLedgerError, "还不存在"):
            swapped.check_genesis(self.genesis)

    def test_construction_refuses_broken_admission_lists(self):
        good = ContentAdmission("rhythm:airi", C, 0, "op")
        cases = {
            "不在已采用版本里": ((("rhythm:mizuki", A),), (), 1, (good,)),
            "入住采用了两次": (
                (("rhythm:mizuki", A), ("rhythm:airi", C)),
                (),
                2,
                (good, dataclasses.replace(good, seq=1)),
            ),
            "不连续": ((("rhythm:mizuki", A), ("rhythm:airi", C)), (), 2, (good,)),
            "按序号排列": (
                (("rhythm:mizuki", A), ("rhythm:airi", C), ("rhythm:minori", B)),
                (),
                2,
                (dataclasses.replace(good, seq=1), ContentAdmission("rhythm:minori", B, 0, "op2")),
            ),
            "最后一次采用记录不一致": ((("rhythm:mizuki", A), ("rhythm:airi", B)), (), 1, (good,)),
        }
        for message, args in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ContentLedgerError, message):
                    ContentLedger(*args)

    def test_adopted_before_replays_up_to_a_seq(self):
        ledger = self.base().offered("rhythm:mizuki", B, registry_revision=1, wall="w")
        ledger = ledger.decided(ledger.pending()[0].conflict_id, "adopted", wall="w")
        ledger = ledger.admitted("rhythm:airi", C, operation_id="op")
        self.assertEqual(ledger.adopted_before(self.genesis, 1), {"rhythm:mizuki": A})
        self.assertEqual(ledger.adopted_before(self.genesis, 2), {"rhythm:mizuki": B})
        self.assertEqual(
            ledger.adopted_before(self.genesis, 3), {"rhythm:mizuki": B, "rhythm:airi": C}
        )
        with self.assertRaises(ContentLedgerError):
            ledger.adopted_before(self.genesis, 4)


# ── 世界夹具 ────────────────────────────────────────────────────────────
def _activity(state, actor, activity, event_id):
    world = state.world_state
    commit_session_event(
        state,
        Event(
            event_id=event_id,
            type=EventType.CHARACTER_ACTIVITY_CHANGED,
            occurred_at=world.clock,
            scope=EventScope.PRIVATE,
            actor_id=actor,
            payload={"activity": activity},
        ),
    )


def _advance(state, minutes, event_id):
    world = state.world_state
    commit_session_event(
        state,
        Event(
            event_id=event_id,
            type=EventType.WORLD_TIME_ADVANCED,
            occurred_at=world.clock,
            scope=EventScope.PUBLIC,
            payload={"minutes": minutes},
        ),
    )


def _offer_before(state):
    """基准之前账本里已有一条操作：基准的序号不是 0。"""
    with state.atomic_commit():
        state.set_content(
            state.content.offered("rhythm:mizuki", B, registry_revision=99, wall="w")
        )


def _admitted_world(admit=("airi",)):
    """旧世界 → 前缀里一条普通事件、账本里一条记录 → 扩展（带基准）→ 入住 → 后缀里的正常生活。"""
    state = _old_world_state()
    _activity(state, "mizuki", "idle", "pre:mizuki")
    _offer_before(state)
    world = state.world_state
    commit_authority_event(
        state, _event(world, EventType.WORLD_LOCATIONS_EXTENDED, _extension_payload(state), "w2:ext")
    )
    for character_id in admit:
        commit_authority_event(
            state,
            _event(
                world,
                EventType.WORLD_RESIDENT_ADMITTED,
                _admission_payload(world, character_id),
                f"w2:admit:{character_id}",
            ),
        )
    # 后缀里的正常生活。新人入住之后没有活动事件：她的活动只由入住事件决定，
    # 所以快照里改她的活动只有重放这一关看得见（已有的活动历史校验只看活动事件）。
    _advance(state, 10, "post:tick")
    _activity(state, "mizuki", "online_chatting", "post:activity")
    return state


def _renumbered(payload):
    for index, entry in enumerate(payload["events"]["events"]):
        entry["sequence"] = index
    return payload


def _restore(payload):
    return verify_world2_history(SessionState.from_dict(payload))


# ── 2. 提交边界 ─────────────────────────────────────────────────────────
class CommitBaselineTests(unittest.TestCase):
    def setUp(self):
        self.state = _old_world_state()
        _activity(self.state, "mizuki", "idle", "pre:mizuki")
        self.world = self.state.world_state

    def _extend(self, payload, event_id="w2:ext"):
        return commit_authority_event(
            self.state, _event(self.world, EventType.WORLD_LOCATIONS_EXTENDED, payload, event_id)
        )

    def assert_refused(self, payload, message):
        graph, events = self.world.locations, len(self.state.events)
        with self.assertRaisesRegex(EventCommitError, message):
            self._extend(payload)
        self.assertIs(self.world.locations, graph)
        self.assertEqual(len(self.state.events), events)

    def test_the_first_extension_records_the_world_as_it_was(self):
        payload = _extension_payload(self.state)
        self._extend(payload)
        baseline = payload["baseline"]
        self.assertEqual(baseline["roster"], list(FOUR))
        self.assertEqual(baseline["prefix"]["length"], 1)
        self.assertEqual(baseline["ledger"]["next_seq"], 0)
        self.assertEqual(set(baseline["rhythms"]), set(FOUR))
        self.assertFalse(set(NEW) & set(baseline["locations"]))

    def test_the_first_extension_must_carry_a_baseline(self):
        payload = _extension_payload(self.state)
        payload["baseline"] = None
        self.assert_refused(payload, "必须带重放基准")

    def test_a_later_extension_must_not_carry_one(self):
        first = _extension_payload(self.state)
        self._extend(first)
        second = copy.deepcopy(first)  # 同一份内容、同一份基准：基准检查先于图检查
        with self.assertRaisesRegex(EventCommitError, "只在世界上第一笔"):
            self._extend(second, "w2:ext2")

    def test_an_admission_cannot_come_first(self):
        payload = _admission_payload(self.world)
        with self.assertRaisesRegex(EventCommitError, "第一笔 WORLD-2 操作必须是带重放基准的地点扩展"):
            commit_authority_event(
                self.state, _event(self.world, EventType.WORLD_RESIDENT_ADMITTED, payload)
            )

    def test_a_baseline_that_is_not_this_world_is_refused(self):
        def roster(b):
            b["roster"] = b["roster"][::-1]

        def position(b):
            b["world"]["character_locations"]["mizuki"] = "city_streets"

        def grants(b):
            b["world"]["location_grants"]["mizuki"].pop(
                sorted(b["world"]["location_grants"]["mizuki"])[0]
            )

        def graph(b):
            b["locations"]["kanade_home"]["name"] += "?"

        def clock(b):
            b["clock"] = "2026-01-01T00:00:00"

        def prefix(b):
            b["prefix"]["fingerprint"] = "0" * 64

        def ledger_seq(b):
            b["ledger"]["next_seq"] = 1

        def calendar(b):
            b["calendar"]["rules_version"] = 99

        for mutate in (roster, position, grants, graph, clock, prefix, ledger_seq, calendar):
            with self.subTest(mutate=mutate.__name__):
                payload = _extension_payload(self.state)
                mutate(payload["baseline"])
                self.assert_refused(payload, "与此刻的世界不符")

    def test_baseline_rhythms_must_be_the_adopted_versions(self):
        payload = _extension_payload(self.state)
        rhythms = BOUNDARY.active().rhythms()
        payload["baseline"]["rhythms"].pop("mizuki")
        self.assert_refused(payload, "作息主体不一致")
        payload = _extension_payload(self.state)
        from pns.world.rhythm import encode_rhythm

        stale = encode_rhythm(rhythms["mizuki"])
        segment = stale["segments"][0]
        segment["activity"] = "composing" if segment["activity"] != "composing" else "idle"
        payload["baseline"]["rhythms"]["mizuki"] = stale
        self.assert_refused(payload, "不是账本已采用的那一版")

    def test_baseline_shape_is_checked(self):
        payload = _extension_payload(self.state)
        payload["baseline"]["schema"] = "pns.world2_baseline/0"
        self.assert_refused(payload, "不认识的重放基准版本")
        payload = _extension_payload(self.state)
        payload["baseline"]["extra"] = 1
        self.assert_refused(payload, "字段不对")

    def test_a_world_without_a_ledger_is_refused(self):
        payload = _extension_payload(self.state)
        self.state.content = None
        self.assert_refused(payload, "WORLD-2 的权威操作只用于带内容账本的正式世界")

    def test_admission_writes_the_ledger_in_the_same_transaction(self):
        self._extend(_extension_payload(self.state))
        payload = _admission_payload(self.world)
        commit_authority_event(
            self.state, _event(self.world, EventType.WORLD_RESIDENT_ADMITTED, payload)
        )
        ledger = self.state.content
        self.assertEqual(
            ledger.admissions,
            (ContentAdmission("rhythm:airi", payload["fingerprints"]["rhythm"], 0, "op-admit-airi"),),
        )

    def test_outer_failure_rolls_the_ledger_back(self):
        self._extend(_extension_payload(self.state))
        ledger = self.state.content
        with self.assertRaises(_Injected):
            with self.state.atomic_commit():
                commit_authority_event(
                    self.state,
                    _event(self.world, EventType.WORLD_RESIDENT_ADMITTED, _admission_payload(self.world)),
                )
                raise _Injected()
        self.assertIs(self.state.content, ledger)


# ── 3. 恢复互验 ─────────────────────────────────────────────────────────
class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.state = _admitted_world()
        self.payload = self.state.to_dict()

    def tampered(self, mutate, message):
        payload = copy.deepcopy(self.payload)
        mutate(payload)
        with self.assertRaisesRegex(WorldReplayError, message):
            _restore(payload)

    def _event_entry(self, payload, event_id):
        for entry in payload["events"]["events"]:
            if entry["event_id"] == event_id:
                return entry
        raise AssertionError(event_id)

    # 正例
    def test_a_faithful_save_restores(self):
        _restore(copy.deepcopy(self.payload))

    def test_later_life_and_rhythm_adoption_restore(self):
        state = _admitted_world(("airi", "minori"))
        with state.atomic_commit():
            state.set_content(state.content.offered("rhythm:airi", A, registry_revision=7, wall="w"))
        with state.atomic_commit():
            state.set_content(
                state.content.decided(state.content.pending()[-1].conflict_id, "adopted", wall="w")
            )
        _advance(state, 30, "post:tick2")
        _activity(state, "minori", "idle", "post:minori")
        _restore(state.to_dict())

    def test_an_old_world_without_world2_is_untouched(self):
        verify_world2_history(_old_world_state())

    # §6.7：只改快照
    def test_snapshot_roster(self):
        def mutate(p):
            p["characters"] = p["characters"][:-1]
            for table in ("histories", "pending_corrections"):
                p[table].pop("airi", None)
            p["current_character_index"] = 0

        # 名单少了她，账本 genesis 那一关还过得去（账本不看名单），重放这一关拦下。
        self.tampered(mutate, "名单")

    def test_snapshot_position(self):
        def mutate(p):
            locations = p["world_state"]["character_locations"]
            locations["mizuki"] = next(
                lid for lid, _ in sorted(p["world_state"]["location_grants"]["mizuki"].items())
                if lid != locations["mizuki"]
            )

        self.tampered(mutate, "character_locations")

    def test_snapshot_activity(self):
        self.tampered(
            lambda p: p["world_state"]["character_activities"]["airi"].update(kind="composing"),
            "character_activities",
        )

    def test_snapshot_grants(self):
        def mutate(p):
            grants = p["world_state"]["location_grants"]["airi"]
            here = p["world_state"]["character_locations"]["airi"]
            grants.pop(next(lid for lid in sorted(grants) if lid != here))

        self.tampered(mutate, "location_grants")

    def test_snapshot_channel_grants(self):
        self.tampered(
            lambda p: p["world_state"]["channel_grants"].update(airi=["nightcord"]),
            "channel_grants",
        )

    def test_snapshot_old_graph_edge(self):
        def mutate(p):
            p["world_state"]["locations"]["kanade_home"]["connections"][0]["travel_minutes"] += 1

        self.tampered(mutate, "locations")

    # §6.7：只改历史 / 账本
    def test_deleted_admission_event_keeps_roster(self):
        def mutate(p):
            p["events"]["events"] = [
                e for e in p["events"]["events"] if e["event_id"] != "w2:admit:airi"
            ]
            _renumbered(p)

        # 名单、授予、位置都还留着她，历史里却没人让她进来。
        self.tampered(mutate, "名单")

    def test_duplicated_admission_with_a_new_id(self):
        def mutate(p):
            events = p["events"]["events"]
            index = next(i for i, e in enumerate(events) if e["event_id"] == "w2:admit:airi")
            twin = copy.deepcopy(events[index])
            twin["event_id"] = "w2:admit:airi:twin"
            events.insert(index + 1, twin)
            _renumbered(p)

        self.tampered(mutate, "从基准重放到事件 'w2:admit:airi:twin'")

    def test_admission_moved_before_the_extension(self):
        def mutate(p):
            events = p["events"]["events"]
            ext = next(i for i, e in enumerate(events) if e["event_id"] == "w2:ext")
            events.insert(ext, events.pop(ext + 1))
            _renumbered(p)

        self.tampered(mutate, "第一笔 WORLD-2 操作不是带重放基准的地点扩展")

    def test_deleted_first_extension(self):
        def mutate(p):
            p["events"]["events"] = [e for e in p["events"]["events"] if e["event_id"] != "w2:ext"]
            _renumbered(p)

        self.tampered(mutate, "第一笔 WORLD-2 操作不是带重放基准的地点扩展")

    def test_deleted_baseline(self):
        self.tampered(
            lambda p: self._event_entry(p, "w2:ext")["payload"].update(baseline=None),
            "第一笔 WORLD-2 操作不是带重放基准的地点扩展",
        )

    def test_a_second_baseline(self):
        state = _admitted_world()
        annex = _annex_extension(state)
        commit_authority_event(
            state, _event(state.world_state, EventType.WORLD_LOCATIONS_EXTENDED, annex, "w2:ext2")
        )
        payload = state.to_dict()
        _restore(copy.deepcopy(payload))  # 第二笔扩展本身合法
        first = self._event_entry(payload, "w2:ext")["payload"]["baseline"]
        self._event_entry(payload, "w2:ext2")["payload"]["baseline"] = first
        with self.assertRaisesRegex(WorldReplayError, "第二份重放基准"):
            _restore(payload)

    def test_changed_prefix_identity(self):
        def fingerprint(p):
            self._event_entry(p, "w2:ext")["payload"]["baseline"]["prefix"]["fingerprint"] = "0" * 64

        def length(p):
            self._event_entry(p, "w2:ext")["payload"]["baseline"]["prefix"]["length"] = 0

        for mutate in (fingerprint, length):
            with self.subTest(mutate=mutate.__name__):
                self.tampered(mutate, "事件前缀")

    def test_baseline_clock_moved(self):
        def mutate(p):
            entry = self._event_entry(p, "w2:ext")
            entry["payload"]["baseline"]["clock"] = "2026-09-27T18:50:00"

        self.tampered(mutate, "重放基准的时钟")

    def test_origin_gains_a_resident(self):
        self.tampered(
            lambda p: p["world_state"]["metadata"]["origin"]["residents"].append("airi"),
            "开局来源里的 resident",
        )

    def test_baseline_rhythms_do_not_match_roster(self):
        def mutate(p):
            baseline = self._event_entry(p, "w2:ext")["payload"]["baseline"]
            baseline["roster"] = baseline["roster"][:-1]
            p["world_state"]["metadata"]["origin"]["residents"] = baseline["roster"]

        self.tampered(mutate, "作息与名单不一致")

    def test_admission_seq_moved_before_the_baseline(self):
        # 基准前那条记录与入住采用对调序号：账本自身仍连续、可重放，但重放到基准的序号时多出了她。
        def mutate(p):
            content = p["content"]
            content["conflicts"][0]["offered_seq"] = 1
            content["admissions"][0]["seq"] = 0
            self._event_entry(p, "w2:ext")["payload"]["baseline"]["ledger"]["next_seq"] = 1

        self.tampered(mutate, "账本重放到基准的序号时")

    def test_baseline_ledger_seq_out_of_range(self):
        self.tampered(
            lambda p: self._event_entry(p, "w2:ext")["payload"]["baseline"]["ledger"].update(
                next_seq=99
            ),
            "账本序号不成立",
        )

    def test_baseline_ledger_shape(self):
        self.tampered(
            lambda p: self._event_entry(p, "w2:ext")["payload"]["baseline"]["ledger"].update(x=1),
            "账本字段不对",
        )

    def test_baseline_world_shape(self):
        self.tampered(
            lambda p: self._event_entry(p, "w2:ext")["payload"]["baseline"]["world"].pop(
                "channel_grants"
            ),
            "世界状态字段不对",
        )

    def test_baseline_rhythm_not_the_adopted_one(self):
        def mutate(p):
            baseline = self._event_entry(p, "w2:ext")["payload"]["baseline"]
            baseline["ledger"]["adopted"]["rhythm:mizuki"] = B

        # 账本重放到基准序号时 mizuki 仍是旧版：基准记下的已采用版本对不上。
        self.tampered(mutate, "账本重放到基准的序号时")

    def test_baseline_rhythm_definition_swapped(self):
        def mutate(p):
            baseline = self._event_entry(p, "w2:ext")["payload"]["baseline"]
            segment = baseline["rhythms"]["mizuki"]["segments"][0]
            segment["activity"] = "composing" if segment["activity"] != "composing" else "idle"

        self.tampered(mutate, "不是账本已采用的那一版")

    def test_ledger_admission_operation_rewritten(self):
        self.tampered(
            lambda p: p["content"]["admissions"][0].update(operation_id="op-forged"),
            "入住采用与世界历史里的入住事件对不上",
        )

    def test_ledger_admission_without_any_world2_event(self):
        state = _old_world_state()
        with state.atomic_commit():
            state.content = state.content.admitted("rhythm:airi", C, operation_id="op")
        with self.assertRaisesRegex(WorldReplayError, "没有任何 WORLD-2 操作"):
            verify_world2_history(state)

    def test_calendar_changed(self):
        with patch(
            "pns.runtime.world2_replay.calendar_binding",
            return_value={"fingerprint": "0" * 64, "rules_version": 2},
        ):
            with self.assertRaisesRegex(WorldReplayError, "日历"):
                _restore(copy.deepcopy(self.payload))


def _annex_extension(state):
    """在已扩展的世界上再加一个小房间（连在 lumina_forum 上）：一笔合法的第二次扩展。"""
    from pns.world.extension import extended_graph, graph_fingerprint

    world = state.world_state
    template = build_default_location_graph().get("lumina_forum_lesson_room").to_dict()
    annex = dict(
        template,
        location_id="w2_test_annex",
        name="annex",
        connections=[{"to_id": "lumina_forum", "travel_minutes": 1, "mode": "walk"}],
    )
    appended = {"lumina_forum": [{"to_id": "w2_test_annex", "travel_minutes": 1, "mode": "walk"}]}
    after = extended_graph(world.locations, [annex], appended)
    return {
        "operation_id": "op-ext2",
        "envelope": {},
        "locations": [annex],
        "append_connections": appended,
        "graph_before": graph_fingerprint(world.locations),
        "graph_after": graph_fingerprint(after),
        "baseline": None,
    }


class ArchiveRestoreTests(unittest.TestCase):
    """接线：存档恢复真的走这道互验（不是只有直接调用才查）。"""

    def archive(self, payload):
        return WorldArchive.from_state_payload("yoake-mae", payload, revision=1)

    def test_a_faithful_archive_restores(self):
        state = _admitted_world()
        restored = self.archive(state.to_dict()).restore_state()
        self.assertEqual(restored.characters, state.characters)

    def test_a_tampered_archive_is_refused(self):
        payload = _admitted_world().to_dict()
        payload["world_state"]["character_activities"]["airi"]["kind"] = "composing"
        with self.assertRaisesRegex(ArchiveError, "WORLD-2"):
            self.archive(payload).restore_state()


if __name__ == "__main__":
    unittest.main()
