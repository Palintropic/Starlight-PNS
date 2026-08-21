# tests/test_agency.py — P9 Agency / Planner 基础的不变量。
#
# 盯住的东西按"错了会怎样"排：
#   1. 动作只能来自类型化目录，任意字典改不动 WorldState
#   2. 交给策略的上下文是角色作用域的：没观察到的事、被拒绝这件事本身、
#      全知的事件历史，一个字都不许渗进去
#   3. 提案不是世界真相：propose() 一个字节都不改
#   4. 提交那一刻重判前置条件，过期的提案不许落地
#   5. 被拒 / 失败 / 超预算都不留事件、不留半截世界状态
#   6. "什么都不做"是显式的合法结果，不是错误，也不是编出来的台词
#   7. 预算是显式且确定的，计数从日志推导，存档往返之后仍然成立
#   8. 调度器 → Agency 的交接只发生一次
#   9. 存档能原样恢复，拼接出来的存档响亮地失败
#  10. 研究会话的确定性 round robin 一点没变
#
# 运行: python -m unittest tests.test_agency -v
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

from pns.models.action import (
    ActionDefinition,
    ActionError,
    ActionEventMismatch,
    ActionId,
    ActionProposal,
    LegalAction,
    Precondition,
    ParticipantSource,
    TargetKind,
    action_definition,
    agency_event_fields,
    verify_agency_event,
    catalogue,
    catalogue_ids,
    new_proposal_id,
)
from pns.models.activation import ActivationDue, ActivationKind, ScheduledActivation
from pns.models.agency import (
    AgencyBudget,
    AgencyError,
    AgencyLog,
    AgencyOutcome,
    AgencyRecord,
)
from pns.models.event import Event, EventScope, EventType
from pns.models.session import SessionState, SessionStateError
from pns.models.world_state import Availability, WorldState
from pns.runtime.agency import context as context_mod
from pns.runtime.agency import effects as effects_mod
from pns.runtime.agency import engine as engine_mod
from pns.runtime.agency.context import AgencyContextError, build_agency_context
from pns.runtime.agency.effects import AgencyEffectError, event_for_proposal
from pns.runtime.agency.engine import AgencyEngine, AgencyEngineError, ProposalPlan
from pns.runtime.agency.policy import (
    AbstainPolicy,
    AgencyPolicy,
    AgencyPolicyError,
    FirstLegalActionPolicy,
    ModelBackedPolicy,
    PolicyDecision,
    ScriptedPolicy,
)
from pns.runtime.agency.preconditions import (
    evaluators,
    failed_preconditions,
    legal_actions,
)
from pns.runtime.event_commit import EventCommitError, commit_session_event
from pns.runtime.scheduler import PersistentScheduler
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph

CLOCK = datetime(2026, 8, 21, 23, 50)
AGENCY_DIR = Path(engine_mod.__file__).resolve().parent


# ── 夹具 ────────────────────────────────────────────────────────────────
def _world(clock=CLOCK, *, join_nightcord=("mizuki",)):
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


def _due(scheduler, activation_id="wake", *, character_id="mizuki", minutes=10):
    """排一条激活并把时间推到它到期，返回那条到期记录。"""
    scheduler.schedule(
        ScheduledActivation(
            activation_id=activation_id,
            kind=ActivationKind.CHARACTER_ACTIVATION,
            due_at=scheduler.clock + timedelta(minutes=minutes),
            character_id=character_id,
        )
    )
    return scheduler.advance_by(minutes).due[0]


def _rig(policy=None, budget=None, world=None, session_id="s1"):
    """一整套：会话 + 调度器 + 引擎。"""
    state = _session(world, session_id=session_id)
    scheduler = PersistentScheduler(state)
    engine = AgencyEngine(state, policy=policy, budget=budget)
    return state, scheduler, engine


def _fingerprint(state):
    """会话里跟 Agency 有关的全部状态。回滚测试拿它做前后比对。"""
    return {
        "clock": state.world_state.clock,
        "world": state.world_state.to_dict(),
        "events": [event.event_id for event in state.events],
        "observations": len(state.observations),
        "exposures": len(state.exposures),
        "turns": len(state.turns),
        "histories": {cid: len(items) for cid, items in state.histories.items()},
        "queue": state.activations.to_dict(),
        "outbox": state.activation_outbox.to_dict(),
        "agency": state.agency.to_dict(),
    }


def _commit_dialogue_at(state, actor, location_id, text, event_id="secret"):
    """在某个地点提交一条发言事件（用来制造"别人没听见"的局面）。"""
    world = state.world_state
    event = Event(
        event_id=event_id,
        type=EventType.DIALOGUE_SPOKEN,
        occurred_at=world.clock,
        scope=EventScope.LOCATION,
        actor_id=actor,
        participants=tuple(world.characters_at(location_id)),
        location_id=location_id,
        payload={"text": text, "char_name": actor},
    )
    return commit_session_event(state, event)


class _CountingPolicy(AgencyPolicy):
    """记下自己被调用了几次的策略包装。"""

    name = "counting"

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def decide(self, context):
        self.calls += 1
        return self.inner.decide(context)


def _move_decision(context, target="mizuki_home", proposal_id=None):
    return PolicyDecision(
        proposals=(
            ActionProposal(
                proposal_id=proposal_id or f"{context.activation.due_id}#0",
                character_id=context.character_id,
                action_id=ActionId.MOVE_TO,
                target_id=target,
            ),
        )
    )


class _MovePolicy(AgencyPolicy):
    """确定性地提出一次移动，目标可指定（用来构造非法/过期的场景）。"""

    name = "move"

    def __init__(self, target="mizuki_home", proposal_id=None):
        self.target = target
        self.proposal_id = proposal_id

    def decide(self, context):
        return _move_decision(context, self.target, self.proposal_id)


# ── AC1 类型化目录 ──────────────────────────────────────────────────────
class ActionCatalogueTests(unittest.TestCase):
    """动作只能来自目录，而且目录里每一条都是真的能执行的。"""

    def test_every_action_id_has_a_definition_and_vice_versa(self):
        self.assertEqual(set(catalogue()), set(ActionId))
        self.assertEqual(set(catalogue_ids()), set(ActionId))
        # 顺序确定，不依赖字典迭代顺序。
        self.assertEqual(
            list(catalogue_ids()), sorted(ActionId, key=lambda a: a.value)
        )

    def test_every_declared_precondition_has_exactly_one_evaluator(self):
        # 声明了却没有求值器 = 这条条件从来没被判过；有求值器却没人声明 =
        # 有条件是从别处偷偷加进来的。两边都必须完全相等。
        self.assertEqual(set(evaluators()), set(Precondition))

    def test_every_action_lands_on_an_event_type_with_a_state_effect(self):
        from pns.runtime.event_commit import _APPLY

        for action_id in catalogue_ids():
            definition = action_definition(action_id)
            self.assertIn(
                definition.event_type,
                _APPLY,
                f"{action_id.value} 落在一个没有状态效果的事件类型上",
            )
            for precondition in definition.preconditions:
                self.assertIsInstance(precondition, Precondition)

    def test_an_unknown_action_cannot_be_proposed(self):
        with self.assertRaises(ActionError):
            action_definition("teleport.somewhere")
        with self.assertRaises(ActionError):
            ActionProposal(
                proposal_id="p", character_id="mizuki", action_id="teleport.somewhere"
            )

    def test_an_undeclared_payload_key_is_refused_outright(self):
        # 这是"任意字典改不动 WorldState"的第一道锁：带着 clock 的 payload
        # 连一条提案都构造不出来，更不可能走到提交边界。
        with self.assertRaises(ActionError) as ctx:
            ActionProposal(
                proposal_id="p",
                character_id="mizuki",
                action_id=ActionId.MOVE_TO,
                target_id="mizuki_home",
                payload={"clock": "2030-01-01T00:00:00"},
            )
        self.assertIn("clock", str(ctx.exception))

    def test_a_missing_required_payload_key_is_refused(self):
        with self.assertRaises(ActionError):
            ActionProposal(
                proposal_id="p", character_id="mizuki", action_id=ActionId.SPEAK_HERE
            )
        with self.assertRaises(ActionError):
            ActionProposal(
                proposal_id="p",
                character_id="mizuki",
                action_id=ActionId.SPEAK_HERE,
                payload={"text": "   "},
            )

    def test_target_requirements_are_enforced_in_both_directions(self):
        # 需要目标却没给
        with self.assertRaises(ActionError):
            ActionProposal(
                proposal_id="p", character_id="mizuki", action_id=ActionId.MOVE_TO
            )
        # 不需要目标却给了 —— 静默忽略会让调用方以为自己能指定说话地点
        with self.assertRaises(ActionError):
            ActionProposal(
                proposal_id="p",
                character_id="mizuki",
                action_id=ActionId.SPEAK_HERE,
                target_id="city_streets",
                payload={"text": "hi"},
            )

    def test_event_payload_only_carries_declared_keys(self):
        proposal = ActionProposal(
            proposal_id="p",
            character_id="mizuki",
            action_id=ActionId.SPEAK_HERE,
            payload={"text": "hi", "char_name": "瑞希"},
        )
        self.assertEqual(
            proposal.event_payload(), {"text": "hi", "char_name": "瑞希"}
        )

    def test_a_proposal_is_immutable_and_round_trips(self):
        proposal = ActionProposal(
            proposal_id="p",
            character_id="mizuki",
            action_id=ActionId.SPEAK_HERE,
            payload={"text": "hi"},
        )
        with self.assertRaises(TypeError):
            proposal.payload["text"] = "changed"
        self.assertEqual(
            ActionProposal.from_dict(proposal.to_dict()), proposal
        )
        self.assertEqual(hash(proposal), hash("p"))

    def test_requires_authored_text_is_derived_not_stored(self):
        self.assertTrue(action_definition(ActionId.SPEAK_HERE).requires_authored_text)
        self.assertFalse(action_definition(ActionId.MOVE_TO).requires_authored_text)
        self.assertTrue(
            LegalAction(action_id=ActionId.SEND_CHANNEL_MESSAGE, target_id="nightcord")
            .requires_authored_text
        )


