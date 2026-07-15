import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from AgentRecord import settings
from AgentRecord.analysis import context, information


class InformationBriefingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.original_diary_dir = settings.DIARY_DIR
        self.original_analysis_dir = settings.ANALYSIS_DIR
        self.original_third_search = settings.CONFIG.get("third_search")
        settings.DIARY_DIR = root / "Records"
        settings.ANALYSIS_DIR = root / "AnalysisReports"
        settings.DIARY_DIR.mkdir()
        settings.CONFIG["third_search"] = {
            "enabled": True,
            "api_key": "test-key",
        }

    def tearDown(self):
        settings.DIARY_DIR = self.original_diary_dir
        settings.ANALYSIS_DIR = self.original_analysis_dir
        if self.original_third_search is None:
            settings.CONFIG.pop("third_search", None)
        else:
            settings.CONFIG["third_search"] = self.original_third_search
        self.temp_dir.cleanup()

    def test_briefing_searches_general_and_sanitized_week_topics(self):
        date = datetime.date(2026, 7, 15)
        (settings.DIARY_DIR / "2026-07-14.md").write_text(
            "# 2026-07-14\n\n<summary>\n\n</summary>\n\n"
            "---\n## 原始记录流\n\n"
            "**09:00:** 研究个人知识管理 user@example.com /mnt/private/a.md\n",
            encoding="utf-8",
        )
        collector_prompts = []

        def fake_call_ai(prompt, model_config, *, allowed_tools=None):
            if allowed_tools == ():
                payload = {
                    "queries": [
                        {
                            "query": "个人知识管理 user@example.com /mnt/private/a.md",
                            "reason": "查找方法",
                        }
                    ]
                }
                return json.dumps(payload), True, 0, {}, 0
            collector_prompts.append(prompt)
            return (
                "## 今日值得关注\n\n新资料 [来源](https://example.com/news)\n\n"
                "## 与本周思考相关的探索\n\n可延伸。\n\n"
                "## 可继续追踪\n\n后续更新。",
                True,
                0,
                {"web_search": 2},
                12,
            )

        with patch.object(information, "call_ai", side_effect=fake_call_ai):
            _, success, path = information.generate_information_briefing(
                date, {"name": "mock", "search": False}
            )

        self.assertTrue(success)
        self.assertEqual(
            settings.ANALYSIS_DIR / "Information" / "2026-07-15.md", path
        )
        self.assertIn("# 2026-07-15 每日信息简报", path.read_text(encoding="utf-8"))
        self.assertNotIn("user@example.com", collector_prompts[0])
        self.assertNotIn("/mnt/private", collector_prompts[0])
        self.assertIn("[email]", collector_prompts[0])
        self.assertIn("[local-path]", collector_prompts[0])

    def test_briefing_fails_when_web_search_is_not_configured(self):
        settings.CONFIG["third_search"] = {"enabled": False, "api_key": ""}

        message, success, path = information.generate_information_briefing(
            datetime.date(2026, 7, 15), {"name": "mock", "search": False}
        )

        self.assertFalse(success)
        self.assertIsNone(path)
        self.assertIn("未启用联网能力", message)

    def test_period_information_context_reads_only_matching_dates(self):
        directory = settings.ANALYSIS_DIR / "Information"
        directory.mkdir(parents=True)
        (directory / "2026-07-14.md").write_text("本周简报", encoding="utf-8")
        (directory / "2026-07-01.md").write_text("过期简报", encoding="utf-8")

        result = context._information_briefings(
            datetime.date(2026, 7, 13), datetime.date(2026, 7, 19)
        )

        self.assertIn("本周简报", result)
        self.assertNotIn("过期简报", result)


if __name__ == "__main__":
    unittest.main()
