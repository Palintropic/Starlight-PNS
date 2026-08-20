# tests/test_config_reload.py — P7 配置重载边界的单测。
#
# 盯住五件事，每一件都是这个边界存在的理由：
#   1. 成功重载会整体换掉配置，并且 revision 前进
#   2. 校验失败必须原样保留 last-known-good，并且服务立刻恢复可用
#   3. 并发重载被拒绝，不排队也不并发执行第二次
#   4. 重载会明确停掉正在跑的 session，期间不接新 session
#   5. 配置重载改不了任何运行时权威状态
#
# 运行: python -m unittest tests.test_config_reload -v
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pns.runtime import content_registry as cr
from pns.runtime.content_registry import (
    ConfigValidationError,
    ContentRegistry,
    build_content_registry,
)
from pns.runtime.reload import (
    ConfigBoundary,
    SessionAdmissionClosed,
    SessionSupervisor,
)
from pns.runtime.session_runtime import (
    SessionRefusedError,
    SessionRuntime,
    SessionSetupError,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class DiskFixture:
    """把真实的 scenes.py / facts.py 复制到临时目录，让测试能改盘上的配置。"""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.scenes = tmp / "scenes.py"
        self.facts = tmp / "facts.py"
        self.env = tmp / ".env"
        shutil.copy2(REPO_ROOT / "pns" / "world" / "scenes.py", self.scenes)
        shutil.copy2(REPO_ROOT / "pns" / "world" / "facts.py", self.facts)
        self.write_env()

    def write_env(self, **overrides) -> None:
        values = {
            "PROVIDER": "test-provider",
            "API_FORMAT": "anthropic",
            "BASE_URL": "https://example.invalid/anthropic",
            "MODEL": "test-model",
            "GENERATOR_MODEL": "test-model",
            "EVALUATOR_MODEL": "test-model",
            "PNS_API_KEY_NAME": "TEST_API_KEY",
            "TEST_API_KEY": "test-key",
        }
        values.update(overrides)
        self.env.write_text(
            "\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding="utf-8"
        )

    def patches(self):
        return (
            patch.object(cr, "SCENES_PATH", self.scenes),
            patch.object(cr, "FACTS_PATH", self.facts),
            patch.object(cr, "ENV_PATH", self.env),
        )

    def set_default_scene(self, scene_id: str) -> None:
        source = self.scenes.read_text(encoding="utf-8")
        source = source.replace('DEFAULT_SCENE = "gate"', f'DEFAULT_SCENE = "{scene_id}"')
        self.scenes.write_text(source, encoding="utf-8")

    def break_syntax(self) -> None:
        self.scenes.write_text("SCENES = {  # 少了右括号\n", encoding="utf-8")

    def add_unmapped_scene(self) -> None:
        """加一个 SCENE_WORLD_MAP 里没有映射的场景（World Editor 能造出来的情况）。"""
        source = self.scenes.read_text(encoding="utf-8")
        entry = (
            'SCENES["user_authored"] = {\n'
            '    "id": "user_authored",\n'
            '    "label": "自建场景",\n'
            '    "time": "傍晚 17:30",\n'
            '    "location": "某处",\n'
            '    "weather": "晴",\n'
            '    "day_phase": "evening",\n'
            '    "scene_type": "area_talk",\n'
            '    "lore_tag": "UNVERIFIED",\n'
            '    "trigger": "两个人碰上了。",\n'
            '    "auto_next": None,\n'
            '    "auto_turns": None,\n'
            '}\n'
        )
        source = source.replace('DEFAULT_SCENE = "gate"', entry + 'DEFAULT_SCENE = "gate"')
        self.scenes.write_text(source, encoding="utf-8")


class BoundaryTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.disk = DiskFixture(Path(self._tmp.name))
        # 成功的构建会把 .env 落进 os.environ，用例之间必须还原，否则夹具里的
        # 假 provider 会渗到别的测试里。
        self._env_backup = dict(os.environ)
        self._patches = self.disk.patches()
        for p in self._patches:
            p.start()
        self.supervisor = SessionSupervisor()
        self.boundary = ConfigBoundary(self.supervisor)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.environ.clear()
        os.environ.update(self._env_backup)
        self._tmp.cleanup()


class BuildEntryPointTests(BoundaryTestBase):
    """单一配置构建入口：它必须是纯的，并且校验完整。"""

    def test_build_reads_from_disk_not_from_imported_modules(self):
        self.disk.set_default_scene("nightcord")
        registry = build_content_registry()
        self.assertEqual(registry.default_scene, "nightcord")

        # 进程里已经 import 的那份 pns.world.scenes 没有被动过。
        import pns.world.scenes as scenes_mod

        self.assertEqual(scenes_mod.DEFAULT_SCENE, "gate")

    def test_build_has_no_side_effects_on_the_active_registry(self):
        before = self.boundary.active()
        self.disk.set_default_scene("nightcord")
        build_content_registry(revision=99)
        self.assertIs(self.boundary.active(), before)
        self.assertEqual(self.boundary.active().default_scene, "gate")

    def test_syntax_error_is_a_validation_error(self):
        self.disk.break_syntax()
        with self.assertRaises(ConfigValidationError):
            build_content_registry()

    def test_a_scene_without_a_world_mapping_fails_the_whole_build(self):
        self.disk.add_unmapped_scene()
        with self.assertRaises(ConfigValidationError) as ctx:
            build_content_registry()
        self.assertIn("user_authored", str(ctx.exception))

    def test_default_scene_must_exist(self):
        self.disk.set_default_scene("no_such_scene")
        with self.assertRaises(ConfigValidationError):
            build_content_registry()

    def test_bad_api_format_fails_the_build(self):
        self.disk.write_env(API_FORMAT="carrier-pigeon")
        with self.assertRaises(ConfigValidationError):
            build_content_registry()

    def test_env_values_reach_the_snapshot(self):
        self.disk.write_env(EVALUATOR_MODEL="another-model")
        registry = build_content_registry()
        self.assertEqual(registry.models.evaluator_model, "another-model")
        self.assertEqual(registry.models.key_name, "TEST_API_KEY")

    def test_a_failed_build_does_not_leak_env_into_the_process(self):
        """失败的重载不能把新 .env 塞进 os.environ —— 否则"仍在用旧配置"是假话。"""
        build_content_registry()
        self.disk.write_env(EVALUATOR_MODEL="leaked-model")
        self.disk.break_syntax()
        with self.assertRaises(ConfigValidationError):
            build_content_registry()
        self.assertNotEqual(os.environ.get("EVALUATOR_MODEL"), "leaked-model")


class SuccessfulReloadTests(BoundaryTestBase):
    def test_reload_swaps_the_registry_and_advances_the_revision(self):
        first = self.boundary.active()
        self.disk.set_default_scene("nightcord")

        result = self.boundary.reload()

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.revision, first.revision + 1)
        self.assertIsNot(self.boundary.active(), first)
        self.assertEqual(self.boundary.active().default_scene, "nightcord")

    def test_the_swap_is_a_whole_registry_not_a_field_patch(self):
        """新配置整体替换旧对象，旧快照本身一个字段都不会被改。"""
        first = self.boundary.active()
        self.disk.set_default_scene("nightcord")
        self.boundary.reload()
        self.assertEqual(first.default_scene, "gate")

    def test_the_gate_is_open_again_after_a_successful_reload(self):
        self.boundary.reload()
        self.assertTrue(self.supervisor.accepting)

    def test_status_reports_the_active_snapshot(self):
        self.boundary.reload()
        status = self.boundary.status()
        self.assertTrue(status["accepting_sessions"])
        self.assertFalse(status["reloading"])
        self.assertEqual(status["last_reload"]["status"], "ok")
        self.assertEqual(status["registry"]["revision"], self.boundary.active().revision)


