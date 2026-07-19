import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from AgentRecord import settings
from AgentRecord.ai_client import ToolResult
from AgentRecord.analysis import automation, context, orchestrator
from AgentRecord.analysis.store import AnalysisStore
from AgentRecord.agents.base import AgentPipelineError


class AnalysisWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.original_diary = settings.DIARY_DIR
        self.original_analysis = settings.ANALYSIS_DIR
        self.original_automation = settings.CONFIG.get("automation")
        self.original_call_ai = orchestrator.call_ai
        settings.DIARY_DIR = root / "Records"
        settings.ANALYSIS_DIR = root / "AnalysisReports"
        settings.DIARY_DIR.mkdir()
        settings.CONFIG["automation"] = {
            "enabled": True,
            "daily_summary": True,
            "daily_information": False,
            "weekly_report": False,
            "monthly_report": False,
        }
        self.ai_calls = []
        orchestrator.call_ai = self.fake_call_ai

    def tearDown(self):
        settings.DIARY_DIR = self.original_diary
        settings.ANALYSIS_DIR = self.original_analysis
        orchestrator.call_ai = self.original_call_ai
        if self.original_automation is None:
            settings.CONFIG.pop("automation", None)
        else:
            settings.CONFIG["automation"] = self.original_automation
        self.temp_dir.cleanup()

    def fake_call_ai(self, prompt, model_cfg, *, allowed_tools=None):
        self.ai_calls.append(prompt)
        if "[程序 Agent 任务:" not in prompt:
            return "测试总结", True, 0, {}, 0
        input_text = prompt.split("【中控提供的输入 JSON】\n", 1)[1]
        data = json.loads(input_text.split("\n\n只输出一个符合契约", 1)[0])
        tool_counts = {}
        result_count = 0
        if "任务:retrospective]" in prompt:
            record = data["records"][0]
            source = record["source_id"]
            payload = {
                "markdown": f"### 本期回顾\n\n本期完成了一次记录与思考。 [{source}]",
                "profile_entries": [
                    {
                        "temp_id": "p1",
                        "category": "viewpoint",
                        "title": "重视可验证性",
                        "statement": "用户开始重视可验证的记录。",
                        "confidence": 0.8,
                        "source_refs": [source],
                        "supersedes_id": None,
                    }
                ],
            }
        elif "任务:research_planner]" in prompt:
            source = data["records"][0]["source_id"]
            payload = {
                "topics": [
                    {
                        "topic_id": "Q001",
                        "title": "记录与反思方法",
                        "query": "记录与反思方法的研究和边界",
                        "reason": "拓宽记录方法的理解",
                        "origin": "records",
                        "source_refs": [source],
                    }
                ]
            }
        elif "任务:researcher]" in prompt:
            source = data["research_topics"][0]["source_refs"][0]
            payload = {
                "markdown": (
                    "### 记录与反思方法\n\n"
                    f"该问题由本期记录引出 [{source}]；"
                    "外部研究提供了不同边界与反例"
                    "（[测试来源](https://example.com/source)）。"
                ),
                "sources": [
                    {
                        "topic_id": "Q001",
                        "title": "测试来源",
                        "url": "https://example.com/source",
                        "published": "2026-07-14",
                    }
                ],
            }
            tool_counts = {"web_search": 1}
            result_count = 1
        else:
            decisions = [
                {
                    "temp_id": temp_id,
                    "status": "accepted",
                    "reason": "记录直接支持",
                }
                for temp_id in data.get("valid_profile_temp_ids", [])
            ]
            payload = {
                "pass": True,
                "entry_decisions": decisions,
                "unsupported_claims": [],
                "required_changes": [],
                "summary": "通过",
            }
        return json.dumps(payload, ensure_ascii=False), True, 0, tool_counts, result_count

    def write_diary(self, date: str):
        path = settings.DIARY_DIR / f"{date}.md"
        raw = "**09:00:** 我开始重视记录是否可以验证。\n\n"
        path.write_text(
            f"# {date}\n\n<summary>\n旧总结\n</summary>\n\n---\n## 原始记录流\n\n{raw}",
            encoding="utf-8",
        )
        return path

    def test_summary_only_replaces_summary_region(self):
        diary = self.write_diary("2026-07-14")
        _, success = orchestrator.summarize_diary("2026-07-14", {"name": "mock"})
        self.assertTrue(success)
        content = diary.read_text(encoding="utf-8")
        self.assertIn("<summary>\n测试总结\n</summary>", content)
        self.assertIn("我开始重视记录是否可以验证", content)

    def test_weekly_report_has_two_independently_generated_sections(self):
        day = datetime.date(2026, 7, 14)
        diary = self.write_diary(day.isoformat())
        original = diary.read_bytes()
        _, success, path = orchestrator.generate_analysis_report(
            "weekly", day, {"name": "mock"}
        )
        self.assertTrue(success)
        content = path.read_text(encoding="utf-8")
        self.assertIn("## 一、整理与回顾", content)
        self.assertIn("## 二、领域探索与研究", content)
        self.assertIn("https://example.com/source", content)
        self.assertEqual(original, diary.read_bytes())
        self.assertEqual(1, len(AnalysisStore().active_profiles("2026-07-20")))
        retrospective_review = next(
            prompt
            for prompt in self.ai_calls
            if "任务:reviewer]" in prompt and '"mode": "retrospective_review"' in prompt
        )
        self.assertIn("我开始重视记录是否可以验证", retrospective_review)

    def test_retry_reuses_reviewed_stages_from_equivalent_failed_run(self):
        day = datetime.date(2026, 7, 14)
        self.write_diary(day.isoformat())
        with patch.object(
            orchestrator,
            "_research_section",
            side_effect=AgentPipelineError("模拟 Researcher 失败"),
        ):
            _, first_success, _ = orchestrator.generate_analysis_report(
                "weekly", day, {"name": "mock"}
            )
        self.assertFalse(first_success)

        self.ai_calls.clear()
        _, second_success, _ = orchestrator.generate_analysis_report(
            "weekly", day, {"name": "mock"}
        )

        self.assertTrue(second_success)
        self.assertFalse(
            any("任务:retrospective]" in prompt for prompt in self.ai_calls)
        )
        self.assertFalse(
            any("任务:research_planner]" in prompt for prompt in self.ai_calls)
        )
        self.assertTrue(any("任务:researcher]" in prompt for prompt in self.ai_calls))

    def test_daily_analysis_is_removed(self):
        self.write_diary("2026-07-14")
        message, success, path = orchestrator.generate_analysis_report(
            "daily", datetime.date(2026, 7, 14), {"name": "mock"}
        )
        self.assertFalse(success)
        self.assertIsNone(path)
        self.assertIn("weekly", message)

    def test_manual_and_automatic_reports_remain_separate(self):
        day = datetime.date(2026, 7, 14)
        self.write_diary(day.isoformat())
        _, manual, manual_path = orchestrator.generate_analysis_report(
            "weekly", day, {"name": "mock"}, origin="manual"
        )
        _, auto, auto_path = orchestrator.generate_analysis_report(
            "weekly", day, {"name": "mock"}, origin="auto", trigger="retry"
        )
        self.assertTrue(manual and auto)
        self.assertNotEqual(manual_path, auto_path)
        self.assertIn("自动任务重试", auto_path.read_text(encoding="utf-8"))

    def test_report_reference_is_not_loaded_but_diary_reference_is(self):
        day = "2026-07-14"
        report = settings.ANALYSIS_DIR / "Weekly" / "old_auto.md"
        report.parent.mkdir(parents=True)
        report.write_text("不应读取的报告内容", encoding="utf-8")
        older = settings.DIARY_DIR / "2026-07-13.md"
        older.write_text("可以读取的日记内容", encoding="utf-8")
        logs = [
            (
                day,
                "**09:00 [引用]:** [旧报告](<../AnalysisReports/Weekly/old_auto.md>)\n\n"
                "**10:00 [引用]:** [日记](<2026-07-13.md>)\n\n",
            )
        ]
        loaded = context._referenced_source_context(logs)
        self.assertIn("可以读取的日记内容", loaded)
        self.assertNotIn("不应读取的报告内容", loaded)

    def test_information_briefing_reaches_research_planner(self):
        day = datetime.date(2026, 7, 14)
        self.write_diary(day.isoformat())
        info = settings.ANALYSIS_DIR / "Information" / day.isoformat()
        info.parent.mkdir(parents=True)
        info.with_suffix(".md").write_text("综合新闻雷达线索", encoding="utf-8")
        orchestrator.generate_analysis_report("weekly", day, {"name": "mock"})
        planner_prompt = next(
            prompt for prompt in self.ai_calls if "任务:research_planner]" in prompt
        )
        self.assertIn("综合新闻雷达线索", planner_prompt)

    def test_kernel_lock_releases_without_deleting_sentinel(self):
        first = automation._acquire_automation_lock()
        self.assertIsNotNone(first)
        self.assertIsNone(automation._acquire_automation_lock())
        first.release()
        second = automation._acquire_automation_lock()
        self.assertIsNotNone(second)
        second.release()
        self.assertTrue((settings.ANALYSIS_DIR / ".automation.lock").exists())

    def test_placeholder_summary_is_detected_from_diary_content(self):
        today = datetime.date(2026, 7, 17)
        yesterday = today - datetime.timedelta(days=1)
        path = settings.DIARY_DIR / f"{yesterday}.md"
        path.write_text(
            f"# {yesterday}\n\n<summary>\n暂无今日总结。\n</summary>\n\n"
            "---\n## 原始记录流\n\n**09:00:** 昨日记录\n",
            encoding="utf-8",
        )
        state = {}

        with patch.object(
            automation, "summarize_diary", return_value=("总结", True)
        ) as summarize:
            automation._run_daily_summaries(today, state, {"name": "mock"})

        summarize.assert_called_once_with(yesterday.isoformat(), {"name": "mock"})

    def test_existing_summary_is_not_overwritten_on_first_automation_run(self):
        today = datetime.date(2026, 7, 17)
        yesterday = today - datetime.timedelta(days=1)
        path = settings.DIARY_DIR / f"{yesterday}.md"
        path.write_text(
            f"# {yesterday}\n\n<summary>\n已有总结。\n</summary>\n\n"
            "---\n## 原始记录流\n\n**09:00:** 昨日记录\n",
            encoding="utf-8",
        )

        with patch.object(automation, "summarize_diary") as summarize:
            automation._run_daily_summaries(today, {}, {"name": "mock"})

        summarize.assert_not_called()

    def test_four_automatic_artifacts_include_daily_information(self):
        now = datetime.datetime(2026, 7, 17, 9, 0)
        yesterday = settings.DIARY_DIR / "2026-07-16.md"
        yesterday.write_text(
            "# 2026-07-16\n\n<summary>\n暂无今日总结。\n</summary>\n\n"
            "---\n## 原始记录流\n\n**09:00:** 昨日记录\n",
            encoding="utf-8",
        )
        self.write_diary("2026-07-10")
        self.write_diary("2026-06-15")

        self.assertTrue(automation._task_missing("daily_summary", now))
        self.assertTrue(automation._task_missing("daily_information", now))
        self.assertTrue(automation._task_missing("weekly_report", now))
        self.assertTrue(automation._task_missing("monthly_report", now))

        yesterday.write_text(
            yesterday.read_text(encoding="utf-8").replace("暂无今日总结。", "已有总结。"),
            encoding="utf-8",
        )
        info_path = automation.information_briefing_path(now.date())
        info_path.parent.mkdir(parents=True, exist_ok=True)
        info_path.write_text("简报", encoding="utf-8")
        week_start, week_end = automation._latest_week_period(now.date())
        month_start, month_end = automation._latest_month_period(now.date())
        weekly_path = context._analysis_report_path(
            "weekly", week_start, week_end, "auto"
        )
        monthly_path = context._analysis_report_path(
            "monthly", month_start, month_end, "auto"
        )
        weekly_path.parent.mkdir(parents=True, exist_ok=True)
        monthly_path.parent.mkdir(parents=True, exist_ok=True)
        weekly_path.write_text("周报", encoding="utf-8")
        monthly_path.write_text("月报", encoding="utf-8")

        for task in automation.AUTOMATION_TASK_LABELS:
            self.assertFalse(automation._task_missing(task, now), task)

    def test_latest_auto_reports_are_detected_from_files(self):
        today = datetime.date(2026, 7, 17)
        self.write_diary("2026-07-10")
        self.write_diary("2026-06-15")
        state = {}

        with patch.object(
            automation,
            "generate_analysis_report",
            return_value=("完成", True, Path("auto.md")),
        ) as generate:
            automation._run_weekly_reports(today, state, {"name": "mock"})
            automation._run_monthly_reports(today, state, {"name": "mock"})

        self.assertEqual(
            ["weekly", "monthly"],
            [call.args[0] for call in generate.call_args_list],
        )

    def test_retrospective_validation_failure_uses_bounded_revisions(self):
        source_id = "R-20260714-001"
        base_input = {
            "period": {"kind": "weekly", "start": "2026-07-13", "end": "2026-07-19"},
            "records": [{"source_id": source_id, "text": "记录"}],
            "historical_profiles": [],
        }
        invalid = {"markdown": "缺少引用", "profile_entries": []}
        store = Mock()

        with patch.object(
            orchestrator, "_call_agent", return_value=invalid
        ) as call_agent, patch.object(orchestrator, "_review") as review, self.assertRaises(
            AgentPipelineError
        ):
            orchestrator._retrospective_section(
                base_input,
                {source_id},
                {source_id},
                {},
                {"name": "mock"},
                store,
                "run-id",
            )

        self.assertEqual(3, call_agent.call_count)
        correction = call_agent.call_args.kwargs["revision_context"]
        self.assertEqual("中控确定性校验", correction["feedback_source"])
        self.assertIn("没有来源引用", correction["problems_to_fix"][0])
        review.assert_not_called()

    def test_reviewer_feedback_is_returned_to_original_agent(self):
        source_id = "R-20260714-001"
        base_input = {
            "period": {"kind": "weekly", "start": "2026-07-13", "end": "2026-07-19"},
            "records": [{"source_id": source_id, "text": "用户记录"}],
            "historical_profiles": [],
        }
        first = {
            "markdown": f"第一稿判断。 [{source_id}]",
            "profile_entries": [],
        }
        revised = {
            "markdown": f"修订后仅保留有依据的判断。 [{source_id}]",
            "profile_entries": [],
        }
        rejected_review = {
            "pass": False,
            "entry_decisions": [],
            "unsupported_claims": ["第一稿判断超出记录支持范围"],
            "required_changes": ["删除无依据判断"],
            "summary": "需要修订",
        }
        store = Mock()

        with patch.object(
            orchestrator, "_call_agent", side_effect=[first, revised]
        ) as call_agent, patch.object(
            orchestrator,
            "_review",
            side_effect=[
                (False, {}, ["删除无依据判断"], rejected_review),
                (True, {}, [], {"pass": True}),
            ],
        ):
            markdown, entries, decisions = orchestrator._retrospective_section(
                base_input,
                {source_id},
                {source_id},
                {},
                {"name": "mock"},
                store,
                "run-id",
            )

        self.assertIn("修订后", markdown)
        self.assertEqual([], entries)
        self.assertEqual({}, decisions)
        correction = call_agent.call_args_list[1].kwargs["revision_context"]
        self.assertEqual("Reviewer 实质审查", correction["feedback_source"])
        self.assertEqual(first, correction["rejected_previous_output"])
        self.assertIn(
            "删除无依据判断", correction["problems_to_fix"]["required_changes"]
        )

    def test_revision_context_does_not_echo_large_internal_telemetry(self):
        correction = orchestrator._revision_context(
            2,
            {
                "markdown": "原稿",
                "_telemetry": {"search_evidence": [{"snippet": "x" * 10000}]},
            },
            ["链接需修正"],
            source="中控确定性校验",
        )

        self.assertEqual(
            {"markdown": "原稿"}, correction["rejected_previous_output"]
        )

    def test_reviewer_receives_only_evidence_used_by_draft(self):
        telemetry = {
            "tool_calls": {"web_search": 1},
            "search_results": 25,
            "search_queries": ["查询"],
            "search_evidence": [
                {"url": "https://example.com/used", "snippet": "支持稿件"},
                {"url": "https://example.com/unused", "snippet": "无关结果"},
            ],
        }
        compact = orchestrator._review_search_telemetry(
            telemetry,
            [{"url": "https://example.com/used", "topic_id": "Q001"}],
        )

        self.assertEqual(1, len(compact["search_evidence"]))
        self.assertEqual(
            "https://example.com/used", compact["search_evidence"][0]["url"]
        )

    def test_research_revision_receives_exact_verified_source_options(self):
        options = orchestrator._verified_source_options(
            [{"topic_id": "Q001", "query": "Public Query"}],
            [
                {
                    "query": "  public   query ",
                    "title": "可核查来源",
                    "url": "https://example.com/exact",
                    "published": "2026-07-01",
                },
                {
                    "query": "另一查询",
                    "title": "无关来源",
                    "url": "https://example.com/other",
                },
            ],
        )

        self.assertEqual(
            "https://example.com/exact", options[0]["sources"][0]["url"]
        )
        self.assertEqual(1, len(options[0]["sources"]))

    def test_research_revision_can_reuse_prior_audited_url_after_researching(self):
        topics = [
            {
                "topic_id": "Q001",
                "title": "公开主题",
                "query": "公开查询",
                "reason": "研究",
                "origin": "news",
                "source_refs": [],
            }
        ]
        prior_url = "https://example.com/prior"
        first = {
            "markdown": "第一稿没有使用来源。",
            "sources": [
                {"topic_id": "Q001", "title": "来源", "url": prior_url}
            ],
            "_telemetry": {
                "search_results": 1,
                "search_evidence": [
                    {"query": "公开查询", "title": "来源", "url": prior_url}
                ],
            },
        }
        second = {
            "markdown": f"修订稿使用已审计来源 [来源]({prior_url})。",
            "sources": [
                {"topic_id": "Q001", "title": "来源", "url": prior_url}
            ],
            "_telemetry": {
                "search_results": 1,
                "search_evidence": [
                    {
                        "query": "公开查询",
                        "title": "本轮另一结果",
                        "url": "https://example.com/current",
                    }
                ],
            },
        }
        store = Mock()

        with patch.object(
            orchestrator, "_call_agent", side_effect=[first, second]
        ) as call_agent, patch.object(
            orchestrator,
            "_review",
            return_value=(True, {}, [], {"pass": True}),
        ):
            markdown = orchestrator._research_section(
                topics, "", set(), {"name": "mock"}, store, "run-id"
            )

        self.assertIn(prior_url, markdown)
        correction = call_agent.call_args_list[1].kwargs["revision_context"]
        whitelist = correction["problems_to_fix"]["verified_source_options"]
        self.assertEqual(prior_url, whitelist[0]["sources"][0]["url"])

    def test_grounded_research_searches_once_and_controller_renders_url(self):
        topics = [
            {
                "topic_id": "Q001",
                "title": "公开主题",
                "query": "公开查询",
                "reason": "研究",
                "origin": "news",
                "source_refs": [],
            }
        ]
        store = Mock()
        result = ToolResult(
            "搜索结果",
            1,
            [
                {
                    "query": "公开查询",
                    "title": "真实来源",
                    "url": "https://example.com/real",
                    "snippet": "支持材料",
                    "published": "2026-07-14",
                }
            ],
        )
        draft = {
            "markdown": "基于证据可以确认边界 [W-Q001-001]。",
            "_telemetry": {"usage": {"total_tokens": 100}},
        }

        with patch.object(
            orchestrator, "third_party_search_available", return_value=True
        ), patch.object(
            orchestrator, "search_web_once", return_value=(result, "")
        ) as search, patch.object(
            orchestrator, "_call_agent", return_value=draft
        ) as call_agent, patch.object(
            orchestrator,
            "_review",
            return_value=(True, {}, [], {"pass": True}),
        ):
            markdown = orchestrator._research_section(
                topics, "不应送入模型", set(), {"name": "mock"}, store, "run-id"
            )

        self.assertEqual(1, search.call_count)
        self.assertIn("https://example.com/real", markdown)
        input_data = call_agent.call_args.args[2]
        self.assertNotIn("information_leads", input_data)
        self.assertNotIn("url", input_data["evidence_sources"][0])
        self.assertEqual(frozenset(), call_agent.call_args.args[0].allowed_tools)

    def test_grounded_research_revision_does_not_repeat_search(self):
        topics = [
            {
                "topic_id": "Q001",
                "title": "公开主题",
                "query": "公开查询",
                "reason": "研究",
                "origin": "news",
                "source_refs": [],
            }
        ]
        result = ToolResult(
            "搜索结果",
            1,
            [
                {
                    "query": "公开查询",
                    "title": "真实来源",
                    "url": "https://example.com/real",
                    "snippet": "支持材料",
                    "published": "",
                }
            ],
        )
        drafts = [
            {"markdown": "缺少证据"},
            {"markdown": "修订后引用证据 [W-Q001-001]。"},
        ]
        store = Mock()

        with patch.object(
            orchestrator, "search_web_once", return_value=(result, "")
        ) as search, patch.object(
            orchestrator, "_call_agent", side_effect=drafts
        ) as call_agent, patch.object(
            orchestrator,
            "_review",
            return_value=(True, {}, [], {"pass": True}),
        ):
            markdown = orchestrator._grounded_research_section(
                topics, set(), {"name": "mock"}, store, "run-id"
            )

        self.assertIn("https://example.com/real", markdown)
        self.assertEqual(1, search.call_count)
        self.assertEqual(2, call_agent.call_count)

    def test_grounded_search_cache_avoids_searching_again_on_report_retry(self):
        topics = [
            {
                "topic_id": "Q001",
                "title": "公开主题",
                "query": "公开查询",
                "reason": "研究",
                "origin": "news",
                "source_refs": [],
            }
        ]
        evidence = [
            {
                "source_id": "W-Q001-001",
                "topic_id": "Q001",
                "query": "公开查询",
                "title": "真实来源",
                "url": "https://example.com/real",
                "snippet": "支持材料",
                "published": "",
            }
        ]
        telemetry = {
            "tool_calls": {"web_search": 1},
            "search_queries": ["公开查询"],
            "search_results": 1,
            "search_evidence": evidence,
        }
        cached = (
            "previous-run",
            {
                "topics": topics,
                "usable_topics": topics,
                "evidence": evidence,
                "_telemetry": telemetry,
            },
        )
        store = Mock()

        with patch.object(orchestrator, "search_web_once") as search:
            usable, restored, restored_telemetry = (
                orchestrator._collect_research_evidence(
                    topics, store, "run-id", cached
                )
            )

        search.assert_not_called()
        self.assertEqual(topics, usable)
        self.assertEqual(evidence, restored)
        self.assertEqual(telemetry, restored_telemetry)
        saved = store.save_artifact.call_args.args[2]
        self.assertTrue(saved["_cache"]["hit"])

    def test_grounded_search_cache_requires_safe_evidence_for_every_topic(self):
        topics = [
            {
                "topic_id": "Q001",
                "title": "主题一",
                "query": "查询一",
                "reason": "研究",
                "origin": "news",
                "source_refs": [],
            },
            {
                "topic_id": "Q002",
                "title": "主题二",
                "query": "查询二",
                "reason": "研究",
                "origin": "news",
                "source_refs": [],
            },
        ]
        payload = {
            "topics": topics,
            "usable_topics": topics,
            "evidence": [
                {
                    "source_id": "W-Q001-001",
                    "topic_id": "Q001",
                    "title": "来源",
                    "url": "https://example.com/unsafe\nlink",
                    "snippet": "材料",
                    "published": "",
                }
            ],
            "_telemetry": {},
        }

        self.assertIsNone(
            orchestrator._valid_cached_research_evidence(payload, topics)
        )

    def test_invalid_agent_json_fails_without_a_repair_call(self):
        parse_error = AgentPipelineError(
            "Agent JSON 无法解析: test",
            response="invalid",
            telemetry={
                "web_citations": 0,
                "tool_calls": {"web_search": 1},
                "search_results": 12,
            },
        )
        store = Mock()

        with patch.object(
            orchestrator, "invoke_agent", side_effect=parse_error
        ) as invoke, self.assertRaises(AgentPipelineError):
            orchestrator._call_agent(
                orchestrator.researcher.SPEC,
                "研究",
                {},
                {"name": "mock"},
                store,
                "run-id",
            )

        self.assertEqual(1, invoke.call_count)
        saved_payload = store.save_artifact.call_args.args[2]
        self.assertEqual(1, saved_payload["_telemetry"]["tool_calls"]["web_search"])
        self.assertEqual(12, saved_payload["_telemetry"]["search_results"])

    def test_api_failure_does_not_enter_content_revision_loop(self):
        store = Mock()
        failure = AgentPipelineError(
            "research_planner 调用失败: 网络异常: timeout"
        )

        with patch.object(
            orchestrator, "_call_agent", side_effect=failure
        ) as call_agent, self.assertRaises(AgentPipelineError):
            orchestrator._validated_agent_call(
                orchestrator.research_planner.SPEC,
                "选题",
                {},
                lambda payload: payload,
                {"name": "mock"},
                store,
                "run-id",
            )

        self.assertEqual(1, call_agent.call_count)

    def test_retry_runs_all_failed_tasks(self):
        settings.ANALYSIS_DIR.mkdir()
        (settings.ANALYSIS_DIR / ".automation-state.json").write_text(
            json.dumps(
                {
                    "errors": {
                        "daily_information": "失败",
                        "weekly_report": "失败",
                        "monthly_report": "失败",
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        calls = []

        def succeed(task, now, state, model):
            calls.append(task)
            automation._clear_task_error(state, task)
            automation._save_automation_state(state)

        with patch.object(
            automation, "_automation_model", return_value={"name": "mock"}
        ), patch.object(automation, "_retry_one_task", side_effect=succeed):
            _, success = automation.retry_failed_automatic_tasks()
        self.assertTrue(success)
        self.assertEqual(
            ["daily_information", "weekly_report", "monthly_report"], calls
        )

    def test_minute_scheduler_does_not_repeat_recorded_failures(self):
        settings.CONFIG["automation"] = {
            "enabled": True,
            "daily_summary": False,
            "daily_information": False,
            "weekly_report": True,
            "monthly_report": True,
        }
        settings.ANALYSIS_DIR.mkdir()
        (settings.ANALYSIS_DIR / ".automation-state.json").write_text(
            json.dumps(
                {
                    "errors": {
                        "weekly_report": "失败",
                        "monthly_report": "失败",
                    }
                }
            ),
            encoding="utf-8",
        )

        with patch.object(
            automation, "_automation_model", return_value={"name": "mock"}
        ), patch.object(automation, "_run_weekly_reports") as weekly, patch.object(
            automation, "_run_monthly_reports"
        ) as monthly:
            automation.run_due_automatic_tasks()

        weekly.assert_not_called()
        monthly.assert_not_called()

    def test_first_minute_after_a_missed_hour_runs_detection(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 7, 17, 10, 23)

        settings.CONFIG["automation"] = {
            "enabled": True,
            "daily_summary": False,
            "daily_information": True,
            "daily_information_time": "08:05",
            "weekly_report": False,
            "monthly_report": False,
        }
        settings.ANALYSIS_DIR.mkdir()
        (settings.ANALYSIS_DIR / ".automation-state.json").write_text(
            json.dumps({"last_detection_hour": "2026-07-17T08"}),
            encoding="utf-8",
        )

        with patch.object(
            automation.datetime, "datetime", FixedDateTime
        ), patch.object(
            automation,
            "_task_missing",
            side_effect=lambda task, now: task == "daily_information",
        ), patch.object(
            automation, "_automation_model", return_value={"name": "mock"}
        ), patch.object(automation, "_run_daily_information") as run_information:
            automation.run_due_automatic_tasks()

        run_information.assert_called_once()
        state = automation._load_automation_state()
        self.assertEqual("2026-07-17T10", state["last_detection_hour"])

    def test_failed_task_becomes_due_at_the_next_clock_hour(self):
        state = {
            "errors": {"weekly_report": "2026-07-17 00:25 周报失败"},
            "retry_after": {"weekly_report": "2026-07-17T01:00:00"},
        }

        self.assertFalse(
            automation._failure_retry_is_due(
                state, "weekly_report", datetime.datetime(2026, 7, 17, 0, 59)
            )
        )
        self.assertTrue(
            automation._failure_retry_is_due(
                state, "weekly_report", datetime.datetime(2026, 7, 17, 1, 0)
            )
        )

    def test_recorded_failure_sets_and_clears_next_hour_deadline(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 7, 17, 0, 25, 41)

        state = {}
        with patch.object(
            automation.datetime, "datetime", FixedDateTime
        ), patch.object(
            automation, "_content_failure_key", return_value="same-input"
        ):
            automation._set_task_error(state, "weekly_report", "周报失败")

        self.assertEqual(
            "2026-07-17T01:00:00", state["retry_after"]["weekly_report"]
        )
        self.assertEqual("hourly", state["retry_kind"]["weekly_report"])
        self.assertEqual(1, state["failure_counts"]["weekly_report"])
        automation._clear_task_error(state, "weekly_report")
        self.assertNotIn("errors", state)
        self.assertNotIn("retry_after", state)
        self.assertNotIn("retry_kind", state)
        self.assertNotIn("failure_counts", state)
        self.assertNotIn("failure_keys", state)

    def test_same_content_failure_stops_after_one_automatic_retry(self):
        state = {}
        with patch.object(
            automation, "_content_failure_key", return_value="same-input"
        ):
            automation._set_task_error(state, "weekly_report", "第一次失败")
            automation._set_task_error(state, "weekly_report", "第二次失败")

        self.assertEqual("content_blocked", state["retry_kind"]["weekly_report"])
        self.assertEqual(2, state["failure_counts"]["weekly_report"])
        self.assertNotIn("weekly_report", state.get("retry_after", {}))
        self.assertFalse(
            automation._failure_retry_is_due(
                state, "weekly_report", datetime.datetime(2026, 7, 17, 12, 0)
            )
        )

    def test_changed_input_unlocks_content_failure_on_hourly_detection(self):
        state = {
            "errors": {"weekly_report": "连续失败"},
            "retry_kind": {"weekly_report": "content_blocked"},
            "failure_counts": {"weekly_report": 2},
            "failure_keys": {"weekly_report": "old-input"},
        }
        now = datetime.datetime(2026, 7, 17, 12, 0)
        with patch.object(
            automation, "_content_failure_key", return_value="new-input"
        ), patch.object(automation, "_task_missing", return_value=True):
            should_run = automation._task_should_run(
                state,
                "weekly_report",
                now,
                initial_detection_due=True,
            )

        self.assertTrue(should_run)
        self.assertNotIn("errors", state)
        self.assertNotIn("failure_counts", state)

    def test_network_failure_sets_five_minute_retry_deadline(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 7, 17, 0, 25, 41)

        state = {}
        with patch.object(automation.datetime, "datetime", FixedDateTime):
            automation._set_task_error(
                state, "daily_information", "网络异常: DNS 解析失败"
            )

        self.assertEqual(
            "2026-07-17T00:30:41",
            state["retry_after"]["daily_information"],
        )
        self.assertEqual("network", state["retry_kind"]["daily_information"])

    def test_auth_failure_waits_for_manual_retry_after_configuration_fix(self):
        state = {}
        automation._set_task_error(
            state, "weekly_report", "配置异常: HTTP 401"
        )

        self.assertEqual("blocked", state["retry_kind"]["weekly_report"])
        self.assertNotIn("weekly_report", state.get("retry_after", {}))
        self.assertFalse(
            automation._failure_retry_is_due(
                state, "weekly_report", datetime.datetime(2026, 7, 17, 12, 0)
            )
        )

    def test_retry_stops_after_global_provider_failure(self):
        settings.ANALYSIS_DIR.mkdir()
        automation._save_automation_state(
            {
                "errors": {
                    "daily_information": "失败",
                    "weekly_report": "失败",
                    "monthly_report": "失败",
                }
            }
        )
        calls = []

        def fail_network(task, now, state, model):
            calls.append(task)
            automation._set_task_error(state, task, "网络异常: DNS")
            automation._save_automation_state(state)

        with patch.object(
            automation, "_automation_model", return_value={"name": "mock"}
        ), patch.object(automation, "_retry_one_task", side_effect=fail_network):
            _, success = automation.retry_failed_automatic_tasks()

        self.assertFalse(success)
        self.assertEqual(["daily_information"], calls)

    def test_monthly_context_excludes_cross_month_weeks_and_deduplicates_origin(self):
        weekly = settings.ANALYSIS_DIR / "Weekly"
        weekly.mkdir(parents=True)
        (weekly / "2026-06-29_to_2026-07-05_auto.md").write_text(
            "跨月内容", encoding="utf-8"
        )
        (weekly / "2026-07-06_to_2026-07-12_auto.md").write_text(
            "自动版", encoding="utf-8"
        )
        (weekly / "2026-07-06_to_2026-07-12_manual.md").write_text(
            "手动版", encoding="utf-8"
        )

        value = context._monthly_supporting_reports(
            datetime.date(2026, 7, 1), datetime.date(2026, 7, 31)
        )

        self.assertNotIn("跨月内容", value)
        self.assertNotIn("自动版", value)
        self.assertIn("手动版", value)

    def test_legacy_completion_cursors_are_removed(self):
        state = {
            "last_daily_date": "2026-07-16",
            "last_information_date": "2026-07-17",
            "last_week_end": "2026-07-12",
            "last_month_end": "2026-06-30",
            "last_deferred_at": "old",
            "deferred_reason": "old",
            "errors": {"weekly_report": "失败"},
        }

        automation._remove_legacy_progress(state)

        self.assertEqual({"errors": {"weekly_report": "失败"}}, state)

    def test_retry_command_launches_detached_process(self):
        settings.ANALYSIS_DIR.mkdir()
        (settings.ANALYSIS_DIR / ".automation-state.json").write_text(
            json.dumps({"errors": {"weekly_report": "失败"}}), encoding="utf-8"
        )
        with patch.object(automation.subprocess, "Popen") as popen:
            started, _ = automation.launch_automation_retry()
        self.assertTrue(started)
        self.assertIn("--retry-automation", popen.call_args.args[0])
        if automation.os.name != "nt":
            self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_cron_install_checks_every_minute(self):
        listed = Mock(returncode=0, stdout="", stderr="")
        installed = Mock(returncode=0, stdout="", stderr="")
        run = Mock(side_effect=[listed, installed])
        with patch.object(automation, "_is_windows", return_value=False), patch.object(
            automation.subprocess, "run", run
        ):
            success, _ = automation.install_system_automation()
        self.assertTrue(success)
        cron_input = run.call_args_list[1].kwargs["input"]
        self.assertIn("@reboot", cron_input)
        self.assertIn("* * * * *", cron_input)
        self.assertIn(" minute", cron_input)

    def test_windows_frozen_automation_uses_windowless_companion(self):
        root = Path(self.temp_dir.name)
        foreground = root / "AgentRecord.exe"
        background = root / "AgentRecordBackground.exe"
        foreground.touch()
        background.touch()

        with patch.object(automation, "_is_windows", return_value=True), patch.object(
            automation.sys, "executable", str(foreground)
        ), patch.object(automation.sys, "frozen", True, create=True):
            command = automation._automation_command()

        self.assertEqual(str(background), command[0])

    def test_windows_status_rejects_old_windowed_task_action(self):
        result = Mock(
            returncode=0,
            stdout="<Task><Actions><Exec><Command>C:\\AgentRecord.exe</Command>"
            "</Exec></Actions></Task>",
            stderr="",
        )
        with patch.object(automation, "_is_windows", return_value=True), patch.object(
            automation.subprocess, "run", return_value=result
        ):
            installed, message = automation.system_automation_status()

        self.assertFalse(installed)
        self.assertIn("旧入口", message)


if __name__ == "__main__":
    unittest.main()