class LegalActionEnumerationTests(unittest.TestCase):
    """枚举出来的正好是前置条件全过的那些 —— 不多也不少。"""

    def setUp(self):
        self.world = _world()

    def _brute_force(self, character_id):
        found = []
        for action_id in catalogue_ids():
            definition = action_definition(action_id)
            if definition.target_kind is TargetKind.NONE:
                targets = [None]
            elif definition.target_kind is TargetKind.LOCATION:
                targets = list(self.world.locations.ids())
            else:
                targets = list(self.world.channels.ids())
            for target in targets:
                if not failed_preconditions(
                    self.world, character_id, action_id, target
                ):
                    found.append(LegalAction(action_id=action_id, target_id=target))
        return sorted(found, key=lambda legal: legal.sort_key)

    def test_enumeration_matches_precondition_truth(self):
        legal, truncated = legal_actions(self.world, "mizuki")
        self.assertFalse(truncated)
        self.assertEqual(list(legal), self._brute_force("mizuki"))

    def test_ordering_is_deterministic(self):
        first, _ = legal_actions(self.world, "mizuki")
        second, _ = legal_actions(_world(), "mizuki")
        self.assertEqual(first, second)
        self.assertEqual(
            [legal.sort_key for legal in first],
            sorted(legal.sort_key for legal in first),
        )

    def test_movement_is_one_step_only(self):
        legal, _ = legal_actions(self.world, "mizuki")
        moves = {
            legal_action.target_id
            for legal_action in legal
            if legal_action.action_id is ActionId.MOVE_TO
        }
        self.assertEqual(moves, {"mizuki_home"})
        # 两步之外的地点不在枚举里，即使它确实存在。
        self.assertNotIn("city_streets", moves)

    def test_an_asleep_character_can_do_nothing(self):
        self.world.set_availability("mizuki", Availability.ASLEEP)
        legal, _ = legal_actions(self.world, "mizuki")
        self.assertEqual(legal, ())

    def test_being_busy_does_not_remove_options(self):
        # busy 只是"在忙"，不是"不能动"。要不要现在动是策略的判断。
        self.world.set_availability("mizuki", Availability.BUSY)
        legal, _ = legal_actions(self.world, "mizuki")
        self.assertTrue(legal)

    def test_an_unknown_character_can_do_nothing(self):
        legal, _ = legal_actions(self.world, "nobody")
        self.assertEqual(legal, ())

    def test_channel_membership_flips_join_and_leave(self):
        legal, _ = legal_actions(self.world, "mizuki")
        pairs = {(l.action_id, l.target_id) for l in legal}
        self.assertIn((ActionId.LEAVE_CHANNEL, "nightcord"), pairs)
        self.assertNotIn((ActionId.JOIN_CHANNEL, "nightcord"), pairs)

        legal, _ = legal_actions(self.world, "ena")
        pairs = {(l.action_id, l.target_id) for l in legal}
        self.assertIn((ActionId.JOIN_CHANNEL, "nightcord"), pairs)
        self.assertNotIn((ActionId.LEAVE_CHANNEL, "nightcord"), pairs)
        self.assertNotIn((ActionId.SEND_CHANNEL_MESSAGE, "nightcord"), pairs)


# ── AC2 角色作用域 ──────────────────────────────────────────────────────
class CharacterScopedContextTests(unittest.TestCase):
    """交给策略的上下文里，不能有这个角色感知不到的东西。"""

    def setUp(self):
        self.state, self.scheduler, self.engine = _rig()
        self.due = _due(self.scheduler)

    def test_an_unobserved_event_leaves_no_trace_in_the_context(self):
        # ena 在自己房间说话，mizuki 不在场 → 判定为 wrong_location，无观察。
        _commit_dialogue_at(
            self.state, "ena", "ena_home_studio", "SECRET-PASSPHRASE-42"
        )
        self.assertEqual(len(self.state.observations.for_character("mizuki")), 0)

        context = self.engine.context_for(self.due)
        blob = json.dumps(context.to_dict(), ensure_ascii=False)
        self.assertNotIn("SECRET-PASSPHRASE-42", blob)
        self.assertNotIn("secret", blob)  # 连事件 ID 都不该出现
        self.assertEqual(context.observations, ())

    def test_the_context_never_carries_denial_reasons(self):
        _commit_dialogue_at(self.state, "ena", "ena_home_studio", "hello")
        # 系统侧确实记下了拒绝理由……
        denials = [d for d in self.state.exposures.for_character("mizuki")]
        self.assertTrue(any(not d.exposed for d in denials))
        # ……但角色不该知道"自己被拒绝过"，那本身就是情报。
        blob = json.dumps(self.engine.context_for(self.due).to_dict(), ensure_ascii=False)
        for reason in ("wrong_location", "no_channel_access", "public_not_perceived"):
            self.assertNotIn(reason, blob)

    def test_the_context_never_carries_provenance(self):
        world = self.state.world_state
        event = Event(
            event_id="own",
            type=EventType.DIALOGUE_SPOKEN,
            occurred_at=world.clock,
            scope=EventScope.LOCATION,
            actor_id="mizuki",
            location_id="mizuki_home_room",
            payload={"text": "hi", "char_name": "瑞希"},
            provenance={"drift_score": 7, "is_ooc": True, "generator_model": "m"},
            correlation_id="corr-1",
            causation_id="cause-1",
        )
        commit_session_event(self.state, event)
        context = self.engine.context_for(self.due)
        self.assertEqual(len(context.observations), 1)  # 自观察确实进来了
        blob = json.dumps(context.to_dict(), ensure_ascii=False)
        for forbidden in (
            "drift_score",
            "is_ooc",
            "generator_model",
            "corr-1",
            "cause-1",
        ):
            self.assertNotIn(forbidden, blob)

    def test_the_context_is_json_safe(self):
        json.dumps(self.engine.context_for(self.due).to_dict())

    def test_another_characters_observation_cannot_be_smuggled_in(self):
        _commit_dialogue_at(self.state, "ena", "ena_home_studio", "hello")
        ena_observations = self.state.observations.for_character("ena")
        self.assertTrue(ena_observations)
        with self.assertRaises(AgencyContextError):
            build_agency_context(
                self.state.world_state, "mizuki", self.due, ena_observations
            )

    def test_a_due_for_someone_else_cannot_build_this_characters_context(self):
        with self.assertRaises(AgencyContextError):
            build_agency_context(self.state.world_state, "ena", self.due, ())

    def test_perceived_characters_follows_the_world_not_the_session_roster(self):
        context = self.engine.context_for(self.due)
        # ena 在会话名单里，但既不同处一地也不同频道 → 感知不到。
        self.assertEqual(context.perceived_characters, ())
        self.state.world_state.join_channel("ena", "nightcord")
        self.assertEqual(
            self.engine.context_for(self.due).perceived_characters, ("ena",)
        )


