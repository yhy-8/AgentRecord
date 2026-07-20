"""Daily web information briefing informed by the current week's records."""

import datetime
import difflib
import json
import logging
import re
from pathlib import Path

from .. import settings
from ..agents.researcher import canonical_url, markdown_urls
from ..ai_client import (
    CONFIG_ERROR_MARKER,
    _normalized_query,
    call_ai,
    response_telemetry,
    third_party_search_available,
)
from .context import _existing_logs, _period_records


logger = logging.getLogger(__name__)

_QUERY_HISTORY_MARKER = "agentrecord-targeted-queries"
_MAX_MODEL_ATTEMPTS = 3


def _revision_prompt(
    original_prompt: str,
    attempt: int,
    previous_output: str,
    errors: list[str],
) -> str:
    """Append correction data after the unchanged original prompt for cache reuse."""
    context = {
        "attempt": attempt,
        "maximum_attempts": _MAX_MODEL_ATTEMPTS,
        "problems_to_fix": errors,
        "rejected_previous_output": previous_output,
    }
    return (
        original_prompt
        + "\n\n【中控修订请求】\n"
        + "这是同一任务的有限修订。保留正确内容，只修正列出的问题，并重新输出完整结果；不要解释修改过程。\n"
        + json.dumps(context, ensure_ascii=False)
    )


def information_briefing_path(date: datetime.date) -> Path:
    return settings.ANALYSIS_DIR / "Information" / f"{date:%Y-%m-%d}.md"


def _week_record_context(date: datetime.date, limit: int = 18000) -> str:
    week_start = date - datetime.timedelta(days=date.weekday())
    records = _period_records(_existing_logs(week_start, date))
    sections: list[str] = []
    size = 0
    for record in reversed(records):
        if record.get("speaker") != "user":
            continue
        text = str(record.get("text", "")).strip()
        section = f"[{record['date']} {record['time']}] {text}\n"
        if size + len(section) > limit:
            if not sections:
                prefix = f"[{record['date']} {record['time']}] "
                sections.append(prefix + text[: max(0, limit - len(prefix) - 1)] + "\n")
            break
        sections.append(section)
        size += len(section)
    sections.reverse()
    return "".join(sections) or "（本周暂无用户记录）"


def _sanitize_text(text: str, limit: int) -> str:
    value = re.sub(r"[\w.+-]+@[\w.-]+", "[email]", text)
    value = re.sub(r"(?<!\d)\d{7,}(?!\d)", "[number]", value)
    value = re.sub(
        r"(?:(?<!\w)[A-Za-z]:[\\/]|(?<![:/\w])/(?!/))[^\s]+",
        "[local-path]",
        value,
    )
    return value.strip()[:limit]


def _query_fingerprint(query: str) -> str:
    value = query.casefold()
    value = re.sub(r"\b20\d{2}(?:[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?)?\b", "", value)
    value = re.sub(
        r"最新|近期|今日|新闻|资料|研究|分析|探索|方法|进展|现状|趋势|"
        r"latest|recent|today|news|research|analysis|explore|method|progress|trend",
        "",
        value,
    )
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value)


def _queries_overlap(left: str, right: str) -> bool:
    first = _query_fingerprint(left)
    second = _query_fingerprint(right)
    if not first or not second:
        return False
    if first == second:
        return True
    length_ratio = min(len(first), len(second)) / max(len(first), len(second))
    if (
        min(len(first), len(second)) >= 6
        and length_ratio >= 0.75
        and (first in second or second in first)
    ):
        return True
    return difflib.SequenceMatcher(None, first, second).ratio() >= 0.88


def _query_history_from_content(content: str) -> list[dict]:
    pattern = rf"<!--\s*{re.escape(_QUERY_HISTORY_MARKER)}:\s*([^\n]*?)\s*-->"
    match = re.search(pattern, content)
    if not match:
        return []
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    history = []
    for item in payload[:3]:
        if not isinstance(item, dict):
            continue
        query = _sanitize_text(str(item.get("query", "")), 240)
        reason = _sanitize_text(str(item.get("reason", "")), 300)
        if query:
            history.append({"query": query, "reason": reason})
    return history


