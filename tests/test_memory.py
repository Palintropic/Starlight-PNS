# tests/test_memory.py — P10 主观持久记忆基础的不变量。
#
# 盯住的东西按"错了会怎样"排：
#   1. 没观察到的信息永远进不了记忆（记忆只能从角色自己的观察长出来）
#   2. 每个记忆类别都有真实行为，没有空标签
#   3. 记录身份稳定、内容不可变、序列化安全
#   4. 编码 ≠ 召回：召回一个字节都不改，存下来的记忆不会因为问法变了被改写
#   5. 召回是角色作用域、确定性、有显式预算，别人的记忆一条都拿不到
#   6. 台词不整段抄进记忆
#   7. 承诺/身份永不衰减也挤不掉；短时痕迹在 TTL 边界上确定性消失
#   8. 重复编码幂等；已知的世界事实不重复记
#   9. 写入原子：失败不留半条记忆，也不留半截世界
#  10. 存档能原样恢复，拼接/篡改出来的存档响亮失败
#  11. 提示投影只含该角色召回到的内容
#  12. 记忆改不动既有的事件历史与观察日志
#  13. 记忆是 cold update：P7 的重载动不了活着的记忆
#  14. 研究会话的确定性 round robin 一点没变
#
# 运行: python -m unittest tests.test_memory -v
import ast
import json
import os
import subprocess
import sys
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from pns.models.event import Event, EventScope, EventType
from pns.models.exposure import ExposureReason
from pns.models.memory import (
    FRAGMENT_CHARS,
    MEMORY_ARCHIVE_VERSION,
    MIN_FRAGMENT,
    MemoryClass,
    MemoryError,
    MemoryMismatch,
    MemoryRecord,
    MemoryStore,
    derive_memory_id,
    derived_salience,
    eligible_classes,
    memory_content,
    memory_fragment,
    verify_memory_against_observation,
)
from pns.models import memory as memory_mod
from pns.models.observation import Observation
from pns.models.session import SessionState, SessionStateError
from pns.models.world_state import WorldState
from pns.runtime.event_commit import commit_session_event
from pns.runtime.memory import encoder as encoder_mod
from pns.runtime.memory import encoding as encoding_mod
from pns.runtime.memory import projection as projection_mod
from pns.runtime.memory import recall as recall_mod
from pns.runtime.memory.encoder import MemoryEncoder, MemoryEncoderError
from pns.runtime.memory.encoding import (
    EncodingOutcome,
    MemoryBudget,
    draft_memories,
    read_signals,
)
from pns.runtime.memory.projection import (
    prompt_block,
    prompt_projection,
    recalled_lines,
)
from pns.runtime.memory.recall import (
    MemoryRecall,
    RecallBudget,
    RecallError,
    RecallQuery,
    recall,
)
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph

CLOCK = datetime(2026, 8, 21, 23, 50)
MEMORY_DIR = Path(encoder_mod.__file__).resolve().parent
SECRET = "这件事我谁都没说过：那首曲子其实是写给她的。"


# ── 夹具 ────────────────────────────────────────────────────────────────
def _world(clock=CLOCK, *, join_nightcord=("mizuki", "ena")):
    world = WorldState(
        clock=clock,
        locations=build_default_location_graph(),
        channels=build_default_channel_registry(),
    )
    world.place_character("mizuki", "mizuki_home_room")
    world.place_character("ena", "ena_home_studio")
    for character_id in join_nightcord:
        world.join_channel(character_id, "nightcord")
    return world


def _session(world=None, session_id="s1"):
    world = _world() if world is None else world
    state = SessionState(
        session_id=session_id, scene="gate", characters=["mizuki", "ena"]
    )
    state.attach_world_state(world)
    state.initialize_runtime("开场")
    return state


def _rig(budget=None, world=None):
    state = _session(world)
    return state, MemoryEncoder(state, budget=budget)


def _message(state, text, *, actor="ena", event_id="e1"):
    world = state.world_state
    return Event(
        event_id=event_id,
        type=EventType.MESSAGE_SENT,
        occurred_at=world.clock,
        scope=EventScope.CHANNEL,
        actor_id=actor,
        participants=world.channel_participants("nightcord"),
        channel_id="nightcord",
        payload={"text": text, "char_name": actor},
    )


def _private(state, text=SECRET, *, actor="ena", event_id="secret"):
    world = state.world_state
    return Event(
        event_id=event_id,
        type=EventType.DIALOGUE_SPOKEN,
        occurred_at=world.clock,
        scope=EventScope.PRIVATE,
        actor_id=actor,
        location_id=world.location_of(actor),
        payload={"text": text, "char_name": actor},
    )


def _move(state, actor, location_id, event_id):
    world = state.world_state
    return Event(
        event_id=event_id,
        type=EventType.CHARACTER_LOCATION_CHANGED,
        occurred_at=world.clock,
        scope=EventScope.LOCATION,
        actor_id=actor,
        location_id=location_id,
    )


def _fingerprint(state):
    """会话里跟记忆有关的全部状态。回滚测试拿它做前后比对。"""
    return {
        "clock": state.world_state.clock,
        "world": state.world_state.to_dict(),
        "events": state.events.to_dict(),
        "observations": state.observations.to_dict(),
        "exposures": state.exposures.to_dict(),
        "memories": state.memories.to_dict(),
        "turns": len(state.turns),
        "histories": {cid: len(items) for cid, items in state.histories.items()},
    }


