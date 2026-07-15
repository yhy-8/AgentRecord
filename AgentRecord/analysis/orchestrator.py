"""Diary summaries and persistent multi-Agent report orchestration."""

import datetime
import hashlib
import json
import logging
from pathlib import Path

from .. import journal, settings
from ..agents import (
    AgentPipelineError,
    cluster,
    compact_nodes,
    explorer,
    extractor,
    invoke_agent,
    report as report_agent,
    reviewer,
    world,
)
from ..agents.graph import inherit_source_refs
from ..ai_client import call_ai
from .context import (
    _analysis_report_path,
    _existing_logs,
    _information_briefings,
    _log_without_summary,
    _monthly_supporting_reports,
    _period_records,
    _recent_summary_context,
    _record_chunks,
    _referenced_source_context,
)
from .store import AnalysisStore


logger = logging.getLogger(__name__)


def _save_validation_failure(
    store: AnalysisStore,
    run_id: str,
    agent: str,
    payload: dict,
    error: AgentPipelineError,
) -> None:
    store.save_artifact(
        run_id,
        agent,
        payload,
        status="failed",
        error=str(error),
    )
    logger.warning(
        "agent_validation_failed run=%s agent=%s reason=%s",
        run_id,
        agent,
        str(error),
    )


def summarize_diary(date: str, model_config: settings.ModelDict) -> tuple[str, bool]:
    """生成指定日期的日记总结，并写回原文件的 <summary> 区域。"""
    file_path = settings.DIARY_DIR / f"{date}.md"
    if not file_path.exists():
        return f"找不到 {date} 的记录。", False

    content = _log_without_summary(file_path.read_text(encoding="utf-8"))
    prompt = f"""[程序日记总结任务]
请总结 {date} 的日记。只输出要写入 <summary> 的 Markdown 正文，不要输出标题、标签、代码围栏或完成提示。

要求：
- 概括当天的重要事件、想法、决定、问题和进展，不要逐条复述流水账。
- 区分用户记录与 AI 回复；AI 回复只能作为咨询结果，不能当作用户已经认同的观点。
- 保留重要的具体信息，禁止编造。
- 内容为空或信息很少时如实简短说明。

【{date} 原始日记】
{content}"""
    summary, success, _, _, _ = call_ai(prompt, model_config)
    if not success:
        return summary, False

    result = journal.update_summary_for_date(date, summary)
    if not result.endswith("总结已写入文档顶部。"):
        return result, False
    return summary, True


def _call_agent(
    spec,
    task: str,
    input_data: dict,
    model_config: settings.ModelDict,
    store: AnalysisStore,
    run_id: str,
) -> dict:
    """Invoke an Agent while the orchestrator owns failure persistence."""
    logger.info("agent_start run=%s agent=%s", run_id, spec.name)
    current_task = task
    current_input = input_data
    for attempt in range(2):
        try:
            payload = invoke_agent(
                spec, current_task, current_input, model_config, call_ai
            )
            break
        except AgentPipelineError as error:
            store.save_artifact(
                run_id,
                spec.name,
                {"response": error.response},
                status="failed",
                error=str(error),
            )
            repairable = str(error).startswith(
                ("Agent 没有返回 JSON 对象", "Agent JSON 无法解析", "Agent JSON 顶层必须是对象")
            )
            logger.warning(
                "agent_failed run=%s agent=%s error_type=%s format_repair=%s",
                run_id,
                spec.name,
                error.__class__.__name__,
                repairable and attempt == 0,
            )
            if not repairable or attempt == 1:
                raise
            current_task = (
                "上次回答只是 JSON 格式无效。仅修复语法和顶层对象格式，"
                "不要重新分析、增删或改写内容；只输出修复后的 JSON 对象。"
            )
            current_input = {
                "validation_error": str(error),
                "invalid_response": error.response,
            }
    logger.info("agent_completed run=%s agent=%s", run_id, spec.name)
    return payload