def _prior_week_briefings(
    date: datetime.date, limit: int = 24000
) -> tuple[str, list[dict]]:
    week_start = date - datetime.timedelta(days=date.weekday())
    reports: list[tuple[datetime.date, str]] = []
    query_history: list[dict] = []
    current = week_start
    while current < date:
        path = information_briefing_path(current)
        if path.is_file():
            content = path.read_text(encoding="utf-8")
            query_history.extend(_query_history_from_content(content))
            visible_content = re.sub(
                rf"<!--\s*{re.escape(_QUERY_HISTORY_MARKER)}:[^\n]*?-->\s*",
                "",
                content,
            ).strip()
            reports.append((current, visible_content))
        current += datetime.timedelta(days=1)

    sections: list[str] = []
    remaining = limit
    for report_date, content in reversed(reports):
        header = f"【{report_date:%Y-%m-%d} 已有信息简报】\n"
        section = header + content
        if remaining <= len(header):
            break
        sections.append(section[:remaining])
        remaining -= min(len(section), remaining)
    sections.reverse()
    context = "\n\n".join(sections) or "（本周此前暂无信息简报）"
    return context, query_history


def _deduplicate_queries(queries: list[dict], history: list[dict]) -> list[dict]:
    accepted: list[dict] = []
    previous = [str(item.get("query", "")) for item in history]
    for item in queries:
        query = item["query"]
        if any(_queries_overlap(query, old_query) for old_query in previous):
            continue
        accepted.append(item)
        previous.append(query)
    return accepted


