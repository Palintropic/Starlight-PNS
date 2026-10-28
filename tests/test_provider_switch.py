# tests/test_provider_switch.py — 模型设置保存与世界入口的闸门（CFG-1 §5.2）
#
# 守的线：
#   1. 世界入口之间互不挡；切换与世界入口互斥，切换与切换互斥，都是立刻拒绝
#      不排队；异常退出也会放开；
#   2. 切换进行中，创建 / 正式开局 / 恢复三个入口都是 409
#      provider_switch_in_progress，磁盘上不多出存档、本进程不多开世界；
#   3. 反过来，三个入口在"读 registry 之前"和"登记、起 worker 之后、返回之前"
#      两个点上都还持着闸门，这时切换被拒；入口完成后切换照常拿得到。
#
# 运行: python -m unittest tests.test_provider_switch -v
import shutil
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pns.runtime.provider_switch import ProviderSwitchBusy, ProviderSwitchGate  # noqa: E402
from tests.test_formal_world import PlaneTestCase  # noqa: E402

LEGACY = {"world_id": "nightcord", "scene": "nightcord", "characters": ["mizuki", "ena"]}


class GateTests(unittest.TestCase):
    def test_world_entries_do_not_block_each_other(self):
        gate = ProviderSwitchGate()
        with gate.world_entry(), gate.world_entry():
            with self.assertRaises(ProviderSwitchBusy):
                with gate.switch():
                    pass
        with gate.switch():
            pass

    def test_a_switch_refuses_world_entries_and_other_switches(self):
        gate = ProviderSwitchGate()
        with gate.switch():
            self.assertTrue(gate.switching)
            with self.assertRaises(ProviderSwitchBusy):
                with gate.world_entry():
                    pass
            with self.assertRaises(ProviderSwitchBusy):
                with gate.switch():
                    pass
        self.assertFalse(gate.switching)
        with gate.world_entry():
            pass

    def test_failures_inside_release_the_gate(self):
        gate = ProviderSwitchGate()
        for enter in (gate.switch, gate.world_entry):
            with self.assertRaises(KeyError):
                with enter():
                    raise KeyError("boom")
        with gate.switch():
            pass
        with gate.world_entry():
            pass


class PlaneGateTests(PlaneTestCase):
    def make_plane(self, registry_provider=None):
        self.gate = ProviderSwitchGate()
        plane = super().make_plane(registry_provider)
        plane._switch_gate = self.gate
        return plane

    def _closed_legacy_world(self):
        self.assertEqual(self.client.post("/api/persistent-worlds", json=LEGACY).status_code, 201)
        self.assertEqual(self.client.post("/api/persistent-worlds/nightcord/close").status_code, 200)
        self.assertIsNone(self.plane.service.opened("nightcord"))

    def _entries(self):
        return (
            ("create", lambda: self.client.post("/api/persistent-worlds", json=LEGACY)),
            ("bootstrap", lambda: self.client.post("/api/persistent-worlds/yoake-mae/bootstrap")),
            ("restore", lambda: self.client.post("/api/persistent-worlds/nightcord/restore")),
        )

    def test_world_entries_are_refused_while_a_switch_runs(self):
        for name, call in self._entries():
            with self.subTest(name):
                self._fresh(name)
                existed = {w: self.plane.store.exists(w) for w in ("nightcord", "yoake-mae")}
                with self.gate.switch():
                    response = call()
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["detail"]["category"], "provider_switch_in_progress")
                self.assertEqual(
                    {w: self.plane.store.exists(w) for w in ("nightcord", "yoake-mae")}, existed
                )
                self.assertIsNone(self.plane.service.opened("nightcord"))
                self.assertIsNone(self.plane.service.opened("yoake-mae"))
                # 切换结束后同一个入口照常能用。
                self.assertIn(call().status_code, (200, 201))
        self.plane.service.release_all()

    def _fresh(self, name):
        """每一轮从干净的现场开始：没有开着的世界；恢复那一轮有一份关好的存档，其余没有存档。"""
        self.plane.service.release_all()
        for world_id in ("nightcord", "yoake-mae"):
            shutil.rmtree(self.plane.store.root / world_id, ignore_errors=True)
        if name == "restore":
            self._closed_legacy_world()

    def test_a_switch_is_refused_while_an_entry_is_in_progress(self):
        # 两个停顿点：读 registry 时（入口刚开始），和组装返回值时（登记、起 worker
        # 都做完了）。任何一个点上闸门没持着，切换就会插进去。
        points = {
            "registry": ("_registry_provider", self.plane._registry_provider),
            "return": ("_with_driver", self.plane._with_driver),
        }
        for name, call in self._entries():
            for point, (attr, real) in points.items():
                with self.subTest(name, at=point):
                    self._fresh(name)
                    reached, release = threading.Event(), threading.Event()

                    def paused(*args, real=real, **kwargs):
                        reached.set()
                        release.wait(10)
                        return real(*args, **kwargs)

                    result = {}
                    with patch.object(self.plane, attr, paused):
                        worker = threading.Thread(target=lambda: result.setdefault("response", call()))
                        worker.start()
                        try:
                            self.assertTrue(reached.wait(10))
                            with self.assertRaises(ProviderSwitchBusy):
                                with self.gate.switch():
                                    pass
                        finally:
                            release.set()
                            worker.join(10)
                    self.assertIn(result["response"].status_code, (200, 201), result["response"].text)
                    with self.gate.switch():
                        pass
        self.plane.service.release_all()


if __name__ == "__main__":
    unittest.main()