def _persist_graph_agent(
    spec,
    validator,
    payload: dict,
    store: AnalysisStore,
    run_id: str,
    *,
    allowed_source_ids: set[str],
    visible_nodes: dict[str, dict],
) -> dict[str, str]:
    normalized_payload = inherit_source_refs(
        payload,
        allowed_source_ids=allowed_source_ids,
        visible_nodes=visible_nodes,
    )
    try:
        nodes, edges = validator(
            normalized_payload,
            allowed_source_ids=allowed_source_ids,
            visible_node_ids=set(visible_nodes),
        )
    except AgentPipelineError as error:
        _save_validation_failure(
            store, run_id, spec.name, normalized_payload, error
        )
        raise
    stored_payload = dict(normalized_payload)
    stored_payload["nodes"] = nodes
    stored_payload["edges"] = edges
    store.save_artifact(run_id, spec.name, stored_payload)
    node_ids = store.add_nodes(run_id, spec.name, nodes)
    store.add_edges(run_id, spec.name, edges, node_ids)
    return node_ids


def _review_candidates(
    store: AnalysisStore,
    run_id: str,
    model_config: settings.ModelDict,
) -> set[str]:
    candidates = store.nodes_for_run(run_id, statuses=("candidate",))
    if not candidates:
        raise AgentPipelineError("本次分析没有产生可审查节点")
    id_to_alias = {
        node["id"]: f"N{index:03d}" for index, node in enumerate(candidates, 1)
    }
    alias_to_id = {alias: node_id for node_id, alias in id_to_alias.items()}
    candidate_input = [
        {**node, "id": id_to_alias[node["id"]]}
        for node in compact_nodes(candidates)
    ]
    relation_input = [
        {
            **relation,
            "source_id": id_to_alias.get(
                relation["source_id"], relation["source_id"]
            ),
            "target_id": id_to_alias.get(
                relation["target_id"], relation["target_id"]
            ),
        }
        for relation in store.edges_for_run(run_id, statuses=("candidate",))
    ]
    review_input = {
        "mode": "candidate_review",
        "candidate_nodes": candidate_input,
        "relations": relation_input,
        "valid_node_ids": list(alias_to_id),
    }
    payload = {}
    normalized_aliases = []
    validation_error = ""
    for attempt in range(2):
        current_input = dict(review_input)
        if validation_error:
            current_input["previous_validation_error"] = validation_error
        task = (
            "审查所有候选节点。每个候选节点必须且只能返回一个决定；"
            "node_id 必须从 valid_node_ids 原样复制，证据不足或价值很低时拒绝。"
            if attempt == 0
            else "上次决定未通过结构校验。请根据错误重新审查全部候选节点；"
            "每个 node_id 必须从 valid_node_ids 原样复制，不要使用持久节点 ID。"
        )
        payload = _call_agent(
            reviewer.SPEC,
            task,
            current_input,
            model_config,
            store,
            run_id,
        )
        try:
            normalized_aliases = reviewer.validate_candidate_review(
                payload, set(alias_to_id)
            )
        except AgentPipelineError as error:
            _save_validation_failure(
                store, run_id, reviewer.SPEC.name, payload, error
            )
            if attempt == 0:
                validation_error = str(error)
                continue
            raise
        break
    normalized = [
        {**decision, "node_id": alias_to_id[decision["node_id"]]}
        for decision in normalized_aliases
    ]
    stored_payload = dict(payload)
    stored_payload["decisions"] = normalized
    stored_payload["node_aliases"] = alias_to_id
    store.save_artifact(run_id, reviewer.SPEC.name, stored_payload)
    store.apply_node_decisions(normalized)
    accepted = {item["node_id"] for item in normalized if item["status"] == "accepted"}
    if not accepted:
        raise AgentPipelineError("Reviewer 没有接受任何节点")
    return accepted


