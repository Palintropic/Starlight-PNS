# tests/test_mvp_generation.py — MVP-1 生产生成层与开局排期的不变量。
#
# 盯住的东西按"错了会怎样"排：
#   1. 产品路径真的走得通：真实组装（AuthoredLinePolicy + PromptedLineGenerator
#      + RouterAuditor）能把一条到期资格变成一条被接受的对话事件 —— 而且走的
#      不是 ScriptedLineGenerator，也没有任何测试专用分支。
#   2. 提示词是角色作用域的。别人的记忆、别人的位置、全知事件历史、曝光判定
#      一个字都渗不进来，而且拦住它们的是**机制**（投影问它就抛错），不是
#      "我们检查过了"。
#   3. provider 侧的异常一个字节都不过边界 —— 消息、类型名、属性全都不行。
#      这一档比别处要紧：策略失败的原文会被写进 Agency 记录，跟着存档落盘。
#   4. 判分是台词进世界历史的唯一通道。判成不接受 = 没有事件、没有观察、
#      没有记忆。
#   5. 开局排期确定性、错开、只带 cue，而且**只在创建时**播一次。
#
# 运行: python -m unittest tests.test_mvp_generation -v
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from pns.interfaces.composition import (  # noqa: E402
    AdaptersUnavailable,
    AutonomySettings,
    ContentUnavailable,
    WorldControlPlane,
)
from pns.runtime.autonomy.driver import DriverConfig  # noqa: E402
from pns.models.action import ActionId  # noqa: E402
from pns.models.activation import ActivationKind  # noqa: E402
from pns.models.agency import AgencyOutcome  # noqa: E402
from pns.models.event import EventType  # noqa: E402
from pns.models.memory import MemoryClass, MemoryRecord  # noqa: E402
from pns.runtime.autonomy.context import (  # noqa: E402
    ActivationCue,
    GenerationContext,
)
from pns.runtime.autonomy.generation import GenerationError  # noqa: E402
from pns.runtime.autonomy.outcome import ActivationOutcome  # noqa: E402
from pns.runtime.autonomy.prompt import (  # noqa: E402
    PROMPT_FAILURE,
    PROVIDER_FAILURE,
    CharacterWorldView,
    PromptScopeError,
    PromptedLineGenerator,
    render_situation,
)
from pns.runtime.autonomy.seeding import (  # noqa: E402
    ActivationCadence,
    SeedingError,
    seed_activation_id,
    seed_character_activations,
)
from pns.runtime.persistence.lifecycle import LifecycleError  # noqa: E402
from pns.runtime.reload import BOUNDARY  # noqa: E402

SCENE = "nightcord"
CHARACTERS = ["mizuki", "ena"]

# 一把只在测试里存在、形状独一无二的"凭据"。它出现在任何一条对外数据里，
# 都说明服务器侧的 API Key 从某条路径漏了出去。
CANARY = "CANARY-SECRET-6f3a9c2e-DO-NOT-LEAK"
# 只属于某一个角色的哨兵。它出现在**别人**的提示词里就是一次作用域泄漏。
SENTINEL = {
    "mizuki": "SENTINEL-MIZUKI-ONLY-4b1d",
    "ena": "SENTINEL-ENA-ONLY-9f2c",
}

ROUTER_MARK = "监督者Router"


class _Block:
    def __init__(self, text):
        self.text = text


class _Response:
    def __init__(self, text, stop_reason=None):
        self.content = [_Block(text)]
        self.stop_reason = stop_reason


class _Messages:
    def __init__(self, owner):
        self._owner = owner

    def create(self, **kwargs):
        return self._owner._create(**kwargs)


