import unittest

from AgentRecord.agents import AGENTS, explorer, extractor, report, world
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
                        "query": "查 user@example.com 13800138000 /home/user/private.txt /mnt/d/private.md",
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

    def test_extractor_accepts_methodology_evidence(self):
        nodes, _ = extractor.validate(
            {
                "nodes": [
                    {
                        "temp_id": "method-1",
                        "node_type": "evidence",
                        "title": "一种复盘方法",
                        "body": "用户记录了自己反复使用的复盘方法。",
                        "confidence": 0.9,
                        "source_refs": ["R-20260714-001"],
                        "metadata": {
                            "kind": "methodology",
                            "speaker": "user",
                        },
                    }
                ],
                "edges": [],
            },
            allowed_source_ids={"R-20260714-001"},
            visible_node_ids=set(),
        )

        self.assertEqual("methodology", nodes[0]["metadata"]["kind"])

    def test_explorer_requires_query_for_research_needed_node(self):
        payload = {
            "nodes": [
                {
                    "temp_id": "insight-1",
                    "node_type": "insight",
                    "title": "一种问题分析方法",
                    "body": "记录显示用户正在形成一种稳定的问题分析方法。",
                    "confidence": 0.8,
                    "source_refs": ["R-20260714-001"],
                    "metadata": {
                        "insight_type": "methodology",
                        "evidence_for": ["evidence-1"],
                        "evidence_against": [],
                        "inference_level": "medium",
                        "why_it_matters": "可以与外部方法比较并继续发展",
                        "research_needed": True,
                    },
                }
            ],
            "edges": [],
            "research_queries": [],
        }

        with self.assertRaisesRegex(AgentPipelineError, "缺少配套研究问题"):
            explorer.validate(
                payload,
                allowed_source_ids={"R-20260714-001"},
                visible_node_ids={"evidence-1"},
            )

    def test_explorer_accepts_methodology_with_matching_research_query(self):
        payload = {
            "nodes": [
                {
                    "temp_id": "insight-1",
                    "node_type": "insight",
                    "title": "一种问题分析方法",
                    "body": "记录显示用户正在形成一种稳定的问题分析方法。",
                    "confidence": 0.8,
                    "source_refs": ["R-20260714-001"],
                    "metadata": {
                        "insight_type": "methodology",
                        "evidence_for": ["evidence-1"],
                        "evidence_against": [],
                        "inference_level": "medium",
                        "why_it_matters": "可以与外部方法比较并继续发展",
                        "research_needed": True,
                    },
                }
            ],
            "edges": [],
            "research_queries": [
                {
                    "target_id": "insight-1",
                    "query": "问题分析方法的验证、局限和相邻理论",
                    "reason": "寻找反例和延伸方向",
                }
            ],
        }

        nodes, _ = explorer.validate(
            payload,
            allowed_source_ids={"R-20260714-001"},
            visible_node_ids={"evidence-1"},
        )

        self.assertEqual("methodology", nodes[0]["metadata"]["insight_type"])

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
