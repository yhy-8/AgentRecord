import datetime
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from AgentRecord import settings
from AgentRecord.ai_client import AIResponse, ToolResult
from AgentRecord.analysis import context, information


class InformationBriefingTests(unittest.TestCase):
    @staticmethod
    def valid_briefing(
        exploration: str = "可延伸。",
        targeted_ids: tuple[str, ...] = (),
        targeted_refs: tuple[str, ...] = (),
    ) -> str:
        highlights = "\n\n".join(
            f"### {number}. 具体事项 {number}\n\n"
            f"**具体变化**：事项 {number} 已经发布明确结果，"
            "相关机构确认变化已经生效，不是尚待发生的日程预告。\n\n"
            f"**关键细节**：结果包含数据 {number}0 与数据 {number}5，"
            "分别对应两个可核查对象，并给出了明确发布日期和适用范围。"
            f" [来源](https://example.com/{number})\n\n"
            "**关注理由**：这些细节会直接改变相关对象当前的判断依据，"
            "后续可用同一口径核对执行结果，而不是泛泛讨论长期影响。"
            for number in range(1, 6)
        )
        if targeted_ids:
            refs = targeted_refs or tuple(
                "R-20260715-001-aaaaaaaaaaaa" for _ in targeted_ids
            )
            exploration = "\n\n".join(
                f"### {topic_id}. 选题\n\n{exploration} "
                f"\n\n本周记录依据：[{refs[index - 1]}]\n\n"
                f"[来源](https://example.com/target-{index})"
                for index, topic_id in enumerate(targeted_ids, 1)
            )
        return (
            f"## 今日值得关注\n\n{highlights}\n\n"
            f"## 与本周思考相关的探索\n\n{exploration}\n\n"
            "## 可继续追踪\n\n后续更新。"
        )

    @staticmethod
    def audited_response(
        markdown: str,
        allowed_search_queries: tuple[str, ...] | list[str] | None = None,
    ) -> AIResponse:
        urls = re.findall(r"https://example\.com/[\w-]+", markdown) or [
            f"https://example.com/{number}" for number in range(1, 6)
        ]
        targeted_queries = list((allowed_search_queries or ())[2:])
        telemetry = {
            "search_evidence": [
                {
                    "url": url,
                    "query": (
                        targeted_queries[int(url.rsplit("-", 1)[1]) - 1]
                        if "/target-" in url
                        and int(url.rsplit("-", 1)[1]) <= len(targeted_queries)
                        else str((allowed_search_queries or ("",))[0])
                    ),
                    "title": "来源",
                }
                for url in dict.fromkeys(urls)
            ]
        }
        if allowed_search_queries is not None:
            telemetry["completed_search_queries"] = [
                information._normalized_query(query)
                for query in allowed_search_queries
            ]
        return AIResponse(
            markdown,
            True,
            len(urls),
            {"web_search": len(allowed_search_queries or ())},
            len(urls),
            telemetry=telemetry,
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
            "api_url": "https://search.example.test",
        }
        evidence = [
            {
                "title": f"来源 {number}",
                "url": f"https://example.com/{number}",
                "snippet": "摘要",
                "published": "2026-07-15",
            }
            for number in range(1, 6)
        ] + [
            {
                "title": f"定向来源 {number}",
                "url": f"https://example.com/target-{number}",
                "snippet": "定向摘要",
                "published": "2026-07-15",
            }
            for number in range(1, 4)
        ]
        self.search_patch = patch.object(
            information,
            "search_web_once",
            side_effect=lambda query: (
                ToolResult(
                    "\n".join(
                        f"[{item['title']}]({item['url']})" for item in evidence
                    ),
                    len(evidence),
                    evidence,
                ),
                "",
            ),
        )
        self.search_mock = self.search_patch.start()

    def tearDown(self):
        self.search_patch.stop()
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
        record_refs = []

        def fake_call_ai(
            prompt,
            model_config,
            *,
            allowed_tools=None,
            allowed_search_queries=None,
        ):
            if prompt.startswith("[程序每日信息选题任务]"):
                record_refs[:] = information._RECORD_REF_PATTERN.findall(prompt)[:1]
                payload = {
                    "queries": [
                        {
                            "query": "个人知识管理 user@example.com /mnt/private/a.md",
                            "reason": "查找方法",
                            "record_refs": record_refs,
                        }
                    ]
                }
                return json.dumps(payload), True, 0, {}, 0
            collector_prompts.append(prompt)
            return self.audited_response(
                self.valid_briefing(
                    targeted_ids=("T001",),
                    targeted_refs=tuple(record_refs),
                ),
                allowed_search_queries,
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
        record_refs = []

        def fake_call_ai(
            prompt,
            model_config,
            *,
            allowed_tools=None,
            allowed_search_queries=None,
        ):
            if prompt.startswith("[程序每日信息选题任务]"):
                planner_prompts.append(prompt)
                record_refs[:] = information._RECORD_REF_PATTERN.findall(prompt)[:1]
                return (
                    json.dumps(
                        {
                            "queries": [
                                {
                                    "query": "个人知识管理最新进展",
                                    "reason": "继续搜索相同主题",
                                    "record_refs": record_refs,
                                },
                                {
                                    "query": "本地优先笔记软件数据迁移风险",
                                    "reason": "核查一个尚未覆盖的新角度",
                                    "record_refs": record_refs,
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
            return self.audited_response(
                self.valid_briefing(
                    "新增内容。",
                    ("T001",),
                    tuple(record_refs),
                ),
                allowed_search_queries,
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

    def test_targeted_query_must_cite_a_current_week_record(self):
        date = datetime.date(2026, 7, 15)
        (settings.DIARY_DIR / "2026-07-15.md").write_text(
            "**09:00:** 本周记录中的具体想法。\n", encoding="utf-8"
        )
        old_briefing = settings.ANALYSIS_DIR / "Information" / "2026-07-14.md"
        old_briefing.parent.mkdir(parents=True)
        old_briefing.write_text(
            "## 可继续追踪\n\n欧洲央行决议结果。\n", encoding="utf-8"
        )

        response = json.dumps(
            {
                "queries": [
                    {
                        "query": "欧洲央行决议结果",
                        "reason": "来自昨日可继续追踪",
                    }
                ]
            },
            ensure_ascii=False,
        )
        with patch.object(
            information,
            "call_ai",
            return_value=(response, True, 0, {}, 0),
        ) as call:
            message, success, path = information.generate_information_briefing(
                date, {"name": "mock", "search": False}
            )

        self.assertFalse(success)
        self.assertIsNone(path)
        self.assertIn("没有返回有效 JSON", message)
        self.assertEqual(3, call.call_count)
        first_prompt = call.call_args_list[0].args[0]
        self.assertIn("严禁从中产生选题", first_prompt)
        self.assertIn("record_refs", first_prompt)

    def test_today_highlights_require_exactly_five_numbered_items(self):
        self.assertTrue(information._has_five_daily_highlights(self.valid_briefing()))
        only_four = re.sub(
            r"\n\n### 5\..*?(?=\n\n## 与本周思考相关的探索)",
            "",
            self.valid_briefing(),
            flags=re.DOTALL,
        )
        self.assertFalse(information._has_five_daily_highlights(only_four))
        six = self.valid_briefing().replace(
            "\n\n## 与本周思考相关的探索",
            "\n\n### 6. 额外事项\n\n新资料 [来源](https://example.com/6)"
            "\n\n## 与本周思考相关的探索",
        )
        self.assertFalse(information._has_five_daily_highlights(six))

    def test_today_highlights_reject_terse_macro_items(self):
        markdown = self.valid_briefing().replace(
            "**具体变化**：事项 3 已经发布明确结果，"
            "相关机构确认变化已经生效，不是尚待发生的日程预告。\n\n"
            "**关键细节**：结果包含数据 30 与数据 35，"
            "分别对应两个可核查对象，并给出了明确发布日期和适用范围。"
            " [来源](https://example.com/3)\n\n"
            "**关注理由**：这些细节会直接改变相关对象当前的判断依据，"
            "后续可用同一口径核对执行结果，而不是泛泛讨论长期影响。",
            "市场高度关注，影响深远。[来源](https://example.com/3)",
        )

        errors = information._briefing_errors(markdown, True, None)

        self.assertTrue(any("第 3 项缺少具体度字段" in error for error in errors))

    def test_briefing_rejects_links_absent_from_search_evidence(self):
        errors = information._briefing_errors(
            self.valid_briefing(),
            True,
            {information.canonical_url("https://example.com/1")},
        )

        self.assertTrue(any("搜索证据" in error for error in errors))

    def test_each_daily_highlight_requires_its_own_nearby_link(self):
        markdown = self.valid_briefing().replace(
            " [来源](https://example.com/3)", ""
        )

        errors = information._briefing_errors(markdown, True, None)

        self.assertTrue(any("没有就近来源链接" in error for error in errors))

    def test_briefing_requires_every_record_driven_topic_in_order(self):
        missing = information._briefing_errors(
            self.valid_briefing(),
            True,
            None,
            ["T001", "T002"],
        )
        covered = information._briefing_errors(
            self.valid_briefing(targeted_ids=("T001", "T002")),
            True,
            None,
            ["T001", "T002"],
        )

        self.assertTrue(any("定向选题" in error for error in missing))
        self.assertFalse(any("定向选题" in error for error in covered))

    def test_briefing_rejects_extra_targeted_exploration_heading(self):
        markdown = self.valid_briefing(targeted_ids=("T001",)).replace(
            "\n\n## 可继续追踪",
            "\n\n### T999. 未授权选题\n\n不应出现。\n\n## 可继续追踪",
        )

        errors = information._briefing_errors(
            markdown, True, None, ["T001"]
        )

        self.assertTrue(any("没有按定向选题逐项生成" in error for error in errors))

    def test_targeted_exploration_requires_evidence_from_its_own_query(self):
        markdown = self.valid_briefing(targeted_ids=("T001",))
        errors = information._briefing_errors(
            markdown,
            True,
            None,
            ["T001"],
            {
                "T001": {
                    information.canonical_url("https://example.com/other")
                }
            },
        )

        self.assertTrue(any("对应查询" in error for error in errors))

    def test_targeted_exploration_must_show_its_week_record_basis(self):
        expected_ref = "R-20260715-001-aaaaaaaaaaaa"
        markdown = self.valid_briefing(
            targeted_ids=("T001",),
            targeted_refs=("R-20260715-002-bbbbbbbbbbbb",),
        )

        errors = information._briefing_errors(
            markdown,
            True,
            None,
            ["T001"],
            None,
            {"T001": {expected_ref}},
        )

        self.assertTrue(any("本周原始记录依据" in error for error in errors))

    def test_invalid_briefing_is_revised_with_original_draft_and_reason(self):
        date = datetime.date(2026, 7, 15)
        drafts = iter(["缺少章节和链接", self.valid_briefing()])

        def fake_call_ai(
            prompt,
            model_config,
            *,
            allowed_tools=None,
            allowed_search_queries=None,
        ):
            return self.audited_response(
                next(drafts), allowed_search_queries
            )

        with patch.object(information, "call_ai", side_effect=fake_call_ai) as call:
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
        self.assertEqual((), call.call_args_list[0].kwargs["allowed_tools"])
        self.assertEqual((), call.call_args_list[1].kwargs["allowed_tools"])
        self.assertNotIn("allowed_search_queries", call.call_args_list[0].kwargs)
        self.assertNotIn("allowed_search_queries", call.call_args_list[1].kwargs)

    def test_controller_executes_every_query_before_collector(self):
        date = datetime.date(2026, 7, 15)
        with patch.object(
            information,
            "call_ai",
            return_value=self.audited_response(self.valid_briefing()),
        ) as collector:
            _, success, path = information.generate_information_briefing(
                date, {"name": "mock", "search": False}
            )

        self.assertTrue(success)
        self.assertIsNotNone(path)
        self.assertEqual(2, self.search_mock.call_count)
        collector.assert_called_once()
        self.assertIn("【中控已审计搜索证据】", collector.call_args.args[0])

    def test_collector_is_not_called_when_all_searches_have_no_evidence(self):
        date = datetime.date(2026, 7, 15)
        self.search_mock.side_effect = lambda query: (ToolResult("搜索无结果"), "")
        with patch.object(information, "call_ai") as collector:
            message, success, path = information.generate_information_briefing(
                date, {"name": "mock", "search": False}
            )

        self.assertFalse(success)
        self.assertIsNone(path)
        self.assertIn("没有返回可审计的来源证据", message)
        collector.assert_not_called()

    def test_zero_evidence_target_is_dropped_before_collector_generation(self):
        date = datetime.date(2026, 7, 15)
        (settings.DIARY_DIR / "2026-07-15.md").write_text(
            "**09:00:** 想研究一个定向问题。\n", encoding="utf-8"
        )
        general_evidence = [
            {
                "title": f"来源 {number}",
                "url": f"https://example.com/{number}",
                "snippet": "摘要",
                "published": "2026-07-15",
            }
            for number in range(1, 6)
        ]

        def search(query):
            if "全球 已公布" in query or "中国 已发布" in query:
                return ToolResult("综合搜索结果", 5, general_evidence), ""
            return ToolResult("搜索无结果"), ""

        collector_calls = []

        def fake_call_ai(prompt, model_config, *, allowed_tools=None, **kwargs):
            if prompt.startswith("[程序每日信息选题任务]"):
                record_ref = information._RECORD_REF_PATTERN.findall(prompt)[0]
                return (
                    json.dumps(
                        {
                            "queries": [
                                {
                                    "query": "无公开资料的定向问题",
                                    "reason": "核查",
                                    "record_refs": [record_ref],
                                }
                            ]
                        },
                        ensure_ascii=False,
                    ),
                    True,
                    0,
                    {},
                    0,
                )
            collector_calls.append(prompt)
            return self.audited_response(self.valid_briefing())

        self.search_mock.side_effect = search
        with patch.object(information, "call_ai", side_effect=fake_call_ai):
            _, success, path = information.generate_information_briefing(
                date, {"name": "mock", "search": False}
            )

        self.assertTrue(success)
        self.assertEqual(1, len(collector_calls))
        self.assertNotIn("无公开资料的定向问题", collector_calls[0])
        saved = path.read_text(encoding="utf-8")
        self.assertIn("零证据移除 1 项", saved)
        self.assertIn("无公开资料的定向问题", saved)

    def test_briefing_fails_when_web_search_is_not_configured(self):
        settings.CONFIG["third_search"] = {"enabled": False, "api_key": ""}

        message, success, path = information.generate_information_briefing(
            datetime.date(2026, 7, 15), {"name": "mock", "search": False}
        )

        self.assertFalse(success)
        self.assertIsNone(path)
        self.assertIn("需要启用第三方搜索", message)

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

    def test_privacy_sanitizer_removes_paths_without_damaging_urls(self):
        value = information._sanitize_text(
            "参考 https://example.com/article，私有文件 /private/note.md",
            300,
        )

        self.assertIn("https://example.com/article", value)
        self.assertNotIn("/private/note.md", value)

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

    def test_week_context_uses_raw_records_not_summary_and_resets_on_monday(self):
        sunday = settings.DIARY_DIR / "2026-07-19.md"
        monday = settings.DIARY_DIR / "2026-07-20.md"
        sunday.write_text(
            "# 2026-07-19\n\n<summary>周日总结</summary>\n\n"
            "**09:00:** 上周记录\n",
            encoding="utf-8",
        )
        monday.write_text(
            "# 2026-07-20\n\n<summary>暂无今日总结。</summary>\n\n"
            "**09:00:** 周一原始记录\n",
            encoding="utf-8",
        )

        value = information._week_record_context(datetime.date(2026, 7, 20))

        self.assertIn("周一原始记录", value)
        self.assertNotIn("暂无今日总结", value)
        self.assertNotIn("上周记录", value)

    def test_week_context_keeps_newest_records_when_character_budget_is_small(self):
        monday = settings.DIARY_DIR / "2026-07-13.md"
        tuesday = settings.DIARY_DIR / "2026-07-14.md"
        monday.write_text("**09:00:** 较旧记录-" + "甲" * 80, encoding="utf-8")
        tuesday.write_text("**09:00:** 最新记录-" + "乙" * 80, encoding="utf-8")

        value = information._week_record_context(
            datetime.date(2026, 7, 14), limit=120
        )

        self.assertIn("最新记录", value)
        self.assertNotIn("较旧记录", value)

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
