"""默认模型不许悄悄退回 mimo-v2.5（北京时间 2026-10-21 10:00 下线）。

v2.5 下线之后，任何一处默认值退回去都会让没在 `.env` 里写模型名的部署
全部调用失败。`.env` 里显式写的模型名仍然优先于默认值。
"""
import inspect
import unittest
from pathlib import Path

import yaml

import pns.logic.router as router
from pns.runtime.content_registry import ModelSettings
from scripts import oobe

ROOT = Path(__file__).resolve().parent.parent
RETIRED = "mimo-v2.5"
DEFAULT = "mimo-v2.6-pro"


class DefaultModelTest(unittest.TestCase):
    def test_settings_default_is_v26(self):
        settings = ModelSettings.from_env({})
        self.assertEqual(settings.generator_model, DEFAULT)
        self.assertEqual(settings.evaluator_model, DEFAULT)

    def test_env_overrides_the_default(self):
        settings = ModelSettings.from_env(
            {"MODEL": "m-base", "GENERATOR_MODEL": "m-gen", "EVALUATOR_MODEL": "m-eval"}
        )
        self.assertEqual(
            (settings.generator_model, settings.evaluator_model), ("m-gen", "m-eval")
        )
        settings = ModelSettings.from_env({"MODEL": "m-base"})
        self.assertEqual(
            (settings.generator_model, settings.evaluator_model), ("m-base", "m-base")
        )

    def test_config_yaml_default_is_v26(self):
        config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(config["api"]["model"], DEFAULT)

    def test_router_fallback_is_not_retired(self):
        source = inspect.getsource(router.judge)
        self.assertNotIn(RETIRED, source)
        self.assertIn(DEFAULT, source)

    def test_oobe_offers_no_retired_mimo_model(self):
        for provider in oobe.PROVIDERS.values():
            for model in provider.get("models", []):
                self.assertFalse(model.startswith(RETIRED), (provider["name"], model))


if __name__ == "__main__":
    unittest.main()
