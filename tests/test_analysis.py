import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from AgentRecord import settings
from AgentRecord.analysis import automation, context, orchestrator
from AgentRecord.analysis import session_state
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

    def test_locked_session_defers_without_model_calls(self):
        with patch.object(automation, "session_is_locked", return_value=True), patch.object(
            automation, "_automation_model"
        ) as model:
            automation.run_due_automatic_tasks()
        model.assert_not_called()
        state = json.loads(
            (settings.ANALYSIS_DIR / ".automation-state.json").read_text(encoding="utf-8")
        )
        self.assertIn("会话已锁定", state["deferred_reason"])
        released = automation._acquire_automation_lock()
        self.assertIsNotNone(released)
        released.release()

    def test_placeholder_summary_is_reconciled_even_when_progress_is_ahead(self):
        today = datetime.date(2026, 7, 17)
        yesterday = today - datetime.timedelta(days=1)
        path = settings.DIARY_DIR / f"{yesterday}.md"
        path.write_text(
            f"# {yesterday}\n\n<summary>\n暂无今日总结。\n</summary>\n\n"
            "---\n## 原始记录流\n\n**09:00:** 昨日记录\n",
            encoding="utf-8",
        )
        state = {"last_daily_date": yesterday.isoformat()}

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

    def test_missing_latest_auto_reports_override_stale_success_progress(self):
        today = datetime.date(2026, 7, 17)
        self.write_diary("2026-07-10")
        self.write_diary("2026-06-15")
        state = {
            "last_week_end": "2026-07-12",
            "last_month_end": "2026-06-30",
        }

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

    def test_retrospective_validation_failure_stops_without_rewriting(self):
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

        self.assertEqual(1, call_agent.call_count)
        review.assert_not_called()

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

    def test_linux_session_lock_uses_logind_locked_hint(self):
        results = [
            Mock(returncode=0, stdout="2 1000 user seat0 tty2\n"),
            Mock(returncode=0, stdout="yes\n"),
            Mock(returncode=0, stdout="yes\n"),
        ]
        with patch.object(session_state.os, "getuid", return_value=1000), patch.object(
            session_state.subprocess, "run", side_effect=results
        ):
            self.assertTrue(session_state._linux_locked())

    def test_session_lock_environment_override_is_deterministic(self):
        with patch.dict(session_state.os.environ, {"AGENTRECORD_SESSION_LOCKED": "1"}):
            self.assertTrue(session_state.session_is_locked())
        with patch.dict(session_state.os.environ, {"AGENTRECORD_SESSION_LOCKED": "0"}):
            self.assertFalse(session_state.session_is_locked())

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

        with patch.object(automation, "session_is_locked", return_value=False), patch.object(
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

        with patch.object(automation, "session_is_locked", return_value=False), patch.object(
            automation, "_automation_model", return_value={"name": "mock"}
        ), patch.object(automation, "_run_weekly_reports") as weekly, patch.object(
            automation, "_run_monthly_reports"
        ) as monthly:
            automation.run_due_automatic_tasks()

        weekly.assert_not_called()
        monthly.assert_not_called()

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
        with patch.object(automation.datetime, "datetime", FixedDateTime):
            automation._set_task_error(state, "weekly_report", "周报失败")

        self.assertEqual(
            "2026-07-17T01:00:00", state["retry_after"]["weekly_report"]
        )
        automation._clear_task_error(state, "weekly_report")
        self.assertNotIn("errors", state)
        self.assertNotIn("retry_after", state)

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


if __name__ == "__main__":
    unittest.main()