class FakeProvider:
    """一个不联网的 provider 替身。

    生成与判分走的是**同一个客户端**（真实接线也是这样），靠 system prompt
    里的 Router 标记区分。它记下每一次调用，测试直接在这些记录上搜关键词 ——
    那份记录就是"真的被发出去的提示词"，不是我们以为发出去的东西。
    """

    def __init__(
        self,
        *,
        line="今天也在这里哦",
        drift=0.0,
        needs_review=False,
        on_generate=None,
        generate_error=None,
        truncated=False,
    ):
        self.messages = _Messages(self)
        self.generations = []
        self.judgements = []
        self._line = line
        self._drift = drift
        self._needs_review = needs_review
        self._on_generate = on_generate
        self._generate_error = generate_error
        self._truncated = bool(truncated)

    # 真实客户端在 anthropic 形态下只被调用这一个方法。
    def _create(self, *, model=None, system="", messages=(), **kwargs):
        if ROUTER_MARK in system:
            self.judgements.append({"model": model, "system": system, "messages": list(messages)})
            return _Response(json.dumps(self._verdict(), ensure_ascii=False))
        call = {"model": model, "system": system, "messages": list(messages)}
        self.generations.append(call)
        if self._on_generate is not None:
            self._on_generate(call)
        if self._generate_error is not None:
            raise self._generate_error
        line = self._line(call) if callable(self._line) else self._line
        return _Response(line, stop_reason="max_tokens" if self._truncated else "end_turn")

    def _verdict(self):
        dimensions = {
            key: {"score": self._drift, "reason": "fake"}
            for key in (
                "character_facts",
                "psychological_mechanism",
                "language_structure",
                "media_authenticity",
                "task_compliance",
                "unsupported_invention",
                "timeline_boundary",
            )
        }
        return {
            "drift_score": self._drift,
            "dimensions": dimensions,
            "confidence": 0.9,
            "needs_human_review": self._needs_review,
            "drift_type": "无",
            "reason": "fake",
        }


class MvpTestCase(unittest.TestCase):
    """每个用例一个独立存档根、一个独立组装边界、一个不联网的 provider。"""

    line = "今天也在这里哦"
    drift = 0.0
    needs_review = False
    # None = 用服务器默认（环境变量）那一份。要压节律或压预算的用例覆盖它。
    autonomy = None

    def setUp(self):
        self.registry = BOUNDARY.active()
        self.assertEqual(
            self.registry.models.api_format,
            "anthropic",
            "这些用例的 provider 替身按 anthropic 形态写；换了形态要一起改",
        )
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "worlds"
        self._env = patch.dict(os.environ, {self.registry.models.key_name: CANARY})
        self._env.start()
        self.provider = self.make_provider()
        self.plane = WorldControlPlane(
            root=self.root,
            client_factory=lambda *a, **k: self.provider,
            autonomy=self.autonomy,
        )

    def make_provider(self):
        return FakeProvider(
            line=self.line, drift=self.drift, needs_review=self.needs_review
        )

    def tearDown(self):
        try:
            self.plane.drivers.stop_all("test teardown", 5.0)
            self.plane.service.release_all()
        finally:
            self._env.stop()
            self._tmp.cleanup()

    # ── 便捷 ────────────────────────────────────────────────────────────
    def create(self, world_id="nightcord", characters=None):
        self.plane.create(
            world_id=world_id,
            scene_id=SCENE,
            character_ids=list(CHARACTERS if characters is None else characters),
        )
        return self.plane.service.opened(world_id)

    def advance(self, world, minutes):
        """手动推一次时间。驱动的并发语义在 test_autonomy_driver 里单独盯。"""
        return world.runtime.advance(minutes)

    def prompt_owner(self, system: str):
        """一份 system prompt 属于哪个角色。按内容包里那份模板的开头来认。"""
        for cid in CHARACTERS:
            head = self.registry.character(cid).system_prompt[:40]
            if head in system:
                return cid
        return None