def generate_analysis_report(
    kind: str,
    anchor: datetime.date,
    model_config: settings.ModelDict,
    *,
    origin: str = "manual",
) -> tuple[str, bool, Path | None]:
    """Run the persistent multi-agent pipeline and atomically save its report."""
    if origin not in ("manual", "auto"):
        return f"未知报告来源: {origin}", False, None
    origin_label = "手动" if origin == "manual" else "自动"
    if kind == "daily":
        start = end = anchor
        report_name = f"{anchor:%Y-%m-%d} {origin_label}分析日报"
    elif kind == "weekly":
        start = anchor - datetime.timedelta(days=anchor.weekday())
        end = start + datetime.timedelta(days=6)
        report_name = (
            f"{start:%Y-%m-%d} 至 {end:%Y-%m-%d} {origin_label}分析周报"
        )
    elif kind == "monthly":
        start = anchor.replace(day=1)
        next_month = (start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        end = next_month - datetime.timedelta(days=1)
        report_name = f"{start:%Y年%m月} {origin_label}分析月报"
    else:
        return f"未知报告类型: {kind}", False, None

    logs = _existing_logs(start, end)
    if not logs:
        return f"{start:%Y-%m-%d} 至 {end:%Y-%m-%d} 没有日记记录。", False, None

    records = _period_records(logs)
    if not records:
        return "日记中没有可识别的标准记录。", False, None
    referenced_sources = _referenced_source_context(logs)
    history = _recent_summary_context(start)
    period_focus = {
        "daily": "关注当天新出现的内容和变化，不必强行上升为长期结论。",
        "weekly": "关注一周内主题的聚合、推进、反复、转变及仍未解决的问题。",
        "monthly": "从更高层观察注意力分配、长期主题演化、判断得到的支持或挑战、反复模式，以及下个月最值得继续探索的少量方向。",
    }[kind]
    supporting_reports = (
        _monthly_supporting_reports(start, end)
        if kind == "monthly"
        else "（本报告不使用下级周期报告）"
    )
    information_briefings = (
        _information_briefings(start, end)
        if kind in ("weekly", "monthly")
        else "（分析日报不使用每日信息简报）"
    )
    report_path = _analysis_report_path(kind, start, end, origin)
    store: AnalysisStore | None = None
    run_id: str | None = None
    try:
        store = AnalysisStore()
        snapshot = {
            "records": records,
            "referenced_sources": referenced_sources,
            "history": history,
            "supporting_reports": supporting_reports,
            "information_briefings": information_briefings,
        }
        input_hash = hashlib.sha256(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        run_id, parent_run_id = store.start_run(
            kind,
            start.isoformat(),
            end.isoformat(),
            origin,
            model_config.get("name", ""),
            input_hash,
        )
        logger.info(
            "analysis_started run=%s kind=%s origin=%s period=%s..%s",
            run_id,
            kind,
            origin,
            start.isoformat(),
            end.isoformat(),
        )
        store.save_sources(run_id, records)

        previous_nodes = (
            store.nodes_for_run(
                parent_run_id, statuses=("accepted", "candidate")
            )
            if parent_run_id
            else []
        )
        historical_nodes = store.accepted_history(
            before=datetime.datetime.now().isoformat(timespec="seconds"),
            exclude_run_id=run_id,
        )
        historical_by_id = {
            node["id"]: node for node in previous_nodes + historical_nodes
        }
        allowed_source_ids = {record["source_id"] for record in records}
        for node in historical_by_id.values():
            allowed_source_ids.update(node.get("source_refs", []))

        for chunk_index, record_chunk in enumerate(_record_chunks(records), 1):
            extractor_payload = _call_agent(
                extractor.SPEC,
                f"提取第 {chunk_index} 个记录分块中的证据；source_id 必须原样引用。",
                {"records": record_chunk},
                model_config,
                store,
                run_id,
            )
            _persist_graph_agent(
                extractor.SPEC,
                extractor.validate,
                extractor_payload,
                store,
                run_id,
                allowed_source_ids={record["source_id"] for record in record_chunk},
                visible_nodes={},
            )

        evidence_nodes = store.nodes_for_run(
            run_id, statuses=("candidate",), node_types=("evidence",)
        )
        if not evidence_nodes:
            raise AgentPipelineError("Extractor 没有产生证据节点")

        cluster_visible = {
            node["id"]: node
            for node in evidence_nodes
            + [
                node
                for node in historical_by_id.values()
                if node["node_type"] == "theme"
            ]
        }
        cluster_payload = _call_agent(
            cluster.SPEC,
            "根据本周期证据形成主题和时间轨迹；只在确有依据时关联历史主题。",
            {
                "period": {"start": start.isoformat(), "end": end.isoformat()},
                "nodes": compact_nodes(list(cluster_visible.values())),
            },
            model_config,
            store,
            run_id,
        )
        _persist_graph_agent(
            cluster.SPEC,
            cluster.validate,
            cluster_payload,
            store,
            run_id,
            allowed_source_ids=allowed_source_ids,
            visible_nodes=cluster_visible,
        )

        run_nodes = store.nodes_for_run(run_id, statuses=("candidate",))
        explorer_visible = {
            node["id"]: node for node in run_nodes + list(historical_by_id.values())
        }
        explorer_payload = _call_agent(
            explorer.SPEC,
            "提取并探索少量高价值观点、思维模型、方法论和点子。"
            "存在可被外部知识实质验证或延伸的内容时，通常选择一至三个方向提出研究问题。"
            "每日信息简报只是外部线索；使用其中信息前必须创建研究问题交给 World 重新查证。"
            "显式引用和下级报告只是分析材料，不能视为用户已经认可的观点。",
            {
                "period_focus": period_focus,
                "nodes": compact_nodes(list(explorer_visible.values())),
                "referenced_sources": referenced_sources[:30000],
                "supporting_reports": supporting_reports[:30000],
                "recent_summaries": history[:30000],
                "information_briefings": information_briefings,
            },
            model_config,
            store,
            run_id,
        )
        explorer_ids = _persist_graph_agent(
            explorer.SPEC,
            explorer.validate,
            explorer_payload,
            store,
            run_id,
            allowed_source_ids=allowed_source_ids,
            visible_nodes=explorer_visible,
        )

        query_payload = dict(explorer_payload)
        query_payload["research_queries"] = [
            {
                **item,
                "target_id": explorer_ids.get(
                    str(item.get("target_id", "")), str(item.get("target_id", ""))
                ),
            }
            for item in explorer_payload.get("research_queries", [])
            if isinstance(item, dict)
        ]
        persisted_explorer = {
            node["id"]: node
            for node in store.nodes_for_run(run_id, statuses=("candidate",))
            if node["id"] in set(explorer_ids.values())
        }
        research_visible = explorer_visible | persisted_explorer
        research_targets = set(research_visible)
        try:
            research_queries = explorer.clean_research_queries(
                query_payload, research_targets
            )
        except AgentPipelineError as error:
            _save_validation_failure(
                store, run_id, explorer.SPEC.name, query_payload, error
            )
            raise
        if research_queries:
            target_ids = list(
                dict.fromkeys(item["target_id"] for item in research_queries)
            )
            world_payload = _call_agent(
                world.SPEC,
                "逐项使用外部知识核查并延伸中控给出的候选观念、方法或点子。"
                "只把 research_queries 中已经去隐私的 query 发送给搜索工具；"
                "没有可靠结果时标为 unverified。",
                {
                    "checked_at": datetime.date.today().isoformat(),
                    "research_queries": research_queries,
                    "target_nodes": [
                        {
                            "id": target_id,
                            "node_type": research_visible[target_id]["node_type"],
                            "title": research_visible[target_id]["title"][:200],
                            "insight_type": research_visible[target_id]
                            .get("metadata", {})
                            .get("insight_type", ""),
                            "inference_level": research_visible[target_id]
                            .get("metadata", {})
                            .get("inference_level", ""),
                            "why_it_matters": str(
                                research_visible[target_id]
                                .get("metadata", {})
                                .get("why_it_matters", "")
                            )[:500],
                        }
                        for target_id in target_ids
                    ],
                },
                model_config,
                store,
                run_id,
            )
            _persist_graph_agent(
                world.SPEC,
                world.validate,
                world_payload,
                store,
                run_id,
                allowed_source_ids=allowed_source_ids,
                visible_nodes=research_visible,
            )

        accepted_current = _review_candidates(store, run_id, model_config)
        accepted_history_ids = {
            node["id"]
            for node in historical_by_id.values()
            if node["status"] == "accepted"
        }
        store.accept_edges_for_run(
            run_id, accepted_current | accepted_history_ids
        )
        accepted_nodes = store.nodes_for_run(run_id, statuses=("accepted",))
        accepted_relations = store.edges_for_run(run_id, statuses=("accepted",))
        if parent_run_id:
            current_source_hashes = {
                record["source_id"]: hashlib.sha256(
                    record["text"].encode("utf-8")
                ).hexdigest()
                for record in records
            }
            previous_source_hashes = {
                source["source_id"]: source["content_hash"]
                for source in store.sources_for_run(parent_run_id)
            }

            def source_is_unchanged(source_id: str) -> bool:
                return (
                    source_id in current_source_hashes
                    and previous_source_hashes.get(source_id)
                    == current_source_hashes[source_id]
                )

            reusable_previous = [
                node
                for node in store.nodes_for_run(
                    parent_run_id, statuses=("accepted",)
                )
                if all(
                    source_is_unchanged(source_id)
                    for source_id in node.get("source_refs", [])
                )
            ]
            accepted_nodes = list(
                {
                    node["id"]: node for node in reusable_previous + accepted_nodes
                }.values()
            )
            accepted_relations = (
                store.edges_for_run(parent_run_id, statuses=("accepted",))
                + accepted_relations
            )
        report_node_ids = {node["id"] for node in accepted_nodes}
        accepted_relations = [
            edge
            for edge in accepted_relations
            if edge["source_id"] in report_node_ids
            and edge["target_id"] in report_node_ids
        ]

        report_input = {
            "report_name": report_name,
            "period_focus": period_focus,
            "accepted_nodes": compact_nodes(accepted_nodes),
            "accepted_relations": accepted_relations,
            "source_ids": sorted(record["source_id"] for record in records),
        }
        report_markdown = ""
        audit_feedback: list[str] = []
        for attempt in range(2):
            task = (
                "生成最终报告正文，不要生成一级标题或来源索引；中控会追加来源索引。"
                if attempt == 0
                else "根据审查意见修订报告。只能修改表达和删减无依据内容，不能新增核心判断。"
            )
            current_input = dict(report_input)
            if attempt:
                current_input["previous_markdown"] = report_markdown
                current_input["required_changes"] = audit_feedback
            report_payload = _call_agent(
                report_agent.SPEC, task, current_input, model_config, store, run_id
            )
            try:
                report_markdown = report_agent.markdown_from_payload(report_payload)
            except AgentPipelineError as error:
                _save_validation_failure(
                    store, run_id, report_agent.SPEC.name, report_payload, error
                )
                raise
            store.save_artifact(run_id, report_agent.SPEC.name, report_payload)

            deterministic_errors = report_agent.validation_errors(
                report_markdown, {record["source_id"] for record in records}
            )
            audit_payload = _call_agent(
                reviewer.SPEC,
                "审查报告草稿是否只使用 accepted 节点、正确区分来源身份且所有核心判断有依据。",
                {
                    "mode": "report_review",
                    "accepted_nodes": compact_nodes(accepted_nodes),
                    "draft_markdown": report_markdown,
                    "deterministic_errors": deterministic_errors,
                },
                model_config,
                store,
                run_id,
            )
            try:
                audit_passed, audit_feedback = reviewer.validate_report_review(
                    audit_payload
                )
            except AgentPipelineError as error:
                _save_validation_failure(
                    store, run_id, "reviewer_report", audit_payload, error
                )
                raise
            store.save_artifact(run_id, "reviewer_report", audit_payload)
            audit_feedback.extend(deterministic_errors)
            if audit_passed and not deterministic_errors:
                break
        else:
            raise AgentPipelineError(
                "最终报告未通过审查: " + "; ".join(audit_feedback)
            )

        report_path.parent.mkdir(parents=True, exist_ok=True)
        title = f"# {report_name}\n\n"
        metadata = (
            f"> 生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M}\n"
            f"> 生成方式：{origin_label}\n"
            f"> 原始日记范围：{start:%Y-%m-%d} 至 {end:%Y-%m-%d}\n"
            f"> 分析运行：{run_id}\n\n"
        )
        final_content = (
            title
            + metadata
            + report_markdown.strip()
            + "\n\n"
            + report_agent.source_appendix(report_markdown, records)
            + "\n"
        )
        temp_path = report_path.with_suffix(report_path.suffix + ".tmp")
        temp_path.write_text(final_content, encoding="utf-8")
        previous_content = report_path.read_bytes() if report_path.exists() else None
        temp_path.replace(report_path)
        try:
            store.complete_run(run_id, report_path)
        except Exception:
            restore_path = report_path.with_suffix(report_path.suffix + ".restore.tmp")
            if previous_content is None:
                report_path.unlink(missing_ok=True)
            else:
                restore_path.write_bytes(previous_content)
                restore_path.replace(report_path)
            raise
        logger.info("analysis_completed run=%s kind=%s", run_id, kind)
        return report_markdown, True, report_path
    except Exception as error:
        message = str(error) or error.__class__.__name__
        if store is not None and run_id is not None:
            try:
                store.fail_run(run_id, message)
            except Exception as state_error:
                message += f"；保存失败状态时又发生异常: {state_error}"
        logger.error(
            "analysis_failed run=%s error_type=%s",
            run_id or "not-started",
            error.__class__.__name__,
        )
        return f"多 Agent 分析失败: {message}", False, None
