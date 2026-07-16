import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from AgentRecord.analysis.store import AnalysisStore, SCHEMA_VERSION


class AnalysisStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "analysis.sqlite3"
        self.store = AnalysisStore(self.path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def start_run(self, *, trigger="manual"):
        return self.store.start_run(
            "weekly",
            "2026-07-06",
            "2026-07-12",
            "manual" if trigger == "manual" else "auto",
            "mock",
            "hash",
            trigger=trigger,
        )

    def save_source(self, run_id, source_id="R-20260707-001", date="2026-07-07"):
        self.store.save_sources(
            run_id,
            [
                {
                    "source_id": source_id,
                    "path": f"{date}.md",
                    "date": date,
                    "time": "09:00",
                    "record_index": 1,
                    "speaker": "user",
                    "tag": "",
                    "text": "原始记录" * 200,
                }
            ],
        )

    def test_new_schema_has_profile_store_and_no_generic_graph(self):
        with closing(sqlite3.connect(self.path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertEqual(SCHEMA_VERSION, version)
        self.assertIn("profile_entries", tables)
        self.assertNotIn("knowledge_edges", tables)

    def test_source_catalog_keeps_location_hash_and_excerpt(self):
        run_id = self.start_run()
        self.save_source(run_id)
        source = self.store.source_records(["R-20260707-001"])[0]
        self.assertEqual("2026-07-07.md", source["relative_path"])
        self.assertEqual(64, len(source["content_hash"]))
        self.assertEqual(500, len(source["excerpt"]))

    def test_accepted_profile_revision_supersedes_previous(self):
        first = self.start_run()
        self.save_source(first)
        old_id = self.store.save_profile_entries(
            first,
            [
                {
                    "temp_id": "p1",
                    "category": "principle",
                    "title": "旧理念",
                    "statement": "旧内容",
                    "confidence": 0.8,
                    "source_refs": ["R-20260707-001"],
                    "first_observed": "2026-07-07",
                    "last_observed": "2026-07-07",
                    "supersedes_id": None,
                }
            ],
            {"p1": "accepted"},
        )["p1"]
        self.store.complete_run(first, Path("first.md"))

        second = self.store.start_run(
            "weekly", "2026-07-13", "2026-07-19", "manual", "mock", "hash2"
        )
        self.save_source(second, "R-20260714-001", "2026-07-14")
        new_id = self.store.save_profile_entries(
            second,
            [
                {
                    "temp_id": "p2",
                    "category": "principle",
                    "title": "新理念",
                    "statement": "修订内容",
                    "confidence": 0.9,
                    "source_refs": ["R-20260714-001"],
                    "first_observed": "2026-07-07",
                    "last_observed": "2026-07-14",
                    "supersedes_id": old_id,
                }
            ],
            {"p2": "accepted"},
        )["p2"]

        active = self.store.active_profiles("2026-07-19")
        self.assertEqual([old_id], [item["id"] for item in active])
        self.store.complete_run(second, Path("second.md"))
        active = self.store.active_profiles("2026-07-19")
        self.assertEqual([new_id], [item["id"] for item in active])

    def test_failed_profile_revision_leaves_completed_version_active(self):
        first = self.start_run()
        self.save_source(first)
        old_id = self.store.save_profile_entries(
            first,
            [
                {
                    "temp_id": "p1",
                    "category": "viewpoint",
                    "title": "已交付观点",
                    "statement": "旧内容",
                    "confidence": 0.8,
                    "source_refs": ["R-20260707-001"],
                    "first_observed": "2026-07-07",
                    "last_observed": "2026-07-07",
                    "supersedes_id": None,
                }
            ],
            {"p1": "accepted"},
        )["p1"]
        self.store.complete_run(first, Path("first.md"))

        failed = self.store.start_run(
            "weekly", "2026-07-13", "2026-07-19", "manual", "mock", "hash2"
        )
        self.save_source(failed, "R-20260714-001", "2026-07-14")
        self.store.save_profile_entries(
            failed,
            [
                {
                    "temp_id": "p2",
                    "category": "viewpoint",
                    "title": "未交付修订",
                    "statement": "新内容",
                    "confidence": 0.9,
                    "source_refs": ["R-20260714-001"],
                    "first_observed": "2026-07-07",
                    "last_observed": "2026-07-14",
                    "supersedes_id": old_id,
                }
            ],
            {"p2": "accepted"},
        )
        self.store.fail_run(failed, "研究板块失败")

        active = self.store.active_profiles("2026-07-19")
        self.assertEqual([old_id], [item["id"] for item in active])

    def test_profile_cutoff_blocks_future_information(self):
        run_id = self.start_run()
        self.save_source(run_id, "R-20260720-001", "2026-07-20")
        self.store.save_profile_entries(
            run_id,
            [
                {
                    "temp_id": "p1",
                    "category": "interest",
                    "title": "未来关注",
                    "statement": "七月二十日才出现",
                    "confidence": 0.8,
                    "source_refs": ["R-20260720-001"],
                    "first_observed": "2026-07-20",
                    "last_observed": "2026-07-20",
                    "supersedes_id": None,
                }
            ],
            {"p1": "accepted"},
        )
        self.store.complete_run(run_id, Path("future.md"))
        self.assertEqual([], self.store.active_profiles("2026-07-12"))

    def test_user_feedback_is_auditable(self):
        run_id = self.start_run()
        self.save_source(run_id)
        entry_id = self.store.save_profile_entries(
            run_id,
            [
                {
                    "temp_id": "p1",
                    "category": "viewpoint",
                    "title": "原观点",
                    "statement": "原内容",
                    "confidence": 0.8,
                    "source_refs": ["R-20260707-001"],
                    "first_observed": "2026-07-07",
                    "last_observed": "2026-07-07",
                    "supersedes_id": None,
                }
            ],
            {"p1": "accepted"},
        )["p1"]
        self.store.complete_run(run_id, Path("report.md"))
        replacement = self.store.record_user_feedback(
            entry_id, "correct", title="修正观点", body="修正内容"
        )
        candidates = self.store.feedback_candidates()
        self.assertEqual(replacement, candidates[0]["id"])
        self.assertEqual("修正观点", candidates[0]["title"])

    def test_v2_is_backed_up_then_replaced(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
        AnalysisStore(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(3, version)
        self.assertTrue(Path(f"{self.path}.v2.legacy.bak").exists())


if __name__ == "__main__":
    unittest.main()