# ── AC1 产品路径真的走得通 ───────────────────────────────────────────────
class ProductionPathTests(MvpTestCase):
    def test_a_due_activation_becomes_an_accepted_dialogue_event(self):
        world = self.create()
        state = world.state
        # 开局排期：第一条在 5 分钟后。推到那一刻，瑞希该被考虑了。
        report = self.advance(world, 5)

        self.assertEqual(len(report["results"]), 1, report)
        result = report["results"][0]
        self.assertEqual(result["outcome"], ActivationOutcome.ACTED.value, result)
        self.assertEqual(result["character_id"], "mizuki")

        sent = state.events.by_type(EventType.MESSAGE_SENT)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].payload["text"], self.line)
        # 显示名来自冻结内容包，不来自模型输出。
        self.assertEqual(
            sent[0].payload["char_name"], self.registry.character_name("mizuki")
        )
        # 生成和判分都真的发生过，而且各一次。
        self.assertEqual(len(self.provider.generations), 1)
        self.assertEqual(len(self.provider.judgements), 1)

    def test_it_runs_through_the_production_composition_not_a_scripted_stand_in(self):
        world = self.create()
        engine = world.state.agency_engine
        self.assertEqual(engine.policy.name, "authored_line")
        generator = engine.policy.generator
        self.assertIsInstance(generator, PromptedLineGenerator)
        self.assertEqual(generator.name, "prompted")
        self.assertEqual(world.runtime.auditor.name, "router")

    def test_both_characters_act_on_their_own_staggered_schedule(self):
        world = self.create()
        first = self.advance(world, 5)
        second = self.advance(world, 5)
        self.assertEqual([r["character_id"] for r in first["results"]], ["mizuki"])
        self.assertEqual([r["character_id"] for r in second["results"]], ["ena"])
        # 两条都提交了，而且是两条不同的事件。
        self.assertEqual(len(world.state.events.by_type(EventType.MESSAGE_SENT)), 2)

    def test_a_line_cut_off_by_max_tokens_never_becomes_world_truth(self):
        """半句话长得跟整句一模一样，所以必须在提交之前就被拦掉。

        它跟"调用失败"不是一回事：这一档**拿到了东西**，而且那东西看起来
        完全正常。不拦的话，角色就说了一句它没说完的话，然后那句话变成世界
        真相、被别人观察到、被记进记忆。
        """
        self.provider = FakeProvider(line="我其实一直想说的是", truncated=True)
        self.plane = WorldControlPlane(
            root=self.root,
            client_factory=lambda *a, **k: self.provider,
            autonomy=self.autonomy,
        )
        world = self.create()
        self.advance(world, 5)
        self.assertEqual(len(world.state.events.by_type(EventType.MESSAGE_SENT)), 0)
        self.assertEqual(len(world.state.observations), 0)
        # 可重试：下一次采样很可能就说得完。所以这一次不留终局记录。
        for _ in range(world.runtime.retry.max_attempts):
            world.runtime.process_pending()
        agency = json.dumps(world.state.agency.to_dict(), ensure_ascii=False)
        self.assertIn("截断", agency)
        self.assertNotIn("我其实一直想说的是", agency)

    def test_the_generated_line_is_still_untrusted_input(self):
        """模型吐一整份上下文回来，不会被当成一句台词写进世界。"""
        self.provider = FakeProvider(line="x" * 5000)
        self.plane = WorldControlPlane(
            root=self.root,
            client_factory=lambda *a, **k: self.provider,
            autonomy=self.autonomy,
        )
        world = self.create()
        report = self.advance(world, 5)
        result = report["results"][0]
        self.assertNotEqual(result["outcome"], ActivationOutcome.ACTED.value)
        self.assertEqual(len(world.state.events.by_type(EventType.MESSAGE_SENT)), 0)
        # 超长输出是**不可重试**的：同一个坏模板再试一百次也一样。
        self.assertEqual(len(self.provider.generations), 1)


