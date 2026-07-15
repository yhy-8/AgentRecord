import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentrecord import journal, settings


class JournalTests(unittest.TestCase):
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

    def test_lists_reference_sources_by_type_and_keyword(self):
        weekly_dir = settings.ANALYSIS_DIR / "Weekly"
        weekly_dir.mkdir(parents=True)
        old = weekly_dir / "2026-06-29_to_2026-07-05_manual.md"
        latest = weekly_dir / "2026-07-06_to_2026-07-12_auto.md"
        old.write_text("旧周报", encoding="utf-8")
        latest.write_text("新周报", encoding="utf-8")
        legacy = weekly_dir / "2026-06-01_to_2026-06-07.md"
        legacy.write_text("未投入生产的旧格式", encoding="utf-8")

        sources = journal.list_reference_sources("weekly")
        filtered = journal.list_reference_sources("weekly", "2026-06-29")

        self.assertEqual(latest, sources[0][1])
        self.assertEqual("自动分析周报 | 2026-07-06 至 2026-07-12", sources[0][0])
        self.assertEqual(
            [("手动分析周报 | 2026-06-29 至 2026-07-05", old)], filtered
        )
        self.assertNotIn(legacy, [path for _, path in sources])

    def test_appends_portable_reference_with_note_and_timestamp(self):
        report = (
            settings.ANALYSIS_DIR
            / "Weekly"
            / "2026-07-06_to_2026-07-12_manual.md"
        )
        report.parent.mkdir(parents=True)
        report.write_text("周报", encoding="utf-8")
        label = "手动分析周报 | 2026-07-06 至 2026-07-12"
        fixed_now = datetime.datetime(2026, 7, 15, 14, 32)

        with patch("agentrecord.journal.datetime.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            journal.append_reference(label, report, "继续展开的想法")

        content = (settings.DIARY_DIR / "2026-07-15.md").read_text(encoding="utf-8")
        self.assertIn("**14:32 [引用]:**", content)
        self.assertIn(f"[{label}](<../AnalysisReports/Weekly/{report.name}>)", content)
        self.assertIn("继续展开的想法", content)

    def test_plain_record_uses_one_submission_time_across_midnight(self):
        submitted_at = datetime.datetime(2026, 7, 15, 23, 59, 59)
        after_midnight = datetime.datetime(2026, 7, 16, 0, 0, 0)

        with patch("agentrecord.journal.datetime.datetime") as mock_datetime:
            mock_datetime.now.side_effect = [submitted_at, after_midnight]
            journal.append_log("跨午夜提交")

        mock_datetime.now.assert_called_once_with()
        content = (settings.DIARY_DIR / "2026-07-15.md").read_text(encoding="utf-8")
        self.assertTrue(content.startswith("# 2026-07-15\n"))
        self.assertIn("**23:59:** 跨午夜提交", content)
        self.assertFalse((settings.DIARY_DIR / "2026-07-16.md").exists())

    def test_reference_uses_one_submission_time_across_midnight(self):
        report = settings.ANALYSIS_DIR / "Monthly" / "2026-06.md"
        report.parent.mkdir(parents=True)
        report.write_text("月报", encoding="utf-8")
        submitted_at = datetime.datetime(2026, 7, 15, 23, 59, 59)
        after_midnight = datetime.datetime(2026, 7, 16, 0, 0, 0)

        with patch("agentrecord.journal.datetime.datetime") as mock_datetime:
            mock_datetime.now.side_effect = [submitted_at, after_midnight]
            journal.append_reference("分析月报 | 2026-06", report, "跨午夜引用")

        mock_datetime.now.assert_called_once_with()
        content = (settings.DIARY_DIR / "2026-07-15.md").read_text(encoding="utf-8")
        self.assertIn("**23:59 [引用]:**", content)
        self.assertIn("跨午夜引用", content)
        self.assertFalse((settings.DIARY_DIR / "2026-07-16.md").exists())

    def test_delete_last_record_removes_multiline_reference_only(self):
        fixed_now = datetime.datetime(2026, 7, 15, 9, 0)
        with patch("agentrecord.journal.datetime.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            journal.append_log("先前记录")
            journal.append_log("[日记 | 2026-07-14](<2026-07-14.md>)\n\n关联想法", "[引用]")

        self.assertTrue(journal.delete_last_record())
        content = (settings.DIARY_DIR / "2026-07-15.md").read_text(encoding="utf-8")
        self.assertIn("先前记录", content)
        self.assertNotIn("关联想法", content)


if __name__ == "__main__":
    unittest.main()