class ContextModuleBoundaryTests(unittest.TestCase):
    """静态保证：上下文构造器读不到全知数据 —— 现在不读，以后也不许读。"""

    def _attributes(self, path):
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        return {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }

    def test_the_context_builder_touches_neither_exposures_nor_events(self):
        attributes = self._attributes(context_mod.__file__)
        self.assertNotIn("exposures", attributes)
        self.assertNotIn("events", attributes)

    def test_no_agency_module_reads_the_exposure_log(self):
        # 引擎可以读事件历史（它要算因果链），但曝光判定日志含拒绝理由，
        # 整个 Agency 层都没有正当理由碰它。
        for path in sorted(AGENCY_DIR.glob("*.py")):
            self.assertNotIn(
                "exposures",
                self._attributes(path),
                f"{path.name} 读了曝光判定日志",
            )

    def test_the_context_builder_never_receives_a_session(self):
        # 按标识符查，不按原文查：模块注释里写着"它不接受 SessionState"，
        # 那是说明，不是引用。
        tree = ast.parse(Path(context_mod.__file__).read_text(encoding="utf-8"))
        names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {
            alias.name for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertNotIn("SessionState", names)


# ── AC3 提案 ≠ 世界真相 ─────────────────────────────────────────────────
class ProposalIsNotTruthTests(unittest.TestCase):
    def setUp(self):
        self.state, self.scheduler, self.engine = _rig(policy=FirstLegalActionPolicy())
        self.due = _due(self.scheduler)

    def test_propose_changes_absolutely_nothing(self):
        before = _fingerprint(self.state)
        plan = self.engine.propose(self.due)
        self.assertTrue(plan.would_act)
        self.assertEqual(_fingerprint(self.state), before)
        # 到期记录仍然待处理 —— 一个没提交的判断等于没发生。
        self.assertEqual([r.due_id for r in self.engine.pending_due()], [self.due.due_id])

    def test_a_plan_can_be_thrown_away(self):
        self.engine.propose(self.due)
        self.engine.propose(self.due)  # 再来一次也没问题，它是纯的
        self.assertEqual(len(self.state.agency), 0)
        self.assertEqual(len(self.state.events), 1)  # 只有那条时间推进事件

    def test_building_a_proposal_alone_touches_nothing(self):
        before = _fingerprint(self.state)
        ActionProposal(
            proposal_id=new_proposal_id(),
            character_id="mizuki",
            action_id=ActionId.MOVE_TO,
            target_id="mizuki_home",
        )
        self.assertEqual(_fingerprint(self.state), before)

    def test_only_commit_reaches_the_world(self):
        plan = self.engine.propose(self.due)
        events_before = len(self.state.events)
        record = self.engine.commit(plan)
        self.assertIs(record.outcome, AgencyOutcome.ACTED)
        self.assertEqual(len(self.state.events), events_before + 1)
        self.assertTrue(self.state.events.has(record.event_id))


# ── AC4 策略 ────────────────────────────────────────────────────────────
class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.state, self.scheduler, self.engine = _rig()
        self.due = _due(self.scheduler)
        self.context = self.engine.context_for(self.due)

    def test_deterministic_policies_repeat_themselves(self):
        policy = FirstLegalActionPolicy()
        self.assertEqual(policy.decide(self.context), policy.decide(self.context))
        # 另一个会话、同样的世界 → 同样的决定（提案身份由 due_id 推导）。
        other_state, other_scheduler, other_engine = _rig(session_id="s1")
        other_due = _due(other_scheduler)
        self.assertEqual(
            policy.decide(self.context),
            policy.decide(other_engine.context_for(other_due)),
        )

    def test_the_default_policy_does_nothing(self):
        self.assertTrue(AbstainPolicy().decide(self.context).abstains)

    def test_a_deterministic_policy_never_invents_dialogue(self):
        decision = FirstLegalActionPolicy().decide(self.context)
        chosen = decision.proposals[0]
        self.assertFalse(chosen.definition.requires_authored_text)
        self.assertEqual(dict(chosen.payload), {})

    def test_first_legal_abstains_when_only_text_actions_remain(self):
        # 一个没有频道的世界，人站在没有邻居的地点：唯一合法的动作是
        # speak.here，而它需要台词 —— 台词属于生成层，策略不许自己编。
        from pns.models.channel import ChannelRegistry

        world = WorldState(
            clock=CLOCK,
            locations=build_default_location_graph(),
            channels=ChannelRegistry(),
        )
        world.place_character("mizuki", "tokyo")
        state, scheduler, engine = _rig(world=world)
        context = engine.context_for(_due(scheduler))
        self.assertEqual(
            [legal.action_id for legal in context.legal_actions],
            [ActionId.SPEAK_HERE],
        )
        self.assertTrue(FirstLegalActionPolicy().decide(context).abstains)

    def test_a_scripted_policy_falls_back_to_abstaining(self):
        self.assertTrue(ScriptedPolicy({}).decide(self.context).abstains)
        scripted = ScriptedPolicy({"mizuki": _move_decision(self.context)})
        self.assertFalse(scripted.decide(self.context).abstains)

    def test_the_model_adapter_only_takes_a_callable(self):
        # 它拿不到会话、世界、事件历史 —— 所以它没有任何提交的能力。
        policy = ModelBackedPolicy(lambda context: None)
        self.assertEqual(
            [name for name in vars(policy) if "state" in name or "session" in name], []
        )
        with self.assertRaises(AgencyPolicyError):
            ModelBackedPolicy("not callable")

    def test_the_model_adapter_translates_but_does_not_judge(self):
        # 适配器不判合法性：一个走不通的目标照样被翻译成提案，由引擎去拒。
        policy = ModelBackedPolicy(
            lambda context: {"action_id": "movement.move_to", "target_id": "tokyo"}
        )
        decision = policy.decide(self.context)
        self.assertEqual(decision.proposals[0].target_id, "tokyo")
        self.assertFalse(self.context.has_legal(ActionId.MOVE_TO, "tokyo"))

    def test_the_model_adapter_turns_garbage_into_a_policy_error(self):
        for raw in ("not a dict", 42, [{"action_id": "no.such.action"}], [7]):
            with self.subTest(raw=raw):
                with self.assertRaises(AgencyPolicyError):
                    ModelBackedPolicy(lambda context, raw=raw: raw).decide(self.context)

    def test_the_model_adapter_cannot_smuggle_an_undeclared_payload_key(self):
        policy = ModelBackedPolicy(
            lambda context: {
                "action_id": "movement.move_to",
                "target_id": "mizuki_home",
                "payload": {"clock": "2030-01-01T00:00:00"},
            }
        )
        with self.assertRaises(AgencyPolicyError):
            policy.decide(self.context)

    def test_a_raising_selector_becomes_a_policy_error(self):
        def boom(context):
            raise RuntimeError("model is down")

        with self.assertRaises(AgencyPolicyError):
            ModelBackedPolicy(boom).decide(self.context)

    def test_an_empty_selection_is_an_abstention(self):
        for raw in (None, {}, {"action_id": None}):
            with self.subTest(raw=raw):
                decision = ModelBackedPolicy(
                    lambda context, raw=raw: raw
                ).decide(self.context)
                self.assertTrue(decision.abstains)


# ── AC5/AC6 提交、重判与原子性 ──────────────────────────────────────────
class CommitTests(unittest.TestCase):
    def setUp(self):
        self.state, self.scheduler, self.engine = _rig(policy=_MovePolicy())
        self.due = _due(self.scheduler)

    def test_an_accepted_action_commits_through_the_p5_boundary(self):
        record = self.engine.evaluate(self.due)
        self.assertIs(record.outcome, AgencyOutcome.ACTED)
        event = self.state.events.get(record.event_id)
        self.assertIs(event.type, EventType.CHARACTER_LOCATION_CHANGED)
        self.assertEqual(event.location_id, "mizuki_home")
        self.assertEqual(event.occurred_at, self.state.world_state.clock)
        self.assertEqual(event.provenance["kind"], "agency")
        self.assertEqual(event.provenance["due_id"], self.due.due_id)
        # 状态效果真的发生了，而且只发生了一次。
        self.assertEqual(self.state.world_state.location_of("mizuki"), "mizuki_home")

    def test_an_accepted_action_produces_observations_not_histories(self):
        history_before = {c: len(i) for c, i in self.state.histories.items()}
        self.engine.evaluate(self.due)
        # 角色历史是研究会话生成路径的投影，自主动作不往里写。
        self.assertEqual(
            {c: len(i) for c, i in self.state.histories.items()}, history_before
        )
        self.assertTrue(self.state.observations.for_character("mizuki"))

    def test_a_stale_proposal_is_refused_after_the_actor_moves(self):
        plan = self.engine.propose(self.due)
        # 挪到一个跟 mizuki_home 不相邻的地方 —— city_streets 是相邻的，
        # 挪过去那条移动仍然合法，证明不了任何事。
        self.state.world_state.place_character("mizuki", "clothing_store_floor")
        record = self.engine.commit(plan)
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_STALE)
        self.assertEqual(record.detail["reason"], "failed_preconditions")
        self.assertIn(
            Precondition.TARGET_LOCATION_REACHABLE.value, record.detail["failed"]
        )
        self.assertIsNone(record.event_id)
        self.assertEqual(len(self.state.events), 1)

    def test_a_stale_proposal_is_refused_after_the_actor_falls_asleep(self):
        plan = self.engine.propose(self.due)
        self.state.world_state.set_availability("mizuki", Availability.ASLEEP)
        record = self.engine.commit(plan)
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_STALE)
        self.assertIn(Precondition.ACTOR_AWAKE.value, record.detail["failed"])
        self.assertEqual(len(self.state.events), 1)

    def test_a_stale_proposal_is_refused_after_channel_membership_changes(self):
        state, scheduler, engine = _rig(
            policy=ScriptedPolicy({}),
        )
        due = _due(scheduler)
        context = engine.context_for(due)
        plan = ProposalPlan(
            due=due,
            character_id="mizuki",
            policy="manual",
            proposed_at=context.observed_at,
            verdict=AgencyOutcome.ACTED,
            proposal=ActionProposal(
                proposal_id="p1",
                character_id="mizuki",
                action_id=ActionId.LEAVE_CHANNEL,
                target_id="nightcord",
            ),
        )
        state.world_state.leave_channel("mizuki", "nightcord")
        record = engine.commit(plan)
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_STALE)
        self.assertIn(
            Precondition.ACTOR_IN_TARGET_CHANNEL.value, record.detail["failed"]
        )

    def test_a_proposal_from_another_moment_is_refused(self):
        plan = self.engine.propose(self.due)
        # 时钟往前走了 —— 这条到期问的是"那一刻要不要动"，不是"现在"。
        self.scheduler.advance_by(5)
        record = self.engine.commit(plan)
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_STALE)
        self.assertEqual(record.detail["reason"], "clock_moved")
        self.assertIsNone(record.event_id)
        self.assertEqual(self.state.world_state.location_of("mizuki"), "mizuki_home_room")

    def test_an_illegal_action_leaves_no_event(self):
        state, scheduler, engine = _rig(policy=_MovePolicy(target="tokyo"))
        due = _due(scheduler)
        before_events = len(state.events)
        record = engine.evaluate(due)
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_ILLEGAL)
        self.assertEqual(record.detail["reason"], "illegal_action")
        self.assertEqual(len(state.events), before_events)
        self.assertEqual(state.world_state.location_of("mizuki"), "mizuki_home_room")

    def test_a_proposal_for_the_wrong_actor_is_refused(self):
        class _WrongActor(AgencyPolicy):
            name = "wrong_actor"

            def decide(self, context):
                return PolicyDecision(
                    proposals=(
                        ActionProposal(
                            proposal_id="p1",
                            character_id="ena",
                            action_id=ActionId.JOIN_CHANNEL,
                            target_id="nightcord",
                        ),
                    )
                )

        state, scheduler, engine = _rig(policy=_WrongActor())
        record = engine.evaluate(_due(scheduler))
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_ILLEGAL)
        self.assertEqual(record.detail["reason"], "actor_mismatch")

    def test_a_policy_failure_leaves_no_event_and_does_not_escape(self):
        def boom(context):
            raise RuntimeError("model is down")

        state, scheduler, engine = _rig(policy=ModelBackedPolicy(boom))
        due = _due(scheduler)
        record = engine.evaluate(due)  # 不抛异常
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_POLICY_ERROR)
        self.assertIsNone(record.event_id)
        self.assertEqual(len(state.events), 1)

    def test_a_policy_returning_the_wrong_type_is_a_policy_error(self):
        class _Bad(AgencyPolicy):
            name = "bad"

            def decide(self, context):
                return {"action_id": "movement.move_to"}

        state, scheduler, engine = _rig(policy=_Bad())
        record = engine.evaluate(_due(scheduler))
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_POLICY_ERROR)
        self.assertEqual(record.detail["reason"], "policy_returned_wrong_type")

    def test_a_rejected_decision_is_still_audited_and_still_acknowledged(self):
        state, scheduler, engine = _rig(policy=_MovePolicy(target="tokyo"))
        due = _due(scheduler)
        record = engine.evaluate(due)
        self.assertTrue(record.outcome.rejected)
        # 审计留下了，交接完成了，但世界一点没变。
        self.assertTrue(state.agency.has(due.due_id))
        self.assertTrue(state.activation_outbox.is_acknowledged(due.due_id))
        self.assertEqual(engine.pending_due(), ())
        self.assertEqual(len(state.observations), 0)

    def test_a_character_removed_after_scheduling_is_refused(self):
        state, scheduler, engine = _rig(policy=_MovePolicy())
        due = _due(scheduler)
        state.world_state.remove_character("mizuki")
        record = engine.evaluate(due)
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_ILLEGAL)
        self.assertEqual(record.detail["reason"], "unknown_character")


