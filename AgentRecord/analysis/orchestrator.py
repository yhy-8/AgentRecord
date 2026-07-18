"""Orchestrate weekly/monthly retrospective and domain-research reports."""

import datetime
import hashlib
import json
import logging
import re
from pathlib import Path

from .. import journal, settings
from ..agents import researcher, research_planner, retrospective, reviewer
from ..agents.base import (
    AgentOutputError,
    AgentPipelineError,
    cited_source_ids,
    invoke_agent,
)
from ..ai_client import call_ai
from .context import (
    _analysis_report_path,
    _existing_logs,
    _information_briefings,
    _monthly_supporting_reports,
    _period_records,
    _recent_summary_context,
    _referenced_source_context,
    _log_without_summary,
)
from .store import AnalysisStore


logger = logging.getLogger(__name__)
_LONG_ID_PATTERN = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")
_MAX_AGENT_ATTEMPTS = 3


def _replace_id_substrings(value: object, replacements: dict[str, str]) -> object:
    if isinstance(value, str):
        return _LONG_ID_PATTERN.sub(
            lambda match: replacements.get(match.group(0), match.group(0)), value
        )
    if isinstance(value, list):
        return [_replace_id_substrings(item, replacements) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace_id_substrings(item, replacements) for item in value)
    if isinstance(value, dict):
        return {
            key: _replace_id_substrings(item, replacements)
            for key, item in value.items()
        }
    return value


def _profile_input(profiles: list[dict]) -> tuple[list[dict], dict[str, str]]:
    id_to_alias = {
        profile["id"]: f"P{index:03d}"
        for index, profile in enumerate(sorted(profiles, key=lambda item: item["id"]), 1)
    }
    alias_to_id = {alias: entry_id for entry_id, alias in id_to_alias.items()}
    compact = [
        {
            "id": profile["id"],
            "category": profile["category"],
            "title": profile["title"],
            "statement": profile["statement"],
            "confidence": profile["confidence"],
            "source_refs": profile["source_refs"],
            "first_observed": profile["first_observed"],
            "last_observed": profile["last_observed"],
        }
        for profile in profiles
    ]
    return _replace_id_substrings(compact, id_to_alias), alias_to_id


def _replace_profile_aliases(value: object, aliases: dict[str, str]) -> object:
    if isinstance(value, str):
        return aliases.get(value, value)
    if isinstance(value, list):
        return [_replace_profile_aliases(item, aliases) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_profile_aliases(item, aliases)
            for key, item in value.items()
        }
    return value


def summarize_diary(date: str, model_config: settings.ModelDict) -> tuple[str, bool]:
    """Generate the compact summary stored in a diary's summary region."""
    file_path = settings.DIARY_DIR / f"{date}.md"
    if not file_path.exists():
        return f"找不到 {date} 的记录。", False
    content = _log_without_summary(file_path.read_text(encoding="utf-8"))
    prompt = f"""[程序日记总结任务]
请总结 {date} 的日记。只输出要写入 <summary> 的 Markdown 正文，不要输出标题、标签、代码围栏或完成提示。

要求：
- 概括当天的重要事件、观点、决定、问题和进展，不逐条复述。
- 区分用户记录与引用的 AI 内容；AI 内容不能当作用户已经认可的观点。
- 保留重要具体信息，禁止编造、心理诊断和行为指导。

【{date} 原始日记】
{content}"""
    summary, success, _, _, _ = call_ai(prompt, model_config, allowed_tools=())
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
    *,
    revision_context: dict | None = None,
) -> dict:
    logger.info("agent_start run=%s agent=%s", run_id, spec.name)
    try:
        payload = invoke_agent(
            spec,
            task,
            input_data,
            model_config,
            call_ai,
            revision_context=revision_context,
        )
    except AgentPipelineError as error:
        store.save_artifact(
            run_id,
            spec.name,
            {"response": error.response, "_telemetry": error.telemetry},
            status="failed",
            error=str(error),
        )
        logger.warning(
            "agent_failed run=%s agent=%s error_type=%s",
            run_id,
            spec.name,
            error.__class__.__name__,
        )
        raise
    logger.info("agent_completed run=%s agent=%s", run_id, spec.name)
    return payload


