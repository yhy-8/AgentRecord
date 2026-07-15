import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main
import settings


class MainCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.original_diary_dir = settings.DIARY_DIR
        self.original_analysis_dir = settings.ANALYSIS_DIR
        settings.DIARY_DIR = root / "Records"
        settings.ANALYSIS_DIR = root / "AnalysisReports"
        settings.DIARY_DIR.mkdir()

    def tearDown(self):
        settings.DIARY_DIR = self.original_diary_dir
        settings.ANALYSIS_DIR = self.original_analysis_dir
        self.temp_dir.cleanup()

    def test_reference_command_selects_report_and_records_note(self):
        report = (
            settings.ANALYSIS_DIR
            / "Weekly"
            / "2026-07-06_to_2026-07-12_auto.md"
        )
        report.parent.mkdir(parents=True)
        report.write_text("报告", encoding="utf-8")

        with patch("main.safe_input", side_effect=["1", "由周报展开的想法"]):
            main._handle_reference("/ref weekly")

        content = next(settings.DIARY_DIR.glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("[引用]", content)
        self.assertIn("自动分析周报 | 2026-07-06 至 2026-07-12", content)
        self.assertIn("由周报展开的想法", content)

    def test_parses_monthly_analysis_command(self):
        self.assertEqual(
            ("monthly", "2026-07-15"),
            main._parse_analysis_arguments("/a monthly 2026-07-15"),
        )

    @patch("main.generate_analysis_report", return_value=("月报", True, Path("report.md")))
    def test_monthly_analysis_accepts_year_month(self, generate_report):
        main._handle_analysis("/a monthly 2026-07", {"name": "mock"})

        self.assertEqual("monthly", generate_report.call_args.args[0])
        self.assertEqual(datetime.date(2026, 7, 1), generate_report.call_args.args[1])
        self.assertEqual("manual", generate_report.call_args.kwargs["origin"])

    @patch("main.generate_analysis_report")
    def test_existing_manual_report_requires_confirmation(self, generate_report):
        report = (
            settings.ANALYSIS_DIR
            / "Weekly"
            / "2026-07-13_to_2026-07-19_manual.md"
        )
        report.parent.mkdir(parents=True)
        report.write_text("已有手动报告", encoding="utf-8")

        with patch("main.safe_input", return_value="n"):
            main._handle_analysis("/a weekly 2026-07-15", {"name": "mock"})

        generate_report.assert_not_called()

    def test_help_commands_are_separated_by_mode(self):
        self.assertEqual(
            {"/h", "/mode", "/v", "/ref", "/d", "/c"},
            set(main.MODE_COMMANDS[main.RECORD_MODE]),
        )
        self.assertEqual(
            {"/h", "/mode", "/s", "/a", "/m"},
            set(main.MODE_COMMANDS[main.REPORT_MODE]),
        )

    def test_windows_background_entry_hides_console_window(self):
        windll = Mock()
        windll.kernel32.GetConsoleWindow.return_value = 123
        with patch.object(main.sys, "platform", "win32"), patch.object(
            main.sys, "argv", ["AgentRecord.exe", "--run-automation"]
        ), patch.object(main.ctypes, "windll", windll, create=True):
            main._hide_background_console()

        windll.user32.ShowWindow.assert_called_once_with(123, 0)

    @patch("main.journal.append_log")
    @patch("main.show_help")
    @patch("main.safe_input", side_effect=["@这只是普通记录", EOFError])
    def test_at_prefix_is_saved_as_plain_record(
        self, safe_input, show_help, append_log
    ):
        submitted_at = datetime.datetime(2026, 7, 16, 0, 1)
        with patch("main.datetime.datetime") as mock_datetime:
            mock_datetime.now.return_value = submitted_at
            main.main()

        append_log.assert_called_once()
        self.assertEqual("@这只是普通记录", append_log.call_args.args[0])
        self.assertEqual(submitted_at, append_log.call_args.kwargs["submitted_at"])

    @patch("main.journal.append_log")
    @patch("main.show_help")
    @patch("main.safe_input", side_effect=["/mode", "不会误记为日记", EOFError])
    def test_plain_text_in_report_mode_is_not_recorded(
        self, safe_input, show_help, append_log
    ):
        main.main()

        append_log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