# ── AC2 提示词是角色作用域的 ─────────────────────────────────────────────
class PromptScopeTests(MvpTestCase):
    def plant_memories(self, state):
        """给两个角色各种一条只属于自己的记忆。

        直接种进存储、不经过事件：这样两条哨兵之间没有任何合法的共享路径，
        一条出现在另一个人的提示词里就只可能是泄漏。
        """
        now = state.world_state.clock
        for index, cid in enumerate(CHARACTERS):
            state.memories._append(
                MemoryRecord(
                    owner_id=cid,
                    memory_class=MemoryClass.EPISODIC,
                    source_event_id=f"planted:{cid}:{index}",
                    observed_at=now,
                    encoded_at=now,
                    content={
                        "kind": "planted",
                        "summary": SENTINEL[cid],
                        "self": True,
                    },
                    salience=9,
                )
            )

    def test_each_prompt_carries_its_own_identity_and_scoped_world(self):
        world = self.create()
        self.advance(world, 10)  # 两个角色各一次
        seen = {}
        for call in self.provider.generations:
            owner = self.prompt_owner(call["system"])
            self.assertIsNotNone(owner, "这份 system prompt 认不出是谁的")
            seen[owner] = call
        self.assertEqual(set(seen), set(CHARACTERS))

        situation = seen["mizuki"]["messages"][0]["content"]
        # 自己的地点在，别人的地点不在。
        self.assertIn("瑞希的房间", situation)
        self.assertNotIn("绘名的房间", situation)
        self.assertIn("Nightcord", situation)
        # 别人的角色提示词一个字都不该出现在我的这份里。
        self.assertNotIn(
            self.registry.character("ena").system_prompt[:40], seen["mizuki"]["system"]
        )

    def test_one_characters_memory_never_reaches_another_characters_prompt(self):
        world = self.create()
        self.plant_memories(world.state)
        self.advance(world, 10)

        for call in self.provider.generations:
            owner = self.prompt_owner(call["system"])
            whole = call["system"] + json.dumps(call["messages"], ensure_ascii=False)
            self.assertIn(SENTINEL[owner], whole, f"{owner} 自己的记忆没进提示词")
            for other in CHARACTERS:
                if other == owner:
                    continue
                self.assertNotIn(
                    SENTINEL[other], whole, f"{other} 的记忆漏进了 {owner} 的提示词"
                )

    def test_a_planted_memory_never_reaches_the_audit_the_result_or_the_archive(self):
        world = self.create()
        self.plant_memories(world.state)
        self.advance(world, 10)
        world.checkpoint("test")

        judged = json.dumps(self.provider.judgements, ensure_ascii=False)
        for cid in CHARACTERS:
            # 判分只看那一句话，不看任何人的记忆。
            self.assertNotIn(SENTINEL[cid], judged)
        # 存档里当然有它自己那条记忆；但**事件、观察、Agency 记录**里不该有。
        events = json.dumps(world.state.events.to_dict(), ensure_ascii=False)
        observations = json.dumps(world.state.observations.to_dict(), ensure_ascii=False)
        agency = json.dumps(world.state.agency.to_dict(), ensure_ascii=False)
        for cid in CHARACTERS:
            for blob in (events, observations, agency):
                self.assertNotIn(SENTINEL[cid], blob)

    def test_the_world_view_refuses_every_omniscient_question(self):
        view = CharacterWorldView(
            character_id="mizuki",
            clock=datetime(2026, 8, 23, 1, 0),
            location_id="mizuki_home_room",
            channel_ids=("nightcord",),
        )
        self.assertEqual(view.location_of("mizuki"), "mizuki_home_room")
        with self.assertRaises(PromptScopeError):
            view.location_of("ena")
        with self.assertRaises(PromptScopeError):
            view.channels_for("ena")
        with self.assertRaises(PromptScopeError):
            view.character_locations
        with self.assertRaises(PromptScopeError):
            view.channel_members
        with self.assertRaises(PromptScopeError):
            view.known_characters()

    def test_a_scope_violation_is_a_loud_non_retryable_failure(self):
        """把闸拆掉就该变红：一次越界读取不许被当成 provider 抖动重试。"""

        def call(character_id, view, history):
            view.location_of("ena")  # 越界

        generator = PromptedLineGenerator(call)
        context = _context("mizuki")
        with self.assertRaises(GenerationError) as caught:
            generator.generate(context)
        self.assertFalse(caught.exception.retryable)
        self.assertIn("越界", str(caught.exception))

    def test_the_situation_only_contains_fields_from_the_generation_context(self):
        context = _context(
            "mizuki",
            observations=(),
            recalled=("我答应过要交那段动画",),
            cue="该睡了吧",
        )
        situation = render_situation(context, channels=self.registry.new_channel_registry())
        self.assertIn("我答应过要交那段动画", situation)
        self.assertIn("该睡了吧", situation)
        self.assertIn("时间：", situation)
        # 排期簿记一个字都不许出现。
        for forbidden in ("due_id", "activation_id", "sequence", "missed", "next_due"):
            self.assertNotIn(forbidden, situation)


