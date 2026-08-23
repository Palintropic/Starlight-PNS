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
import asyncio
import os
import shutil
import tempfile
import threading
import time
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
    ReloadResult,
    SessionAdmissionClosed,
    SessionSupervisor,
)
from pns.runtime.reload import write_and_reload
from pns.runtime.session_runtime import (
    SessionRefusedError,
    SessionRuntime,
    SessionSetupError,
)
from pns.world import codegen
from pns.world.data_module import DataModuleError, evaluate_data_source

REPO_ROOT = Path(__file__).resolve().parent.parent

# 用例里等待旧会话退出的上限。生产默认 60 秒，测试不需要真的等那么久。
STOP_TIMEOUT = 1.0


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
        # codegen 也指到临时文件：World Editor 的保存路径要跟重载读的是同一份。
        return (
            patch.object(cr, "SCENES_PATH", self.scenes),
            patch.object(cr, "FACTS_PATH", self.facts),
            patch.object(cr, "ENV_PATH", self.env),
            patch.object(codegen, "SCENES_PATH", self.scenes),
            patch.object(codegen, "FACTS_PATH", self.facts),
        )

    def set_default_scene(self, scene_id: str) -> None:
        source = self.scenes.read_text(encoding="utf-8")
        source = source.replace('DEFAULT_SCENE = "gate"', f'DEFAULT_SCENE = "{scene_id}"')
        self.scenes.write_text(source, encoding="utf-8")

    def break_syntax(self) -> None:
        self.scenes.write_text("SCENES = {  # 少了右括号\n", encoding="utf-8")

    def add_unmapped_scene(self) -> None:
        """加一个 SCENE_WORLD_MAP 里没有映射的场景（World Editor 能造出来的情况）。

        必须写成字面量的一部分：严格求值器不接受 `SCENES["x"] = ...` 这种下标赋值。
        """
        source = self.scenes.read_text(encoding="utf-8")
        entry = (
            '    "user_authored": {\n'
            '        "id": "user_authored",\n'
            '        "label": "自建场景",\n'
            '        "time": "傍晚 17:30",\n'
            '        "location": "某处",\n'
            '        "weather": "晴",\n'
            '        "day_phase": "evening",\n'
            '        "scene_type": "area_talk",\n'
            '        "lore_tag": "UNVERIFIED",\n'
            '        "trigger": "两个人碰上了。",\n'
            '        "auto_next": None,\n'
            '        "auto_turns": None,\n'
            '    },\n'
            '}\n'
        )
        # SCENES 字面量的收尾大括号在 DEFAULT_SCENE 之前，最后一个 "}\n"
        head, sep, tail = source.rpartition("}\n")
        assert sep, "scenes.py 的形状变了"
        self.scenes.write_text(head + entry + tail, encoding="utf-8")

    def write_scenes_source(self, source: str) -> None:
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
        self.boundary = ConfigBoundary(self.supervisor, stop_timeout=STOP_TIMEOUT)

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

    def test_the_authored_rhythms_are_part_of_the_snapshot(self):
        registry = build_content_registry()
        rhythms = registry.rhythms()
        self.assertIn("mizuki", rhythms)
        self.assertEqual(
            registry.rhythm("mizuki").character_id, "mizuki"
        )
        self.assertIsNone(registry.rhythm("kanade"), "没写作息表是正常的")

    def test_a_broken_rhythm_fails_the_whole_build(self):
        """作息表写错了，整份配置作废 —— 而不是那个角色悄悄少一张表。

        它跟"场景没有世界映射"是同一档：内容之间对不上，必须在切换之前暴露。
        """
        pack = cr.load_pack_data()
        broken = dict(pack)
        characters = dict(pack["characters"])
        entry = dict(characters["mizuki"])
        entry["daily_rhythm"] = [
            {"at": "08:00", "activity": "studying", "location_id": "atlantis"}
        ]
        characters["mizuki"] = entry
        broken["characters"] = characters
        with patch.object(cr, "load_pack_data", return_value=broken):
            with self.assertRaises(ConfigValidationError) as ctx:
                build_content_registry()
        self.assertIn("daily_rhythm", str(ctx.exception))
        # 上一份可用快照原样还在。
        self.assertIn("mizuki", self.boundary.active().rhythms())

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
        self.boundary = ConfigBoundary(self.supervisor, stop_timeout=STOP_TIMEOUT)

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

    async def _reload_while_draining(self, runtime, stream, messages):
        """在后台线程里发起重载，等它把停止信号打上，再把会话排空。

        reload() 现在会一直等到旧会话退出为止，所以它不能跟消费 generator 的
        代码待在同一个线程里 —— 生产环境里前者在 FastAPI 的线程池、后者在事件
        循环上，这里用 to_thread 复现同样的分工。
        """
        task = asyncio.create_task(asyncio.to_thread(self.boundary.reload))
        while runtime.stop_reason is None:
            await asyncio.sleep(0)  # 让出控制权，但不推进 generator
        async for message in stream:
            messages.append(message)
        return await task

    async def test_a_running_session_is_stopped_by_a_reload(self):
        runtime = self._create()
        stream = runtime.run()
        messages = [await anext(stream), await anext(stream)]  # start + generating

        result = await self._reload_while_draining(runtime, stream, messages)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.stopped_sessions, (runtime.session_id,))
        self.assertEqual(result.pending_sessions, ())

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
        messages = [await anext(stream)]
        await self._reload_while_draining(runtime, stream, messages)
        self.assertEqual(self.supervisor.live_session_ids(), [])

    async def test_the_swap_happens_only_after_the_old_session_is_gone(self):
        """新旧配置不并存：切换发生时，旧会话已经退出了。"""
        runtime = self._create()
        old = self.boundary.active()
        stream = runtime.run()
        messages = [await anext(stream)]
        self.disk.set_default_scene("nightcord")

        task = asyncio.create_task(asyncio.to_thread(self.boundary.reload))
        while runtime.stop_reason is None:
            await asyncio.sleep(0)

        # 会话还没排空的这段时间里，生效的必须还是旧配置。
        for _ in range(50):
            await asyncio.sleep(0)
        self.assertIs(self.boundary.active(), old)
        self.assertEqual(self.supervisor.live_session_ids(), [runtime.session_id])

        async for message in stream:
            messages.append(message)
        result = await task

        self.assertEqual(result.status, "ok")
        self.assertIsNot(self.boundary.active(), old)
        self.assertEqual(self.supervisor.live_session_ids(), [])

    async def test_a_session_keeps_its_own_snapshot_across_a_reload(self):
        runtime = self._create()
        original = runtime.registry
        self.disk.set_default_scene("nightcord")
        stream = runtime.run()
        messages = [await anext(stream)]
        await self._reload_while_draining(runtime, stream, messages)

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
        drained = []
        result = await self._reload_while_draining(runtime, stream, drained)
        self.assertEqual(result.status, "ok")

        self.assertEqual(world.clock, before["clock"])
        self.assertEqual(dict(world.character_locations), before["locations"])
        self.assertEqual(sorted(world.channels_for("mizuki")), before["channels"])
        self.assertEqual(len(runtime.state.events), before["events"])
        self.assertEqual(len(runtime.state.observations), before["observations"])


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