def _parse_queries(response: str) -> list[dict]:
    stripped = response.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*\n?(.*?)\n?```", stripped, re.DOTALL | re.IGNORECASE
    )
    if fenced:
        stripped = fenced.group(1).strip()
    payload = json.loads(stripped)
    if not isinstance(payload, dict) or not isinstance(payload.get("queries"), list):
        raise ValueError("定向信息查询格式无效")
    queries = []
    for item in payload["queries"][:3]:
        if not isinstance(item, dict):
            continue
        query = _sanitize_text(str(item.get("query", "")), 240)
        reason = _sanitize_text(str(item.get("reason", "")), 300)
        if query and reason:
            queries.append({"query": query, "reason": reason})
    return queries


def _targeted_queries(
    date: datetime.date,
    week_context: str,
    prior_briefings: str,
    query_history: list[dict],
    model_config: settings.ModelDict,
) -> tuple[list[dict], str]:
    if week_context == "（本周暂无用户记录）":
        return [], ""
    prompt = f"""[程序每日信息选题任务]
今天是 {date:%Y-%m-%d}。请综合本周记录、本周此前的信息简报和已用定向查询，提出 0 至 3 个今天仍值得联网搜索的高价值信息问题，用于核查观念、延伸想法或发现新材料。

去重要求：
- 已有简报已经覆盖的主题、事实或搜索角度不得换一种说法再次搜索；长期兴趣本身不构成每天重复搜索的理由。
- 只有出现新的事件、数据、争议、来源，或已有简报留下了明确待核查问题时，才可继续追踪同一领域；查询和 reason 都必须写清新增角度。
- 如果今天没有真正不同且有价值的定向问题，返回空数组，不要为了凑数重复搜索。

隐私要求：
- 查询必须抽象化，不得包含姓名、联系方式、长数字、本地路径或可识别的私人细节。
- 不要搜索纯私人事件或无法通过公开资料改善的问题。
- 只输出 JSON，格式为 {{"queries":[{{"query":"...","reason":"..."}}]}}。

【本周记录，仅用于生成去隐私查询】
{week_context}

【本周此前的信息简报，用于避免重复选题】
{prior_briefings}

【本周已经使用的定向查询】
{json.dumps(query_history, ensure_ascii=False)}"""
    current_prompt = prompt
    for attempt in range(1, _MAX_MODEL_ATTEMPTS + 1):
        try:
            response, success, _, _, _ = call_ai(
                current_prompt,
                model_config,
                allowed_tools=(),
                structured_output=True,
            )
        except TypeError as error:
            if "structured_output" not in str(error):
                raise
            response, success, _, _, _ = call_ai(
                current_prompt, model_config, allowed_tools=()
            )
        if not success:
            return [], response
        try:
            return _deduplicate_queries(_parse_queries(response), query_history), ""
        except (ValueError, json.JSONDecodeError) as error:
            if attempt == _MAX_MODEL_ATTEMPTS:
                return [], f"定向信息查询连续 {_MAX_MODEL_ATTEMPTS} 次没有返回有效 JSON"
            current_prompt = _revision_prompt(
                prompt,
                attempt + 1,
                response,
                [f"定向信息查询格式无效: {error}"],
            )
    return [], "定向信息查询格式无效"


def _has_five_daily_highlights(markdown: str) -> bool:
    match = re.search(
        r"^## 今日值得关注\s*$([\s\S]*?)(?=^##\s|\Z)",
        markdown,
        re.MULTILINE,
    )
    if not match:
        return False
    headings = re.findall(r"^###\s+(.+?)\s*$", match.group(1), re.MULTILINE)
    numbers = [
        heading_match.group(1)
        for heading in headings
        if (heading_match := re.fullmatch(r"([1-5])[.、]\s+\S.*", heading))
    ]
    return len(headings) == 5 and numbers == ["1", "2", "3", "4", "5"]


def _targeted_exploration_ids(markdown: str) -> list[str]:
    match = re.search(
        r"^## 与本周思考相关的探索\s*$([\s\S]*?)(?=^##\s|\Z)",
        markdown,
        re.MULTILINE,
    )
    if not match:
        return []
    return re.findall(r"^###\s+(T\d{3})[.、]\s+\S", match.group(1), re.MULTILINE)


def _targeted_exploration_headings(markdown: str) -> list[str]:
    match = re.search(
        r"^## 与本周思考相关的探索\s*$([\s\S]*?)(?=^##\s|\Z)",
        markdown,
        re.MULTILINE,
    )
    if not match:
        return []
    return re.findall(r"^###\s+(.+?)\s*$", match.group(1), re.MULTILINE)


def _heading_link_map(markdown: str, section: str, id_pattern: str) -> dict[str, set]:
    match = re.search(
        rf"^## {re.escape(section)}\s*$([\s\S]*?)(?=^##\s|\Z)",
        markdown,
        re.MULTILINE,
    )
    if not match:
        return {}
    result = {}
    block_pattern = re.compile(
        rf"^###\s+({id_pattern})[.、]\s+\S.*?$([\s\S]*?)(?=^###\s|\Z)",
        re.MULTILINE,
    )
    for item in block_pattern.finditer(match.group(1)):
        result[item.group(1)] = markdown_urls(item.group(2))
    return result


def _briefing_errors(
    markdown: str,
    used_search: bool,
    evidence_urls: set[tuple] | None = None,
    targeted_ids: list[str] | None = None,
    targeted_evidence_urls: dict[str, set[tuple]] | None = None,
) -> list[str]:
    errors = []
    if not used_search:
        errors.append("没有实际执行联网搜索")
    linked_urls = markdown_urls(markdown)
    if not linked_urls:
        errors.append("没有可验证的 Markdown 来源链接")
    if evidence_urls is not None:
        if not evidence_urls:
            errors.append("联网搜索没有返回可审计的来源证据")
        elif linked_urls - evidence_urls:
            errors.append("正文包含未出现在本轮搜索证据中的 URL")
    if markdown.lstrip().startswith(("```", "# ")):
        errors.append("输出包含代码围栏或一级标题")
    required_sections = (
        "## 今日值得关注",
        "## 与本周思考相关的探索",
        "## 可继续追踪",
    )
    missing = [section for section in required_sections if section not in markdown]
    if missing:
        errors.append("缺少必需章节: " + "、".join(missing))
    if not _has_five_daily_highlights(markdown):
        errors.append("“今日值得关注”没有严格生成编号 1 至 5 的五项")
    highlight_links = _heading_link_map(
        markdown, "今日值得关注", r"[1-5]"
    )
    missing_highlight_links = [
        str(number)
        for number in range(1, 6)
        if not highlight_links.get(str(number))
    ]
    if missing_highlight_links:
        errors.append(
            "“今日值得关注”存在没有就近来源链接的项: "
            + "、".join(missing_highlight_links)
        )
    actual_targeted_ids = _targeted_exploration_ids(markdown)
    targeted_headings = _targeted_exploration_headings(markdown)
    if (
        actual_targeted_ids != (targeted_ids or [])
        or len(targeted_headings) != len(actual_targeted_ids)
    ):
        errors.append(
            "“与本周思考相关的探索”没有按定向选题逐项生成: "
            f"期望 {targeted_ids or []}，实际 {actual_targeted_ids}"
        )
    if targeted_evidence_urls is not None:
        targeted_links = _heading_link_map(
            markdown, "与本周思考相关的探索", r"T\d{3}"
        )
        unsupported = [
            topic_id
            for topic_id in (targeted_ids or [])
            if not (
                targeted_links.get(topic_id, set())
                & targeted_evidence_urls.get(topic_id, set())
            )
        ]
        if unsupported:
            errors.append(
                "定向探索没有引用对应查询返回的来源: "
                + "、".join(unsupported)
            )
    return errors


