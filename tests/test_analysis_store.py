import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from AgentRecord.agents import cluster
from AgentRecord.agents.base import AgentPipelineError
from AgentRecord.analysis import orchestrator
from AgentRecord.analysis.store import AnalysisStore


class AnalysisStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = AnalysisStore(Path(self.temp_dir.name) / "analysis.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_completed_run_becomes_parent_of_same_period_rerun(self):
        first, first_parent = self.store.start_run(
            "weekly", "2026-07-06", "2026-07-12", "manual", "mock", "hash-1"
        )
        self.assertIsNone(first_parent)
        self.store.complete_run(first, Path("first.md"))

        second, second_parent = self.store.start_run(
            "weekly", "2026-07-06", "2026-07-12", "manual", "mock", "hash-2"
        )

        self.assertEqual(first, second_parent)
        self.assertEqual("running", self.store.run_record(second)["status"])

    def test_source_mapping_keeps_location_hash_and_bounded_excerpt(self):
        run_id, _ = self.store.start_run(
            "daily", "2026-07-14", "2026-07-14", "manual", "mock", "hash"
        )
        self.store.save_sources(
            run_id,
            [
                {
                    "source_id": "R-20260714-001",
                    "path": "2026-07-14.md",
                    "date": "2026-07-14",
                    "time": "09:00",
                    "record_index": 1,
                    "speaker": "user",
                    "tag": "",
                    "text": "原始记录" * 200,
                }
            ],
        )

        source = self.store.sources_for_run(run_id)[0]
        self.assertEqual("2026-07-14.md", source["relative_path"])
        self.assertEqual(64, len(source["content_hash"]))
        self.assertEqual(500, len(source["excerpt"]))

    def test_accepting_revision_supersedes_previous_accepted_node(self):
        first, _ = self.store.start_run(
            "daily", "2026-07-14", "2026-07-14", "manual", "mock", "hash-1"
        )
        first_ids = self.store.add_nodes(
            first,
            "extractor",
            [
                {
                    "temp_id": "old",
                    "node_type": "evidence",
                    "title": "旧判断",
                    "body": "旧版本",
                    "confidence": 0.7,
                    "source_refs": ["R-20260714-001"],
                    "metadata": {},
                }
            ],
        )
        old_id = first_ids["old"]
        self.store.apply_node_decisions(
            [{"node_id": old_id, "status": "accepted"}]
        )
        self.store.complete_run(first, Path("first.md"))

        second, _ = self.store.start_run(
            "daily", "2026-07-14", "2026-07-14", "manual", "mock", "hash-2"
        )
        second_ids = self.store.add_nodes(
            second,
            "extractor",
            [
                {
                    "temp_id": "new",
                    "node_type": "evidence",
                    "title": "修订判断",
                    "body": "保留历史的修订版本",
                    "confidence": 0.9,
                    "source_refs": ["R-20260714-001"],
                    "metadata": {},
                    "supersedes_id": old_id,
                }
            ],
        )
        new_id = second_ids["new"]
        self.store.apply_node_decisions(
            [{"node_id": new_id, "status": "accepted"}]
        )

        old = self.store.nodes_for_run(first)[0]
        new = self.store.nodes_for_run(second)[0]
        self.assertEqual("superseded", old["status"])
        self.assertEqual("accepted", new["status"])
        self.assertEqual(old_id, new["supersedes_id"])
        self.assertEqual(2, new["revision"])

    def test_relations_are_accepted_only_when_both_nodes_are_accepted(self):
        run_id, _ = self.store.start_run(
            "daily", "2026-07-14", "2026-07-14", "manual", "mock", "hash"
        )
        node_ids = self.store.add_nodes(
            run_id,
            "cluster",
            [
                {
                    "temp_id": "a",
                    "node_type": "evidence",
                    "title": "A",
                    "body": "A",
                    "source_refs": ["R-20260714-001"],
                },
                {
                    "temp_id": "b",
                    "node_type": "theme",
                    "title": "B",
                    "body": "B",
                    "source_refs": ["R-20260714-001"],
                },
            ],
        )
        self.store.add_edges(
            run_id,
            "cluster",
            [
                {
                    "source_id": "a",
                    "target_id": "b",
                    "relation_type": "member_of",
                }
            ],
            node_ids,
        )
        self.store.apply_node_decisions(
            [
                {"node_id": node_ids["a"], "status": "accepted"},
                {"node_id": node_ids["b"], "status": "rejected"},
            ]
        )
        self.store.accept_edges_for_run(run_id, {node_ids["a"]})

        self.assertEqual("rejected", self.store.edges_for_run(run_id)[0]["status"])

    def test_validation_failure_is_saved_as_failed_agent_artifact(self):
        run_id, _ = self.store.start_run(
            "daily", "2026-07-14", "2026-07-14", "manual", "mock", "hash"
        )
        payload = {
            "nodes": [
                {
                    "temp_id": "theme-1",
                    "node_type": "theme",
                    "title": "主题",
                    "body": "内容",
                    "source_refs": ["unknown-source"],
                    "metadata": {"trajectory": "new"},
                }
            ],
            "edges": [],
        }

        with self.assertRaises(AgentPipelineError):
            orchestrator._persist_graph_agent(
                cluster.SPEC,
                cluster.validate,
                payload,
                self.store,
                run_id,
                allowed_source_ids={"R-20260714-001"},
                visible_nodes={},
            )

        connection = sqlite3.connect(self.store.path)
        try:
            status, error = connection.execute(
                "SELECT status, error FROM agent_artifacts WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual("failed", status)
        self.assertIn("未知来源", error)

    def _completed_theme(self) -> tuple[str, str]:
        run_id, _ = self.store.start_run(
            "daily", "2026-07-14", "2026-07-14", "manual", "mock", "hash"
        )
        node_id = self.store.add_nodes(
            run_id,
            "cluster",
            [
                {
                    "temp_id": "theme",
                    "node_type": "theme",
                    "title": "原观念",
                    "body": "原内容",
                    "confidence": 0.8,
                    "source_refs": ["R-20260714-001"],
                }
            ],
        )["theme"]
        self.store.apply_node_decisions([{"node_id": node_id, "status": "accepted"}])
        self.store.complete_run(run_id, Path("report.md"))
        return run_id, node_id

    def test_user_correction_creates_auditable_accepted_revision(self):
        run_id, old_id = self._completed_theme()

        new_id = self.store.record_user_feedback(
            old_id, "correct", title="修正后观念", body="修正后内容"
        )

        nodes = {node["id"]: node for node in self.store.nodes_for_run(run_id)}
        self.assertEqual("superseded", nodes[old_id]["status"])
        self.assertEqual("accepted", nodes[new_id]["status"])
        self.assertEqual("user", nodes[new_id]["created_by"])
        self.assertEqual(old_id, nodes[new_id]["supersedes_id"])
        self.assertEqual(2, nodes[new_id]["revision"])
        self.assertEqual("修正后观念", nodes[new_id]["title"])

    def test_user_rejection_removes_node_from_feedback_candidates(self):
        _, node_id = self._completed_theme()
        self.assertEqual(node_id, self.store.feedback_candidates()[0]["id"])

        self.store.record_user_feedback(node_id, "reject")

        self.assertEqual([], self.store.feedback_candidates())

    def test_v1_database_is_backed_up_and_migrated_to_v2(self):
        with closing(sqlite3.connect(self.store.path)) as connection:
            connection.execute("DROP TABLE node_feedback")
            connection.execute("PRAGMA user_version = 1")
            connection.commit()

        AnalysisStore(self.store.path)

        with closing(sqlite3.connect(self.store.path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'node_feedback'"
            ).fetchone()
        self.assertEqual(2, version)
        self.assertIsNotNone(table)
        self.assertTrue(Path(f"{self.store.path}.v1.bak").exists())


if __name__ == "__main__":
    unittest.main()