class StopConfirmationTests(BoundaryTestBase):
    """重载必须等旧 session 确认退出；等不到就整件事作废。"""

    class FakeSession:
        """只实现 request_stop 的假会话，用来精确控制"什么时候退出"。"""

        def __init__(self):
            self.stopped_with = None

        def request_stop(self, reason):
            self.stopped_with = reason

    def test_reload_waits_until_the_last_session_is_gone(self):
        session = self.FakeSession()
        self.supervisor.admit("s1", session)
        released = threading.Event()

        def release_later():
            # 让 reload 先进到 wait_until_idle 里，再放行。
            for _ in range(200):
                if session.stopped_with is not None:
                    break
                threading.Event().wait(0.005)
            self.supervisor.release("s1")
            released.set()

        threading.Thread(target=release_later, daemon=True).start()
        result = self.boundary.reload()

        self.assertTrue(released.is_set(), "reload 必须等到会话退出之后才返回")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.stopped_sessions, ("s1",))
        self.assertEqual(result.pending_sessions, ())
        self.assertIsNotNone(session.stopped_with)

    def test_a_session_that_refuses_to_stop_fails_the_reload(self):
        good = self.boundary.active()
        session = self.FakeSession()
        self.supervisor.admit("stubborn", session)
        self.disk.set_default_scene("nightcord")

        with patch("pns.runtime.reload.build_content_registry") as build:
            result = self.boundary.reload()

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.pending_sessions, ("stubborn",))
        self.assertIn("stubborn", result.error)
        build.assert_not_called()  # 连构建都不该发生
        self.assertIs(self.boundary.active(), good, "不许切换")
        self.assertEqual(self.boundary.active().default_scene, "gate")
        self.assertTrue(self.supervisor.accepting, "失败之后服务仍要可用")

    def test_the_gate_stays_closed_for_the_whole_wait(self):
        session = self.FakeSession()
        self.supervisor.admit("s1", session)
        seen = []

        def watch():
            for _ in range(200):
                if session.stopped_with is not None:
                    break
                threading.Event().wait(0.005)
            seen.append(self.supervisor.accepting)
            self.supervisor.release("s1")

        threading.Thread(target=watch, daemon=True).start()
        self.boundary.reload()
        self.assertEqual(seen, [False], "等待期间不许放新会话进来")

    def test_supervisor_idle_tracking_survives_repeated_admits(self):
        a, b = self.FakeSession(), self.FakeSession()
        self.supervisor.admit("a", a)
        self.supervisor.admit("b", b)
        self.assertFalse(self.supervisor.wait_until_idle(0.01))
        self.supervisor.release("a")
        self.assertFalse(self.supervisor.wait_until_idle(0.01))
        self.supervisor.release("b")
        self.assertTrue(self.supervisor.wait_until_idle(0.01))


