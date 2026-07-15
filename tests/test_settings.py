import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import settings


class ModelSettingsTests(unittest.TestCase):
    def test_selected_model_is_persisted_without_rewriting_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                "# 保留这条注释\n"
                "current_model: first\n"
                "models:\n"
                "  - name: first\n"
                "  - name: second\n",
                encoding="utf-8",
            )
            original_config = settings.CONFIG
            settings.CONFIG = {
                "current_model": "first",
                "models": [{"name": "first"}, {"name": "second"}],
            }
            try:
                with patch("settings._get_config_path", return_value=config_path):
                    selected = settings.ModelConfig.select("second")
            finally:
                settings.CONFIG = original_config

            self.assertEqual("second", selected["name"])
            content = config_path.read_text(encoding="utf-8")
            self.assertIn("# 保留这条注释", content)
            self.assertIn("current_model: \"second\"", content)


if __name__ == "__main__":
    unittest.main()
