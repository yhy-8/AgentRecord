import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from AgentRecord import settings
from AgentRecord.analysis import automation, context, orchestrator
from AgentRecord.analysis.store import AnalysisStore


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
        self.assertIn("手动重试自动任务", auto_path.read_text(encoding="utf-8"))

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
        self.assertIn("* * * * *", cron_input)
        self.assertIn(" minute", cron_input)


if __name__ == "__main__":
    unittest.main()