# ── AC1 没观察到的信息永远进不了记忆 ────────────────────────────────────
class UnseenInformationTests(unittest.TestCase):
    def setUp(self):
        self.state, self.encoder = _rig()

    def test_a_private_line_never_reaches_the_other_character(self):
        self.encoder.commit_and_encode(_private(self.state))
        mine = self.state.memories.for_owner("mizuki")
        self.assertEqual(mine, ())
        self.assertNotIn(
            SECRET,
            json.dumps(
                [record.to_dict() for record in mine], ensure_ascii=False
            ),
        )
        # 说话的人自己记得住 —— 缺的不是记忆管线，是曝光资格。
        self.assertTrue(self.state.memories.for_owner("ena"))

    def test_the_denied_character_recalls_and_projects_nothing_about_it(self):
        self.encoder.commit_and_encode(_private(self.state))
        result = MemoryRecall(self.state).recall_for("mizuki")
        self.assertEqual(result.memories, ())
        blob = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn(SECRET, prompt_block(result))

    def test_a_hand_built_observation_cannot_grow_a_memory(self):
        """伪造一条没人观察到的"观察"，直接拒绝。"""
        forged = Observation(
            source_event_id="ghost",
            observer_id="mizuki",
            reason=ExposureReason.CHANNEL_MEMBER,
            observed_at=self.state.world_state.clock,
            perceived={"type": "message.sent", "actor_id": "ena", "text": SECRET},
        )
        with self.assertRaises(MemoryEncoderError):
            self.encoder.encode([forged])
        self.assertEqual(len(self.state.memories), 0)

    def test_a_tampered_observation_cannot_grow_a_memory(self):
        commit_session_event(self.state, _message(self.state, "普通的一句话"))
        real = self.state.observations.for_character("mizuki")[0]
        tampered = Observation(
            source_event_id=real.source_event_id,
            observer_id=real.observer_id,
            reason=real.reason,
            observed_at=real.observed_at,
            perceived={**real.to_dict()["perceived"], "text": SECRET},
        )
        with self.assertRaises(MemoryEncoderError):
            self.encoder.encode([tampered])
        self.assertEqual(len(self.state.memories), 0)

    def test_encoding_never_reads_the_exposure_denial_log(self):
        """曝光拒绝本身就是情报：静态上这一层碰不到它。"""
        for path in MEMORY_DIR.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            attrs = {
                node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
            }
            self.assertNotIn("exposures", attrs, f"{path.name} 读到了曝光判定日志")
            names = {
                node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
            } | {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names
            }
            self.assertNotIn("ExposureLog", names, f"{path.name} 引入了曝光日志")
            self.assertNotIn("ExposureDecision", names, f"{path.name} 引入了曝光决策")

    def test_only_the_wiring_module_touches_the_omniscient_event_history(self):
        """规则、召回、投影三层碰不到全知事件历史。"""
        for name in ("encoding.py", "recall.py", "projection.py"):
            tree = ast.parse((MEMORY_DIR / name).read_text(encoding="utf-8"))
            attrs = {
                node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
            }
            self.assertNotIn("events", attrs, f"{name} 读到了全知事件历史")


# ── AC2 类别都有真实行为 ────────────────────────────────────────────────
class MemoryClassBehaviorTests(unittest.TestCase):
    def test_every_class_declares_decay_pinning_and_weight(self):
        for memory_class in MemoryClass:
            behavior = memory_class.behavior
            self.assertIsInstance(behavior.pinned, bool)
            self.assertIsInstance(behavior.recall_weight, int)
            self.assertGreater(behavior.recall_weight, 0)
            if behavior.decay_minutes is not None:
                self.assertGreater(behavior.decay_minutes, 0)

    def test_every_class_has_an_encoding_rule(self):
        self.assertEqual(
            {memory_class for memory_class, _ in memory_mod._RULES},
            set(MemoryClass),
        )

    def test_every_class_has_a_prompt_tag(self):
        self.assertEqual(set(projection_mod._TAGS), set(MemoryClass))

    def test_every_class_is_actually_produced_by_the_rules(self):
        """不是"表里有"，是**真的能长出来**：六类各给一个局面。"""
        state, encoder = _rig()
        encoder.commit_and_encode(
            _message(state, "mizuki，我答应了明天把和声写完。", event_id="e1")
        )
        state.world_state.advance_time(1)
        encoder.commit_and_encode(_move(state, "ena", "mizuki_home_room", "e2"))
        produced = {record.memory_class for record in state.memories.records()}
        self.assertEqual(produced, set(MemoryClass))


# ── AC3 记录身份与不可变 ────────────────────────────────────────────────
class RecordIdentityTests(unittest.TestCase):
    def setUp(self):
        self.state, self.encoder = _rig()
        self.encoder.commit_and_encode(_message(self.state, "今天也熬夜了。"))
        self.record = self.state.memories.for_owner("mizuki")[0]

    def test_the_identity_is_derived_not_random(self):
        self.assertEqual(
            self.record.memory_id,
            derive_memory_id("mizuki", "e1", self.record.memory_class),
        )
        self.assertEqual(self.record.source_observation_id, "mizuki@e1")
        observation = self.state.observations.find("mizuki", "e1")
        self.assertEqual(observation.observation_id, "mizuki@e1")

    def test_the_content_cannot_be_mutated_through_the_record(self):
        with self.assertRaises(TypeError):
            self.record.content["summary"] = "改掉了"
        with self.assertRaises(Exception):
            self.record.owner_id = "ena"

    def test_serialization_hands_back_a_fresh_structure(self):
        payload = self.record.to_dict()
        payload["content"]["summary"] = "改掉了"
        self.assertNotEqual(
            self.record.content["summary"], "改掉了"
        )
        self.assertEqual(
            MemoryRecord.from_dict(self.record.to_dict()), self.record
        )

    def test_a_record_encoded_before_it_was_perceived_is_refused(self):
        with self.assertRaises(MemoryError):
            MemoryRecord(
                owner_id="mizuki",
                memory_class=MemoryClass.WORKING,
                source_event_id="e1",
                observed_at=CLOCK,
                encoded_at=CLOCK - timedelta(minutes=1),
                content={"kind": "trace", "summary": "x"},
            )

    def test_a_stored_id_that_disagrees_with_the_fields_is_refused(self):
        payload = self.record.to_dict()
        payload["memory_id"] = "someone_else@e1#working"
        with self.assertRaises(MemoryError):
            MemoryRecord.from_dict(payload)

    def test_the_store_refuses_duplicates_and_backwards_time(self):
        store = MemoryStore(self.state.memories.records())
        with self.assertRaises(MemoryError):
            store._append(self.record)
        older = MemoryRecord(
            owner_id="mizuki",
            memory_class=MemoryClass.SEMANTIC,
            source_event_id="other",
            observed_at=CLOCK - timedelta(minutes=5),
            encoded_at=CLOCK - timedelta(minutes=5),
            content={"kind": "world_fact", "about": "ena", "fact": "location:ena",
                     "value": "ena_home_studio"},
        )
        with self.assertRaises(MemoryError):
            store._append(older)


