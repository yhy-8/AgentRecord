import datetime
import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from AgentRecord import journal, settings
from AgentRecord.analysis import automation, context, orchestrator
from AgentRecord.analysis.store import AnalysisStore


class AnalysisWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.original_diary_dir = settings.DIARY_DIR
        self.original_analysis_dir = settings.ANALYSIS_DIR
        self.original_call_ai = orchestrator.call_ai
        self.original_automation = settings.CONFIG.get("automation")

        settings.DIARY_DIR = root / "Records"
        settings.ANALYSIS_DIR = root / "AnalysisReports"
        settings.DIARY_DIR.mkdir()
        self.ai_calls = []

        def fake_call_ai(prompt, model_cfg, *, allowed_tools=None):
            self.ai_calls.append(prompt)
            if "[程序 Agent 任务:" not in prompt:
                return "测试生成内容", True, 0, {}, 0

            input_text = prompt.split("【中控提供的输入 JSON】\n", 1)[1]
            input_text = input_text.split("\n\n只输出一个符合契约", 1)[0]
            data = json.loads(input_text)
            if "[程序 Agent 任务:extractor]" in prompt:
                record = data["records"][0]
                payload = {
                    "nodes": [
                        {
                            "temp_id": "e1",
                            "node_type": "evidence",
                            "title": "测试证据",
                            "body": record["text"],
                            "confidence": 0.9,
                            "source_refs": [record["source_id"]],
                            "metadata": {"kind": "idea", "speaker": record["speaker"]},
                        }
                    ]
                }
            elif "[程序 Agent 任务:cluster]" in prompt:
                evidence = data["nodes"][0]
                payload = {
                    "nodes": [
                        {
                            "temp_id": "t1",
                            "node_type": "theme",
                            "title": "测试主题",
                            "body": "测试主题正在形成。",
                            "confidence": 0.8,
                            "source_refs": [evidence["id"]],
                            "metadata": {"trajectory": "new"},
                        }
                    ],
                    "edges": [
                        {
                            "source_id": evidence["id"],
                            "target_id": "t1",
                            "relation_type": "member_of",
                            "weight": 0.8,
                            "confidence": 0.8,
                            "rationale": "证据属于该主题",
                        }
                    ],
                }
            elif "[程序 Agent 任务:explorer]" in prompt:
                evidence = next(
                    node for node in data["nodes"] if node["node_type"] == "evidence"
                )
                payload = {
                    "nodes": [
                        {
                            "temp_id": "i1",
                            "node_type": "insight",
                            "title": "测试洞见",
                            "body": "材料中出现了值得继续判断的方向。",
                            "confidence": 0.8,
                            "source_refs": [evidence["id"]],
                            "metadata": {
                                "insight_type": "methodology",
                                "evidence_for": [evidence["id"]],
                                "evidence_against": [],
                                "inference_level": "low",
                                "why_it_matters": "用于验证报告流程",
                                "research_needed": True,
                            },
                        }
                    ],
                    "edges": [
                        {
                            "source_id": evidence["id"],
                            "target_id": "i1",
                            "relation_type": "supports",
                            "weight": 0.8,
                            "confidence": 0.8,
                            "rationale": "原始证据支持洞见",
                        }
                    ],
                    "research_queries": [
                        {
                            "target_id": "i1",
                            "query": "如何验证并延伸一种个人问题分析方法",
                            "reason": "查找相关方法、反例和相邻概念",
                        }
                    ],
                }
            elif "[程序 Agent 任务:world]" in prompt:
                target = data["target_nodes"][0]
                query = data["research_queries"][0]
                payload = {
                    "nodes": [
                        {
                            "temp_id": "research-1",
                            "node_type": "research",
                            "title": "外部方法研究",
                            "body": "外部资料提供了验证、反例和延伸方向。",
                            "confidence": 0.8,
                            "source_refs": [],
                            "metadata": {
                                "target_id": target["id"],
                                "query": query["query"],
                                "checked_at": data["checked_at"],
                                "sources": [
                                    {
                                        "title": "测试来源",
                                        "url": "https://example.com/source",
                                        "published": "2026-07-14",
                                    }
                                ],
                                "result": "mixed",
                            },
                        }
                    ],
                    "edges": [
                        {
                            "source_id": "research-1",
                            "target_id": target["id"],
                            "relation_type": "supports",
                            "weight": 0.7,
                            "confidence": 0.8,
                            "rationale": "外部资料提供了可比较的方法",
                        }
                    ],
                }
            elif "[程序 Agent 任务:report]" in prompt:
                source_id = data["source_ids"][0]
                payload = {
                    "markdown": f"## 核心发现\n\n测试生成内容 [{source_id}]"
                }
            elif data.get("mode") == "candidate_review":
                payload = {
                    "decisions": [
                        {
                            "node_id": node["id"],
                            "status": "accepted",
                            "reason": "测试接受",
                            "confidence": 0.9,
                        }
                        for node in data["candidate_nodes"]
                    ],
                    "revision_guidance": "",
                }
            else:
                payload = {
                    "pass": True,
                    "unsupported_claims": [],
                    "required_changes": [],
                    "summary": "通过",
                }
            return json.dumps(payload, ensure_ascii=False), True, 0, {}, 0

        orchestrator.call_ai = fake_call_ai
        settings.CONFIG["automation"] = {
            "enabled": True,
            "daily_summary": True,
            "daily_information": False,
            "weekly_report": False,
            "monthly_report": False,
        }

    def tearDown(self):
        settings.DIARY_DIR = self.original_diary_dir
        settings.ANALYSIS_DIR = self.original_analysis_dir
        orchestrator.call_ai = self.original_call_ai
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

        summary, success = orchestrator.summarize_diary(date, {"name": "mock"})

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

        report, success, report_path = orchestrator.generate_analysis_report(
            "daily", day, {"name": "mock"}
        )

        self.assertTrue(success)
        self.assertIn("测试生成内容", report)
        self.assertTrue(
            any("[程序 Agent 任务:extractor]" in prompt for prompt in self.ai_calls)
        )
        self.assertTrue(
            any("[程序 Agent 任务:report]" in prompt for prompt in self.ai_calls)
        )
        world_prompt = next(
            prompt for prompt in self.ai_calls if "[程序 Agent 任务:world]" in prompt
        )
        self.assertIn('"target_nodes": [{', world_prompt)
        self.assertIn("外部知识", world_prompt)
        self.assertEqual(original, diary.read_bytes())
        self.assertEqual(
            settings.ANALYSIS_DIR / "Daily" / "2026-07-14_manual.md", report_path
        )
        self.assertTrue(report_path.exists())
        run_id = re.search(
            r"分析运行：([0-9a-f]+)", report_path.read_text(encoding="utf-8")
        ).group(1)
        store = AnalysisStore(settings.ANALYSIS_DIR / ".analysis.sqlite3")
        self.assertEqual("completed", store.run_record(run_id)["status"])
        self.assertTrue(store.nodes_for_run(run_id, statuses=("accepted",)))
        derived_nodes = store.nodes_for_run(
            run_id,
            statuses=("accepted",),
            node_types=("theme", "insight"),
        )
        self.assertTrue(derived_nodes)
        self.assertTrue(
            all(node["source_refs"] == ["R-20260714-001"] for node in derived_nodes)
        )
        self.assertEqual("R-20260714-001", store.sources_for_run(run_id)[0]["source_id"])

    def test_failed_pipeline_does_not_overwrite_existing_report(self):
        day = datetime.date(2026, 7, 14)
        self.write_diary(day.isoformat())
        report_path = settings.ANALYSIS_DIR / "Daily" / "2026-07-14_manual.md"
        report_path.parent.mkdir(parents=True)
        report_path.write_text("已有报告", encoding="utf-8")

        def failed_call_ai(prompt, model_cfg, *, allowed_tools=None):
            return "模型失败", False, 0, {}, 0

        orchestrator.call_ai = failed_call_ai
        _, success, returned_path = orchestrator.generate_analysis_report(
            "daily", day, {"name": "mock"}
        )

        self.assertFalse(success)
        self.assertIsNone(returned_path)
        self.assertEqual("已有报告", report_path.read_text(encoding="utf-8"))

    def test_candidate_review_uses_aliases_and_retries_once(self):
        day = datetime.date(2026, 7, 14)
        self.write_diary(day.isoformat())
        working_call_ai = orchestrator.call_ai
        review_inputs = []

        def flaky_reviewer(prompt, model_cfg, *, allowed_tools=None):
            if "[程序 Agent 任务:reviewer]" not in prompt:
                return working_call_ai(
                    prompt, model_cfg, allowed_tools=allowed_tools
                )
            input_text = prompt.split("【中控提供的输入 JSON】\n", 1)[1]
            input_text = input_text.split("\n\n只输出一个符合契约", 1)[0]
            data = json.loads(input_text)
            if data.get("mode") != "candidate_review":
                return working_call_ai(
                    prompt, model_cfg, allowed_tools=allowed_tools
                )
            review_inputs.append(data)
            decisions = [
                {
                    "node_id": node["id"],
                    "status": "accepted",
                    "reason": "测试接受",
                    "confidence": 0.9,
                }
                for node in data["candidate_nodes"]
            ]
            if len(review_inputs) == 1:
                decisions[0]["node_id"] = "N999"
            payload = {"decisions": decisions, "revision_guidance": ""}
            return json.dumps(payload), True, 0, {}, 0

        orchestrator.call_ai = flaky_reviewer
        _, success, report_path = orchestrator.generate_analysis_report(
            "daily", day, {"name": "mock"}
        )

        self.assertTrue(success)
        self.assertEqual(2, len(review_inputs))
        for data in review_inputs:
            self.assertTrue(
                all(
                    re.fullmatch(r"N\d{3}", node["id"])
                    for node in data["candidate_nodes"]
                )
            )
        run_id = re.search(
            r"分析运行：([0-9a-f]+)", report_path.read_text(encoding="utf-8")
        ).group(1)
        with closing(
            sqlite3.connect(settings.ANALYSIS_DIR / ".analysis.sqlite3")
        ) as connection:
            artifacts = connection.execute(
                """
                SELECT revision, status FROM agent_artifacts
                WHERE run_id = ? AND agent = 'reviewer'
                ORDER BY revision
                """,
                (run_id,),
            ).fetchall()
        self.assertEqual([(1, "failed"), (2, "completed")], artifacts)

    def test_candidate_review_stops_after_one_retry(self):
        day = datetime.date(2026, 7, 14)
        self.write_diary(day.isoformat())
        working_call_ai = orchestrator.call_ai
        review_calls = 0

        def invalid_reviewer(prompt, model_cfg, *, allowed_tools=None):
            nonlocal review_calls
            if "[程序 Agent 任务:reviewer]" not in prompt:
                return working_call_ai(
                    prompt, model_cfg, allowed_tools=allowed_tools
                )
            input_text = prompt.split("【中控提供的输入 JSON】\n", 1)[1]
            input_text = input_text.split("\n\n只输出一个符合契约", 1)[0]
            data = json.loads(input_text)
            if data.get("mode") != "candidate_review":
                return working_call_ai(
                    prompt, model_cfg, allowed_tools=allowed_tools
                )
            review_calls += 1
            payload = {
                "decisions": [
                    {
                        "node_id": "N999",
                        "status": "accepted",
                        "reason": "无效别名",
                        "confidence": 0.9,
                    }
                ],
                "revision_guidance": "",
            }
            return json.dumps(payload), True, 0, {}, 0

        orchestrator.call_ai = invalid_reviewer
        _, success, report_path = orchestrator.generate_analysis_report(
            "daily", day, {"name": "mock"}
        )

        self.assertFalse(success)
        self.assertIsNone(report_path)
        self.assertEqual(2, review_calls)

    def test_malformed_agent_json_gets_one_format_only_repair(self):
        day = datetime.date(2026, 7, 14)
        self.write_diary(day.isoformat())
        working_call_ai = orchestrator.call_ai
        extractor_calls = 0

        def malformed_then_repaired(prompt, model_cfg, *, allowed_tools=None):
            nonlocal extractor_calls
            if "[程序 Agent 任务:extractor]" not in prompt:
                return working_call_ai(prompt, model_cfg, allowed_tools=allowed_tools)
            extractor_calls += 1
            if extractor_calls == 1:
                return '{"nodes": [', True, 0, {}, 0
            payload = {
                "nodes": [
                    {
                        "temp_id": "e1",
                        "node_type": "evidence",
                        "title": "修复后证据",
                        "body": "一个原始想法。",
                        "confidence": 0.9,
                        "source_refs": ["R-20260714-001"],
                        "metadata": {"kind": "idea", "speaker": "user"},
                    }
                ]
            }
            return json.dumps(payload, ensure_ascii=False), True, 0, {}, 0

        orchestrator.call_ai = malformed_then_repaired
        _, success, report_path = orchestrator.generate_analysis_report(
            "daily", day, {"name": "mock"}
        )

        self.assertTrue(success)
        self.assertEqual(2, extractor_calls)
        run_id = re.search(
            r"分析运行：([0-9a-f]+)", report_path.read_text(encoding="utf-8")
        ).group(1)
        with closing(sqlite3.connect(settings.ANALYSIS_DIR / ".analysis.sqlite3")) as connection:
            statuses = connection.execute(
                """
                SELECT status FROM agent_artifacts
                WHERE run_id = ? AND agent = 'extractor' ORDER BY revision
                """,
                (run_id,),
            ).fetchall()
        self.assertEqual([("failed",), ("completed",)], statuses)

    def test_format_repair_does_not_retry_model_or_network_failure(self):
        store = AnalysisStore()
        run_id, _ = store.start_run(
            "daily", "2026-07-14", "2026-07-14", "manual", "mock", "hash"
        )
        failed_call = Mock(return_value=("network down", False, 0, {}, 0))
        with patch.object(orchestrator, "call_ai", failed_call):
            with self.assertRaisesRegex(Exception, "调用失败"):
                orchestrator._call_agent(
                    orchestrator.extractor.SPEC,
                    "提取",
                    {"records": []},
                    {"name": "mock"},
                    store,
                    run_id,
                )

        failed_call.assert_called_once()

    def test_manual_and_automatic_reports_have_separate_fixed_paths(self):
        day = datetime.date(2026, 7, 14)
        self.write_diary(day.isoformat())

        _, manual_success, manual_path = orchestrator.generate_analysis_report(
            "weekly", day, {"name": "mock"}, origin="manual"
        )
        _, auto_success, auto_path = orchestrator.generate_analysis_report(
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

        _, success, report_path = orchestrator.generate_analysis_report(
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
        self.assertTrue(any("同期周报分析" in prompt for prompt in self.ai_calls))
        self.assertEqual(original, diary.read_bytes())

    def test_weekly_report_uses_daily_information_as_research_leads(self):
        day = datetime.date(2026, 7, 14)
        self.write_diary(day.isoformat())
        information_dir = settings.ANALYSIS_DIR / "Information"
        information_dir.mkdir(parents=True)
        (information_dir / f"{day:%Y-%m-%d}.md").write_text(
            "一条需要 World 重新查证的外部线索",
            encoding="utf-8",
        )

        _, success, _ = orchestrator.generate_analysis_report(
            "weekly", day, {"name": "mock"}
        )

        self.assertTrue(success)
        self.assertTrue(
            any("一条需要 World 重新查证的外部线索" in prompt for prompt in self.ai_calls)
        )

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

        orchestrator.generate_analysis_report("daily", day, {"name": "mock"})

        self.assertTrue(
            any("被引用周报中的关键判断" in prompt for prompt in self.ai_calls)
        )

    def test_reference_context_rejects_paths_outside_data_directories(self):
        outside = Path(self.temp_dir.name) / "outside.md"
        outside.write_text("不应读取的内容", encoding="utf-8")
        logs = [("2026-07-14", "**11:00 [引用]:** [外部](<../outside.md>)")]

        referenced = context._referenced_source_context(logs)

        self.assertNotIn("不应读取的内容", referenced)

    def test_automatic_tasks_process_yesterday_and_record_state(self):
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        diary, raw_stream = self.write_diary(yesterday.isoformat())

        automation.run_due_automatic_tasks()

        state_path = settings.ANALYSIS_DIR / ".automation-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(yesterday.isoformat(), state["last_daily_date"])
        self.assertIn("last_check_started_at", state)
        self.assertIn("last_check_completed_at", state)
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
            "daily_information": False,
            "weekly_report": True,
            "monthly_report": False,
        }

        automation.run_due_automatic_tasks()

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

    def test_daily_information_runs_after_configured_time_and_retries(self):
        settings.CONFIG["automation"] = {
            "enabled": True,
            "daily_information": True,
            "daily_information_time": "08:05",
        }
        state = {}
        model = {"name": "mock"}
        failure = ("失败", False, None)
        success = ("完成", True, settings.ANALYSIS_DIR / "Information/test.md")
        with patch.object(
            automation,
            "generate_information_briefing",
            side_effect=[failure, success],
        ) as generate:
            automation._run_daily_information(
                datetime.datetime(2026, 7, 15, 8, 4), state, model
            )
            automation._run_daily_information(
                datetime.datetime(2026, 7, 15, 8, 5), state, model
            )
            automation._run_daily_information(
                datetime.datetime(2026, 7, 15, 9, 5), state, model
            )

        self.assertEqual(2, generate.call_count)
        self.assertEqual("2026-07-15", state["last_information_date"])
        self.assertNotIn("daily_information", state.get("errors", {}))

    def test_monthly_task_generates_last_complete_calendar_month_once(self):
        today = datetime.date(2026, 8, 15)
        self.write_diary("2026-07-10")
        state = {}
        model = {"name": "mock"}

        automation._run_monthly_reports(today, state, model)
        calls_after_first_run = len(self.ai_calls)
        automation._run_monthly_reports(today, state, model)

        report_path = settings.ANALYSIS_DIR / "Monthly" / "2026-07_auto.md"
        self.assertTrue(report_path.exists())
        self.assertEqual("2026-07-31", state["last_month_end"])
        self.assertEqual(calls_after_first_run, len(self.ai_calls))

    def test_monthly_task_catches_up_from_last_successful_month(self):
        self.write_diary("2026-06-10")
        self.write_diary("2026-07-10")
        state = {"last_month_end": "2026-05-31"}

        automation._run_monthly_reports(
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

        def failed_call_ai(prompt, model_cfg, *, allowed_tools=None):
            return "失败", False, 0, {}, 0

        orchestrator.call_ai = failed_call_ai
        state = {}
        automation._run_monthly_reports(
            datetime.date(2026, 8, 1), state, {"name": "mock"}
        )

        self.assertNotIn("last_month_end", state)
        self.assertIn("monthly_report", state["errors"])

    def test_installs_hourly_cron_task_for_one_shot_runner(self):
        listed = Mock(returncode=0, stdout="15 2 * * * existing\n", stderr="")
        installed = Mock(returncode=0, stdout="", stderr="")
        with patch("AgentRecord.analysis.automation._is_windows", return_value=False), patch(
            "AgentRecord.analysis.automation.subprocess.run", side_effect=[listed, installed]
        ) as run:
            success, _ = automation.install_system_automation()

        self.assertTrue(success)
        cron_input = run.call_args_list[1].kwargs["input"]
        self.assertIn("@reboot", cron_input)
        self.assertIn("5 * * * *", cron_input)
        self.assertIn("--run-automation", cron_input)
        self.assertIn("# AgentRecord automation", cron_input)

    def test_reports_complete_cron_automation_status(self):
        listed = Mock(
            returncode=0,
            stdout=(
                "@reboot python main.py --run-automation "
                "# AgentRecord automation startup\n"
                "5 * * * * python main.py --run-automation "
                "# AgentRecord automation hourly\n"
            ),
            stderr="",
        )
        with patch(
            "AgentRecord.analysis.automation._is_windows", return_value=False
        ), patch(
            "AgentRecord.analysis.automation.subprocess.run", return_value=listed
        ):
            installed, message = automation.system_automation_status()

        self.assertTrue(installed)
        self.assertIn("已安装", message)

    def test_reports_missing_cron_automation_status(self):
        listed = Mock(returncode=1, stdout="", stderr="no crontab")
        with patch(
            "AgentRecord.analysis.automation._is_windows", return_value=False
        ), patch(
            "AgentRecord.analysis.automation.subprocess.run", return_value=listed
        ):
            installed, message = automation.system_automation_status()

        self.assertFalse(installed)
        self.assertIn("未安装", message)

    def test_source_automation_command_uses_root_entry(self):
        with patch.object(automation.sys, "frozen", False, create=True):
            command = automation._automation_command()

        self.assertEqual(settings.CONFIG_DIR / "main.py", Path(command[1]))
        self.assertEqual("--run-automation", command[2])

    def test_installs_windows_scheduled_task_for_one_shot_runner(self):
        installed = Mock(returncode=0, stdout="", stderr="")
        with patch("AgentRecord.analysis.automation._is_windows", return_value=True), patch(
            "AgentRecord.analysis.automation.subprocess.run", return_value=installed
        ) as run:
            success, _ = automation.install_system_automation()

        self.assertTrue(success)
        self.assertEqual(2, run.call_count)
        commands = [call.args[0] for call in run.call_args_list]
        schedules = {command[command.index("/SC") + 1] for command in commands}
        self.assertEqual({"HOURLY", "ONLOGON"}, schedules)
        for command in commands:
            self.assertEqual("schtasks", command[0])
            self.assertIn("/TR", command)
            self.assertIn("--run-automation", command[command.index("/TR") + 1])

if __name__ == "__main__":
    unittest.main()
