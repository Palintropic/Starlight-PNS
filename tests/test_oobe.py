import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import oobe


class WriteEnvTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.env_file = Path(self._tmp.name) / ".env"
        self.provider = {
            "name": "Test Provider",
            "format": "openai",
            "base_url": "https://example.test/v1",
            "key_name": "TEST_API_KEY",
        }

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_independent_generator_and_evaluator_models(self):
        with patch.object(oobe, "ENV_FILE", self.env_file):
            oobe.write_env(
                self.provider,
                "legacy-model",
                "secret",
                generator_model="generator-model",
                evaluator_model="evaluator-model",
            )

        values = dict(
            line.split("=", 1)
            for line in self.env_file.read_text(encoding="utf-8").splitlines()
        )
        self.assertEqual(values["MODEL"], "legacy-model")
        self.assertEqual(values["GENERATOR_MODEL"], "generator-model")
        self.assertEqual(values["EVALUATOR_MODEL"], "evaluator-model")

    def test_reconfiguration_replaces_stale_split_models(self):
        self.env_file.write_text(
            "GENERATOR_MODEL=stale-generator\n"
            "EVALUATOR_MODEL=stale-evaluator\n"
            "UNRELATED=value\n",
            encoding="utf-8",
        )

        with patch.object(oobe, "ENV_FILE", self.env_file):
            oobe.write_env(self.provider, "new-model", "secret")

        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("GENERATOR_MODEL=new-model\n", text)
        self.assertIn("EVALUATOR_MODEL=new-model\n", text)
        self.assertIn("UNRELATED=value\n", text)
        self.assertNotIn("stale-generator", text)
        self.assertNotIn("stale-evaluator", text)


if __name__ == "__main__":
    unittest.main()
