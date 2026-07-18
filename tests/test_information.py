import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from AgentRecord import settings
from AgentRecord.analysis import context, information


class InformationBriefingTests(unittest.TestCase):
    @staticmethod
    def valid_briefing(exploration: str = "可延伸。") -> str:
        highlights = "\n\n".join(
            f"### {number}. 事项 {number}\n\n新资料 [来源](https://example.com/{number})"
            for number in range(1, 6)
        )
        return (
            f"## 今日值得关注\n\n{highlights}\n\n"
            f"## 与本周思考相关的探索\n\n{exploration}\n\n"
            "## 可继续追踪\n\n后续更新。"
        )

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
                self.valid_briefing(),
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
        self.assertIn(
            "agentrecord-targeted-queries", path.read_text(encoding="utf-8")
        )
        self.assertNotIn("user@example.com", collector_prompts[0])
        self.assertNotIn("/mnt/private", collector_prompts[0])
        self.assertIn("[email]", collector_prompts[0])
        self.assertIn("[local-path]", collector_prompts[0])

    def test_briefing_uses_same_week_reports_and_drops_repeated_queries(self):
        date = datetime.date(2026, 7, 16)
        (settings.DIARY_DIR / "2026-07-16.md").write_text(
            "# 2026-07-16\n\n<summary>\n\n</summary>\n\n"
            "---\n## 原始记录流\n\n"
            "**09:00:** 继续考虑个人知识管理和本地优先软件。\n",
            encoding="utf-8",
        )
        information_dir = settings.ANALYSIS_DIR / "Information"
        information_dir.mkdir(parents=True)
        (information_dir / "2026-07-15.md").write_text(
            "# 2026-07-15 每日信息简报\n\n"
            '<!-- agentrecord-targeted-queries: [{"query":"个人知识管理 方法研究",'
            '"reason":"比较不同方法"}] -->\n\n'
            "## 今日值得关注\n\n昨日已讨论知识管理方法。\n",
            encoding="utf-8",
        )
        planner_prompts = []
        collector_prompts = []

        def fake_call_ai(prompt, model_config, *, allowed_tools=None):
            if allowed_tools == ():
                planner_prompts.append(prompt)
                return (
                    json.dumps(
                        {
                            "queries": [
                                {
                                    "query": "个人知识管理最新进展",
                                    "reason": "继续搜索相同主题",
                                },
                                {
                                    "query": "本地优先笔记软件数据迁移风险",
                                    "reason": "核查一个尚未覆盖的新角度",
                                },
                            ]
                        },
                        ensure_ascii=False,
                    ),
                    True,
                    0,
                    {},
                    0,
                )
            collector_prompts.append(prompt)
            return (
                self.valid_briefing("新增内容。"),
                True,
                0,
                {"web_search": 3},
                8,
            )

        with patch.object(information, "call_ai", side_effect=fake_call_ai):
            _, success, path = information.generate_information_briefing(
                date, {"name": "mock", "search": False}
            )

        self.assertTrue(success)
        self.assertIn("昨日已讨论知识管理方法", planner_prompts[0])
        self.assertIn("个人知识管理 方法研究", planner_prompts[0])
        query_block = collector_prompts[0].split("【已去隐私的查询】", 1)[1].split(
            "【本周此前的信息简报", 1
        )[0]
        self.assertNotIn("个人知识管理最新进展", query_block)
        self.assertIn("本地优先笔记软件数据迁移风险", query_block)
        generated = path.read_text(encoding="utf-8")
        self.assertNotIn("个人知识管理最新进展", generated)
        self.assertIn("本地优先笔记软件数据迁移风险", generated)

    def test_invalid_query_json_uses_bounded_correction(self):
        date = datetime.date(2026, 7, 15)
        (settings.DIARY_DIR / "2026-07-15.md").write_text(
            "# 2026-07-15\n\n<summary>\n\n</summary>\n\n"
            "---\n## 原始记录流\n\n**09:00:** 研究一个问题。\n",
            encoding="utf-8",
        )

        with patch.object(
            information,
            "call_ai",
            return_value=("不是 JSON", True, 0, {}, 0),
        ) as call:
            message, success, path = information.generate_information_briefing(
                date, {"name": "mock", "search": False}
            )

        self.assertFalse(success)
        self.assertIsNone(path)
        self.assertIn("没有返回有效 JSON", message)
        self.assertEqual(3, call.call_count)
        retry_prompt = call.call_args.args[0]
        self.assertIn("【中控修订请求】", retry_prompt)
        self.assertIn("不是 JSON", retry_prompt)

    def test_today_highlights_require_exactly_five_numbered_items(self):
        self.assertTrue(information._has_five_daily_highlights(self.valid_briefing()))
        only_four = self.valid_briefing().replace(
            "### 5. 事项 5\n\n新资料 [来源](https://example.com/5)\n\n", ""
        )
        self.assertFalse(information._has_five_daily_highlights(only_four))

    def test_invalid_briefing_is_revised_with_original_draft_and_reason(self):
        date = datetime.date(2026, 7, 15)
        responses = [
            ("缺少章节和链接", True, 0, {"web_search": 1}, 5),
            (self.valid_briefing(), True, 0, {"web_search": 2}, 8),
        ]

        with patch.object(information, "call_ai", side_effect=responses) as call:
            _, success, path = information.generate_information_briefing(
                date, {"name": "mock", "search": False}
            )

        self.assertTrue(success)
        self.assertTrue(path.exists())
        self.assertEqual(2, call.call_count)
        original_prompt = call.call_args_list[0].args[0]
        revision_prompt = call.call_args_list[1].args[0]
        self.assertTrue(revision_prompt.startswith(original_prompt))
        self.assertIn("缺少章节和链接", revision_prompt)
        self.assertIn("缺少必需章节", revision_prompt)

    def test_briefing_fails_when_web_search_is_not_configured(self):
        settings.CONFIG["third_search"] = {"enabled": False, "api_key": ""}

        message, success, path = information.generate_information_briefing(
            datetime.date(2026, 7, 15), {"name": "mock", "search": False}
        )

        self.assertFalse(success)
        self.assertIsNone(path)
        self.assertIn("未启用联网能力", message)

    def test_query_dedup_allows_a_concrete_new_event_in_the_same_field(self):
        result = information._deduplicate_queries(
            [
                {"query": "个人知识管理最新进展", "reason": "泛化重复"},
                {
                    "query": "个人知识管理 Obsidian 插件安全事件影响",
                    "reason": "出现了具体的新事件",
                },
            ],
            [{"query": "个人知识管理 方法研究", "reason": "已有主题"}],
        )

        self.assertEqual(
            ["个人知识管理 Obsidian 插件安全事件影响"],
            [item["query"] for item in result],
        )

    def test_prior_briefings_exclude_target_date_and_previous_week(self):
        directory = settings.ANALYSIS_DIR / "Information"
        directory.mkdir(parents=True)
        (directory / "2026-07-12.md").write_text("上周简报", encoding="utf-8")
        (directory / "2026-07-15.md").write_text("本周昨日简报", encoding="utf-8")
        (directory / "2026-07-16.md").write_text("当天旧简报", encoding="utf-8")

        context_text, _ = information._prior_week_briefings(
            datetime.date(2026, 7, 16)
        )

        self.assertIn("本周昨日简报", context_text)
        self.assertNotIn("上周简报", context_text)
        self.assertNotIn("当天旧简报", context_text)

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