# ── AC4 / AC6 编码 ≠ 召回，台词不整段抄 ─────────────────────────────────
class EncodingIsNotRecallTests(unittest.TestCase):
    def setUp(self):
        self.state, self.encoder = _rig()
        self.long_text = "啊" * 300
        self.encoder.commit_and_encode(_message(self.state, self.long_text))

    def test_recall_never_writes_no_matter_how_it_is_asked(self):
        before = self.state.memories.to_dict()
        service = MemoryRecall(self.state)
        for cues in ([], ["和声"], ["熬夜", "曲子"]):
            for about in (None, "ena"):
                service.recall_for("mizuki", cues=cues, about_id=about)
                service.recall_for("ena", cues=cues, about_id=about)
        self.assertEqual(self.state.memories.to_dict(), before)

    def test_the_exact_line_is_not_copied_into_memory(self):
        blob = json.dumps(self.state.memories.to_dict(), ensure_ascii=False)
        self.assertNotIn(self.long_text, blob)
        # 精确原文仍然留在世界历史里供审计 —— 那是另一种数据产品。
        self.assertEqual(self.state.events.get("e1").payload["text"], self.long_text)

    def test_a_short_line_is_not_copied_either(self):
        """短台词同样不整段进记忆 —— 上限之内不等于可以照抄。"""
        for text in ("熬夜写歌。", "今天的天气不错。", "我在。", "好啊，明天见。"):
            with self.subTest(text):
                state, encoder = _rig()
                encoder.commit_and_encode(_message(state, text))
                self.assertTrue(state.memories.records())
                blob = json.dumps(state.memories.to_dict(), ensure_ascii=False)
                self.assertNotIn(text, blob)
                block = prompt_block(MemoryRecall(state).recall_for("mizuki"))
                self.assertNotIn(text, block)

    def test_the_fragment_is_bounded_by_length_and_by_ratio(self):
        self.assertEqual(memory_fragment("  一   二  "), "")  # 太短，一个字都不留
        for text in ("啊" * 300, "啊" * 30, "啊" * 8, "啊" * 7, "啊"):
            with self.subTest(len(text)):
                fragment = memory_fragment(text)
                if not fragment:
                    continue
                body = fragment[:-1]  # 去掉截断标记
                self.assertLessEqual(len(body), FRAGMENT_CHARS)
                self.assertLessEqual(len(body), len(text) // 2)
                self.assertGreaterEqual(len(body), MIN_FRAGMENT)
                self.assertNotIn(text, fragment)


# ── AC5 召回是角色作用域、确定性、有预算 ────────────────────────────────
class RecallTests(unittest.TestCase):
    def setUp(self):
        self.state, self.encoder = _rig()
        self.encoder.commit_and_encode(
            _message(self.state, "mizuki，我答应了明天把和声写完。", event_id="e1")
        )
        self.state.world_state.advance_time(5)
        self.encoder.commit_and_encode(
            _message(self.state, "今天的天气不错。", event_id="e2")
        )
        self.service = MemoryRecall(self.state)

    def test_the_same_query_gives_the_same_answer_every_time(self):
        first = self.service.recall_for("mizuki", cues=["和声"])
        second = self.service.recall_for("mizuki", cues=["和声"])
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_the_answer_survives_an_archive_round_trip(self):
        before = self.service.recall_for("mizuki", cues=["和声"]).to_dict()
        restored = SessionState.from_dict(deepcopy(self.state.to_dict()))
        after = MemoryRecall(restored).recall_for("mizuki", cues=["和声"]).to_dict()
        self.assertEqual(after, before)

    def test_the_query_changes_what_comes_to_mind(self):
        """同一批记忆，换个线索就想起不同的东西 —— 这正是记忆与召回分开的意义。

        两条地位完全相同的旁听记忆（同一时刻、同一类别、同样的显著度），
        谁先被想起来只由线索决定。
        """
        state, encoder = _rig()
        encoder.commit_and_encode(
            _message(state, "今天的天气真好，晒得人想睡觉。", event_id="w1")
        )
        encoder.commit_and_encode(
            _message(state, "和声部分再改一版，明天给你听。", event_id="w2")
        )
        service = MemoryRecall(state)
        # 两条线索都落在各自记忆保留下来的那段片段里 —— 否则这条测试会靠
        # ID 兜底"通过"，而不是靠线索。
        first = service.recall_for("mizuki", cues=["和声"])
        second = service.recall_for("mizuki", cues=["天气"])
        self.assertGreater(first.memories[0].score, first.memories[1].score)
        self.assertGreater(second.memories[0].score, second.memories[1].score)
        self.assertEqual(first.memories[0].record.source_event_id, "w2")
        self.assertEqual(second.memories[0].record.source_event_id, "w1")
        # 想起的顺序变了，存下来的东西一个字节都没变。
        self.assertEqual(
            [s.record.memory_id for s in first.memories],
            list(reversed([s.record.memory_id for s in second.memories])),
        )

    def test_another_characters_memory_cannot_be_recalled(self):
        foreign = self.state.memories.for_owner("ena")
        self.assertTrue(foreign)
        query = RecallQuery(owner_id="mizuki", now=self.state.world_state.clock)
        with self.assertRaises(RecallError):
            recall(foreign, query)
        mine = self.service.recall_for("mizuki")
        self.assertEqual(
            {s.record.owner_id for s in mine.memories}, {"mizuki"}
        )

    def test_the_budget_bounds_the_answer_and_flags_truncation(self):
        result = self.service.recall_for(
            "mizuki", budget=RecallBudget(max_items=1, max_per_class=1, max_pinned=1)
        )
        self.assertEqual(len(result), 1)
        self.assertTrue(result.truncated)
        self.assertGreater(result.considered, 1)

    def test_the_ordering_is_a_total_order_with_no_ties(self):
        result = self.service.recall_for("mizuki", budget=RecallBudget(max_items=99))
        keys = [
            (-s.score, s.record.encoded_at, s.record.memory_id) for s in result.memories
        ]
        self.assertEqual(len(set(keys)), len(keys))
        scores = [s.score for s in result.memories]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_a_class_filter_is_honoured(self):
        result = self.service.recall_for(
            "mizuki", classes=(MemoryClass.COMMITMENT,)
        )
        self.assertEqual(
            {s.record.memory_class for s in result.memories},
            {MemoryClass.COMMITMENT},
        )

    def test_a_memory_from_the_future_is_never_returned(self):
        query = RecallQuery(owner_id="mizuki", now=CLOCK - timedelta(minutes=1))
        result = recall(self.state.memories.for_owner("mizuki"), query)
        self.assertEqual(result.memories, ())


# ── AC7 持久化与衰减 ────────────────────────────────────────────────────
class PersistenceAndDecayTests(unittest.TestCase):
    def setUp(self):
        self.state, self.encoder = _rig()
        self.encoder.commit_and_encode(
            _message(self.state, "mizuki，我答应了明天把和声写完。")
        )
        self.records = {
            record.memory_class: record
            for record in self.state.memories.for_owner("mizuki")
        }

    def _recall_at(self, minutes, **kwargs):
        return recall(
            self.state.memories.for_owner("mizuki"),
            RecallQuery(owner_id="mizuki", now=CLOCK + timedelta(minutes=minutes)),
            **kwargs,
        )

    def test_the_short_term_trace_disappears_exactly_at_the_boundary(self):
        ttl = MemoryClass.WORKING.decay_minutes
        classes_at = lambda m: {s.record.memory_class for s in self._recall_at(m).memories}
        self.assertIn(MemoryClass.WORKING, classes_at(ttl))
        self.assertNotIn(MemoryClass.WORKING, classes_at(ttl + 1))

    def test_decay_does_not_rewrite_the_stored_record(self):
        before = self.state.memories.to_dict()
        self._recall_at(MemoryClass.WORKING.decay_minutes + 999)
        self.assertEqual(self.state.memories.to_dict(), before)
        self.assertTrue(self.state.memories.has(self.records[MemoryClass.WORKING].memory_id))

    def test_commitments_and_identity_never_decay(self):
        result = self._recall_at(60 * 24 * 30)
        classes = {s.record.memory_class for s in result.memories}
        self.assertIn(MemoryClass.COMMITMENT, classes)
        self.assertIn(MemoryClass.IDENTITY, classes)

    def test_a_tight_budget_cannot_squeeze_out_a_commitment(self):
        result = self._recall_at(
            1, budget=RecallBudget(max_items=1, max_per_class=1, max_pinned=1)
        )
        self.assertEqual(
            [s.record.memory_class for s in result.memories],
            [MemoryClass.COMMITMENT],
        )

    def test_pinned_and_decaying_are_exactly_the_declared_sets(self):
        self.assertEqual(
            {c for c in MemoryClass if c.pinned},
            {MemoryClass.COMMITMENT, MemoryClass.IDENTITY},
        )
        self.assertEqual(
            {c for c in MemoryClass if c.decay_minutes is not None},
            {MemoryClass.WORKING},
        )


# ── AC8 幂等 ────────────────────────────────────────────────────────────
class IdempotentEncodingTests(unittest.TestCase):
    def setUp(self):
        self.state, self.encoder = _rig()

    def test_encoding_the_same_observation_twice_changes_nothing(self):
        self.encoder.commit_and_encode(_message(self.state, "熬夜写歌。"))
        before = self.state.memories.to_dict()
        again = self.encoder.encode_event("e1")
        self.assertTrue(again)
        self.assertEqual(
            {decision.outcome for decision in again},
            {EncodingOutcome.SKIPPED_DUPLICATE},
        )
        self.assertEqual(self.state.memories.to_dict(), before)

    def test_a_retry_after_a_failed_transaction_encodes_exactly_once(self):
        event = _message(self.state, "熬夜写歌。")
        with patch.object(
            encoder_mod, "draft_memories", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.encoder.commit_and_encode(event)
        self.assertEqual(len(self.state.memories), 0)
        self.assertEqual(len(self.state.events), 0)
        self.encoder.commit_and_encode(event)
        counted = len(self.state.memories)
        self.encoder.encode_event("e1")
        self.assertEqual(len(self.state.memories), counted)

    def _semantic(self, state, owner):
        return [
            r for r in state.memories.for_owner(owner)
            if r.memory_class is MemoryClass.SEMANTIC
        ]

    def test_a_world_fact_already_known_is_not_stored_twice(self):
        """已经知道的事实不重复记。

        ena 走进 mizuki 的房间（mizuki 记下"ena 在这儿"），中途走开时 mizuki
        看不见（目的地不在它这儿），再回来时对 mizuki 来说事实没变 —— 它本来
        就以为 ena 在这儿。于是不产生第二条。
        """
        state, encoder = _rig()
        encoder.commit_and_encode(_move(state, "ena", "mizuki_home_room", "m1"))
        self.assertEqual(len(self._semantic(state, "mizuki")), 1)

        state.world_state.advance_time(1)
        encoder.commit_and_encode(_move(state, "ena", "ena_home_studio", "m2"))
        self.assertEqual(state.observations.find("mizuki", "m2"), None)

        state.world_state.advance_time(1)
        _, decisions = encoder.commit_and_encode(
            _move(state, "ena", "mizuki_home_room", "m3")
        )
        outcomes = {(d.owner_id, d.memory_class): d.outcome for d in decisions}
        self.assertEqual(
            outcomes.get(("mizuki", MemoryClass.SEMANTIC)),
            EncodingOutcome.SKIPPED_KNOWN_FACT,
        )
        self.assertEqual(len(self._semantic(state, "mizuki")), 1)

    def test_a_world_fact_whose_value_changed_is_a_new_memory(self):
        """取值变了就是新事实：自己换了地方，自己一定看得见。"""
        state, encoder = _rig()
        encoder.commit_and_encode(_move(state, "mizuki", "city_streets", "m1"))
        state.world_state.advance_time(1)
        encoder.commit_and_encode(_move(state, "mizuki", "mizuki_home_room", "m2"))
        values = [r.content["value"] for r in self._semantic(state, "mizuki")]
        self.assertEqual(values, ["city_streets", "mizuki_home_room"])


# ── AC 编码资格与显式不编码 ─────────────────────────────────────────────
class EligibilityTests(unittest.TestCase):
    def test_a_clock_tick_leaves_no_memory_at_all(self):
        """时钟前进是系统心跳：曝光那一层就没给它任何观察，记忆自然无从长起。"""
        state, encoder = _rig()
        event = Event(
            event_id="tick",
            type=EventType.WORLD_TIME_ADVANCED,
            occurred_at=state.world_state.clock,
            scope=EventScope.PUBLIC,
            payload={"minutes": 10},
        )
        _, decisions = encoder.commit_and_encode(event)
        self.assertEqual(state.observations.for_event("tick"), ())
        self.assertEqual(decisions, ())
        self.assertEqual(len(state.memories), 0)

    def test_an_unregistered_observation_type_is_explicitly_not_encoded(self):
        """白名单之外的观察类型什么都不记，而且留下带理由的显式决策。

        这条走内部接口塞了一条观察进日志：现有事件类型全都在白名单里，而
        "新类型默认什么都不记"这条保证必须现在就可测，不能等到有人加了新类型
        才发现它其实会顺手记一条。
        """
        state, encoder = _rig()
        commit_session_event(state, _message(state, "普通一句。"))
        exotic = Observation(
            source_event_id="weather",
            observer_id="mizuki",
            reason=ExposureReason.SAME_LOCATION,
            observed_at=state.world_state.clock,
            perceived={"type": "weather.changed", "actor_id": None},
        )
        self.assertEqual(draft_memories(exotic), ())
        state.observations._append(exotic)
        decisions = encoder.encode([exotic])
        self.assertEqual(len(decisions), 1)
        self.assertIs(decisions[0].outcome, EncodingOutcome.SKIPPED_NOT_ELIGIBLE)
        self.assertEqual(decisions[0].detail["reason"], "no_rule_matched")
        self.assertEqual(len(state.memories), 0)

    def test_an_overheard_line_only_leaves_a_short_term_trace(self):
        state, encoder = _rig()
        encoder.commit_and_encode(_message(state, "随便说说而已。"))
        self.assertEqual(
            {r.memory_class for r in state.memories.for_owner("mizuki")},
            {MemoryClass.WORKING},
        )

    def test_being_addressed_by_name_promotes_the_memory(self):
        state, encoder = _rig()
        encoder.commit_and_encode(_message(state, "mizuki，这段你怎么看？"))
        classes = {r.memory_class for r in state.memories.for_owner("mizuki")}
        self.assertIn(MemoryClass.RELATIONAL, classes)
        self.assertIn(MemoryClass.EPISODIC, classes)

    def test_every_stored_field_can_be_re_derived_from_the_observation(self):
        """资格、内容、显著度三样都只依赖观察本身 —— 所以恢复时能重算。

        任何一样依赖了只有编码那一刻才知道的输入（比如一张外部别名表），
        它就变成"存档说了算"，伪造也就无从判起。
        """
        state, encoder = _rig()
        encoder.commit_and_encode(_message(state, "mizuki，这段你怎么看？"))
        for record in state.memories.records():
            observation = state.observations.find(
                record.owner_id, record.source_event_id
            )
            self.assertIn(record.memory_class, eligible_classes(observation))
            self.assertEqual(record.salience, derived_salience(observation))
            self.assertEqual(
                json.loads(json.dumps(record.to_dict()["content"])),
                memory_content(record.memory_class, observation),
            )

    def test_the_encoder_takes_no_input_that_restore_cannot_see(self):
        """静态可证：编码器构造签名里没有会影响资格判断的外部输入。"""
        import inspect

        parameters = set(inspect.signature(MemoryEncoder.__init__).parameters)
        self.assertEqual(parameters, {"self", "state", "budget", "name"})

    def test_the_per_observation_cap_drops_the_least_durable_first(self):
        state, encoder = _rig(budget=MemoryBudget(max_records_per_observation=1))
        _, decisions = encoder.commit_and_encode(
            _message(state, "mizuki，我答应了明天把和声写完。")
        )
        kept = {r.memory_class for r in state.memories.for_owner("mizuki")}
        self.assertEqual(kept, {MemoryClass.COMMITMENT})
        dropped = {
            d.memory_class
            for d in decisions
            if d.owner_id == "mizuki" and d.outcome is EncodingOutcome.SKIPPED_BUDGET
        }
        self.assertIn(MemoryClass.WORKING, dropped)

    def test_the_session_ceiling_is_derived_from_the_store(self):
        state, encoder = _rig(budget=MemoryBudget(max_records_per_session=1))
        _, decisions = encoder.commit_and_encode(_message(state, "一句话。"))
        self.assertEqual(len(state.memories), 1)
        self.assertIn(
            EncodingOutcome.SKIPPED_BUDGET, {d.outcome for d in decisions}
        )
        # 存档往返之后上限不会被重新发一遍额度。
        restored = SessionState.from_dict(deepcopy(state.to_dict()))
        restored_encoder = MemoryEncoder(
            restored, budget=MemoryBudget(max_records_per_session=1)
        )
        restored.world_state.advance_time(1)
        _, more = restored_encoder.commit_and_encode(
            _message(restored, "又一句。", event_id="e2")
        )
        self.assertEqual(len(restored.memories), 1)
        self.assertEqual(
            {d.outcome for d in more}, {EncodingOutcome.SKIPPED_BUDGET}
        )

    def test_signals_are_read_only_from_the_observation(self):
        state, encoder = _rig()
        commit_session_event(state, _message(state, "普通一句。"))
        observation = state.observations.find("mizuki", "e1")
        signals = read_signals(observation)
        self.assertFalse(signals.is_self)
        self.assertTrue(signals.is_utterance)
        self.assertTrue(signals.encodable)
        self.assertEqual(len(draft_memories(observation)), 1)


# ── AC9 原子性 ──────────────────────────────────────────────────────────
class AtomicityTests(unittest.TestCase):
    def setUp(self):
        self.state, self.encoder = _rig()

    def test_a_failure_during_encoding_rolls_the_event_back_too(self):
        before = _fingerprint(self.state)
        with patch.object(
            encoder_mod, "draft_memories", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.encoder.commit_and_encode(_message(self.state, "熬夜写歌。"))
        self.assertEqual(_fingerprint(self.state), before)

    def test_a_failure_while_storing_leaves_no_half_memory(self):
        commit_session_event(self.state, _message(self.state, "熬夜写歌。"))
        before = _fingerprint(self.state)
        calls = {"n": 0}

        def explode(records):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("store failed")
            SessionState.record_memories(self.state, records)

        with patch.object(self.state, "record_memories", explode):
            with self.assertRaises(RuntimeError):
                self.encoder.encode_event("e1")
        self.assertEqual(_fingerprint(self.state), before)
        self.assertEqual(len(self.state.memories), 0)

    def test_nothing_leaks_into_the_prompt_projection_after_a_rollback(self):
        with patch.object(
            encoder_mod, "draft_memories", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.encoder.commit_and_encode(_private(self.state))
        block = prompt_block(MemoryRecall(self.state).recall_for("ena"))
        self.assertEqual(block, "")

    def test_a_second_encoder_cannot_be_attached(self):
        with self.assertRaises(MemoryEncoderError):
            MemoryEncoder(self.state)


# ── AC10 / AC12 存档与权威边界 ──────────────────────────────────────────
class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.state, self.encoder = _rig()
        self.encoder.commit_and_encode(
            _message(self.state, "mizuki，我答应了明天把和声写完。")
        )
        self.archive = self.state.to_dict()

    def test_the_archive_round_trips_through_the_production_path(self):
        restored = SessionState.from_dict(deepcopy(self.archive))
        self.assertEqual(restored.to_dict(), self.archive)
        self.assertEqual(len(restored.memories), len(self.state.memories))

    def test_the_archive_shape_has_exactly_one_definition(self):
        self.assertEqual(self.archive["memory"], self.state.memory_archive())
        self.assertEqual(self.archive["memory"]["version"], MEMORY_ARCHIVE_VERSION)

    def test_an_archive_without_the_memory_section_is_refused_with_a_way_out(self):
        """P10 之前的存档：明确拒绝，而不是静默恢复成一个失忆的角色。

        兼容策略是显式的 —— 错误信息里直接给出要补的那一段形状，升级由人做
        一次决定，不由恢复路径替他决定。
        """
        broken = deepcopy(self.archive)
        broken.pop("memory")
        with self.assertRaises(SessionStateError) as caught:
            SessionState.from_dict(broken)
        message = str(caught.exception)
        self.assertIn("memory", message)
        self.assertIn(str(MEMORY_ARCHIVE_VERSION), message)
        # 按说明补上空的那一段，就能恢复（只是什么都没记住）。
        broken["memory"] = {
            "session_id": broken["session_id"],
            "version": MEMORY_ARCHIVE_VERSION,
            "clock": broken["world_state"]["clock"],
            "store": {"records": []},
        }
        restored = SessionState.from_dict(broken)
        self.assertEqual(len(restored.memories), 0)

    def test_an_archive_that_lost_only_the_store_is_rejected(self):
        broken = deepcopy(self.archive)
        broken["memory"].pop("store")
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(broken)

    def test_corrupt_archives_are_rejected_one_by_one(self):
        def unknown_version(archive):
            archive["memory"]["version"] = 99

        def another_session(archive):
            archive["memory"]["session_id"] = "someone_else"

        def another_moment(archive):
            archive["memory"]["clock"] = "2026-08-22T05:00:00"

        def encoded_in_the_future(archive):
            archive["memory"]["store"]["records"][0]["encoded_at"] = (
                "2026-08-23T00:00:00"
            )

        def encoded_before_it_was_perceived(archive):
            archive["memory"]["store"]["records"][0]["observed_at"] = (
                "2026-08-22T00:00:00"
            )

        def out_of_order(archive):
            records = archive["memory"]["store"]["records"]
            records[0]["encoded_at"] = "2026-08-21T23:40:00"
            records[0]["observed_at"] = "2026-08-21T23:40:00"

        def unknown_observation(archive):
            record = archive["memory"]["store"]["records"][0]
            record["source_event_id"] = "ghost"
            record["memory_id"] = derive_memory_id(
                record["owner_id"], "ghost", record["memory_class"]
            )
            record["source_observation_id"] = f"{record['owner_id']}@ghost"

        def someone_elses_observation(archive):
            record = archive["memory"]["store"]["records"][0]
            record["owner_id"] = "kanade"
            record["memory_id"] = derive_memory_id(
                "kanade", record["source_event_id"], record["memory_class"]
            )
            record["source_observation_id"] = f"kanade@{record['source_event_id']}"

        def rewritten_content(archive):
            archive["memory"]["store"]["records"][0]["content"]["summary"] = (
                "他答应了绝不再写那首曲子"
            )

        def forged_identity(archive):
            archive["memory"]["store"]["records"][0]["memory_id"] = "mizuki@e1#working"

        def broken_sequence(archive):
            archive["memory"]["store"]["records"][0]["sequence"] = 7

        for break_it in (
            unknown_version,
            another_session,
            another_moment,
            encoded_in_the_future,
            encoded_before_it_was_perceived,
            out_of_order,
            unknown_observation,
            someone_elses_observation,
            rewritten_content,
            forged_identity,
            broken_sequence,
        ):
            with self.subTest(break_it.__name__):
                broken = deepcopy(self.archive)
                break_it(broken)
                with self.assertRaises(SessionStateError):
                    SessionState.from_dict(broken)

    def test_content_verification_reads_the_same_declaration_as_construction(self):
        record = self.state.memories.for_owner("mizuki")[0]
        observation = self.state.observations.find("mizuki", "e1")
        verify_memory_against_observation(record, observation)
        stranger = self.state.observations.find("ena", "e1")
        with self.assertRaises(MemoryMismatch):
            verify_memory_against_observation(record, stranger)

    def test_memory_never_mutates_events_or_observations(self):
        state, encoder = _rig()
        commit_session_event(state, _message(state, "熬夜写歌。"))
        events_before = state.events.to_dict()
        observations_before = state.observations.to_dict()
        encoder.encode_event("e1")
        MemoryRecall(state).recall_for("mizuki")
        self.assertEqual(state.events.to_dict(), events_before)
        self.assertEqual(state.observations.to_dict(), observations_before)


# ── 独立审查发现的伪造路径 ──────────────────────────────────────────────
class ForgedMemoryTests(unittest.TestCase):
    """存档里把类别改掉、再按新类别重算 ID 和内容 —— 这是独立审查里真的过关过的改法。

    只核对内容是不够的：`memory_content()` 对任何一条台词观察都推得出一个
    合法的 commitment 内容，所以一句路人的闲话可以被改写成一条永不衰减、
    召回预算也挤不掉的"承诺"，而每个字段单独看都合法、ID 也对得上。
    资格必须一起重判。
    """

    def setUp(self):
        self.state, self.encoder = _rig()
        self.encoder.commit_and_encode(_message(self.state, "今天的天气不错。"))
        # 前提：这句话没点名任何人，对 mizuki 只该留下一条会过期的短时痕迹。
        self.assertEqual(
            {r.memory_class for r in self.state.memories.for_owner("mizuki")},
            {MemoryClass.WORKING},
        )
        self.archive = self.state.to_dict()

    def _promote(self, memory_class):
        """把 mizuki 那条痕迹改成另一个类别，ID 与内容全部按新类别重算。"""
        archive = deepcopy(self.archive)
        record = next(
            r
            for r in archive["memory"]["store"]["records"]
            if r["owner_id"] == "mizuki"
        )
        observation = self.state.observations.find("mizuki", record["source_event_id"])
        record["memory_class"] = memory_class.value
        record["content"] = memory_content(memory_class, observation)
        record["memory_id"] = derive_memory_id(
            "mizuki", record["source_event_id"], memory_class
        )
        return archive, record

    def test_a_plain_line_cannot_be_promoted_to_a_commitment(self):
        archive, _ = self._promote(MemoryClass.COMMITMENT)
        with self.assertRaises(SessionStateError) as caught:
            SessionState.from_dict(archive)
        self.assertIn("资格", str(caught.exception))

    def test_the_same_forgery_fails_for_every_durable_class(self):
        for memory_class in (
            MemoryClass.COMMITMENT,
            MemoryClass.IDENTITY,
            MemoryClass.RELATIONAL,
            MemoryClass.EPISODIC,
        ):
            with self.subTest(memory_class.value):
                archive, _ = self._promote(memory_class)
                with self.assertRaises(SessionStateError):
                    SessionState.from_dict(archive)

    def test_content_verification_alone_would_have_accepted_the_forgery(self):
        """证明这道闸确实是新加的那一道，而不是旧检查的另一种说法。"""
        archive, record = self._promote(MemoryClass.COMMITMENT)
        observation = self.state.observations.find("mizuki", record["source_event_id"])
        # 内容与"承诺该长什么样"完全一致，ID 也与字段自洽 —— 旧检查全过。
        self.assertEqual(
            record["content"], memory_content(MemoryClass.COMMITMENT, observation)
        )
        self.assertEqual(
            record["memory_id"],
            derive_memory_id("mizuki", record["source_event_id"], MemoryClass.COMMITMENT),
        )
        # 但这条观察根本没资格长出承诺。
        self.assertNotIn(MemoryClass.COMMITMENT, eligible_classes(observation))
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(archive)

    def _mizuki_record(self, archive):
        return next(
            r
            for r in archive["memory"]["store"]["records"]
            if r["owner_id"] == "mizuki"
        )

    def test_the_forgery_cannot_be_pushed_down_into_the_observation(self):
        """把伪造往下挪一层：改存档里那条观察的台词，让伪造的类别"有资格"。

        这样一来记忆与观察之间完全自洽 —— 只核对这一段就会放行。来源链必须
        一路核到事件为止。
        """
        archive, record = self._promote(MemoryClass.COMMITMENT)
        promised = "我答应了明天把和声写完。"
        entry = next(
            e
            for e in archive["observations"]["observations"]
            if e["observer_id"] == "mizuki"
        )
        entry["perceived"]["text"] = promised
        # 让伪造的记忆内容与被改过的那条观察也完全对得上。
        rewritten = Observation.from_dict(entry)
        record["content"] = memory_content(MemoryClass.COMMITMENT, rewritten)
        self.assertIn(MemoryClass.COMMITMENT, eligible_classes(rewritten))

        with self.assertRaises(SessionStateError) as caught:
            SessionState.from_dict(archive)
        self.assertIn("台词", str(caught.exception))

    def test_a_rewritten_actor_or_participant_list_is_rejected(self):
        for field, value in (
            ("actor_id", "kanade"),
            ("type", "dialogue.spoken"),
        ):
            with self.subTest(field):
                archive = deepcopy(self.archive)
                for entry in archive["observations"]["observations"]:
                    if entry["observer_id"] == "mizuki":
                        entry["perceived"][field] = value
                with self.assertRaises(SessionStateError):
                    SessionState.from_dict(archive)

    def test_a_forged_salience_is_rejected(self):
        """显著度也不能靠存档拔高 —— 否则一条痕迹能压过所有真正重要的记忆。"""
        archive = deepcopy(self.archive)
        self._mizuki_record(archive)["salience"] = 100
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(archive)

    def test_a_forged_perception_channel_is_rejected(self):
        """把"我旁听到的"改写成"我自己说的"也不行。"""
        archive = deepcopy(self.archive)
        self._mizuki_record(archive)["provenance"]["reason"] = "self_action"
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(archive)

    def test_a_class_that_was_genuinely_eligible_still_restores(self):
        """闸门不能宽到放行伪造，也不能紧到把真记忆判成损坏。"""
        state, encoder = _rig()
        encoder.commit_and_encode(_message(state, "mizuki，我答应了明天把和声写完。"))
        classes = {r.memory_class for r in state.memories.for_owner("mizuki")}
        self.assertIn(MemoryClass.COMMITMENT, classes)
        archive = state.to_dict()
        self.assertEqual(SessionState.from_dict(deepcopy(archive)).to_dict(), archive)


# ── AC11 提示投影 ───────────────────────────────────────────────────────
class PromptProjectionTests(unittest.TestCase):
    def setUp(self):
        self.state, self.encoder = _rig()
        self.encoder.commit_and_encode(
            _message(self.state, "mizuki，我答应了明天把和声写完。")
        )
        self.state.world_state.advance_time(2)
        self.encoder.commit_and_encode(_private(self.state, event_id="secret"))
        self.result = MemoryRecall(self.state).recall_for("mizuki")
        self.block = prompt_block(self.result)

    def test_the_projection_carries_no_system_side_bookkeeping(self):
        for forbidden in (
            "memory_id",
            "e1",
            "secret",
            "salience",
            "score",
            "provenance",
            "channel_member",
            "self_action",
            "observation",
            "encoder",
            "declared-rules",
        ):
            self.assertNotIn(forbidden, self.block, forbidden)

    def test_the_projection_carries_nothing_the_owner_did_not_perceive(self):
        self.assertNotIn(SECRET, self.block)
        self.assertNotIn(SECRET, json.dumps(prompt_projection(self.result), ensure_ascii=False))

    def test_every_line_traces_back_to_a_recalled_memory(self):
        summaries = [
            s.record.content.get("summary") or s.record.content.get("fact") or ""
            for s in self.result.memories
        ]
        for line in recalled_lines(self.result):
            self.assertTrue(
                any(summary and summary in line for summary in summaries), line
            )

    def test_the_same_line_is_not_repeated_once_per_class(self):
        lines = recalled_lines(self.result)
        self.assertEqual(len(lines), len(set(lines)))
        bodies = [line.split("：", 1)[-1] for line in lines]
        self.assertEqual(len(bodies), len(set(bodies)))

    def test_recent_observation_sources_can_be_excluded_from_prompt_recall(self):
        sources = {
            scored.record.source_event_id for scored in self.result.memories
        }
        self.assertTrue(recalled_lines(self.result))
        self.assertEqual(
            recalled_lines(self.result, exclude_source_event_ids=sources), ()
        )

    def test_the_owners_own_action_reads_as_first_person_on_one_line(self):
        """同一件事不该在自己的提示词里出现两次，一次"我"一次"我的 ID"。"""
        state, encoder = _rig()
        encoder.commit_and_encode(_move(state, "mizuki", "city_streets", "m1"))
        lines = recalled_lines(MemoryRecall(state).recall_for("mizuki"))
        self.assertTrue(lines)
        for line in lines:
            self.assertNotIn("mizuki ", line)
            self.assertIn("我", line)
        bodies = [line.split("：", 1)[-1] for line in lines]
        self.assertEqual(len(bodies), len(set(bodies)))

    def test_nothing_recalled_renders_nothing(self):
        empty = MemoryRecall(self.state).recall_for(
            "mizuki", classes=(MemoryClass.SEMANTIC,)
        )
        self.assertEqual(prompt_block(empty), "")


# ── AC13 / AC14 与既有运行时的边界 ──────────────────────────────────────
import tempfile  # noqa: E402  （下面这几组用例才需要）

from pns.runtime.content_registry import ContentRegistry  # noqa: E402
from pns.runtime.reload import ConfigBoundary, SessionSupervisor  # noqa: E402
from pns.runtime.session_runtime import SessionRuntime  # noqa: E402
import pns.runtime.session_runtime as session_runtime_mod  # noqa: E402


async def _reply(client, character, history, world, model, *args, **kwargs):
    return f"reply-from-{character}"


async def _judge(*args, **kwargs):
    return {"drift_score": 1, "is_ooc": False, "evaluator_model": "test-judge"}


class RuntimeSessionTestBase:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.history_dir = tmp / "history"
        self.drift_file = tmp / "drift.jsonl"
        self.drift_file.parent.mkdir(parents=True, exist_ok=True)
        self.drift_file.touch()
        self._env_backup = dict(os.environ)
        self._patches = [
            patch(
                "pns.runtime.session_runtime.router_mod._get_api_key",
                return_value="test-key",
            ),
            patch(
                "pns.runtime.session_runtime.router_mod.create_client",
                return_value=object(),
            ),
        ]
        for p in self._patches:
            p.start()
        self.supervisor = SessionSupervisor()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.environ.clear()
        os.environ.update(self._env_backup)
        self._tmp.cleanup()

    def _create(self, registry=None, **params):
        base = {"characters": ["mizuki", "ena"], "max_turns": 4, "api_delay": 0}
        base.update(params)
        return SessionRuntime.create(
            base,
            registry=registry,
            supervisor=self.supervisor,
            history_dir=self.history_dir,
            drift_scores_file=self.drift_file,
        )


class RoundRobinUnchangedTests(RuntimeSessionTestBase, unittest.IsolatedAsyncioTestCase):
    """确定性研究会话一点没变，而且完全不经过记忆层。"""

    async def test_a_research_session_still_alternates_deterministically(self):
        runtime = self._create()
        clock_before = runtime.world.clock
        with patch("pns.runtime.session_runtime.call_character_async", _reply), patch(
            "pns.runtime.session_runtime.judge_async", _judge
        ):
            messages = [m async for m in runtime.run()]
        turns = [m["character"] for m in messages if m["type"] == "turn"]
        self.assertEqual(turns, ["mizuki", "ena", "mizuki", "ena"])
        self.assertEqual(runtime.world.clock, clock_before)
        self.assertEqual(len(runtime.state.memories), 0)
        self.assertIsNone(runtime.state.memory_encoder)

    async def test_a_research_session_archive_carries_an_empty_memory_section(self):
        runtime = self._create(max_turns=2)
        with patch("pns.runtime.session_runtime.call_character_async", _reply), patch(
            "pns.runtime.session_runtime.judge_async", _judge
        ):
            [m async for m in runtime.run()]
        archive = runtime.state.to_dict()
        self.assertEqual(archive["memory"]["store"], {"records": []})
        restored = SessionState.from_dict(archive)
        self.assertEqual(restored.to_dict(), archive)

    async def test_memory_can_be_attached_to_a_research_session_afterwards(self):
        """记忆是**另外一条**路：要用就显式接上去，不是默认开着。"""
        runtime = self._create(max_turns=2)
        with patch("pns.runtime.session_runtime.call_character_async", _reply), patch(
            "pns.runtime.session_runtime.judge_async", _judge
        ):
            [m async for m in runtime.run()]
        turns_before = len(runtime.state.turns)
        encoder = MemoryEncoder(runtime.state)
        decisions = encoder.encode(runtime.state.observations.observations())
        self.assertTrue(decisions)
        self.assertEqual(len(runtime.state.turns), turns_before)
        runtime.close()


class SeparateRuntimePathTests(unittest.TestCase):
    """研究会话的代码路径里没有记忆层 —— 静态可证。"""

    def test_session_runtime_does_not_import_memory(self):
        tree = ast.parse(
            Path(session_runtime_mod.__file__).read_text(encoding="utf-8")
        )
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertEqual(
            [name for name in imported if "memory" in name],
            [],
            "研究会话的轮转路径不该依赖记忆层",
        )

    def test_importing_memory_does_not_initialize_the_reload_boundary(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import pns.runtime.memory; "
                "assert 'pns.runtime.reload' not in sys.modules, "
                "'导入记忆层顺带拉起了重载边界'; print('ok')",
            ],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_memory_package_holds_no_live_module_level_state(self):
        for module in (encoder_mod, encoding_mod, recall_mod, projection_mod):
            instances = [
                name
                for name, value in vars(module).items()
                if isinstance(value, (MemoryEncoder, MemoryStore, MemoryRecord))
            ]
            self.assertEqual(instances, [], module.__name__)


class ReloadCannotTouchMemoryTests(RuntimeSessionTestBase, unittest.TestCase):
    """记忆是 cold update：P7 的配置重载动不了活着的记忆。"""

    def setUp(self):
        super().setUp()
        self.boundary = ConfigBoundary(self.supervisor, stop_timeout=0.5)
        self.runtime = self._create(registry=self.boundary.active())
        self.encoder = MemoryEncoder(self.runtime.state)
        state = self.runtime.state
        self.encoder.commit_and_encode(
            _message(state, "熬夜写歌。", event_id="mem-e1")
        )
        self.before = state.memories.to_dict()

    def tearDown(self):
        self.runtime.close()
        super().tearDown()

    def test_a_successful_reload_leaves_the_memory_store_alone(self):
        self.runtime.close()  # 让重载能等到 idle
        old_registry = self.boundary.active()
        result = self.boundary.reload()
        self.assertEqual(result.status, "ok")
        self.assertIsNot(self.boundary.active(), old_registry)
        self.assertIs(self.runtime.state.memory_encoder, self.encoder)
        self.assertEqual(self.runtime.state.memories.to_dict(), self.before)

    def test_a_failed_reload_leaves_the_memory_store_alone(self):
        result = self.boundary.reload()  # 会话还活着 → 等不到 idle → 失败
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.runtime.state.memories.to_dict(), self.before)

    def test_the_registry_carries_no_memory_state(self):
        forbidden = {"memory", "memories", "recall", "encoder", "salience"}
        fields = {f.name for f in ContentRegistry.__dataclass_fields__.values()}
        self.assertEqual(fields & forbidden, set())

    def test_the_registry_exposes_no_way_to_encode_or_recall(self):
        writers = [
            name
            for name in dir(ContentRegistry)
            if name.startswith(("memor", "recall", "encode", "forget"))
        ]
        self.assertEqual(writers, [])


if __name__ == "__main__":
    unittest.main()
