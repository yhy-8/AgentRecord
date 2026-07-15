import unittest

from AgentRecord.agents import AGENTS, explorer, report, world
from AgentRecord.agents.base import AgentPipelineError
from AgentRecord.agents.graph import inherit_source_refs


class AgentModuleTests(unittest.TestCase):
    def test_six_agents_have_independent_registered_specs(self):
        self.assertEqual(
            {"extractor", "cluster", "explorer", "world", "reviewer", "report"},
            set(AGENTS),
        )
        self.assertEqual(frozenset({"web_search"}), AGENTS["world"].allowed_tools)
        for name, spec in AGENTS.items():
            if name != "world":
                self.assertEqual(frozenset(), spec.allowed_tools)

    def test_explorer_removes_basic_private_data_from_search_queries(self):
        queries = explorer.clean_research_queries(
            {
                "research_queries": [
                    {
                        "target_id": "insight-1",
                        "query": "查 user@example.com 13800138000 /home/user/private.txt",
                        "reason": "核查",
                    }
                ]
            },
            {"insight-1"},
        )

        self.assertEqual(1, len(queries))
        self.assertIn("[email]", queries[0]["query"])
        self.assertIn("[number]", queries[0]["query"])
        self.assertIn("[local-path]", queries[0]["query"])

    def test_visible_node_reference_inherits_original_source(self):
        payload = {
            "nodes": [
                {
                    "temp_id": "theme-1",
                    "source_refs": ["evidence-1"],
                    "metadata": {},
                }
            ],
            "edges": [],
        }

        normalized = inherit_source_refs(
            payload,
            allowed_source_ids={"R-20260714-001"},
            visible_nodes={
                "evidence-1": {"source_refs": ["R-20260714-001"]}
            },
        )

        self.assertEqual(
            ["R-20260714-001"], normalized["nodes"][0]["source_refs"]
        )
        self.assertEqual(["evidence-1"], payload["nodes"][0]["source_refs"])

    def test_world_rejects_verified_claim_without_external_url(self):
        payload = {
            "nodes": [
                {
                    "temp_id": "research-1",
                    "node_type": "research",
                    "title": "核查结果",
                    "body": "声称已经核查。",
                    "confidence": 0.8,
                    "source_refs": [],
                    "metadata": {
                        "target_id": "insight-1",
                        "result": "supported",
                        "sources": [],
                    },
                }
            ],
            "edges": [],
        }

        with self.assertRaises(AgentPipelineError):
            world.validate(
                payload,
                allowed_source_ids=set(),
                visible_node_ids={"insight-1"},
            )

    def test_report_rejects_unknown_source_reference(self):
        errors = report.validation_errors(
            "## 洞见\n\n内容 [R-20260714-999]", {"R-20260714-001"}
        )

        self.assertTrue(any("未知来源" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