class AtomicityTests(unittest.TestCase):
    """事务里任何一步失败，世界、事件、观察、日志、投递箱一起回到原样。"""

    def setUp(self):
        self.state, self.scheduler, self.engine = _rig(policy=_MovePolicy())
        self.due = _due(self.scheduler)
        self.before = _fingerprint(self.state)

    def _assert_rolled_back(self):
        self.assertEqual(_fingerprint(self.state), self.before)
        # 到期记录仍然待处理 —— 这正是"可以重试"所需要的状态。
        self.assertEqual(
            [r.due_id for r in self.engine.pending_due()], [self.due.due_id]
        )
        self.assertFalse(self.state.activation_outbox.is_acknowledged(self.due.due_id))

    def test_a_failing_event_commit_rolls_everything_back(self):
        with patch.object(
            engine_mod, "commit_session_event", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.engine.evaluate(self.due)
        self._assert_rolled_back()

    def test_a_failing_audit_write_rolls_everything_back(self):
        with patch.object(AgencyLog, "_append", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.engine.evaluate(self.due)
        self._assert_rolled_back()

    def test_a_failing_acknowledgement_rolls_everything_back(self):
        from pns.models.activation_outbox import ActivationOutbox

        with patch.object(
            ActivationOutbox, "_acknowledge", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.engine.evaluate(self.due)
        self._assert_rolled_back()

    def test_an_event_the_p5_boundary_refuses_leaves_no_record_at_all(self):
        world = self.state.world_state

        def _bogus(*args, **kwargs):
            return Event(
                event_id="bogus",
                type=EventType.CHARACTER_LOCATION_CHANGED,
                occurred_at=world.clock,
                scope=EventScope.LOCATION,
                actor_id="mizuki",
                location_id="mizuki_home_room",  # 已经在那儿 → 提交边界拒绝
            )

        with patch.object(engine_mod, "event_for_proposal", _bogus):
            with self.assertRaises(EventCommitError):
                self.engine.evaluate(self.due)
        self._assert_rolled_back()
        self.assertEqual(len(self.state.agency), 0)


# ── AC7 显式不动 ────────────────────────────────────────────────────────
class AbstentionTests(unittest.TestCase):
    def setUp(self):
        self.state, self.scheduler, self.engine = _rig(policy=AbstainPolicy())
        self.due = _due(self.scheduler)

    def test_doing_nothing_is_a_valid_recorded_outcome(self):
        record = self.engine.evaluate(self.due)  # 不抛异常
        self.assertIs(record.outcome, AgencyOutcome.ABSTAINED)
        self.assertIsNone(record.event_id)
        self.assertIsNone(record.proposal)
        self.assertEqual(len(self.state.events), 1)
        self.assertEqual(len(self.state.observations), 0)

    def test_abstaining_fabricates_no_dialogue(self):
        self.engine.evaluate(self.due)
        blob = json.dumps(self.state.agency.to_dict(), ensure_ascii=False)
        self.assertNotIn("text", blob)
        self.assertEqual(
            self.state.events.by_type(EventType.DIALOGUE_SPOKEN), ()
        )

    def test_the_policys_stated_reason_reaches_the_audit(self):
        record = self.engine.evaluate(self.due)
        self.assertEqual(
            record.detail["rationale"], "default policy takes no action"
        )
        # 它是系统侧审计，从来不是角色经验：观察一条都没产生。
        self.assertEqual(len(self.state.observations), 0)

    def test_evaluated_and_abstained_is_distinguishable_from_never_evaluated(self):
        other_due = _due(self.scheduler, "later", minutes=20)
        self.engine.evaluate(self.due)
        self.assertTrue(self.state.agency.has(self.due.due_id))
        self.assertFalse(self.state.agency.has(other_due.due_id))
        self.assertEqual(
            [r.due_id for r in self.engine.pending_due()], [other_due.due_id]
        )

    def test_abstention_still_completes_the_handoff_exactly_once(self):
        self.engine.evaluate(self.due)
        self.assertTrue(self.state.activation_outbox.is_acknowledged(self.due.due_id))
        with self.assertRaises(AgencyEngineError):
            self.engine.evaluate(self.due)


# ── AC8 预算 ────────────────────────────────────────────────────────────
class BudgetTests(unittest.TestCase):
    def test_budget_values_must_be_positive_integers(self):
        for bad in (0, -1, True, 1.5, "3"):
            with self.subTest(bad=bad):
                with self.assertRaises(AgencyError):
                    AgencyBudget(max_proposals_per_activation=bad)

    def test_too_many_proposals_for_one_activation_is_refused(self):
        class _Greedy(AgencyPolicy):
            name = "greedy"

            def decide(self, context):
                return PolicyDecision(
                    proposals=(
                        ActionProposal(
                            proposal_id="p1",
                            character_id="mizuki",
                            action_id=ActionId.MOVE_TO,
                            target_id="mizuki_home",
                        ),
                        ActionProposal(
                            proposal_id="p2",
                            character_id="mizuki",
                            action_id=ActionId.LEAVE_CHANNEL,
                            target_id="nightcord",
                        ),
                    )
                )

        state, scheduler, engine = _rig(policy=_Greedy())
        record = engine.evaluate(_due(scheduler))
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_BUDGET)
        self.assertEqual(record.detail["reason"], "max_proposals_per_activation")
        self.assertEqual(len(state.events), 1)

    def test_the_session_action_ceiling_is_enforced(self):
        state, scheduler, engine = _rig(
            policy=FirstLegalActionPolicy(),
            budget=AgencyBudget(max_committed_actions_per_session=1),
        )
        first = engine.evaluate(_due(scheduler, "a1", minutes=10))
        self.assertIs(first.outcome, AgencyOutcome.ACTED)
        second = engine.evaluate(_due(scheduler, "a2", minutes=10))
        self.assertIs(second.outcome, AgencyOutcome.REJECTED_BUDGET)
        self.assertEqual(second.detail["reason"], "max_committed_actions_per_session")
        self.assertEqual(state.agency.committed_actions(), 1)

    def test_the_ceiling_survives_an_archive_round_trip(self):
        # 计数从日志推导，不是另存的计数器：恢复出来的会话不能把上限再用一遍。
        state, scheduler, engine = _rig(
            policy=FirstLegalActionPolicy(),
            budget=AgencyBudget(max_committed_actions_per_session=1),
        )
        engine.evaluate(_due(scheduler, "a1", minutes=10))
        restored = SessionState.from_dict(state.to_dict())
        restored_scheduler = PersistentScheduler(restored)
        restored_engine = AgencyEngine(
            restored,
            policy=FirstLegalActionPolicy(),
            budget=AgencyBudget(max_committed_actions_per_session=1),
        )
        self.assertEqual(restored.agency.committed_actions(), 1)
        record = restored_engine.evaluate(_due(restored_scheduler, "a2", minutes=10))
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_BUDGET)

    def test_legal_actions_are_truncated_deterministically_and_visibly(self):
        state, scheduler, engine = _rig(budget=AgencyBudget(max_legal_actions=2))
        due = _due(scheduler)
        context = engine.context_for(due)
        self.assertEqual(len(context.legal_actions), 2)
        self.assertTrue(context.legal_actions_truncated)
        full, _ = legal_actions(state.world_state, "mizuki")
        self.assertEqual(context.legal_actions, full[:2])

    def test_observations_keep_the_newest_ones(self):
        state, scheduler, engine = _rig(budget=AgencyBudget(max_observations=1))
        due = _due(scheduler)
        for index in range(3):
            _commit_dialogue_at(
                state, "mizuki", "mizuki_home_room", f"line-{index}", f"e{index}"
            )
        context = engine.context_for(due)
        self.assertEqual(len(context.observations), 1)
        self.assertTrue(context.observations_truncated)
        self.assertEqual(context.observations[0].source_event_id, "e2")

    def test_the_policy_is_consulted_exactly_once_per_activation(self):
        counting = _CountingPolicy(FirstLegalActionPolicy())
        state, scheduler, engine = _rig(policy=counting)
        engine.evaluate(_due(scheduler))
        self.assertEqual(counting.calls, 1)


# ── AC9 交接只发生一次 ──────────────────────────────────────────────────
class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.state, self.scheduler, self.engine = _rig(policy=FirstLegalActionPolicy())
        self.due = _due(self.scheduler)

    def test_the_same_due_cannot_be_evaluated_twice(self):
        self.engine.evaluate(self.due)
        with self.assertRaises(AgencyEngineError):
            self.engine.evaluate(self.due)
        self.assertEqual(len(self.state.agency), 1)
        self.assertEqual(len(self.state.events), 2)

    def test_a_fabricated_due_is_refused(self):
        fake = ActivationDue(
            activation_id="ghost",
            kind=ActivationKind.CHARACTER_ACTIVATION,
            due_at=self.state.world_state.clock,
            fired_at=self.state.world_state.clock,
            sequence=0,
            character_id="mizuki",
        )
        with self.assertRaises(AgencyEngineError):
            self.engine.propose(fake)

    def test_a_tampered_due_is_refused(self):
        tampered = ActivationDue(
            activation_id=self.due.activation_id,
            kind=self.due.kind,
            due_at=self.due.due_at,
            fired_at=self.due.fired_at,
            sequence=self.due.sequence + 1,  # 同一个 due_id，改过的字段
            character_id=self.due.character_id,
        )
        self.assertEqual(tampered.due_id, self.due.due_id)
        with self.assertRaises(AgencyEngineError):
            self.engine.propose(tampered)

    def test_a_due_already_acknowledged_elsewhere_is_refused(self):
        self.scheduler.acknowledge(self.due.due_id)
        with self.assertRaises(AgencyEngineError):
            self.engine.propose(self.due)

    def test_a_due_this_session_never_produced_is_refused(self):
        """交接的权威是"这条记录在**我的**投递箱里"，不是调用方说它是谁的。

        注意 due_id 是从 activation_id + 触发时刻推导的，不带会话号：两个
        状态完全相同的会话会产出字段完全相同的到期记录，那种情况下引擎处理
        的仍然是自己投递箱里的那一条，没有任何东西会串。真正要拦的是这个
        会话根本没产出过的那些。
        """
        other_state, other_scheduler, _ = _rig(session_id="s2")
        other_due = _due(other_scheduler, "someone_elses_activation")
        self.assertFalse(self.state.activation_outbox.has(other_due.due_id))
        with self.assertRaises(AgencyEngineError):
            self.engine.propose(other_due)

    def test_evaluate_pending_drains_in_firing_order_exactly_once(self):
        second = _due(self.scheduler, "later", minutes=20)
        records = self.engine.evaluate_pending()
        self.assertEqual(
            [r.due_id for r in records], [self.due.due_id, second.due_id]
        )
        self.assertEqual(self.engine.evaluate_pending(), ())

    def test_the_record_identity_is_derived_from_the_due(self):
        record = self.engine.evaluate(self.due)
        self.assertEqual(record.due_id, self.due.due_id)
        restored = SessionState.from_dict(self.state.to_dict())
        self.assertTrue(restored.agency.has(self.due.due_id))
        self.assertEqual(restored.agency.get(self.due.due_id), record)

    def test_a_due_without_a_character_cannot_be_evaluated(self):
        headless = ActivationDue(
            activation_id="x",
            kind=ActivationKind.CHARACTER_ACTIVATION,
            due_at=self.state.world_state.clock,
            fired_at=self.state.world_state.clock,
            sequence=0,
        )
        with self.assertRaises(AgencyEngineError):
            self.engine.propose(headless)


class DuplicateIdentityTests(unittest.TestCase):
    def test_the_log_refuses_a_second_record_for_the_same_due(self):
        log = AgencyLog()
        record = AgencyRecord(
            due_id="d1",
            character_id="mizuki",
            decided_at=CLOCK,
            outcome=AgencyOutcome.ABSTAINED,
        )
        log._append(record)
        with self.assertRaises(AgencyError):
            log._append(record)

    def test_the_log_refuses_a_duplicate_proposal_id(self):
        log = AgencyLog()
        proposal = ActionProposal(
            proposal_id="p1",
            character_id="mizuki",
            action_id=ActionId.MOVE_TO,
            target_id="mizuki_home",
        )
        for index in range(2):
            record = AgencyRecord(
                due_id=f"d{index}",
                character_id="mizuki",
                decided_at=CLOCK,
                outcome=AgencyOutcome.ACTED,
                proposal=proposal,
                event_id=f"e{index}",
            )
            if index == 0:
                log._append(record)
            else:
                with self.assertRaises(AgencyError):
                    log._append(record)

    def test_the_engine_refuses_a_proposal_id_it_already_committed(self):
        state, scheduler, engine = _rig(policy=_MovePolicy(proposal_id="fixed"))
        first = engine.evaluate(_due(scheduler, "a1", minutes=10))
        self.assertIs(first.outcome, AgencyOutcome.ACTED)
        # 第二次同样的提案 ID：被拒，而且这条拒绝记录自己落得进日志
        # （被拒的提案不进记录，所以不会撞上唯一性约束把事务拖垮）。
        state.world_state.place_character("mizuki", "mizuki_home_room")
        second = engine.evaluate(_due(scheduler, "a2", minutes=10))
        self.assertIs(second.outcome, AgencyOutcome.REJECTED_ILLEGAL)
        self.assertEqual(second.detail["reason"], "duplicate_proposal_id")
        self.assertEqual(len(state.agency), 2)

    def test_a_record_cannot_disagree_with_itself(self):
        proposal = ActionProposal(
            proposal_id="p1",
            character_id="mizuki",
            action_id=ActionId.MOVE_TO,
            target_id="mizuki_home",
        )
        # acted 却没有事件
        with self.assertRaises(AgencyError):
            AgencyRecord(
                due_id="d",
                character_id="mizuki",
                decided_at=CLOCK,
                outcome=AgencyOutcome.ACTED,
                proposal=proposal,
            )
        # 没行动却指着一条事件
        with self.assertRaises(AgencyError):
            AgencyRecord(
                due_id="d",
                character_id="mizuki",
                decided_at=CLOCK,
                outcome=AgencyOutcome.ABSTAINED,
                event_id="e1",
            )
        # 被拒却带着提案对象
        with self.assertRaises(AgencyError):
            AgencyRecord(
                due_id="d",
                character_id="mizuki",
                decided_at=CLOCK,
                outcome=AgencyOutcome.REJECTED_ILLEGAL,
                proposal=proposal,
            )
        # 提案角色跟记录角色不一致
        with self.assertRaises(AgencyError):
            AgencyRecord(
                due_id="d",
                character_id="ena",
                decided_at=CLOCK,
                outcome=AgencyOutcome.ACTED,
                proposal=proposal,
                event_id="e1",
            )


# ── 台词动作：结构性关闭，不是可配置的 ──────────────────────────────────
class AuthoredTextIsNotCommittableTests(unittest.TestCase):
    """需要台词的动作在本阶段**没有提交路径**。

    不是"默认关着"：一句台词要成为世界真相，该走 生成 → Router 判分 →
    漂移审计落盘 → 提交 那条链，而它还没接到 Agency 这一侧。安全边界不能是
    一个调用方翻得动的布尔量 —— 那样它就不是边界，只是一句建议。
    """

    class _Talker(AgencyPolicy):
        name = "talker"

        def decide(self, context):
            return PolicyDecision(
                proposals=(
                    ActionProposal(
                        proposal_id="p1",
                        character_id=context.character_id,
                        action_id=ActionId.SPEAK_HERE,
                        payload={"text": "こんばんは"},
                    ),
                )
            )

    def test_a_policy_proposing_dialogue_is_refused(self):
        state, scheduler, engine = _rig(policy=self._Talker())
        record = engine.evaluate(_due(scheduler))
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_ILLEGAL)
        self.assertEqual(record.detail["reason"], "authored_text_not_committable")
        self.assertEqual(state.events.by_type(EventType.DIALOGUE_SPOKEN), ())
        self.assertEqual(len(state.observations), 0)

    def test_a_hand_built_plan_cannot_smuggle_dialogue_past_propose(self):
        state, scheduler, engine = _rig(policy=ScriptedPolicy({}))
        due = _due(scheduler)
        plan = ProposalPlan(
            due=due,
            character_id="mizuki",
            policy="manual",
            proposed_at=state.world_state.clock,
            verdict=AgencyOutcome.ACTED,
            proposal=ActionProposal(
                proposal_id="p1",
                character_id="mizuki",
                action_id=ActionId.SPEAK_HERE,
                payload={"text": "我说了算"},
            ),
        )
        record = engine.commit(plan)
        self.assertIs(record.outcome, AgencyOutcome.REJECTED_ILLEGAL)
        self.assertEqual(record.detail["reason"], "authored_text_not_committable")
        self.assertEqual(state.events.by_type(EventType.DIALOGUE_SPOKEN), ())

    def test_a_channel_message_is_equally_uncommittable(self):
        state, scheduler, engine = _rig(policy=ScriptedPolicy({}))
        due = _due(scheduler)
        plan = ProposalPlan(
            due=due,
            character_id="mizuki",
            policy="manual",
            proposed_at=state.world_state.clock,
            verdict=AgencyOutcome.ACTED,
            proposal=ActionProposal(
                proposal_id="p1",
                character_id="mizuki",
                action_id=ActionId.SEND_CHANNEL_MESSAGE,
                target_id="nightcord",
                payload={"text": "在吗"},
            ),
        )
        self.assertIs(engine.commit(plan).outcome, AgencyOutcome.REJECTED_ILLEGAL)
        self.assertEqual(state.events.by_type(EventType.MESSAGE_SENT), ())

    def test_the_event_builder_itself_has_no_path_for_authored_text(self):
        # 第三道闸，也是结构性的那道：构造 agency 事件的函数只有一个，它直接
        # 拒绝。所以运行时里不存在把未判分台词写进世界历史的代码路径。
        state, scheduler, engine = _rig()
        due = _due(scheduler)
        proposal = ActionProposal(
            proposal_id="p1",
            character_id="mizuki",
            action_id=ActionId.SPEAK_HERE,
            payload={"text": "hi"},
        )
        with self.assertRaises(ActionEventMismatch):
            event_for_proposal(
                state.world_state, state.events, "s1", due, proposal
            )
        with self.assertRaises(ActionEventMismatch):
            agency_event_fields(
                "s1",
                due,
                proposal,
                occurred_at=state.world_state.clock,
                location_id="mizuki_home_room",
                channel_id=None,
            )

    def test_no_budget_field_or_keyword_can_reopen_it(self):
        fields = {f.name for f in AgencyBudget.__dataclass_fields__.values()}
        self.assertEqual(
            [name for name in fields if "text" in name or "allow" in name], []
        )
        with self.assertRaises(TypeError):
            AgencyBudget(allow_authored_text=True)

    def test_the_switch_does_not_exist_anywhere_in_the_package(self):
        # 这条盯的是它别悄悄回来。
        for path in sorted(Path(engine_mod.__file__).resolve().parent.parent.parent.rglob("*.py")):
            self.assertNotIn(
                "allow_authored_text",
                path.read_text(encoding="utf-8"),
                f"{path} 里又出现了那个开关",
            )

    def test_the_action_schema_is_kept_for_later_wiring(self):
        # 保留 schema 是刻意的：生成层接上来之后，这两个动作原样可用，
        # 那时这道闸自然消失。它们现在仍然出现在合法枚举里，只是不可提交。
        for action_id in (ActionId.SPEAK_HERE, ActionId.SEND_CHANNEL_MESSAGE):
            self.assertIn(action_id, catalogue())
            self.assertTrue(action_definition(action_id).requires_authored_text)
        state, scheduler, engine = _rig()
        context = engine.context_for(_due(scheduler))
        self.assertTrue(context.has_legal(ActionId.SPEAK_HERE))
        # 但确定性策略挑不到它们。
        self.assertNotIn(
            ActionId.SPEAK_HERE,
            [legal.action_id for legal in context.legal_without_authored_text()],
        )