def _save_validation_failure(
    store: AnalysisStore, run_id: str, agent: str, payload: dict, error: Exception
) -> None:
    store.save_artifact(
        run_id, agent, payload, status="failed", error=str(error)
    )
    logger.warning(
        "agent_validation_failed run=%s agent=%s reason=%s",
        run_id,
        agent,
        str(error),
    )


def _revision_context(
    attempt: int,
    previous_output: object,
    feedback: object,
    *,
    source: str,
) -> dict:
    """Build the common correction suffix while keeping the original prompt stable."""
    return {
        "attempt": attempt,
        "maximum_attempts": _MAX_AGENT_ATTEMPTS,
        "feedback_source": source,
        "problems_to_fix": feedback,
        "rejected_previous_output": previous_output,
    }


def _validated_agent_call(
    spec,
    task: str,
    input_data: dict,
    validator,
    model_config: settings.ModelDict,
    store: AnalysisStore,
    run_id: str,
    *,
    attempt_budget: list[int] | None = None,
):
    """Run one non-reviewed Agent stage with bounded output correction."""
    budget = attempt_budget if attempt_budget is not None else [_MAX_AGENT_ATTEMPTS]
    revision_context = None
    while budget[0] > 0:
        attempt = _MAX_AGENT_ATTEMPTS - budget[0] + 1
        budget[0] -= 1
        try:
            payload = _call_agent(
                spec,
                task,
                input_data,
                model_config,
                store,
                run_id,
                revision_context=revision_context,
            )
        except AgentOutputError as error:
            if budget[0] == 0:
                raise
            revision_context = _revision_context(
                attempt + 1,
                error.response,
                [str(error)],
                source="中控 JSON 解析",
            )
            continue
        try:
            result = validator(payload)
        except AgentPipelineError as error:
            _save_validation_failure(store, run_id, spec.name, payload, error)
            if budget[0] == 0:
                raise
            revision_context = _revision_context(
                attempt + 1,
                payload,
                [str(error)],
                source="中控确定性校验",
            )
            continue
        return payload, result
    raise RuntimeError("unreachable")


def _review_feedback(payload: dict) -> dict:
    return {
        "summary": payload.get("summary", ""),
        "required_changes": payload.get("required_changes", []),
        "unsupported_claims": payload.get("unsupported_claims", []),
        "entry_decisions": payload.get("entry_decisions", []),
    }


def _review(
    mode: str,
    section_payload: dict,
    entry_ids: set[str],
    review_context: dict,
    model_config: settings.ModelDict,
    store: AnalysisStore,
    run_id: str,
    *,
    attempt_budget: list[int] | None = None,
) -> tuple[bool, dict[str, str], list[str], dict]:
    review_input = {
        "mode": mode,
        "section": section_payload,
        "valid_profile_temp_ids": sorted(entry_ids),
        "review_context": review_context,
    }
    payload, result = _validated_agent_call(
        reviewer.SPEC,
        "审查该板块；逐项检查核心判断和来源，只报告会影响真实性、可追溯性或交付质量的实质问题。",
        review_input,
        lambda candidate: reviewer.validate(
            candidate, expected_entry_ids=entry_ids
        ),
        model_config,
        store,
        run_id,
        attempt_budget=attempt_budget,
    )
    store.save_artifact(run_id, f"reviewer_{mode}", payload)
    return (*result, payload)