class FailedReloadTests(BoundaryTestBase):
    def test_a_broken_config_keeps_the_last_known_good_registry(self):
        good = self.boundary.active()
        self.disk.break_syntax()

        result = self.boundary.reload()

        self.assertEqual(result.status, "failed")
        self.assertIsNotNone(result.error)
        self.assertIs(self.boundary.active(), good)
        self.assertEqual(result.revision, good.revision)

    def test_a_failed_reload_does_not_burn_the_revision_number(self):
        good = self.boundary.active()
        self.disk.break_syntax()
        self.boundary.reload()
        DiskFixture.__init__(self.disk, Path(self._tmp.name))  # 恢复成一份好配置
        result = self.boundary.reload()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.revision, good.revision + 1)

    def test_the_service_is_restored_after_a_failed_reload(self):
        self.boundary.active()
        self.disk.break_syntax()
        self.boundary.reload()

        self.assertTrue(self.supervisor.accepting)
        with patch("pns.runtime.session_runtime.router_mod._get_api_key", return_value="k"), \
             patch("pns.runtime.session_runtime.router_mod.create_client", return_value=object()):
            runtime = SessionRuntime.create(
                {"characters": ["mizuki", "ena"]},
                registry=self.boundary.active(),
                supervisor=self.supervisor,
            )
        self.assertIsNotNone(runtime.session_id)
        runtime.close()

    def test_an_unexpected_exception_is_also_a_clean_failure(self):
        good = self.boundary.active()
        with patch.object(cr, "load_pack_data", side_effect=RuntimeError("boom")):
            result = self.boundary.reload()
        self.assertEqual(result.status, "failed")
        self.assertIn("boom", result.error)
        self.assertIs(self.boundary.active(), good)
        self.assertTrue(self.supervisor.accepting)