# ── 存档里的事件被改过 ──────────────────────────────────────────────────
class ArchiveEventTamperingTests(unittest.TestCase):
    """acted 记录指着的那条事件，内容必须真的是这条提案 + 这条到期产出的。

    只核对 event_id 是不够的：它从 proposal_id 推导，保持它正确、改掉事件的
    actor / 类型 / scope / 落点 / payload / provenance，就能拼出一份"审计说
    做了 A、世界历史说发生了 B"而两边 ID 又对得上的存档。
    """

    class _JoinPolicy(AgencyPolicy):
        name = "join"

        def decide(self, context):
            return PolicyDecision(
                proposals=(
                    ActionProposal(
                        proposal_id="p1",
                        character_id=context.character_id,
                        action_id=ActionId.JOIN_CHANNEL,
                        target_id="nightcord",
                    ),
                )
            )

    def setUp(self):
        # ena 不在频道里，所以"加入频道"合法；它不需要台词，落点是频道，
        # 在场名单是提交那一刻的成员快照。
        self.state, self.scheduler, self.engine = _rig(policy=self._JoinPolicy())
        self.due = _due(self.scheduler, character_id="ena")
        self.record = self.engine.evaluate(self.due)
        self.assertIs(self.record.outcome, AgencyOutcome.ACTED)
        self.archive = self.state.to_dict()

    def _tampered(self, mutate):
        archive = deepcopy(self.archive)
        for entry in archive["events"]["events"]:
            if entry["event_id"] == self.record.event_id:
                mutate(entry)
                break
        else:
            self.fail("存档里找不到那条 agency 事件")
        return archive

    def _assert_refused(self, mutate):
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(self._tampered(mutate))

    def test_the_untampered_archive_restores(self):
        restored = SessionState.from_dict(deepcopy(self.archive))
        self.assertEqual(restored.to_dict(), self.archive)

    def test_a_tampered_actor_is_refused(self):
        def swap_actor(event):
            event["actor_id"] = "mizuki"

        self._assert_refused(swap_actor)

    def test_a_tampered_event_type_is_refused(self):
        # 换成一个**结构上同样合法**的类型：join → leave 只差一个词，
        # Event 自己校验不出来，世界历史里却是完全相反的一件事。
        def swap_type(event):
            event["type"] = EventType.PRESENCE_LEFT_CHANNEL.value

        self._assert_refused(swap_type)

    def test_a_tampered_scope_is_refused(self):
        def widen_scope(event):
            event["scope"] = EventScope.PUBLIC.value

        self._assert_refused(widen_scope)

    def test_a_tampered_target_landing_is_refused(self):
        def move_channel(event):
            event["channel_id"] = "some_other_channel"

        self._assert_refused(move_channel)

    def test_a_smuggled_location_on_a_channel_action_is_refused(self):
        def add_location(event):
            event["location_id"] = "city_streets"

        self._assert_refused(add_location)

    def test_a_tampered_payload_is_refused(self):
        # 这个动作的目录声明不接受任何 payload 键。塞一句话进去，恢复出来的
        # 世界历史里就多了一句谁也没审计过的台词。
        def inject_text(event):
            event["payload"] = {"text": "我其实说了这句"}

        self._assert_refused(inject_text)

    def test_a_tampered_participant_roster_is_refused(self):
        # join 的前置条件是"当时还不在频道里"，所以快照里不可能有它自己。
        # 快照本身事后不可重新推导，但这条关系是从声明推出来的。
        def add_actor(event):
            event["participants"] = sorted(set(event["participants"]) | {"ena"})

        self._assert_refused(add_actor)

    def test_a_roster_missing_a_required_actor_is_refused(self):
        # 在场名单的约束是双向的：join 要求执行者当时不在里面，leave 要求它
        # 当时就在。只盯住一个方向，另一个方向的篡改就能过。
        state, scheduler, engine = _rig(
            policy=ScriptedPolicy({}),
        )
        due = _due(scheduler)
        record = engine.commit(
            ProposalPlan(
                due=due,
                character_id="mizuki",
                policy="manual",
                proposed_at=state.world_state.clock,
                verdict=AgencyOutcome.ACTED,
                proposal=ActionProposal(
                    proposal_id="p1",
                    character_id="mizuki",
                    action_id=ActionId.LEAVE_CHANNEL,
                    target_id="nightcord",
                ),
            )
        )
        self.assertIs(record.outcome, AgencyOutcome.ACTED)
        archive = state.to_dict()
        for entry in archive["events"]["events"]:
            if entry["event_id"] == record.event_id:
                self.assertEqual(entry["participants"], ["mizuki"])
                entry["participants"] = []
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(archive)

    def test_a_tampered_occurrence_time_is_refused(self):
        def shift_earlier(event):
            event["occurred_at"] = "2026-08-21T23:55:00"

        self._assert_refused(shift_earlier)

    def test_a_tampered_correlation_id_is_refused(self):
        def other_session(event):
            event["correlation_id"] = "some_other_session"

        self._assert_refused(other_session)

    def test_every_agency_provenance_field_is_checked(self):
        forgeries = {
            "kind": "generation",
            "session_id": "some_other_session",
            "due_id": "ghost@2026-08-22T00:00:00",
            "activation_id": "some_other_activation",
            "proposal_id": "some_other_proposal",
            "action_id": ActionId.MOVE_TO.value,
            "policy": "some_other_policy",
        }
        for key, forged in forgeries.items():
            with self.subTest(provenance=key):
                def forge(event, key=key, forged=forged):
                    event["provenance"] = {**event["provenance"], key: forged}

                self._assert_refused(forge)

    def test_a_dropped_provenance_field_is_refused(self):
        def drop(event):
            provenance = dict(event["provenance"])
            provenance.pop("proposal_id")
            event["provenance"] = provenance

        self._assert_refused(drop)

    def test_a_record_whose_due_belongs_to_another_character_is_refused(self):
        archive = deepcopy(self.archive)
        archive["agency"]["log"]["records"][0]["character_id"] = "mizuki"
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(archive)

    def test_a_tampered_policy_name_on_the_record_is_refused(self):
        # 记录里的策略名和事件 provenance 里的必须是同一个：只改一边，审计就
        # 会说这个动作是另一个策略选的。
        archive = deepcopy(self.archive)
        archive["agency"]["log"]["records"][0]["policy"] = "someone_else"
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(archive)

    def test_construction_and_verification_share_one_definition(self):
        # 校验走的就是当初构造它的那段声明：想放宽校验，得先放宽构造。
        event = self.state.events.get(self.record.event_id)
        verify_agency_event(
            event,
            self.state.session_id,
            self.due,
            self.record.proposal,
            occurred_at=self.record.decided_at,
            policy=self.record.policy,
        )
        rebuilt = agency_event_fields(
            self.state.session_id,
            self.due,
            self.record.proposal,
            occurred_at=self.record.decided_at,
            location_id=None,
            channel_id="nightcord",
            participants=tuple(event.participants),
            policy=self.record.policy,
        )
        self.assertEqual(rebuilt["payload"], dict(event.payload))
        self.assertEqual(rebuilt["provenance"], dict(event.provenance))
        self.assertEqual(rebuilt["event_id"], event.event_id)