def _retrospective_section(
    base_input: dict,
    allowed_source_ids: set[str],
    current_source_ids: set[str],
    profile_aliases: dict[str, str],
    model_config: settings.ModelDict,
    store: AnalysisStore,
    run_id: str,
) -> tuple[str, list[dict], dict[str, str]]:
    revision_context = None
    last_feedback: list[str] = []
    review_attempt_budget = [_MAX_AGENT_ATTEMPTS]
    for attempt in range(1, _MAX_AGENT_ATTEMPTS + 1):
        try:
            payload = _call_agent(
                retrospective.SPEC,
                "生成整理与回顾板块和人物画像候选。没有值得长期保存的画像时返回空数组，不要凑数。",
                base_input,
                model_config,
                store,
                run_id,
                revision_context=revision_context,
            )
        except AgentOutputError as error:
            if attempt == _MAX_AGENT_ATTEMPTS:
                raise
            revision_context = _revision_context(
                attempt + 1,
                error.response,
                [str(error)],
                source="中控 JSON 解析",
            )
            continue
        payload = _replace_profile_aliases(payload, profile_aliases)
        try:
            markdown, entries = retrospective.validate(
                payload,
                allowed_source_ids=allowed_source_ids,
                current_source_ids=current_source_ids,
                visible_profile_ids=set(profile_aliases.values()),
            )
        except AgentPipelineError as error:
            _save_validation_failure(
                store, run_id, retrospective.SPEC.name, payload, error
            )
            if attempt == _MAX_AGENT_ATTEMPTS:
                raise
            revision_context = _revision_context(
                attempt + 1,
                payload,
                [str(error)],
                source="中控确定性校验",
            )
            continue

        normalized_payload = {"markdown": markdown, "profile_entries": entries}
        cited_ids = cited_source_ids(markdown)
        cited_ids.update(ref for entry in entries for ref in entry["source_refs"])
        review_context = {
            "period": base_input["period"],
            "records": [
                record
                for record in base_input["records"]
                if record["source_id"] in cited_ids
            ],
            "historical_profiles": base_input["historical_profiles"],
        }
        passed, decisions, last_feedback, review_payload = _review(
            "retrospective_review",
            normalized_payload,
            {entry["temp_id"] for entry in entries},
            review_context,
            model_config,
            store,
            run_id,
            attempt_budget=review_attempt_budget,
        )
        if passed:
            store.save_artifact(run_id, retrospective.SPEC.name, normalized_payload)
            return markdown, entries, decisions

        error = AgentPipelineError(
            "整理与回顾未通过审查: " + "; ".join(last_feedback)
        )
        _save_validation_failure(
            store, run_id, retrospective.SPEC.name, normalized_payload, error
        )
        if attempt == _MAX_AGENT_ATTEMPTS or review_attempt_budget[0] == 0:
            raise error
        revision_context = _revision_context(
            attempt + 1,
            normalized_payload,
            _review_feedback(review_payload),
            source="Reviewer 实质审查",
        )
    raise AgentPipelineError("整理与回顾修订次数耗尽: " + "; ".join(last_feedback))


def _research_topics(
    planner_input: dict,
    current_source_ids: set[str],
    model_config: settings.ModelDict,
    store: AnalysisStore,
    run_id: str,
) -> list[dict]:
    _, topics = _validated_agent_call(
        research_planner.SPEC,
        "选择少量记录驱动或信息雷达驱动的公开研究主题。",
        planner_input,
        lambda candidate: research_planner.validate(
            candidate, current_source_ids
        ),
        model_config,
        store,
        run_id,
    )
    store.save_artifact(run_id, research_planner.SPEC.name, {"topics": topics})
    return topics