class SafeEvaluationTests(unittest.TestCase):
    """数据文件是严格 AST 白名单求值，不是"禁用 builtins 的 exec"。"""

    REJECTED = {
        "无限循环": "while True:\n    pass\n",
        "for 循环": "for i in ():\n    pass\n",
        "调用表达式": 'SCENES = dict(a=1)',
        "内建绕过": "X = __import__('os')",
        "属性访问": "X = ().__class__.__base__",
        "子类遍历": "X = ().__class__.__base__.__subclasses__",
        "import": "import os\nSCENES = {}",
        "from import": "from os import system\nSCENES = {}",
        "函数定义": "def f():\n    return 1\n",
        "类定义": "class C:\n    pass\n",
        "下标赋值": 'SCENES = {}\nSCENES["x"] = 1',
        "属性赋值": "SCENES = {}\nSCENES.x = 1",
        "推导式": "X = [i for i in ()]",
        "lambda": "X = lambda: 1",
        "f-string": "X = f'{1}'",
        "二元运算": "X = 1 + 1",
        "比较": "X = 1 < 2",
        "条件表达式": "X = 1 if True else 2",
        "海象": "X = (Y := 1)",
        "增量赋值": "X = 1\nX += 1",
        "字典展开": "A = {}\nX = {**A}",
        "星号展开": "A = []\nX = [*A]",
        "with": "with open('x') as f:\n    pass\n",
        "try": "try:\n    X = 1\nexcept Exception:\n    X = 2\n",
        "if": "if True:\n    X = 1\n",
        "解包赋值": "X, Y = 1, 2",
        "未定义的名字": "X = SOMETHING_ELSE",
        "非字符串键": "X = {1: 'a'}",
    }

    def test_every_executable_node_is_rejected(self):
        for label, source in self.REJECTED.items():
            with self.subTest(label=label):
                with self.assertRaises(DataModuleError):
                    evaluate_data_source(source, f"<{label}>")

    def test_an_infinite_loop_is_rejected_without_being_run(self):
        """拒绝发生在求值之前 —— 否则这条用例本身就会挂死。"""
        started = time.monotonic()
        with self.assertRaises(DataModuleError):
            evaluate_data_source("while True:\n    pass\n")
        self.assertLess(time.monotonic() - started, 1.0)

    def test_the_real_data_files_still_evaluate(self):
        for path in (
            REPO_ROOT / "pns" / "world" / "scenes.py",
            REPO_ROOT / "pns" / "world" / "facts.py",
        ):
            namespace = evaluate_data_source(path.read_text(encoding="utf-8"), path.name)
            self.assertTrue(namespace)

    def test_literals_that_should_pass_do_pass(self):
        namespace = evaluate_data_source(
            '"""模块文档字符串。"""\n'
            'TAG = "CANON"\n'
            'DATA = {"a": [1, 2.5, True, None], "b": (1,), "c": {"d": TAG}}\n'
            "NEG = -3\n"
        )
        self.assertEqual(namespace["DATA"]["c"]["d"], "CANON")
        self.assertEqual(namespace["NEG"], -3)

    def test_the_world_editor_uses_the_same_evaluator(self):
        """没有鉴权的写接口必须走同一套白名单，不能各有一份宽严不一的实现。"""
        for label, source in self.REJECTED.items():
            with self.subTest(label=label):
                with self.assertRaises(codegen.CodegenError):
                    codegen.validate_module_source(source, "SCENES")