class ParticipantSourceTests(unittest.TestCase):
    """在场名单的来源是声明出来的，构造和校验读的是同一条。"""

    def test_every_action_declares_where_its_roster_comes_from(self):
        for action_id in catalogue_ids():
            definition = action_definition(action_id)
            self.assertIsInstance(definition.participants_from, ParticipantSource)

    def test_a_move_declares_an_empty_roster_and_it_is_checkable(self):
        self.assertIs(
            action_definition(ActionId.MOVE_TO).participants_from,
            ParticipantSource.NONE,
        )
        state, scheduler, engine = _rig(policy=FirstLegalActionPolicy())
        record = engine.evaluate(_due(scheduler))
        self.assertEqual(state.events.get(record.event_id).participants, ())
        archive = state.to_dict()
        for entry in archive["events"]["events"]:
            if entry["event_id"] == record.event_id:
                entry["participants"] = ["ena"]
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(archive)


# ── AC11 归属与隔离 ─────────────────────────────────────────────────────
class OwnershipTests(unittest.TestCase):
    def test_a_session_accepts_only_one_engine(self):
        state, _, engine = _rig()
        with self.assertRaises(AgencyEngineError):
            AgencyEngine(state)
        self.assertIs(state.agency_engine, engine)

    def test_the_engine_log_is_the_session_log(self):
        state, _, engine = _rig()
        self.assertIs(engine.log, state.agency)

    def test_an_engine_needs_an_authoritative_world(self):
        state = SessionState(session_id="s", scene="x", characters=["mizuki"])
        with self.assertRaises(AgencyEngineError):
            AgencyEngine(state)

    def test_two_sessions_do_not_see_each_other(self):
        first_state, first_scheduler, first_engine = _rig(
            policy=FirstLegalActionPolicy(), session_id="s1"
        )
        second_state, second_scheduler, second_engine = _rig(
            policy=FirstLegalActionPolicy(), session_id="s2"
        )
        first_engine.evaluate(_due(first_scheduler))
        self.assertEqual(len(first_state.agency), 1)
        self.assertEqual(len(second_state.agency), 0)
        self.assertEqual(len(second_state.events), 0)
        self.assertEqual(
            second_state.world_state.location_of("mizuki"), "mizuki_home_room"
        )

    def test_the_engine_refuses_a_bad_policy_or_budget(self):
        state = _session()
        with self.assertRaises(AgencyEngineError):
            AgencyEngine(state, policy=object())
        state = _session()
        with self.assertRaises(AgencyEngineError):
            AgencyEngine(state, budget={"max_legal_actions": 3})

    def test_the_debug_projection_is_json_safe_and_system_side(self):
        state, scheduler, engine = _rig(policy=FirstLegalActionPolicy())
        engine.evaluate(_due(scheduler))
        projection = engine.debug_projection()
        json.dumps(projection)
        self.assertEqual(projection["committed_actions"], 1)
        self.assertEqual(projection["outcomes"]["acted"], 1)