class ConcurrentReloadTests(BoundaryTestBase):
    def test_a_second_reload_is_refused_while_one_is_running(self):
        self.boundary.active()
        entered = threading.Event()
        release = threading.Event()
        builds = []
        real_build = build_content_registry

        def slow_build(revision=0):
            builds.append(revision)
            entered.set()
            release.wait(timeout=5)
            return real_build(revision=revision)

        with patch("pns.runtime.reload.build_content_registry", slow_build):
            worker_result = {}

            def run_first():
                worker_result["result"] = self.boundary.reload()

            first = threading.Thread(target=run_first)
            first.start()
            self.assertTrue(entered.wait(timeout=5))

            second = self.boundary.reload()
            self.assertEqual(second.status, "busy")
            self.assertIsNotNone(second.error)

            release.set()
            first.join(timeout=5)

        self.assertEqual(worker_result["result"].status, "ok")
        self.assertEqual(len(builds), 1, "第二次重载不该真的去构建配置")

    def test_the_lock_is_released_even_when_the_build_explodes(self):
        self.boundary.active()
        with patch("pns.runtime.reload.build_content_registry", side_effect=RuntimeError("boom")):
            self.assertEqual(self.boundary.reload().status, "failed")
        self.assertEqual(self.boundary.reload().status, "ok")


