import unittest

from AgentRecord.agents import (
    AGENTS,
    researcher,
    research_planner,
    retrospective,
    reviewer,
)
from AgentRecord.agents.base import (
    AgentPipelineError,
    _parse_json,
    _prompt,
    cited_source_ids,
)


class AgentModuleTests(unittest.TestCase):
    def test_json_parser_accepts_one_outer_markdown_fence(self):
        self.assertEqual(
            {"markdown": "内容"},
            _parse_json('```json\n{"markdown":"内容"}\n```'),
        )

    def test_json_parser_does_not_extract_json_from_explanatory_prose(self):
        with self.assertRaisesRegex(AgentPipelineError, "JSON 无法解析"):
            _parse_json('我已经完成了：\n{"markdown":"内容"}')

    def test_json_parser_recovers_lone_trailing_delimiters(self):
        self.assertEqual(
            {"markdown": "内容"},
            _parse_json('{"markdown":"内容"}"}'),
        )

    def test_json_parser_rejects_two_concatenated_objects(self):
        with self.assertRaisesRegex(AgentPipelineError, "JSON 无法解析"):
            _parse_json('{"markdown":"第一份"}{"markdown":"第二份"}')

    def test_four_agents_have_separate_responsibilities(self):
        self.assertEqual(
            {"retrospective", "research_planner", "researcher", "reviewer"},
            set(AGENTS),
        )
        self.assertEqual(frozenset(), AGENTS["researcher"].allowed_tools)
        self.assertEqual(
            frozenset({"web_search"}), researcher.NATIVE_SEARCH_SPEC.allowed_tools
        )
        self.assertEqual(frozenset(), AGENTS["retrospective"].allowed_tools)
        self.assertTrue(AGENTS["reviewer"].can_read_raw)

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

    def test_grouped_record_citations_are_all_recognized(self):
        markdown = "整理内容 [R-20260714-001, R-20260714-002]"

        result, _ = retrospective.validate(
            {"markdown": markdown, "profile_entries": []},
            allowed_source_ids={"R-20260714-001", "R-20260714-002"},
            current_source_ids={"R-20260714-001", "R-20260714-002"},
            visible_profile_ids=set(),
        )

        self.assertEqual(markdown, result)

    def test_record_range_citation_expands_for_review_context(self):
        self.assertEqual(
            {
                "R-20260707-007",
                "R-20260707-008",
                "R-20260707-009",
                "R-20260707-010",
            },
            cited_source_ids("采购过程 [R-20260707-007~010]"),
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

    def test_behavior_pattern_requires_two_distinct_records(self):
        with self.assertRaisesRegex(AgentPipelineError, "至少两条"):
            retrospective.validate(
                {
                    "markdown": "整理内容 [R-20260714-001]",
                    "profile_entries": [
                        {
                            "temp_id": "p1",
                            "category": "behavior_pattern",
                            "title": "行为模式",
                            "statement": "反复表现出的模式",
                            "confidence": 0.8,
                            "source_refs": ["R-20260714-001"],
                            "supersedes_id": None,
                        }
                    ],
                },
                allowed_source_ids={"R-20260714-001"},
                current_source_ids={"R-20260714-001"},
                visible_profile_ids=set(),
            )

    def test_profile_candidate_cannot_duplicate_an_existing_profile(self):
        profile = {
            "category": "viewpoint",
            "title": "已有观点",
            "statement": "已有内容",
        }
        with self.assertRaisesRegex(AgentPipelineError, "与现有条目重复"):
            retrospective.validate(
                {
                    "markdown": "整理内容 [R-20260714-001]",
                    "profile_entries": [
                        {
                            "temp_id": "p1",
                            **profile,
                            "confidence": 0.8,
                            "source_refs": ["R-20260714-001"],
                            "supersedes_id": None,
                        }
                    ],
                },
                allowed_source_ids={"R-20260714-001"},
                current_source_ids={"R-20260714-001"},
                visible_profile_ids={"entry-id"},
                visible_profiles={"entry-id": profile},
            )

    def test_one_output_cannot_repeat_the_same_profile_candidate(self):
        candidate = {
            "category": "interest",
            "title": "长期关注",
            "statement": "持续关注同一领域",
            "confidence": 0.8,
            "source_refs": ["R-20260714-001"],
            "supersedes_id": None,
        }
        with self.assertRaisesRegex(AgentPipelineError, "重复的人物画像候选"):
            retrospective.validate(
                {
                    "markdown": "整理内容 [R-20260714-001]",
                    "profile_entries": [
                        {"temp_id": "p1", **candidate},
                        {"temp_id": "p2", **candidate},
                    ],
                },
                allowed_source_ids={"R-20260714-001"},
                current_source_ids={"R-20260714-001"},
                visible_profile_ids=set(),
            )

    def test_research_planner_sanitizes_private_query_data(self):
        topics = research_planner.validate(
            {
                "topics": [
                    {
                        "topic_id": "Q001",
                        "title": "公开研究问题 D:/private/title.txt",
                        "query": "研究 /private/a 和 12345678",
                        "reason": "拓宽视野",
                        "origin": "records",
                        "source_refs": ["R-20260714-001"],
                    }
                ]
            },
            {"R-20260714-001"},
        )
        self.assertNotIn("/private", topics[0]["query"])
        self.assertNotIn("D:/private", topics[0]["title"])
        self.assertNotIn("12345678", topics[0]["query"])

    def test_research_planner_accepts_up_to_five_topics(self):
        topics = research_planner.validate(
            {
                "topics": [
                    {
                        "topic_id": f"Q{index:03d}",
                        "title": f"主题 {index}",
                        "query": f"公开查询 {index}",
                        "reason": "值得研究",
                        "origin": "news",
                        "source_refs": [],
                    }
                    for index in range(1, 6)
                ]
            },
            set(),
        )

        self.assertEqual(5, len(topics))

    def test_research_planner_rejects_more_than_five_topics(self):
        with self.assertRaisesRegex(AgentPipelineError, "一至五个"):
            research_planner.validate(
                {
                    "topics": [
                        {
                            "topic_id": f"Q{index:03d}",
                            "title": f"主题 {index}",
                            "query": f"公开查询 {index}",
                            "reason": "值得研究",
                            "origin": "news",
                            "source_refs": [],
                        }
                        for index in range(1, 7)
                    ]
                },
                set(),
            )

    def test_researcher_requires_external_url(self):
        with self.assertRaisesRegex(AgentPipelineError, "外部来源"):
            researcher.validate(
                {
                    "markdown": "### 记录主题\n\n研究内容 [R-20260714-001]",
                    "sources": [],
                },
                [
                    {
                        "topic_id": "Q001",
                        "title": "记录主题",
                        "origin": "records",
                        "source_refs": ["R-20260714-001"],
                    }
                ],
                {"R-20260714-001"},
            )

    def test_researcher_accepts_equivalent_percent_encoded_markdown_url(self):
        markdown, _ = researcher.validate(
            {
                "markdown": (
                    "### 公开主题\n\n外部事实 [论文]"
                    "(https://doi.org/10.1037%2F0022-006X.50.6.880)"
                ),
                "sources": [
                    {
                        "topic_id": "Q001",
                        "title": "论文",
                        "url": "https://doi.org/10.1037/0022-006X.50.6.880",
                    }
                ],
            },
            [
                {
                    "topic_id": "Q001",
                    "title": "公开主题",
                    "origin": "news",
                    "source_refs": [],
                }
            ],
            set(),
        )

        self.assertIn("%2F", markdown)

    def test_researcher_discards_unused_source_metadata(self):
        used = "https://example.com/used"
        markdown, sources = researcher.validate(
            {
                "markdown": f"### 公开主题\n\n外部事实 [来源]({used})",
                "sources": [
                    {"topic_id": "Q001", "title": "采用", "url": used},
                    {
                        "topic_id": "Q001",
                        "title": "未采用备选",
                        "url": "https://example.com/unused",
                    },
                ],
            },
            [
                {
                    "topic_id": "Q001",
                    "title": "公开主题",
                    "origin": "news",
                    "source_refs": [],
                }
            ],
            set(),
        )

        self.assertIn(used, markdown)
        self.assertEqual([used], [source["url"] for source in sources])

    def test_researcher_rejects_markdown_link_missing_from_sources(self):
        with self.assertRaisesRegex(AgentPipelineError, "未列入 sources"):
            researcher.validate(
                {
                    "markdown": (
                        "### 公开主题\n\n"
                        "事实 [已声明](https://example.com/declared)，"
                        "另一个事实 [未声明](https://example.com/undeclared)。"
                    ),
                    "sources": [
                        {
                            "topic_id": "Q001",
                            "title": "已声明",
                            "url": "https://example.com/declared",
                        }
                    ],
                },
                [
                    {
                        "topic_id": "Q001",
                        "title": "公开主题",
                        "origin": "news",
                        "source_refs": [],
                    }
                ],
                set(),
            )

    def test_grounded_researcher_uses_controller_owned_evidence_ids(self):
        topics = [
            {
                "topic_id": "Q001",
                "title": "记录与研究",
                "origin": "records",
                "source_refs": ["R-20260714-001"],
            }
        ]
        evidence = [
            {
                "source_id": "W-Q001-001",
                "topic_id": "Q001",
                "title": "权威来源",
                "url": "https://example.com/article_(one)",
                "published": "2026-07-14",
            }
        ]

        grounded, cited = researcher.validate_grounded(
            {
                "markdown": (
                    "### 记录与研究\n\n"
                    "该问题由记录引出 [R-20260714-001]，"
                    "外部证据说明了适用边界 [W-Q001-001]。"
                )
            },
            topics,
            evidence,
            {"R-20260714-001"},
        )
        rendered, sources = researcher.render_grounded(
            grounded, cited, evidence
        )

        self.assertNotIn("W-Q001-001", rendered)
        self.assertIn("https://example.com/article_%28one%29", rendered)
        self.assertEqual(["W-Q001-001"], cited)
        self.assertEqual(["https://example.com/article_(one)"], [s["url"] for s in sources])

    def test_grounded_researcher_rejects_model_written_url(self):
        with self.assertRaisesRegex(AgentPipelineError, "不得自行输出 URL"):
            researcher.validate_grounded(
                {"markdown": "事实 [来源](https://example.com) [W-Q001-001]"},
                [
                    {
                        "topic_id": "Q001",
                        "title": "公开主题",
                        "origin": "news",
                        "source_refs": [],
                    }
                ],
                [
                    {
                        "source_id": "W-Q001-001",
                        "topic_id": "Q001",
                        "url": "https://example.com",
                    }
                ],
                set(),
            )

    def test_grounded_researcher_requires_evidence_for_every_topic(self):
        with self.assertRaisesRegex(AgentPipelineError, "Q002"):
            researcher.validate_grounded(
                {
                    "markdown": (
                        "### 主题一\n\n已覆盖 [W-Q001-001]。\n\n"
                        "### 主题二\n\n暂无外部证据。"
                    )
                },
                [
                    {
                        "topic_id": "Q001",
                        "title": "主题一",
                        "origin": "news",
                        "source_refs": [],
                    },
                    {
                        "topic_id": "Q002",
                        "title": "主题二",
                        "origin": "news",
                        "source_refs": [],
                    },
                ],
                [
                    {
                        "source_id": "W-Q001-001",
                        "topic_id": "Q001",
                        "url": "https://example.com/one",
                    },
                    {
                        "source_id": "W-Q002-001",
                        "topic_id": "Q002",
                        "url": "https://example.com/two",
                    },
                ],
                set(),
            )

    def test_researcher_requires_record_citation_for_each_driven_topic(self):
        with self.assertRaisesRegex(AgentPipelineError, "Q002"):
            researcher.validate(
                {
                    "markdown": (
                        "### 主题一\n\n[R-20260714-001] "
                        "[来源一](https://example.com/one)\n\n"
                        "### 主题二\n\n缺少记录引用 "
                        "[来源二](https://example.com/two)"
                    ),
                    "sources": [
                        {
                            "topic_id": f"Q{index:03d}",
                            "title": "来源",
                            "url": f"https://example.com/{word}",
                        }
                        for index, word in ((1, "one"), (2, "two"))
                    ],
                },
                [
                    {
                        "topic_id": "Q001",
                        "title": "主题一",
                        "origin": "records",
                        "source_refs": ["R-20260714-001"],
                    },
                    {
                        "topic_id": "Q002",
                        "title": "主题二",
                        "origin": "records",
                        "source_refs": ["R-20260714-002"],
                    },
                ],
                {"R-20260714-001", "R-20260714-002"},
            )

    def test_grounded_researcher_requires_one_ordered_heading_per_topic(self):
        topics = [
            {
                "topic_id": "Q001",
                "title": "主题一",
                "origin": "news",
                "source_refs": [],
            },
            {
                "topic_id": "Q002",
                "title": "主题二",
                "origin": "news",
                "source_refs": [],
            },
        ]
        evidence = [
            {
                "source_id": "W-Q001-001",
                "topic_id": "Q001",
                "url": "https://example.com/one",
            },
            {
                "source_id": "W-Q002-001",
                "topic_id": "Q002",
                "url": "https://example.com/two",
            },
        ]

        with self.assertRaisesRegex(AgentPipelineError, "三级标题"):
            researcher.validate_grounded(
                {
                    "markdown": (
                        "### 主题二\n\n[W-Q002-001]\n\n"
                        "### 主题一\n\n[W-Q001-001]"
                    )
                },
                topics,
                evidence,
                set(),
            )

    def test_grounded_researcher_rejects_evidence_under_wrong_topic(self):
        with self.assertRaisesRegex(AgentPipelineError, "其他主题"):
            researcher.validate_grounded(
                {
                    "markdown": (
                        "### 主题一\n\n[W-Q002-001]\n\n"
                        "### 主题二\n\n[W-Q002-001]"
                    )
                },
                [
                    {
                        "topic_id": "Q001",
                        "title": "主题一",
                        "origin": "news",
                        "source_refs": [],
                    },
                    {
                        "topic_id": "Q002",
                        "title": "主题二",
                        "origin": "news",
                        "source_refs": [],
                    },
                ],
                [
                    {
                        "source_id": "W-Q001-001",
                        "topic_id": "Q001",
                        "url": "https://example.com/one",
                    },
                    {
                        "source_id": "W-Q002-001",
                        "topic_id": "Q002",
                        "url": "https://example.com/two",
                    },
                ],
                set(),
            )

    def test_profile_cannot_be_superseded_twice_in_one_report(self):
        entries = [
            {
                "temp_id": temp_id,
                "category": "viewpoint",
                "title": temp_id,
                "statement": "更新",
                "confidence": 0.8,
                "source_refs": ["R-20260714-001"],
                "supersedes_id": "profile-1",
            }
            for temp_id in ("p1", "p2")
        ]
        with self.assertRaisesRegex(AgentPipelineError, "多个候选"):
            retrospective.validate(
                {
                    "markdown": "整理 [R-20260714-001]",
                    "profile_entries": entries,
                },
                allowed_source_ids={"R-20260714-001"},
                current_source_ids={"R-20260714-001"},
                visible_profile_ids={"profile-1"},
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

    def test_rejected_profile_candidate_does_not_fail_section_by_itself(self):
        passed, decisions, feedback = reviewer.validate(
            {
                "pass": True,
                "entry_decisions": [
                    {
                        "temp_id": "p1",
                        "status": "rejected",
                        "reason": "只出现一次，不值得跨周期保存",
                    }
                ],
                "unsupported_claims": [],
                "required_changes": [],
            },
            expected_entry_ids={"p1"},
        )

        self.assertTrue(passed)
        self.assertEqual({"p1": "rejected"}, decisions)
        self.assertEqual([], feedback)

    def test_revision_prompt_preserves_original_request_as_prefix(self):
        original = _prompt(retrospective.SPEC, "生成", {"records": ["内容"]})
        revised = _prompt(
            retrospective.SPEC,
            "生成",
            {"records": ["内容"]},
            {
                "problems_to_fix": ["缺少引用"],
                "rejected_previous_output": {"markdown": "原稿"},
            },
        )

        shared_prefix = original.rsplit(
            "\n\n只输出一个符合契约的 JSON 对象", 1
        )[0]
        self.assertTrue(revised.startswith(shared_prefix))
        self.assertIn("缺少引用", revised)


if __name__ == "__main__":
    unittest.main()