# ── AC12 序列化 ─────────────────────────────────────────────────────────
class SerializationTests(unittest.TestCase):
    def setUp(self):
        self.state, self.scheduler, self.engine = _rig(policy=FirstLegalActionPolicy())
        self.acted = self.engine.evaluate(_due(self.scheduler, "a1", minutes=10))
        self.state.world_state.set_availability("mizuki", Availability.ASLEEP)
        self.abstained = self.engine.evaluate(_due(self.scheduler, "a2", minutes=10))
        self.archive = self.state.to_dict()

    def test_the_archive_round_trips_through_the_production_path(self):
        restored = SessionState.from_dict(deepcopy(self.archive))
        self.assertEqual(restored.to_dict(), self.archive)
        self.assertEqual(len(restored.agency), 2)
        self.assertEqual(restored.agency.get(self.acted.due_id), self.acted)
        self.assertEqual(restored.agency.committed_actions(), 1)

    def test_an_archive_without_the_agency_section_is_rejected(self):
        broken = deepcopy(self.archive)
        broken.pop("agency")
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(broken)

    def test_an_archive_that_lost_only_the_log_is_rejected(self):
        broken = deepcopy(self.archive)
        broken["agency"].pop("log")
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(broken)

    def test_pieces_from_different_moments_cannot_be_stitched_together(self):
        def agency_from_another_moment(archive):
            archive["agency"]["clock"] = "2026-08-22T05:00:00"

        def another_session(archive):
            archive["agency"]["session_id"] = "someone_else"

        def decided_in_the_future(archive):
            archive["agency"]["log"]["records"][0]["decided_at"] = (
                "2026-08-23T00:00:00"
            )

        def unknown_due(archive):
            archive["agency"]["log"]["records"][0]["due_id"] = "ghost@2026-08-22T00:00:00"

        def unacknowledged_due(archive):
            due_id = archive["agency"]["log"]["records"][0]["due_id"]
            for entry in archive["scheduler"]["outbox"]["records"]:
                if entry["due_id"] == due_id:
                    entry["acknowledged"] = False

        def event_not_in_history(archive):
            archive["agency"]["log"]["records"][0]["event_id"] = "no_such_event"

        def event_from_somewhere_else(archive):
            # 指向一条**真实存在**的别的事件。只查"这条事件在不在"是拦不住
            # 这种拼接的：审计会说这个动作产出了一件它没做过的事。
            other = archive["events"]["events"][0]["event_id"]
            archive["agency"]["log"]["records"][0]["event_id"] = other

        def out_of_order_sequence(archive):
            archive["agency"]["log"]["records"][0]["sequence"] = 5

        def duplicate_due_id(archive):
            records = archive["agency"]["log"]["records"]
            clone = deepcopy(records[0])
            clone["sequence"] = len(records)
            records.append(clone)

        def decided_before_the_due_fired(archive):
            archive["agency"]["log"]["records"][0]["decided_at"] = "2026-08-21T23:55:00"

        def duplicate_proposal_id(archive):
            records = archive["agency"]["log"]["records"]
            clone = deepcopy(records[0])
            clone["sequence"] = len(records)
            clone["due_id"] = "other@2026-08-22T00:00:00"
            records.append(clone)

        for corrupt in (
            agency_from_another_moment,
            another_session,
            decided_in_the_future,
            unknown_due,
            unacknowledged_due,
            event_not_in_history,
            event_from_somewhere_else,
            out_of_order_sequence,
            duplicate_due_id,
            duplicate_proposal_id,
            decided_before_the_due_fired,
        ):
            with self.subTest(corrupt=corrupt.__name__):
                broken = deepcopy(self.archive)
                corrupt(broken)
                with self.assertRaises(SessionStateError):
                    SessionState.from_dict(broken)

    def test_a_failed_restore_installs_nothing(self):
        broken = deepcopy(self.archive)
        broken["agency"]["log"]["records"][0]["event_id"] = "no_such_event"
        with self.assertRaises(SessionStateError):
            SessionState.from_dict(broken)
        # 原来的会话一点没被碰过。
        self.assertEqual(self.state.to_dict(), self.archive)

    def test_a_session_without_a_world_cannot_hold_agency_records(self):
        state = SessionState(session_id="s", scene="x", characters=["mizuki"])
        with self.assertRaises(SessionStateError):
            state.restore_agency_archive(
                {
                    "session_id": "s",
                    "clock": None,
                    "log": AgencyLog(
                        [
                            AgencyRecord(
                                due_id="d",
                                character_id="mizuki",
                                decided_at=CLOCK,
                                outcome=AgencyOutcome.ABSTAINED,
                            )
                        ]
                    ).to_dict(),
                }
            )

    def test_the_archive_shape_has_exactly_one_definition(self):
        self.assertEqual(self.state.agency_archive(), self.archive["agency"])

    def test_a_record_detail_stays_json_safe_and_frozen(self):
        record = AgencyRecord(
            due_id="d",
            character_id="mizuki",
            decided_at=CLOCK,
            outcome=AgencyOutcome.REJECTED_ILLEGAL,
            detail={"reason": "illegal_action", "failed": ["actor_awake"]},
        )
        with self.assertRaises(TypeError):
            record.detail["reason"] = "changed"
        self.assertEqual(AgencyRecord.from_dict(record.to_dict()), record)
        with self.assertRaises(AgencyError):
            AgencyRecord(
                due_id="d",
                character_id="mizuki",
                decided_at=CLOCK,
                outcome=AgencyOutcome.ABSTAINED,
                detail={"world": object()},
            )


# ── 效果层 ──────────────────────────────────────────────────────────────
class EffectTests(unittest.TestCase):
    def setUp(self):
        self.state, self.scheduler, self.engine = _rig()
        self.due = _due(self.scheduler)

    def _event(self, proposal):
        return event_for_proposal(
            self.state.world_state,
            self.state.events,
            self.state.session_id,
            self.due,
            proposal,
            policy="test",
        )

    def test_a_channel_action_carries_no_location(self):
        event = self._event(
            ActionProposal(
                proposal_id="p",
                character_id="mizuki",
                action_id=ActionId.LEAVE_CHANNEL,
                target_id="nightcord",
            )
        )
        self.assertEqual(event.channel_id, "nightcord")
        self.assertIsNone(event.location_id)
        self.assertIs(event.scope, EventScope.CHANNEL)
        # 频道成员快照，按目录声明取。
        self.assertEqual(event.participants, ("mizuki",))

    def test_a_no_target_action_lands_on_the_current_location(self):
        # speak.here 本阶段不可提交，但它的落点推导仍然要正确 —— 生成层接上
        # 来之后走的就是这条。只读投影不经过构造闸门，所以能单独验它。
        projection = effects_mod.debug_projection(
            self.state.world_state,
            ActionProposal(
                proposal_id="p",
                character_id="mizuki",
                action_id=ActionId.SPEAK_HERE,
                payload={"text": "hi"},
            ),
        )
        self.assertEqual(projection["location_id"], "mizuki_home_room")
        self.assertIsNone(projection["channel_id"])
        self.assertFalse(projection["committable"])

    def test_a_move_carries_no_participant_roster(self):
        # location_id 是目的地，而状态效果还没应用：写一份注定作废的名单
        # 不如不写。
        event = self._event(
            ActionProposal(
                proposal_id="p",
                character_id="mizuki",
                action_id=ActionId.MOVE_TO,
                target_id="mizuki_home",
            )
        )
        self.assertEqual(event.participants, ())
        self.assertEqual(event.location_id, "mizuki_home")

    def test_the_event_id_derivation_has_a_single_definition(self):
        proposal = ActionProposal(
            proposal_id="p",
            character_id="mizuki",
            action_id=ActionId.MOVE_TO,
            target_id="mizuki_home",
        )
        self.assertEqual(
            self._event(proposal).event_id, proposal.derived_event_id("s1")
        )

    def test_the_event_id_is_derived_from_the_proposal(self):
        event = self._event(
            ActionProposal(
                proposal_id="p",
                character_id="mizuki",
                action_id=ActionId.MOVE_TO,
                target_id="mizuki_home",
            )
        )
        self.assertEqual(event.event_id, "s1:agency:p")
        self.assertEqual(event.correlation_id, "s1")

    def test_an_action_without_a_landing_place_refuses_to_become_an_event(self):
        self.state.world_state.remove_character("mizuki")
        with self.assertRaises(AgencyEffectError):
            effects_mod.debug_projection(
                self.state.world_state,
                ActionProposal(
                    proposal_id="p",
                    character_id="mizuki",
                    action_id=ActionId.SPEAK_HERE,
                    payload={"text": "hi"},
                ),
            )

    def test_a_scope_without_participant_semantics_is_refused(self):
        # 目录现在没有 private / participant 档的动作。真加了的话，参与者
        # 名单必须由动作显式声明，不能从在场快照里推 —— 那两档的
        # participants 是授权依据。
        definition = action_definition(ActionId.MOVE_TO)
        private = ActionDefinition(
            action_id=definition.action_id,
            event_type=definition.event_type,
            event_scope=EventScope.PRIVATE,
            target_kind=definition.target_kind,
            preconditions=definition.preconditions,
        )
        proposal = ActionProposal(
            proposal_id="p",
            character_id="mizuki",
            action_id=ActionId.MOVE_TO,
            target_id="mizuki_home",
        )
        with patch.object(
            ActionProposal, "definition", property(lambda self: private)
        ):
            with self.assertRaises(ActionEventMismatch):
                self._event(proposal)


# ── 与曝光层的接合 ──────────────────────────────────────────────────────
class ExposureIntegrationTests(unittest.TestCase):
    """自主动作跟别的已提交事件一样，逐个候选角色判定曝光。"""

    def test_a_committed_action_is_perceived_by_the_people_who_could(self):
        world = _world(join_nightcord=("mizuki", "ena"))
        state = _session(world)
        scheduler = PersistentScheduler(state)
        world.leave_channel("ena", "nightcord")
        engine = AgencyEngine(state, policy=ScriptedPolicy({}))
        due = _due(scheduler, character_id="ena")
        plan = ProposalPlan(
            due=due,
            character_id="ena",
            policy="manual",
            proposed_at=world.clock,
            verdict=AgencyOutcome.ACTED,
            proposal=ActionProposal(
                proposal_id="p1",
                character_id="ena",
                action_id=ActionId.JOIN_CHANNEL,
                target_id="nightcord",
            ),
        )
        record = engine.commit(plan)
        self.assertIs(record.outcome, AgencyOutcome.ACTED)
        observers = set(state.observations.observers_of(record.event_id))
        self.assertEqual(observers, {"mizuki", "ena"})

    def test_a_committed_action_is_not_perceived_by_someone_out_of_range(self):
        state, scheduler, engine = _rig(policy=_MovePolicy())
        record = engine.evaluate(_due(scheduler))
        observers = set(state.observations.observers_of(record.event_id))
        self.assertEqual(observers, {"mizuki"})  # ena 在别处
        # 系统侧仍然记下了拒绝，但那是解释通道，不是角色经验。
        decision = state.exposures.explain(record.event_id, "ena")
        self.assertIsNotNone(decision)
        self.assertFalse(decision.exposed)

    def test_provenance_never_reaches_an_observation(self):
        state, scheduler, engine = _rig(policy=_MovePolicy())
        record = engine.evaluate(_due(scheduler))
        for observation in state.observations.for_event(record.event_id):
            blob = json.dumps(observation.to_dict(), ensure_ascii=False)
            for forbidden in ("policy", "proposal_id", "due_id", "provenance"):
                self.assertNotIn(forbidden, blob)
            # 事件 ID 里带着 ":agency:" 是系统侧簿记，它本来就在每条观察的
            # source_event_id 上（P6 起就是如此），但**感知内容**里一个字
            # 都不该有。
            perceived = json.dumps(
                dict(observation.perceived), ensure_ascii=False
            )
            for forbidden in ("agency", "policy", "proposal", "due"):
                self.assertNotIn(forbidden, perceived)


# ── AC10 / AC13 与既有运行时的边界 ──────────────────────────────────────
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
    """确定性研究会话一点没变，而且完全不经过 Agency。"""

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
        # 没有到期资格、没有 Agency 记录、没有绑定引擎。
        self.assertEqual(len(runtime.state.activation_outbox), 0)
        self.assertEqual(len(runtime.state.agency), 0)
        self.assertIsNone(runtime.state.agency_engine)

    async def test_a_research_session_archive_carries_an_empty_agency_section(self):
        runtime = self._create(max_turns=2)
        with patch("pns.runtime.session_runtime.call_character_async", _reply), patch(
            "pns.runtime.session_runtime.judge_async", _judge
        ):
            [m async for m in runtime.run()]
        archive = runtime.state.to_dict()
        self.assertEqual(archive["agency"]["log"], {"records": []})
        restored = SessionState.from_dict(archive)
        self.assertEqual(restored.to_dict(), archive)

    async def test_agency_can_be_attached_to_a_research_session_afterwards(self):
        """自主路径是**另外一条**路：要用就显式接上去，不是默认开着。"""
        runtime = self._create(max_turns=2)
        with patch("pns.runtime.session_runtime.call_character_async", _reply), patch(
            "pns.runtime.session_runtime.judge_async", _judge
        ):
            [m async for m in runtime.run()]
        turns_before = len(runtime.state.turns)
        engine = AgencyEngine(runtime.state, policy=FirstLegalActionPolicy())
        due = _due(runtime.scheduler, "later", minutes=30)
        record = engine.evaluate(due)
        self.assertIs(record.outcome, AgencyOutcome.ACTED)
        # 轮转记录一条没多，Agency 不往生成审计里写东西。
        self.assertEqual(len(runtime.state.turns), turns_before)