def _research_section(
    topics: list[dict],
    information_leads: str,
    current_source_ids: set[str],
    model_config: settings.ModelDict,
    store: AnalysisStore,
    run_id: str,
) -> str:
    research_input = {
        "research_topics": topics,
        "information_leads": information_leads,
    }
    revision_context = None
    last_feedback: list[str] = []
    review_attempt_budget = [_MAX_AGENT_ATTEMPTS]
    for attempt in range(1, _MAX_AGENT_ATTEMPTS + 1):
        try:
            payload = _call_agent(
                researcher.SPEC,
                "逐项联网查证并生成领域探索与研究板块。",
                research_input,
                model_config,
                store,
                run_id,
                revision_context=revision_context,
            )
        except AgentOutputError as error:
            if attempt == _MAX_AGENT_ATTEMPTS:
                raise
            revision_context = _revision_context(
                attempt + 1,
                error.response,
                [str(error)],
                source="中控 JSON 解析",
            )
            continue
        try:
            markdown, sources = researcher.validate(
                payload, topics, current_source_ids
            )
            telemetry = payload.get("_telemetry", {})
            used_search = bool(
                model_config.get("search", False)
                or telemetry.get("web_citations", 0)
                or telemetry.get("search_results", 0)
                or telemetry.get("tool_calls", {}).get("web_search", 0)
            )
            if not used_search:
                raise AgentPipelineError("领域研究没有实际执行联网搜索")
        except AgentPipelineError as error:
            _save_validation_failure(
                store, run_id, researcher.SPEC.name, payload, error
            )
            if attempt == _MAX_AGENT_ATTEMPTS:
                raise
            revision_context = _revision_context(
                attempt + 1,
                payload,
                [str(error)],
                source="中控确定性校验",
            )
            continue

        normalized_payload = {"markdown": markdown, "sources": sources}
        passed, _, last_feedback, review_payload = _review(
            "research_review",
            normalized_payload,
            set(),
            {
                "research_topics": topics,
                "information_leads": information_leads,
                "search_telemetry": telemetry,
            },
            model_config,
            store,
            run_id,
            attempt_budget=review_attempt_budget,
        )
        if passed:
            store.save_artifact(run_id, researcher.SPEC.name, normalized_payload)
            return markdown

        error = AgentPipelineError(
            "领域研究未通过审查: " + "; ".join(last_feedback)
        )
        _save_validation_failure(
            store, run_id, researcher.SPEC.name, normalized_payload, error
        )
        if attempt == _MAX_AGENT_ATTEMPTS or review_attempt_budget[0] == 0:
            raise error
        revision_context = _revision_context(
            attempt + 1,
            normalized_payload,
            _review_feedback(review_payload),
            source="Reviewer 实质审查",
        )
    raise AgentPipelineError("领域研究修订次数耗尽: " + "; ".join(last_feedback))


def _observed_dates(
    entries: list[dict], profiles_by_id: dict[str, dict], store: AnalysisStore
) -> None:
    refs = list(
        dict.fromkeys(ref for entry in entries for ref in entry["source_refs"])
    )
    source_dates = {
        source["source_id"]: source["source_date"]
        for source in store.source_records(refs)
    }
    for entry in entries:
        dates = [source_dates[ref] for ref in entry["source_refs"] if ref in source_dates]
        if not dates:
            raise AgentPipelineError("人物画像来源无法映射到记录日期")
        first_observed = min(dates)
        last_observed = max(dates)
        previous = profiles_by_id.get(entry.get("supersedes_id"))
        if previous:
            first_observed = min(first_observed, previous["first_observed"])
        entry["first_observed"] = first_observed
        entry["last_observed"] = last_observed


def _source_appendix(markdown: str, store: AnalysisStore) -> str:
    cited = sorted(cited_source_ids(markdown))
    records = {record["source_id"]: record for record in store.source_records(cited)}
    lines = ["## 来源索引"]
    for source_id in cited:
        record = records.get(source_id)
        if not record:
            continue
        lines.append(
            f"- [{source_id}] {record['source_date']} {record['source_time']} "
            f"— `{record['relative_path']}` 第 {record['record_index']} 条记录"
        )
    return "\n".join(lines)


