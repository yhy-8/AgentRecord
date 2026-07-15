import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import analysis
import journal
import settings


class AnalysisWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.original_diary_dir = settings.DIARY_DIR
        self.original_analysis_dir = settings.ANALYSIS_DIR
        self.original_call_ai = analysis.call_ai
        self.original_automation = settings.CONFIG.get("automation")

        settings.DIARY_DIR = root / "Records"
        settings.ANALYSIS_DIR = root / "AnalysisReports"
        settings.DIARY_DIR.mkdir()
        self.ai_calls = []

        def fake_call_ai(prompt, model_cfg):
            self.ai_calls.append(prompt)
            return "测试生成内容", True, 0, {}, 0

        analysis.call_ai = fake_call_ai
        settings.CONFIG["automation"] = {
            "enabled": True,
            "daily_summary": True,
            "weekly_report": False,
            "monthly_report": False,
        }

    def tearDown(self):
        settings.DIARY_DIR = self.original_diary_dir
        settings.ANALYSIS_DIR = self.original_analysis_dir
        analysis.call_ai = self.original_call_ai
        if self.original_automation is None:
            settings.CONFIG.pop("automation", None)
        else:
            settings.CONFIG["automation"] = self.original_automation
        self.temp_dir.cleanup()

    def write_diary(self, date: str, summary: str = "旧总结") -> tuple[Path, str]:
        raw_stream = (
            "**09:00:** 一个原始想法。\n\n"
            "**10:00 @AI:** 请分析\n\n"
            "**10:01 [AI回复] test:** 一个回答\n\n"
        )
        path = settings.DIARY_DIR / f"{date}.md"
        path.write_text(
            f"# {date}\n\n<summary>\n{summary}\n</summary>\n\n"
            f"---\n## 原始记录流\n\n{raw_stream}",
            encoding="utf-8",
        )
        return path, raw_stream

    def test_summary_only_replaces_summary_region(self):
        date = "2026-07-14"
        diary, raw_stream = self.write_diary(date)

        summary, success = analysis.summarize_diary(date, {"name": "mock"})

        self.assertTrue(success)
        self.assertEqual("测试生成内容", summary)
        self.assertIn("[程序日记总结任务]", self.ai_calls[-1])
        content = diary.read_text(encoding="utf-8")
        self.assertIn("<summary>\n测试生成内容\n</summary>", content)
        self.assertTrue(content.endswith(raw_stream))

    def test_report_is_saved_separately_without_changing_diary(self):
        day = datetime.date(2026, 7, 14)
        diary, _ = self.write_diary(day.isoformat())
        original = diary.read_bytes()

        report, success, report_path = analysis.generate_analysis_report(
            "daily", day, {"name": "mock"}
        )

        self.assertTrue(success)
        self.assertEqual("测试生成内容", report)
        self.assertIn("[程序分析报告任务]", self.ai_calls[-1])
        self.assertEqual(original, diary.read_bytes())
        self.assertEqual(
            settings.ANALYSIS_DIR / "Daily" / "2026-07-14_manual.md", report_path
        )
        self.assertTrue(report_path.exists())

    def test_manual_and_automatic_reports_have_separate_fixed_paths(self):
        day = datetime.date(2026, 7, 14)
        self.write_diary(day.isoformat())

        _, manual_success, manual_path = analysis.generate_analysis_report(
            "weekly", day, {"name": "mock"}, origin="manual"
        )
        _, auto_success, auto_path = analysis.generate_analysis_report(
            "weekly", day, {"name": "mock"}, origin="auto"
        )

        self.assertTrue(manual_success and auto_success)
        self.assertNotEqual(manual_path, auto_path)
        self.assertTrue(manual_path.exists())
        self.assertTrue(auto_path.exists())

    def test_monthly_report_uses_calendar_month_and_weekly_context(self):
        day = datetime.date(2026, 7, 15)
        diary, _ = self.write_diary(day.isoformat())
        original = diary.read_bytes()
        weekly = (
            settings.ANALYSIS_DIR
            / "Weekly"
            / "2026-07-06_to_2026-07-12_auto.md"
        )
        weekly.parent.mkdir(parents=True)
        weekly.write_text("同期周报分析", encoding="utf-8")

        _, success, report_path = analysis.generate_analysis_report(
            "monthly", day, {"name": "mock"}
        )

        self.assertTrue(success)
        self.assertEqual(
            settings.ANALYSIS_DIR / "Monthly" / "2026-07_manual.md", report_path
        )
        self.assertIn(
            "2026年07月 手动分析月报",
            report_path.read_text(encoding="utf-8"),
        )
        self.assertIn("原始日记范围：2026-07-01 至 2026-07-31", report_path.read_text(encoding="utf-8"))
        self.assertIn("同期周报分析", self.ai_calls[-1])
        self.assertEqual(original, diary.read_bytes())

    def test_report_receives_explicitly_referenced_report_content(self):
        day = datetime.date(2026, 7, 14)
        diary, _ = self.write_diary(day.isoformat())
        source = (
            settings.ANALYSIS_DIR
            / "Weekly"
            / "2026-07-06_to_2026-07-12_manual.md"
        )
        source.parent.mkdir(parents=True)
        source.write_text("被引用周报中的关键判断", encoding="utf-8")
        with diary.open("a", encoding="utf-8") as file:
            file.write(
                "**11:00 [引用]:** "
                "[分析周报 | 2026-07-06 至 2026-07-12]"
                "(<../AnalysisReports/Weekly/2026-07-06_to_2026-07-12_manual.md>)\n\n"
                "由此继续展开。\n\n"
            )

        analysis.generate_analysis_report("daily", day, {"name": "mock"})

        self.assertIn("【本周期显式引用的来源】", self.ai_calls[-1])
        self.assertIn("被引用周报中的关键判断", self.ai_calls[-1])

    def test_reference_context_rejects_paths_outside_data_directories(self):
        outside = Path(self.temp_dir.name) / "outside.md"
        outside.write_text("不应读取的内容", encoding="utf-8")
        logs = [("2026-07-14", "**11:00 [引用]:** [外部](<../outside.md>)")]

        context = analysis._referenced_source_context(logs)

        self.assertNotIn("不应读取的内容", context)

    def test_automatic_tasks_process_yesterday_and_record_state(self):
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        diary, raw_stream = self.write_diary(yesterday.isoformat())

        analysis.run_due_automatic_tasks()

        state_path = settings.ANALYSIS_DIR / ".automation-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(yesterday.isoformat(), state["last_daily_date"])
        self.assertFalse((settings.ANALYSIS_DIR / "Daily" / f"{yesterday.isoformat()}.md").exists())
        self.assertIn("<summary>\n测试生成内容\n</summary>", diary.read_text(encoding="utf-8"))
        self.assertTrue(diary.read_text(encoding="utf-8").endswith(raw_stream))

    def test_automatic_tasks_generate_last_complete_week_report(self):
        today = datetime.date.today()
        week_end = today - datetime.timedelta(days=today.weekday() + 1)
        week_start = week_end - datetime.timedelta(days=6)
        self.write_diary(week_start.isoformat())
        settings.CONFIG["automation"] = {
            "enabled": True,
            "daily_summary": False,
            "weekly_report": True,
            "monthly_report": False,
        }

        analysis.run_due_automatic_tasks()

        report_path = (
            settings.ANALYSIS_DIR
            / "Weekly"
            / f"{week_start.isoformat()}_to_{week_end.isoformat()}_auto.md"
        )
        state = json.loads(
            (settings.ANALYSIS_DIR / ".automation-state.json").read_text(encoding="utf-8")
        )
        self.assertTrue(report_path.exists())
        self.assertEqual(week_end.isoformat(), state["last_week_end"])

    def test_monthly_task_generates_last_complete_calendar_month_once(self):
        today = datetime.date(2026, 8, 15)
        self.write_diary("2026-07-10")
        state = {}
        model = {"name": "mock"}

        analysis._run_monthly_reports(today, state, model)
        calls_after_first_run = len(self.ai_calls)
        analysis._run_monthly_reports(today, state, model)

        report_path = settings.ANALYSIS_DIR / "Monthly" / "2026-07_auto.md"
        self.assertTrue(report_path.exists())
        self.assertEqual("2026-07-31", state["last_month_end"])
        self.assertEqual(calls_after_first_run, len(self.ai_calls))

    def test_monthly_task_catches_up_from_last_successful_month(self):
        self.write_diary("2026-06-10")
        self.write_diary("2026-07-10")
        state = {"last_month_end": "2026-05-31"}

        analysis._run_monthly_reports(
            datetime.date(2026, 8, 15), state, {"name": "mock"}
        )

        self.assertTrue(
            (settings.ANALYSIS_DIR / "Monthly" / "2026-06_auto.md").exists()
        )
        self.assertTrue(
            (settings.ANALYSIS_DIR / "Monthly" / "2026-07_auto.md").exists()
        )
        self.assertEqual("2026-07-31", state["last_month_end"])

    def test_failed_monthly_task_keeps_position_and_records_error(self):
        self.write_diary("2026-07-10")

        def failed_call_ai(prompt, model_cfg):
            return "失败", False, 0, {}, 0

        analysis.call_ai = failed_call_ai
        state = {}
        analysis._run_monthly_reports(
            datetime.date(2026, 8, 1), state, {"name": "mock"}
        )

        self.assertNotIn("last_month_end", state)
        self.assertIn("monthly_report", state["errors"])

    def test_installs_hourly_cron_task_for_one_shot_runner(self):
        listed = Mock(returncode=0, stdout="15 2 * * * existing\n", stderr="")
        installed = Mock(returncode=0, stdout="", stderr="")
        with patch("analysis._is_windows", return_value=False), patch(
            "analysis.subprocess.run", side_effect=[listed, installed]
        ) as run:
            success, _ = analysis.install_system_automation()

        self.assertTrue(success)
        cron_input = run.call_args_list[1].kwargs["input"]
        self.assertIn("5 * * * *", cron_input)
        self.assertIn("--run-automation", cron_input)
        self.assertIn("# AgentRecord automation", cron_input)

    def test_installs_windows_scheduled_task_for_one_shot_runner(self):
        installed = Mock(returncode=0, stdout="", stderr="")
        with patch("analysis._is_windows", return_value=True), patch(
            "analysis.subprocess.run", return_value=installed
        ) as run:
            success, _ = analysis.install_system_automation()

        self.assertTrue(success)
        command = run.call_args.args[0]
        self.assertEqual("schtasks", command[0])
        self.assertIn("/TR", command)
        self.assertIn("--run-automation", command[command.index("/TR") + 1])

if __name__ == "__main__":
    unittest.main()