def generate_information_briefing(
    date: datetime.date,
    model_config: settings.ModelDict,
) -> tuple[str, bool, Path | None]:
    """Search current information and atomically save one briefing for ``date``."""
    if not third_party_search_available():
        return (
            f"{CONFIG_ERROR_MARKER} 每日信息收集需要启用第三方搜索，"
            "以便中控逐条审计查询和来源。",
            False,
            None,
        )

    week_context = _week_record_context(date)
    prior_briefings, query_history = _prior_week_briefings(date)
    targeted, error = _targeted_queries(
        date, week_context, prior_briefings, query_history, model_config
    )
    if error:
        return f"生成定向信息查询失败: {error}", False, None
    targeted = [
        {"topic_id": f"T{index:03d}", **item}
        for index, item in enumerate(targeted, 1)
    ]
    queries = [
        {
            "query": f"{date:%Y-%m-%d} 全球重要新闻 国际 科技 科学 经济",
            "reason": "获取当日高价值综合信息",
        },
        {
            "query": f"{date:%Y-%m-%d} 中国重要新闻 人工智能 科技 商业 社会",
            "reason": "补充中文世界与技术变化",
        },
        *targeted,
    ]
    prompt = f"""[程序每日信息收集任务]
今天是 {date:%Y-%m-%d}。中控会在调用前逐项执行下列固定查询，并把本轮结果作为附加输入提供。你不得改写或补充查询，只基于这些已审计结果生成一份可独立阅读的中文信息简报。格式修订会复用同一证据集，不再重复搜索。

要求：
- 只收录具有较高信息量、可验证且对理解变化有价值的内容，不做热搜堆砌。
- 优先一手、权威和多源可交叉验证的资料；区分事实、来源观点和 AI 推断。
- 包含“今日值得关注”、“与本周思考相关的探索”、“可继续追踪”三个二级标题。
- “今日值得关注”用于纯粹拓宽视野，固定选择五项彼此独立的重要信息，并严格使用“### 1. 标题”至“### 5. 标题”作为三级标题。
- “与本周思考相关的探索”只对应定向查询；每项必须按输入顺序生成，并严格使用“### T001. 标题”等携带 topic_id 的三级标题。没有定向查询时该章节不写三级标题，只说明本次没有合适选题。
- 每项关键信息就近附上 Markdown 来源链接和日期；无可靠来源时不要写成事实。
- 定向查询是本周记录抽象后的研究方向，不得推测或还原私人细节。
- 对照本周此前的信息简报，已经报道过的事件、背景和结论不得重复写入。只有出现实质性新事件、新数据、新来源或相反证据时才继续同一主题，并明确说明“新在哪里”。
- 只输出 Markdown 正文，不要一级标题、代码围栏或完成提示。

【已去隐私的查询】
{json.dumps(queries, ensure_ascii=False)}

【本周此前的信息简报，仅用于查重，不得直接当作新事实复述】
{prior_briefings}"""
    current_prompt = prompt
    markdown = ""
    citations = result_count = 0
    tool_counts: dict[str, int] = {}
    audit_telemetry: dict = {}
    audit_used_search = False
    revision_evidence = ""
    for attempt in range(1, _MAX_MODEL_ATTEMPTS + 1):
        if attempt == 1:
            try:
                response = call_ai(
                    current_prompt,
                    model_config,
                    allowed_tools={"web_search"},
                    allowed_search_queries=[item["query"] for item in queries],
                )
            except TypeError as error:
                if "allowed_search_queries" not in str(error):
                    raise
                response = call_ai(
                    current_prompt, model_config, allowed_tools={"web_search"}
                )
        else:
            response = call_ai(
                current_prompt
                + "\n\n【中控复用的已审计搜索证据】\n"
                + revision_evidence,
                model_config,
                allowed_tools=(),
            )
        markdown, success, citations, tool_counts, result_count = response
        if not success:
            return markdown, False, None
        if attempt == 1:
            audit_telemetry = response_telemetry(response)
            audit_used_search = bool(citations or result_count)
            revision_evidence = json.dumps(
                audit_telemetry.get("search_evidence", []),
                ensure_ascii=False,
            )
        telemetry = audit_telemetry
        evidence_urls = {
            canonical_url(str(item.get("url", "")))
            for item in telemetry.get("search_evidence", [])
            if isinstance(item, dict) and item.get("url")
        }
        evidence_by_query: dict[str, set[tuple]] = {}
        for item in telemetry.get("search_evidence", []):
            if not isinstance(item, dict) or not item.get("url"):
                continue
            evidence_by_query.setdefault(
                _normalized_query(item.get("query", "")), set()
            ).add(canonical_url(str(item["url"])))
        targeted_evidence_urls = {
            item["topic_id"]: evidence_by_query.get(
                _normalized_query(item["query"]), set()
            )
            for item in targeted
        }
        errors = _briefing_errors(
            markdown,
            audit_used_search,
            evidence_urls,
            [item["topic_id"] for item in targeted],
            targeted_evidence_urls,
        )
        completed_queries = telemetry.get("completed_search_queries")
        if not isinstance(completed_queries, list):
            errors.append("联网搜索缺少可审计的已完成查询记录")
        else:
            missing_queries = [
                item["query"]
                for item in queries
                if _normalized_query(item["query"]) not in completed_queries
            ]
            if missing_queries:
                errors.append(
                    "没有逐项执行全部授权查询: " + "、".join(missing_queries)
                )
        if not errors:
            break
        if attempt == _MAX_MODEL_ATTEMPTS:
            return (
                f"信息简报连续 {_MAX_MODEL_ATTEMPTS} 次未通过校验: "
                + "；".join(errors),
                False,
                None,
            )
        current_prompt = _revision_prompt(
            prompt, attempt + 1, markdown, errors
        )

    path = information_briefing_path(date)
    path.parent.mkdir(parents=True, exist_ok=True)
    final_content = (
        f"# {date:%Y-%m-%d} 每日信息简报\n\n"
        f"> 生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M}\n"
        f"> 定向研究：{len(targeted)} 项\n\n"
        f"<!-- {_QUERY_HISTORY_MARKER}: "
        f"{json.dumps(targeted, ensure_ascii=False, separators=(',', ':'))} -->\n\n"
        f"{markdown.strip()}\n"
    )
    temp_path = path.with_suffix(
        path.suffix + f".{datetime.datetime.now():%Y%m%d%H%M%S%f}.tmp"
    )
    temp_path.write_text(final_content, encoding="utf-8")
    temp_path.replace(path)
    logger.info(
        "information_briefing_completed date=%s searches=%s results=%s",
        date.isoformat(),
        citations + tool_counts.get("web_search", 0),
        result_count,
    )
    return markdown, True, path