class AdmissionTests(BoundaryTestBase):
    def test_the_gate_refuses_new_sessions_while_it_is_closed(self):
        self.supervisor.close_gate()
        with patch("pns.runtime.session_runtime.router_mod._get_api_key", return_value="k"), \
             patch("pns.runtime.session_runtime.router_mod.create_client", return_value=object()):
            with self.assertRaises(SessionRefusedError):
                SessionRuntime.create(
                    {"characters": ["mizuki", "ena"]},
                    registry=self.boundary.active(),
                    supervisor=self.supervisor,
                )

    def test_a_refused_session_is_a_setup_error_on_the_wire(self):
        """/ws/run 只 catch SessionSetupError，拒绝必须落在那把伞下面。"""
        self.assertTrue(issubclass(SessionRefusedError, SessionSetupError))

    def test_admission_is_decided_at_registration_not_at_the_early_check(self):
        supervisor = SessionSupervisor()
        supervisor.close_gate()
        with self.assertRaises(SessionAdmissionClosed):
            supervisor.admit("s1", object())
        self.assertEqual(supervisor.live_session_ids(), [])

    def test_a_session_that_missed_a_whole_reload_is_refused(self):
        """在"抓快照"和"登记"之间整段错过一次重载的会话，不许带着旧配置跑起来。"""
        stale = self.boundary.active()
        self.boundary.reload()
        self.assertIsNot(self.boundary.active(), stale)

        with patch("pns.runtime.session_runtime.router_mod._get_api_key", return_value="k"), \
             patch("pns.runtime.session_runtime.router_mod.create_client", return_value=object()), \
             patch("pns.runtime.session_runtime.BOUNDARY.active", return_value=stale):
            with self.assertRaises(SessionRefusedError):
                # registry 不传 => 走全局边界 => 会做登记后的引用复核
                SessionRuntime.create(
                    {"characters": ["mizuki", "ena"]}, supervisor=self.supervisor
                )
        self.assertEqual(self.supervisor.live_session_ids(), [])

    def test_reload_closes_and_reopens_the_gate(self):
        seen = []
        real_build = build_content_registry

        def spy(revision=0):
            seen.append(self.supervisor.accepting)
            return real_build(revision=revision)

        with patch("pns.runtime.reload.build_content_registry", spy):
            self.boundary.reload()
        self.assertEqual(seen, [False], "构建期间闸门必须是关着的")
        self.assertTrue(self.supervisor.accepting)


async def _reply(client, character, *args, **kwargs):
    return f"{character} 说话"


async def _judge(*args, **kwargs):
    return {"drift_score": 0, "is_ooc": False, "confidence": 1.0, "dimensions": {}}