def generate_analysis_report(
    kind: str,
    anchor: datetime.date,
    model_config: settings.ModelDict,
    *,
    origin: str = "manual",
    trigger: str | None = None,
) -> tuple[str, bool, Path | None]:
    """Generate one weekly/monthly two-section report and atomically save it."""
    if kind == "weekly":
        start = anchor - datetime.timedelta(days=anchor.weekday())
        end = start + datetime.timedelta(days=6)
        report_name = f"{start:%Y-%m-%d} 至 {end:%Y-%m-%d} 分析周报"
    elif kind == "monthly":
        start = anchor.replace(day=1)
        next_month = (start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        end = next_month - datetime.timedelta(days=1)
        report_name = f"{start:%Y年%m月} 分析月报"
    else:
        return "分析报告只支持 weekly 或 monthly。", False, None
    if origin not in {"manual", "auto"}:
        return f"未知报告来源: {origin}", False, None
    trigger = trigger or ("manual" if origin == "manual" else "scheduled")

    logs = _existing_logs(start, end)
    if not logs:
        return f"{start:%Y-%m-%d} 至 {end:%Y-%m-%d} 没有日记记录。", False, None
    records = _period_records(logs)
    if not records:
        return "日记中没有可识别的标准记录。", False, None

    report_path = _analysis_report_path(kind, start, end, origin)
    store: AnalysisStore | None = None
    run_id: str | None = None
    try:
        store = AnalysisStore()
        current_source_ids = {record["source_id"] for record in records}
        profiles = store.active_profiles(end.isoformat())
        profiles_by_id = {profile["id"]: profile for profile in profiles}
        historical_source_ids = {
            ref for profile in profiles for ref in profile["source_refs"]
        }
        allowed_source_ids = current_source_ids | historical_source_ids
        referenced_sources = _referenced_source_context(logs)
        recent_summaries = _recent_summary_context(start)
        supporting_reports = (
            _monthly_supporting_reports(start, end)
            if kind == "monthly"
            else "（周报不读取下级周期报告）"
        )
        information_leads = _information_briefings(start, end)
        snapshot = {
            "records": records,
            "profiles": profiles,
            "referenced_sources": referenced_sources,
            "recent_summaries": recent_summaries,
            "supporting_reports": supporting_reports,
            "information_leads": information_leads,
        }
        input_hash = hashlib.sha256(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        run_id = store.start_run(
            kind,
            start.isoformat(),
            end.isoformat(),
            origin,
            model_config.get("name", ""),
            input_hash,
            trigger=trigger,
        )
        logger.info(
            "analysis_started run=%s kind=%s origin=%s trigger=%s period=%s..%s",
            run_id,
            kind,
            origin,
            trigger,
            start,
            end,
        )
        store.save_sources(run_id, records)

        compact_profiles, profile_aliases = _profile_input(profiles)
        retrospective_input = {
            "period": {"kind": kind, "start": start.isoformat(), "end": end.isoformat()},
            "records": records,
            "historical_profiles": compact_profiles,
            "referenced_sources": referenced_sources,
            "recent_summaries": recent_summaries,
            "supporting_reports": supporting_reports,
        }
        retrospective_markdown, entries, decisions = _retrospective_section(
            retrospective_input,
            allowed_source_ids,
            current_source_ids,
            profile_aliases,
            model_config,
            store,
            run_id,
        )
        _observed_dates(entries, profiles_by_id, store)
        store.save_profile_entries(run_id, entries, decisions)

        planner_input = {
            "period": {"kind": kind, "start": start.isoformat(), "end": end.isoformat()},
            "records": records,
            "retrospective": retrospective_markdown,
            "daily_information_briefings": information_leads,
        }
        topics = _research_topics(
            planner_input, current_source_ids, model_config, store, run_id
        )
        research_markdown = _research_section(
            topics,
            information_leads,
            current_source_ids,
            model_config,
            store,
            run_id,
        )

        body = (
            "## 一、整理与回顾\n\n"
            + retrospective_markdown
            + "\n\n## 二、领域探索与研究\n\n"
            + research_markdown
        )
        origin_label = "手动" if origin == "manual" else "自动"
        trigger_label = {
            "manual": "手动生成",
            "scheduled": "系统调度",
            "retry": "自动任务重试",
        }[trigger]
        final_content = (
            f"# {report_name}\n\n"
            f"> 生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M}\n"
            f"> 报告来源：{origin_label}\n"
            f"> 触发方式：{trigger_label}\n"
            f"> 原始日记范围：{start:%Y-%m-%d} 至 {end:%Y-%m-%d}\n"
            f"> 分析运行：{run_id}\n\n"
            + body
            + "\n\n"
            + _source_appendix(body, store)
            + "\n"
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = report_path.with_suffix(report_path.suffix + ".tmp")
        previous_content = report_path.read_bytes() if report_path.exists() else None
        temp_path.write_text(final_content, encoding="utf-8")
        temp_path.replace(report_path)
        try:
            store.complete_run(run_id, report_path)
        except Exception:
            if previous_content is None:
                report_path.unlink(missing_ok=True)
            else:
                restore = report_path.with_suffix(report_path.suffix + ".restore.tmp")
                restore.write_bytes(previous_content)
                restore.replace(report_path)
            raise
        logger.info("analysis_completed run=%s kind=%s", run_id, kind)
        return body, True, report_path
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
        return f"分析失败: {message}", False, None
