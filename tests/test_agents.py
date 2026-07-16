import unittest

from AgentRecord.agents import (
    AGENTS,
    researcher,
    research_planner,
    retrospective,
    reviewer,
)
from AgentRecord.agents.base import AgentPipelineError


class AgentModuleTests(unittest.TestCase):
    def test_four_agents_have_separate_responsibilities(self):
        self.assertEqual(
            {"retrospective", "research_planner", "researcher", "reviewer"},
            set(AGENTS),
        )
        self.assertEqual(
            frozenset({"web_search"}), AGENTS["researcher"].allowed_tools
        )
        self.assertEqual(frozenset(), AGENTS["retrospective"].allowed_tools)

    def test_retrospective_requires_citations_in_each_content_paragraph(self):
        with self.assertRaisesRegex(AgentPipelineError, "没有来源引用"):
            retrospective.validate(
                {
                    "markdown": "第一段没有引用。\n\n第二段 [R-20260714-001]",
                    "profile_entries": [],
                },
                allowed_source_ids={"R-20260714-001"},
                current_source_ids={"R-20260714-001"},
                visible_profile_ids=set(),
            )

    def test_profile_update_requires_current_period_evidence(self):
        with self.assertRaisesRegex(AgentPipelineError, "本周期来源"):
            retrospective.validate(
                {
                    "markdown": "整理内容 [R-20260714-001]",
                    "profile_entries": [
                        {
                            "temp_id": "p1",
                            "category": "viewpoint",
                            "title": "观点",
                            "statement": "一个观点",
                            "confidence": 0.8,
                            "source_refs": ["R-20260701-001"],
                            "supersedes_id": None,
                        }
                    ],
                },
                allowed_source_ids={"R-20260701-001", "R-20260714-001"},
                current_source_ids={"R-20260714-001"},
                visible_profile_ids=set(),
            )

    def test_research_planner_sanitizes_private_query_data(self):
        topics = research_planner.validate(
            {
                "topics": [
                    {
                        "topic_id": "Q001",
                        "title": "公开研究问题",
                        "query": "研究 /mnt/private/a 和 12345678",
                        "reason": "拓宽视野",
                        "origin": "records",
                        "source_refs": ["R-20260714-001"],
                    }
                ]
            },
            {"R-20260714-001"},
        )
        self.assertNotIn("/mnt/private", topics[0]["query"])
        self.assertNotIn("12345678", topics[0]["query"])

    def test_researcher_requires_external_url(self):
        with self.assertRaisesRegex(AgentPipelineError, "外部来源"):
            researcher.validate(
                {
                    "markdown": "研究内容 [R-20260714-001]",
                    "sources": [],
                },
                [
                    {
                        "topic_id": "Q001",
                        "origin": "records",
                        "source_refs": ["R-20260714-001"],
                    }
                ],
                {"R-20260714-001"},
            )

    def test_reviewer_must_decide_every_profile_entry(self):
        with self.assertRaisesRegex(AgentPipelineError, "未审查全部"):
            reviewer.validate(
                {
                    "pass": True,
                    "entry_decisions": [],
                    "unsupported_claims": [],
                    "required_changes": [],
                },
                expected_entry_ids={"p1"},
            )


if __name__ == "__main__":
    unittest.main()