class SeparateRuntimePathTests(unittest.TestCase):
    """研究会话的代码路径里没有 Agency —— 静态可证。"""

    def test_session_runtime_does_not_import_agency(self):
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
            [name for name in imported if "agency" in name],
            [],
            "研究会话的轮转路径不该依赖 Agency",
        )

    def test_importing_agency_does_not_initialize_the_reload_boundary(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import pns.runtime.agency; "
                "assert 'pns.runtime.reload' not in sys.modules, "
                "'导入 Agency 顺带拉起了重载边界'; print('ok')",
            ],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_agency_package_holds_no_live_module_level_state(self):
        instances = [
            name
            for name, value in vars(engine_mod).items()
            if isinstance(value, (AgencyEngine, AgencyLog))
        ]
        self.assertEqual(instances, [])


class ReloadCannotTouchAgencyStateTests(RuntimeSessionTestBase, unittest.TestCase):
    """Agency 是 cold update：P7 的配置重载动不了活着的日志。"""

    def setUp(self):
        super().setUp()
        self.boundary = ConfigBoundary(self.supervisor, stop_timeout=0.5)
        self.runtime = self._create(registry=self.boundary.active())
        self.engine = AgencyEngine(self.runtime.state, policy=FirstLegalActionPolicy())
        self.due = _due(self.runtime.scheduler, "morning", minutes=30)
        self.record = self.engine.evaluate(self.due)
        self.before = self.runtime.state.agency.to_dict()

    def tearDown(self):
        self.runtime.close()
        super().tearDown()

    def test_a_successful_reload_leaves_the_agency_log_alone(self):
        self.runtime.close()  # 让重载能等到 idle
        old_registry = self.boundary.active()
        result = self.boundary.reload()
        self.assertEqual(result.status, "ok")
        self.assertIsNot(self.boundary.active(), old_registry)
        self.assertIs(self.runtime.state.agency_engine, self.engine)
        self.assertEqual(self.runtime.state.agency.to_dict(), self.before)

    def test_a_failed_reload_leaves_the_agency_log_alone(self):
        result = self.boundary.reload()  # 会话还活着 → 等不到 idle → 失败
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.runtime.state.agency.to_dict(), self.before)

    def test_the_registry_carries_no_agency_state(self):
        forbidden = {"agency", "policy", "budget", "proposals", "actions"}
        fields = {f.name for f in ContentRegistry.__dataclass_fields__.values()}
        self.assertEqual(fields & forbidden, set())

    def test_the_registry_exposes_no_way_to_propose_or_act(self):
        writers = [
            name
            for name in dir(ContentRegistry)
            if name.startswith(("propose", "act", "decide", "agency", "evaluate"))
        ]
        self.assertEqual(writers, [])


# ── 自查里真正把手伸进去掰断过的那几处 ────────────────────────────────
class AdversarialEdgeTests(unittest.TestCase):
    """这一组每一条都对应一个**当初真的漏掉了**的洞，不是想象出来的边缘。"""

    def _same_tick_dues(self, scheduler, clock):
        """两条同一刻到期的激活 → 一次推进产出两条到期记录。

        这是绕过"只在提案期查一次"的关键形状：两条记录共享同一个提案时刻，
        所以可以先把两个计划都提出来，再逐条提交。
        """
        for character_id in ("mizuki", "ena"):
            scheduler.schedule(
                ScheduledActivation(
                    activation_id=f"a_{character_id}",
                    kind=ActivationKind.CHARACTER_ACTIVATION,
                    due_at=clock + timedelta(minutes=10),
                    character_id=character_id,
                )
            )
        return scheduler.advance_by(10).due

    def test_the_session_ceiling_cannot_be_bypassed_by_proposing_first(self):
        # propose() 是纯的，所以"先全部提案、再逐条提交"是合法用法。只在提案
        # 期查预算的话，两条计划都会看到"已提交 0 个"，上限就形同虚设。
        state, scheduler, engine = _rig(
            policy=FirstLegalActionPolicy(),
            budget=AgencyBudget(max_committed_actions_per_session=1),
        )
        dues = self._same_tick_dues(scheduler, CLOCK)
        plans = [engine.propose(due) for due in dues]
        self.assertTrue(all(plan.would_act for plan in plans))

        outcomes = [engine.commit(plan).outcome for plan in plans]
        self.assertEqual(
            outcomes, [AgencyOutcome.ACTED, AgencyOutcome.REJECTED_BUDGET]
        )
        self.assertEqual(state.agency.committed_actions(), 1)

    def test_a_proposal_id_claimed_by_another_plan_becomes_a_rejection(self):
        # 事件 ID 由提案 ID 推导。两个计划共用一个提案 ID 时，硬走下去会撞上
        # 世界历史的重复 ID，整笔回滚，到期记录卡在"处理不掉也说不清"。
        class _FixedId(AgencyPolicy):
            name = "fixed_id"

            def decide(self, context):
                action = (
                    ActionId.LEAVE_CHANNEL
                    if context.character_id == "mizuki"
                    else ActionId.JOIN_CHANNEL
                )
                return PolicyDecision(
                    proposals=(
                        ActionProposal(
                            proposal_id="SAME",
                            character_id=context.character_id,
                            action_id=action,
                            target_id="nightcord",
                        ),
                    )
                )

        state, scheduler, engine = _rig(policy=_FixedId())
        dues = self._same_tick_dues(scheduler, CLOCK)
        plans = [engine.propose(due) for due in dues]
        records = [engine.commit(plan) for plan in plans]  # 不抛异常
        self.assertEqual(
            [r.outcome for r in records],
            [AgencyOutcome.ACTED, AgencyOutcome.REJECTED_STALE],
        )
        self.assertEqual(records[1].detail["reason"], "duplicate_proposal_id")
        # 两条都被审计了，交接都完成了，世界历史里只多了一条事件。
        self.assertEqual(len(state.agency), 2)
        self.assertEqual(engine.pending_due(), ())
        self.assertEqual(len(state.events.by_type(EventType.PRESENCE_LEFT_CHANNEL)), 1)
        self.assertEqual(len(state.events.by_type(EventType.PRESENCE_JOINED_CHANNEL)), 0)

    def test_a_hand_built_plan_without_a_proposal_is_refused_loudly(self):
        state, scheduler, engine = _rig()
        due = _due(scheduler)
        plan = ProposalPlan(
            due=due,
            character_id="mizuki",
            policy="manual",
            proposed_at=state.world_state.clock,
            verdict=AgencyOutcome.ACTED,
            proposal=None,
        )
        # 以前这里是一句 AttributeError：既没说清哪里错了，也不该由调用方
        # 去猜"NoneType 没有 character_id"是什么意思。
        with self.assertRaises(AgencyEngineError):
            engine.commit(plan)
        self.assertEqual(len(state.agency), 0)

    def test_a_plan_whose_proposal_names_another_actor_never_reaches_the_world(self):
        state, scheduler, engine = _rig()
        due = _due(scheduler)
        plan = ProposalPlan(
            due=due,
            character_id="mizuki",
            policy="manual",
            proposed_at=state.world_state.clock,
            verdict=AgencyOutcome.ACTED,
            proposal=ActionProposal(
                proposal_id="p1",
                character_id="ena",
                action_id=ActionId.JOIN_CHANNEL,
                target_id="nightcord",
            ),
        )
        before = _fingerprint(state)
        with self.assertRaises(AgencyEngineError):
            engine.commit(plan)
        # 拦在事务之前：事件一次都没被提交过，也就谈不上回滚。
        self.assertEqual(_fingerprint(state), before)
        self.assertFalse(state.world_state.is_in_channel("ena", "nightcord"))

    def test_a_plan_for_a_different_character_than_the_due_is_refused(self):
        state, scheduler, engine = _rig()
        due = _due(scheduler)  # 属于 mizuki
        plan = ProposalPlan(
            due=due,
            character_id="ena",
            policy="manual",
            proposed_at=state.world_state.clock,
            verdict=AgencyOutcome.ABSTAINED,
        )
        with self.assertRaises(AgencyEngineError):
            engine.commit(plan)

    def test_an_archive_restore_inside_a_transaction_is_rolled_back(self):
        # restore_agency_archive 整个换掉日志容器。回滚只回滚内容的话，
        # 留下的会是换过之后的那一份 —— 跟 P8 队列/投递箱同一个陷阱。
        state, scheduler, engine = _rig(policy=FirstLegalActionPolicy())
        engine.evaluate(_due(scheduler))
        original = state.agency
        archive = state.agency_archive()

        with self.assertRaises(RuntimeError):
            with state.atomic_commit():
                state.restore_agency_archive(archive)
                self.assertIsNot(state.agency, original)
                raise RuntimeError("boom")

        self.assertIs(state.agency, original)
        self.assertEqual(state.agency.to_dict(), archive["log"])
        self.assertIs(engine.log, state.agency)

    def test_a_policy_cannot_reach_the_world_through_its_context(self):
        seen = {}

        class _Snooper(AgencyPolicy):
            name = "snooper"

            def decide(self, context):
                seen["attrs"] = [
                    name for name in vars(context) if not name.startswith("_")
                ]
                return PolicyDecision()

        state, scheduler, engine = _rig(policy=_Snooper())
        due = _due(scheduler)
        engine.evaluate(due)
        # 上下文里没有任何通往世界、会话或事件历史的字段 —— 策略想改也够不着。
        for forbidden in ("world", "state", "session", "events", "exposures"):
            self.assertNotIn(forbidden, seen["attrs"])
        for value in vars(
            build_agency_context(state.world_state, "mizuki", due)
        ).values():
            self.assertNotIsInstance(value, (WorldState, SessionState))


if __name__ == "__main__":
    unittest.main()