# ── AC3/AC8 provider 侧的东西一个字节都不过边界 ─────────────────────────
class ProviderLeakTests(MvpTestCase):
    def test_a_provider_error_carrying_the_key_leaks_nothing_anywhere(self):
        """异常的消息、类型名和属性里都埋着那把 key，一处都不许漏出去。

        这一档比别处要紧：策略失败的原文会被 Agency 引擎写进 detail，
        跟着存档一起落盘，再从状态接口交出去。
        """
        hostile = type(CANARY, (RuntimeError,), {})(f"provider rejected {CANARY}")
        hostile.api_key = CANARY
        self.provider = FakeProvider(generate_error=hostile)
        self.plane = WorldControlPlane(
            root=self.root,
            client_factory=lambda *a, **k: self.provider,
            autonomy=self.autonomy,
        )
        world = self.create()
        # 生成失败是可重试的，所以要把重试预算跑完，才会留下那条耐久的终局
        # 记录 —— 泄漏要查的正是那条记录。
        self.advance(world, 5)
        for _ in range(world.runtime.retry.max_attempts):
            world.runtime.process_pending()
        world.checkpoint("test")

        blobs = {
            "agency": json.dumps(world.state.agency.to_dict(), ensure_ascii=False),
            "events": json.dumps(world.state.events.to_dict(), ensure_ascii=False),
            "status": json.dumps(self.plane.status("nightcord"), ensure_ascii=False),
            "archive": (self.root / "nightcord" / "world.json").read_text(
                encoding="utf-8"
            ),
            "outcomes": json.dumps(
                world.runtime.recent_outcomes(), ensure_ascii=False
            ),
        }
        for where, blob in blobs.items():
            self.assertNotIn(CANARY, blob, f"{where} 里出现了那把 key")
        # 留下的是那句固定的话，而且它确实被记下来了 —— 不是被吞掉。
        self.assertIn(PROVIDER_FAILURE, blobs["agency"])

    def test_the_provider_failure_message_is_a_constant(self):
        """它不是模板：换一个异常，那句话逐字不变。"""
        messages = set()
        for error in (
            RuntimeError(CANARY),
            type(CANARY, (ValueError,), {})("boom"),
            TimeoutError("timed out"),
        ):
            generator = PromptedLineGenerator(
                lambda character_id, view, history, e=error: (_ for _ in ()).throw(e)
            )
            with self.assertRaises(GenerationError) as caught:
                generator.generate(_context("mizuki"))
            self.assertTrue(caught.exception.retryable)
            self.assertIs(caught.exception.__cause__, error)
            messages.add(str(caught.exception))
        self.assertEqual(messages, {PROVIDER_FAILURE})

    def test_a_broken_prompt_template_fails_without_quoting_the_template(self):
        def call(character_id, view, history):  # pragma: no cover - 不该走到
            raise AssertionError("模板都渲染不出来，不该真的去调模型")

        generator = PromptedLineGenerator(call)
        broken = _context("mizuki")
        with patch(
            "pns.runtime.autonomy.prompt.render_situation",
            side_effect=KeyError(f"{CANARY} 模板片段"),
        ):
            with self.assertRaises(GenerationError) as caught:
                generator.generate(broken)
        self.assertEqual(str(caught.exception), PROMPT_FAILURE)
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn(CANARY, str(caught.exception))

    def test_missing_generation_config_fails_before_taking_ownership(self):
        """配置不全就在这里响亮失败，绝不退回一个永远不说话的世界。"""
        with patch.dict(os.environ, {self.registry.models.key_name: ""}):
            with self.assertRaises(AdaptersUnavailable):
                self.plane.create(
                    world_id="nokey", scene_id=SCENE, character_ids=CHARACTERS
                )
        self.assertFalse((self.root / "nokey").exists())
        self.assertIsNone(self.plane.service.opened("nokey"))

    def test_a_character_without_a_prompt_cannot_enter_a_speaking_world(self):
        with self.assertRaises(ContentUnavailable):
            self.plane.create(
                world_id="notready",
                scene_id=SCENE,
                character_ids=["mizuki", "akito"],
            )
        self.assertFalse((self.root / "notready").exists())


