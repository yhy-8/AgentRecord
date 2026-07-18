"""Daily web information briefing informed by the current week's records."""

import datetime
import difflib
import json
import logging
import re
from pathlib import Path

from .. import settings
from ..ai_client import call_ai, web_search_available
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
    sections = []
    size = 0
    for record in records:
        if record.get("speaker") != "user":
            continue
        text = str(record.get("text", "")).strip()
        section = f"[{record['date']} {record['time']}] {text}\n"
        if size + len(section) > limit:
            break
        sections.append(section)
        size += len(section)
    return "".join(sections) or "（本周暂无用户记录）"


def _sanitize_text(text: str, limit: int) -> str:
    value = re.sub(r"[\w.+-]+@[\w.-]+", "[email]", text)
    value = re.sub(r"(?<!\d)\d{7,}(?!\d)", "[number]", value)
    value = re.sub(
        r"(?:[A-Za-z]:\\|/home/|/Users/|/mnt/)[^\s]+", "[local-path]", value
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
    numbers = re.findall(r"^###\s+([1-5])[.、]\s+\S", match.group(1), re.MULTILINE)
    return numbers == ["1", "2", "3", "4", "5"]


def _briefing_errors(markdown: str, used_search: bool) -> list[str]:
    errors = []
    if not used_search:
        errors.append("没有实际执行联网搜索")
    if not re.search(r"https?://", markdown):
        errors.append("没有可验证的来源链接")
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
    return errors


def generate_information_briefing(
    date: datetime.date,
    model_config: settings.ModelDict,
) -> tuple[str, bool, Path | None]:
    """Search current information and atomically save one briefing for ``date``."""
    if not web_search_available(model_config):
        return "当前模型和第三方搜索都未启用联网能力。", False, None

    week_context = _week_record_context(date)
    prior_briefings, query_history = _prior_week_briefings(date)
    targeted, error = _targeted_queries(
        date, week_context, prior_briefings, query_history, model_config
    )
    if error:
        return f"生成定向信息查询失败: {error}", False, None
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
今天是 {date:%Y-%m-%d}。你必须使用 web_search 逐项搜索下列查询，生成一份可独立阅读的中文信息简报。

要求：
- 只收录具有较高信息量、可验证且对理解变化有价值的内容，不做热搜堆砌。
- 优先一手、权威和多源可交叉验证的资料；区分事实、来源观点和 AI 推断。
- 包含“今日值得关注”、“与本周思考相关的探索”、“可继续追踪”三个二级标题。
- “今日值得关注”用于纯粹拓宽视野，固定选择五项彼此独立的重要信息，并严格使用“### 1. 标题”至“### 5. 标题”作为三级标题。
- “与本周思考相关的探索”只对应定向查询；定向查询可以为零至三项，不得为了数量重复本周已有主题。
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
    for attempt in range(1, _MAX_MODEL_ATTEMPTS + 1):
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
        markdown, success, citations, tool_counts, result_count = response
        if not success:
            return markdown, False, None
        used_search = bool(
            citations or result_count
        )
        errors = _briefing_errors(markdown, used_search)
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