class DeepFreezeTests(unittest.TestCase):
    """快照必须是深冻结的：浅冻结挡不住改嵌套结构。"""

    @classmethod
    def setUpClass(cls):
        cls.registry = build_content_registry()

    def test_nested_scene_fields_cannot_be_mutated(self):
        gate = self.registry.scenes["gate"]
        with self.assertRaises((TypeError, AttributeError)):
            gate["label"] = "改了"
        with self.assertRaises((TypeError, AttributeError)):
            gate["gate_triggers"]["A"] = "改了"

    def test_nested_settings_cannot_be_mutated(self):
        with self.assertRaises((TypeError, AttributeError)):
            self.registry.settings["simulation"]["session_length"] = 999
        with self.assertRaises((TypeError, AttributeError)):
            self.registry.settings["characters"]["default_pairs"].append(["x", "y"])

    def test_world_facts_cannot_be_mutated(self):
        with self.assertRaises((TypeError, AttributeError)):
            self.registry.world_facts["school"] = "改了"

    def test_character_metadata_cannot_be_mutated(self):
        metadata = self.registry.characters["mizuki"].metadata
        with self.assertRaises((TypeError, AttributeError)):
            metadata["name"] = "改了"
        for value in metadata.values():
            self.assertNotIsInstance(value, (dict, list))

    def test_lists_are_frozen_into_tuples(self):
        pairs = self.registry.settings["characters"]["default_pairs"]
        self.assertIsInstance(pairs, tuple)
        self.assertTrue(all(isinstance(pair, tuple) for pair in pairs))

    def test_readers_get_an_independent_thawed_copy(self):
        first = self.registry.scene("gate")
        first["label"] = "本地改动"
        first["gate_triggers"]["A"] = "本地改动"
        second = self.registry.scene("gate")
        self.assertNotEqual(second["label"], "本地改动")
        self.assertNotEqual(second["gate_triggers"]["A"], "本地改动")
        self.assertIsInstance(second["gate_triggers"], dict)

    def test_snapshots_are_mutable_copies(self):
        scenes = self.registry.scenes_snapshot()
        scenes["gate"]["label"] = "本地改动"
        self.assertNotEqual(self.registry.scenes["gate"]["label"], "本地改动")

        facts = self.registry.facts_snapshot()
        facts["school"] = "本地改动"
        self.assertNotEqual(self.registry.world_facts["school"], "本地改动")

        metadata = self.registry.character_metadata("mizuki")
        metadata["name"] = "本地改动"
        self.assertNotEqual(self.registry.characters["mizuki"].metadata["name"], "本地改动")