class RunningSessionTests(unittest.IsolatedAsyncioTestCase):
    """重载必须明确停掉正在跑的会话，而且不能污染它们已经持有的配置。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.disk = DiskFixture(tmp)
        self._patches = [
            *self.disk.patches(),
            patch("pns.runtime.session_runtime.router_mod._get_api_key", return_value="k"),
            patch("pns.runtime.session_runtime.router_mod.create_client", return_value=object()),
            patch("pns.runtime.session_runtime.call_character_async", _reply),
            patch("pns.runtime.session_runtime.judge_async", _judge),
        ]
        self._env_backup = dict(os.environ)
        for p in self._patches:
            p.start()
        self.history_dir = tmp / "history"
        self.drift_file = tmp / "drift.jsonl"
        self.supervisor = SessionSupervisor()
        self.boundary = ConfigBoundary(self.supervisor)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.environ.clear()
        os.environ.update(self._env_backup)
        self._tmp.cleanup()

    def _create(self, **params):
        base = {"characters": ["mizuki", "ena"], "max_turns": 6, "api_delay": 0}
        base.update(params)
        return SessionRuntime.create(
            base,
            registry=self.boundary.active(),
            supervisor=self.supervisor,
            history_dir=self.history_dir,
            drift_scores_file=self.drift_file,
        )

    async def test_a_running_session_is_stopped_by_a_reload(self):
        runtime = self._create()
        stream = runtime.run()
        messages = [await anext(stream), await anext(stream)]  # start + generating

        result = self.boundary.reload()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.stopped_sessions, (runtime.session_id,))

        async for message in stream:
            messages.append(message)

        kinds = [m["type"] for m in messages]
        self.assertIn("stopped", kinds)
        self.assertEqual(kinds[-1], "done", "停止之后仍要按协议收尾")
        stopped = next(m for m in messages if m["type"] == "stopped")
        self.assertEqual(stopped["session_id"], runtime.session_id)
        self.assertTrue(stopped["reason"])
        # 停在轮次边界，不是掐在半路：已提交的轮次都是完整的。
        self.assertLess(len(runtime.state.turns), 6)
        self.assertEqual(len(runtime.state.turns), len(runtime.state.events))

    async def test_a_stopped_session_deregisters_itself(self):
        runtime = self._create()
        self.assertEqual(self.supervisor.live_session_ids(), [runtime.session_id])
        stream = runtime.run()
        await anext(stream)
        self.boundary.reload()
        async for _ in stream:
            pass
        self.assertEqual(self.supervisor.live_session_ids(), [])

    async def test_a_session_keeps_its_own_snapshot_across_a_reload(self):
        runtime = self._create()
        original = runtime.registry
        self.disk.set_default_scene("nightcord")
        self.boundary.reload()

        self.assertIs(runtime.registry, original)
        self.assertEqual(runtime.registry.default_scene, "gate")
        self.assertEqual(self.boundary.active().default_scene, "nightcord")

    async def test_a_reload_cannot_touch_authoritative_runtime_state(self):
        """配置重载动不了世界时间、位置、频道成员、事件和观察。"""
        runtime = self._create()
        stream = runtime.run()
        async for message in stream:
            if message["type"] == "turn":
                break

        world = runtime.world
        world.advance_time(45)
        world.place_character("mizuki", "city_streets")
        world.join_channel("mizuki", "nightcord")
        before = {
            "clock": world.clock,
            "locations": dict(world.character_locations),
            "channels": sorted(world.channels_for("mizuki")),
            "events": len(runtime.state.events),
            "observations": len(runtime.state.observations),
        }

        self.disk.set_default_scene("nightcord")
        self.assertEqual(self.boundary.reload().status, "ok")

        self.assertEqual(world.clock, before["clock"])
        self.assertEqual(dict(world.character_locations), before["locations"])
        self.assertEqual(sorted(world.channels_for("mizuki")), before["channels"])
        self.assertEqual(len(runtime.state.events), before["events"])
        self.assertEqual(len(runtime.state.observations), before["observations"])

        async for _ in stream:
            pass


class ClassificationTests(unittest.TestCase):
    """重载边界的分类本身也是被测对象 —— 它不该被悄悄挪动。"""

    def test_the_registry_carries_no_runtime_authoritative_state(self):
        forbidden = {
            "clock", "character_locations", "channel_members", "availability",
            "environment", "events", "event_store", "observations", "history",
            "turns", "relationships", "memories", "world_state",
        }
        fields = {f.name for f in ContentRegistry.__dataclass_fields__.values()}
        self.assertEqual(fields & forbidden, set())

    def test_the_registry_exposes_no_way_to_write_runtime_state(self):
        writers = [
            name
            for name in dir(ContentRegistry)
            if name.startswith(("set_", "place_", "join_", "leave_", "advance_",
                                "record_", "commit_", "append_"))
        ]
        self.assertEqual(writers, [])

    def test_each_session_gets_its_own_structural_objects(self):
        registry = build_content_registry()
        scene = registry.scene("nightcord")
        a = registry.new_world_state(scene, ["mizuki", "ena"])
        b = registry.new_world_state(scene, ["mizuki", "ena"])
        self.assertIsNot(a, b)
        self.assertIsNot(a.locations, b.locations)
        self.assertIsNot(a.channels, b.channels)

        a.place_character("mizuki", "city_streets")
        self.assertNotEqual(
            a.location_of("mizuki"), b.location_of("mizuki"),
            "一个会话的世界状态不能渗到另一个会话里",
        )

    def test_nothing_in_the_runtime_reloads_python_modules(self):
        """代码属于 cold update：跑着的进程里不许重新执行模块。"""
        import ast

        offenders = []
        for path in (REPO_ROOT / "pns").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "reload"
                    and isinstance(func.value, ast.Name)
                    and func.value.id in ("importlib", "imp")
                ):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [])

    def test_the_three_classes_do_not_overlap(self):
        self.assertTrue(cr.RELOADABLE_SOURCES)
        self.assertTrue(cr.COLD_UPDATE_SOURCES)
        self.assertTrue(cr.RUNTIME_AUTHORITATIVE_STATE)
        combined = (
            set(cr.RELOADABLE_SOURCES)
            | set(cr.COLD_UPDATE_SOURCES)
            | set(cr.RUNTIME_AUTHORITATIVE_STATE)
        )
        self.assertEqual(
            len(combined),
            len(cr.RELOADABLE_SOURCES)
            + len(cr.COLD_UPDATE_SOURCES)
            + len(cr.RUNTIME_AUTHORITATIVE_STATE),
        )


if __name__ == "__main__":
    unittest.main()