# ── AC3 判分是唯一通道 ───────────────────────────────────────────────────
class AuditGateTests(MvpTestCase):
    drift = 9.0

    def test_a_rejected_line_produces_no_event_no_observation_no_memory(self):
        world = self.create()
        state = world.state
        report = self.advance(world, 5)
        result = report["results"][0]

        self.assertEqual(result["outcome"], ActivationOutcome.REJECTED.value, result)
        self.assertEqual(len(state.events.by_type(EventType.MESSAGE_SENT)), 0)
        self.assertEqual(len(state.observations), 0)
        self.assertEqual(len(state.memories), 0)
        # 判分真的发生过，而且结论被耐久地记了下来。
        self.assertEqual(len(self.provider.judgements), 1)
        record = state.agency.get(result["due_id"])
        self.assertIsNot(record.outcome, AgencyOutcome.ACTED)

    def test_the_rejected_line_text_is_not_in_the_world_history(self):
        world = self.create()
        self.advance(world, 5)
        events = json.dumps(world.state.events.to_dict(), ensure_ascii=False)
        self.assertNotIn(self.line, events)


class AuditUnavailableTests(MvpTestCase):
    def test_a_broken_judge_never_degrades_into_acceptance(self):
        """判分器打不通 —— 那句台词就是进不去，绝不"那就当它通过吧"。"""
        calls = {"n": 0}

        def explode(**kwargs):
            if ROUTER_MARK in kwargs.get("system", ""):
                calls["n"] += 1
                raise RuntimeError(f"router down {CANARY}")
            return _Response(self.line)

        with patch.object(self.provider, "_create", side_effect=explode):
            world = self.create()
            self.advance(world, 5)
        self.assertGreater(calls["n"], 0)
        self.assertEqual(len(world.state.events.by_type(EventType.MESSAGE_SENT)), 0)
        record = world.state.agency.records()[0]
        self.assertIsNot(record.outcome, AgencyOutcome.ACTED)
        # 拦住它的是那道机制：pns.logic.router.judge 调不通时会返回一份
        # "0 分 + 七维不全 + 待人工复核"的兜底结果，而 RouterAuditor 把
        # 「不全」当成不知道、把不知道当成不接受。
        self.assertTrue(record.detail["needs_human_review"])
        self.assertFalse(record.detail["audit"]["dimensions_complete"])
        self.assertFalse(record.detail["audit"]["accepted"])
        # 而且判分器那侧的异常原文一个字节都没进这条耐久记录。
        agency = json.dumps(world.state.agency.to_dict(), ensure_ascii=False)
        self.assertNotIn(CANARY, agency)
        self.assertNotIn("router down", agency)