class TransactionalSaveTests(BoundaryTestBase):
    """写盘 + 重载是一次事务：没生效，磁盘上就不许留下新内容。"""

    def _save_scenes_source(self, source):
        return write_and_reload(
            self.boundary,
            [self.disk.scenes],
            lambda: codegen.save_scenes_source(source),
            reason="测试保存",
        )

    def test_a_good_save_is_kept(self):
        self.boundary.active()
        source = self.disk.scenes.read_text(encoding="utf-8").replace(
            'DEFAULT_SCENE = "gate"', 'DEFAULT_SCENE = "nightcord"'
        )
        result = self._save_scenes_source(source)

        self.assertEqual(result.status, "ok")
        self.assertIn('DEFAULT_SCENE = "nightcord"', self.disk.scenes.read_text(encoding="utf-8"))
        self.assertEqual(self.boundary.active().default_scene, "nightcord")

    def test_a_save_that_fails_validation_is_rolled_back_on_disk(self):
        good = self.boundary.active()
        before = self.disk.scenes.read_text(encoding="utf-8")
        self.disk.add_unmapped_scene()
        bad = self.disk.scenes.read_text(encoding="utf-8")
        self.disk.write_scenes_source(before)  # 复原，改从事务里写进去

        result = self._save_scenes_source(bad)

        self.assertEqual(result.status, "failed")
        self.assertEqual(self.disk.scenes.read_text(encoding="utf-8"), before)
        self.assertNotIn("user_authored", self.disk.scenes.read_text(encoding="utf-8"))
        self.assertIs(self.boundary.active(), good)

    def test_after_a_failed_save_a_restart_still_reads_the_old_config(self):
        """回滚的意义就在这里：进程重启之后必须还能起得来。"""
        self.boundary.active()
        before = self.disk.scenes.read_text(encoding="utf-8")
        self.disk.add_unmapped_scene()
        bad = self.disk.scenes.read_text(encoding="utf-8")
        self.disk.write_scenes_source(before)

        self.assertEqual(self._save_scenes_source(bad).status, "failed")

        # 模拟重启：全新的边界，只能看到磁盘上的内容。
        fresh = ConfigBoundary(SessionSupervisor(), stop_timeout=STOP_TIMEOUT)
        self.assertEqual(fresh.active().default_scene, "gate")
        self.assertNotIn("user_authored", fresh.active().scenes)

    def test_a_save_during_a_reload_writes_nothing_at_all(self):
        """保存和重载共用同一把互斥锁：拿不到就一个字节都不写。

        如果它们能交错，另一次重载可能刚好读到本次的候选内容并切换上去，
        而这边却以为自己已经回滚了。
        """
        self.boundary.active()
        before = self.disk.scenes.read_text(encoding="utf-8")
        new_source = before.replace('DEFAULT_SCENE = "gate"', 'DEFAULT_SCENE = "nightcord"')

        entered = threading.Event()
        release = threading.Event()
        real_locked = self.boundary._reload_locked

        def slow(reason):
            entered.set()
            release.wait(timeout=5)
            return real_locked(reason)

        with patch.object(self.boundary, "_reload_locked", slow):
            worker = threading.Thread(target=self.boundary.reload)
            worker.start()
            self.assertTrue(entered.wait(timeout=5))

            result = self._save_scenes_source(new_source)
            self.assertEqual(result.status, "busy")
            self.assertEqual(
                self.disk.scenes.read_text(encoding="utf-8"), before,
                "拿不到锁时不许写盘",
            )

            release.set()
            worker.join(timeout=5)

    def test_a_reload_that_returns_not_ok_rolls_the_file_back(self):
        self.boundary.active()
        before = self.disk.scenes.read_text(encoding="utf-8")
        new_source = before.replace('DEFAULT_SCENE = "gate"', 'DEFAULT_SCENE = "nightcord"')

        failed = ReloadResult(status="failed", revision=1, finished_at="now", error="x")
        with patch.object(self.boundary, "_reload_locked", return_value=failed):
            result = self._save_scenes_source(new_source)

        self.assertEqual(result.status, "failed")
        self.assertEqual(self.disk.scenes.read_text(encoding="utf-8"), before)

    def test_a_write_that_explodes_leaves_the_file_untouched(self):
        before = self.disk.scenes.read_text(encoding="utf-8")

        def broken_write():
            self.disk.scenes.write_text("SCENES = {}\nDEFAULT_SCENE = \"gate\"\n", encoding="utf-8")
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            write_and_reload(
                self.boundary, [self.disk.scenes], broken_write, reason="测试保存"
            )
        self.assertEqual(self.disk.scenes.read_text(encoding="utf-8"), before)

    def test_a_file_that_did_not_exist_is_removed_again_on_rollback(self):
        missing = Path(self._tmp.name) / "brand_new.env"
        self.boundary.active()
        failed = ReloadResult(status="failed", revision=1, finished_at="now", error="x")
        with patch.object(self.boundary, "_reload_locked", return_value=failed):
            write_and_reload(
                self.boundary,
                [missing],
                lambda: missing.write_text("A=1\n", encoding="utf-8"),
                reason="测试保存",
            )
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