# ── AC6 开局排期 ─────────────────────────────────────────────────────────
class SeedingTests(MvpTestCase):
    def test_a_new_world_is_seeded_once_deterministically_and_staggered(self):
        world = self.create()
        pending = world.state.activations.pending()
        self.assertEqual(len(pending), len(CHARACTERS))
        cadence = self.plane.autonomy.cadence
        clock = world.state.world_state.clock
        by_id = {a.activation_id: a for a in pending}
        for index, cid in enumerate(CHARACTERS):
            activation = by_id[seed_activation_id(cid)]
            self.assertEqual(activation.character_id, cid)
            self.assertIs(activation.kind, ActivationKind.CHARACTER_ACTIVATION)
            self.assertEqual(activation.interval_minutes, cadence.interval_minutes)
            self.assertEqual(
                activation.due_at,
                clock + timedelta(minutes=cadence.first_due_offset(index)),
            )
        # 错开是真的错开：没有两个角色在同一分钟被叫醒。
        self.assertEqual(len({a.due_at for a in pending}), len(pending))

    def test_the_seeded_payload_carries_no_bookkeeping(self):
        world = self.create()
        for activation in world.state.activations.pending():
            self.assertEqual(dict(activation.payload), {})

    def test_a_cue_is_the_only_thing_a_character_can_see(self):
        cadence = ActivationCadence(cue="想想今天要做什么")
        world = self.create()
        state = world.state
        # 直接对着调度器播一次（换一个世界，免得撞 ID）。
        state.activations._remove(seed_activation_id("mizuki"))
        state.activations._remove(seed_activation_id("ena"))
        seed_character_activations(state.scheduler, CHARACTERS, cadence)
        for activation in state.activations.pending():
            self.assertEqual(dict(activation.payload), {"cue": "想想今天要做什么"})

    def test_seeding_the_same_world_twice_fails_loudly(self):
        world = self.create()
        with self.assertRaises(SeedingError):
            seed_character_activations(
                world.state.scheduler, CHARACTERS, self.plane.autonomy.cadence
            )

    def test_restore_refuses_adapters_that_carry_a_seed(self):
        """恢复路径带播种器是一道真闸，不是一句约定。"""
        world = self.create()
        self.plane.close("nightcord")
        adapters = self.plane.build_adapters(self.registry, seed=lambda state: None)
        with self.assertRaises(LifecycleError):
            self.plane.service.restore("nightcord", adapters=adapters)
        # 一次被拒的恢复不许留下锁。
        self.assertIsNone(self.plane.service.opened("nightcord"))
        self.assertEqual(self.plane.restore("nightcord")["world_id"], "nightcord")

    def test_restore_keeps_exactly_the_archived_queue(self):
        world = self.create()
        self.advance(world, 5)  # 瑞希跑掉一次，它的下一次被重排
        before = {
            a.activation_id: a.due_at for a in world.state.activations.pending()
        }
        self.plane.close("nightcord")
        self.plane.restore("nightcord")
        restored = self.plane.service.opened("nightcord")
        after = {
            a.activation_id: a.due_at for a in restored.state.activations.pending()
        }
        self.assertEqual(after, before)
        self.assertEqual(len(after), len(CHARACTERS))

    def test_a_cadence_with_nonsense_bounds_is_refused(self):
        for bad in (
            {"interval_minutes": 0},
            {"interval_minutes": True},
            {"first_delay_minutes": -1},
            {"stagger_minutes": 0},
            {"interval_minutes": 10**9},
            {"cue": "x" * 500},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(SeedingError):
                    ActivationCadence(**bad)


# ── 花费边界：一次 Start 的额度 vs 世界一生的动作上限 ────────────────────
class WorldLifetimeBudgetTests(MvpTestCase):
    """P9 的 128 是给**研究会话**定的，不是给一个世界的一辈子定的。

    按默认双角色节拍，一个持久世界一个半小时就会撞上它，然后每一条激活都被
    静静判成 rejected_budget —— 而且恢复存档也救不回来，因为计数就在存档里。
    那不是安全网，是定时哑火。这一组盯的就是它不再存在。
    """

    # 把节律压密，好在一个用例里真的跑过 128 那条旧边界。
    autonomy = AutonomySettings(
        driver=DriverConfig(interval_seconds=0.01, stop_timeout_seconds=1.0),
        cadence=ActivationCadence(
            interval_minutes=1, first_delay_minutes=1, stagger_minutes=1
        ),
    )

    def push_until(self, world, done, ticks=200, what="条件"):
        """推时间直到条件成立，**推的次数有上限**。

        上限是刻意的：这一组用例的回归形态就是"世界不再说话了"，而一个用
        `while` 死等的测试碰到它会挂住而不是变红。挂住的测试等于没有测试。
        """
        for _ in range(ticks):
            if done():
                return
            self.advance(world, 5)
        self.fail(f"推了 {ticks} 轮还等不到{what} —— 这个世界失声了")

    def test_a_world_keeps_speaking_past_the_old_session_cap(self):
        world = self.create()
        state = world.state
        # 一路推到远远越过旧的 128 边界。
        self.push_until(
            world,
            lambda: state.agency.committed_actions() > 140,
            what="累计提交超过旧的 128 边界",
        )

        committed = state.agency.committed_actions()
        self.assertGreater(committed, 128)
        self.assertEqual(len(state.events.by_type(EventType.MESSAGE_SENT)), committed)
        # 一条 rejected_budget 都不该有：旧默认下第 129 条起全是它。
        self.assertEqual(state.agency.for_outcome(AgencyOutcome.REJECTED_BUDGET), ())

        # 而且它此刻仍然说得出下一句 —— 这才是"没失声"。
        before = committed
        self.push_until(
            world,
            lambda: state.agency.committed_actions() > before,
            ticks=20,
            what="越过边界之后的下一句台词",
        )

    def test_the_lifetime_cap_still_exists_and_survives_a_restore(self):
        """上限没有被删掉，只是换成了世界尺度的数字 —— 而且偷不走。"""
        engine = self.create().state.agency_engine
        self.assertEqual(
            engine.budget.max_committed_actions_per_session,
            self.plane.autonomy.world_action_cap,
        )
        world = self.plane.service.opened("nightcord")
        self.advance(world, 5)
        committed = world.state.agency.committed_actions()
        self.assertGreater(committed, 0)

        self.plane.close("nightcord")
        self.plane.restore("nightcord")
        restored = self.plane.service.opened("nightcord")
        # 计数从耐久日志推导，所以一次恢复换不来新的额度。
        self.assertEqual(restored.state.agency.committed_actions(), committed)

    def test_the_configured_cap_is_what_the_engine_enforces(self):
        plane = WorldControlPlane(
            root=self.root / "capped",
            client_factory=lambda *a, **k: self.provider,
            autonomy=AutonomySettings(
                driver=DriverConfig(interval_seconds=0.01, stop_timeout_seconds=1.0),
                cadence=ActivationCadence(
                    interval_minutes=1, first_delay_minutes=1, stagger_minutes=1
                ),
                world_action_cap=3,
            ),
        )
        try:
            plane.create(
                world_id="tiny", scene_id=SCENE, character_ids=list(CHARACTERS)
            )
            world = plane.service.opened("tiny")
            for _ in range(6):
                world.runtime.advance(5)
            # 引擎那道硬闸仍然在，而且认的就是配置里那个数。
            self.assertEqual(world.state.agency.committed_actions(), 3)
            self.assertNotEqual(
                world.state.agency.for_outcome(AgencyOutcome.REJECTED_BUDGET), ()
            )
        finally:
            plane.drivers.stop_all("test", 5.0)
            plane.service.release_all()


def _context(character_id, *, observations=(), recalled=(), cue=None):
    now = datetime(2026, 8, 23, 1, 0)
    return GenerationContext(
        character_id=character_id,
        activation=ActivationCue(
            kind=ActivationKind.CHARACTER_ACTIVATION.value, at=now, cue=cue
        ),
        now=now,
        action_id=ActionId.SEND_CHANNEL_MESSAGE,
        target_id="nightcord",
        location_id="mizuki_home_room",
        channel_ids=("nightcord",),
        observations=tuple(observations),
        recalled=tuple(recalled),
    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
