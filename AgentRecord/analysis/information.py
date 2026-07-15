"""Daily web information briefing informed by the current week's records."""

import datetime
import json
import logging
import re
from pathlib import Path

from .. import settings
from ..ai_client import call_ai
from .context import _existing_logs, _period_records


logger = logging.getLogger(__name__)


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


def _parse_queries(response: str) -> list[dict]:
    stripped = response.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, count=1)
        stripped = re.sub(r"\s*```$", "", stripped, count=1)
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
    model_config: settings.ModelDict,
) -> tuple[list[dict], str]:
    if week_context == "（本周暂无用户记录）":
        return [], ""
    prompt = f"""[程序每日信息选题任务]
今天是 {date:%Y-%m-%d}。请根据本周记录，提出 1 至 3 个适合今天联网搜索的高价值信息问题，用于核查观念、延伸想法或发现新材料。

隐私要求：
- 查询必须抽象化，不得包含姓名、联系方式、长数字、本地路径或可识别的私人细节。
- 不要搜索纯私人事件或无法通过公开资料改善的问题。
- 只输出 JSON，格式为 {{"queries":[{{"query":"...","reason":"..."}}]}}。

【本周记录，仅用于生成去隐私查询】
{week_context}"""
    invalid_response = ""
    for attempt in range(2):
        current_prompt = prompt
        if attempt:
            current_prompt = (
                "上次回答不是指定 JSON。仅修复格式，不增删查询；"
                "只输出 {\"queries\":[{\"query\":\"...\",\"reason\":\"...\"}]}。\n"
                f"待修复回答：{invalid_response}"
            )
        response, success, _, _, _ = call_ai(
            current_prompt, model_config, allowed_tools=()
        )
        if not success:
            return [], response
        try:
            return _parse_queries(response), ""
        except (ValueError, json.JSONDecodeError):
            invalid_response = response
    return [], "定向信息查询连续两次未返回有效 JSON"


def _web_search_available(model_config: settings.ModelDict) -> bool:
    third_search = settings.CONFIG.get("third_search", {})
    return bool(
        model_config.get("search", False)
        or (
            third_search.get("enabled", False)
            and third_search.get("api_key", "")
        )
    )


def generate_information_briefing(
    date: datetime.date,
    model_config: settings.ModelDict,
) -> tuple[str, bool, Path | None]:
    """Search current information and atomically save one briefing for ``date``."""
    if not _web_search_available(model_config):
        return "当前模型和第三方搜索都未启用联网能力。", False, None

    week_context = _week_record_context(date)
    targeted, error = _targeted_queries(date, week_context, model_config)
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
- 每项关键信息就近附上 Markdown 来源链接和日期；无可靠来源时不要写成事实。
- 定向查询是本周记录抽象后的研究方向，不得推测或还原私人细节。
- 只输出 Markdown 正文，不要一级标题、代码围栏或完成提示。

【已去隐私的查询】
{json.dumps(queries, ensure_ascii=False)}"""
    markdown, success, citations, tool_counts, result_count = call_ai(
        prompt, model_config, allowed_tools={"web_search"}
    )
    if not success:
        return markdown, False, None
    used_search = bool(
        model_config.get("search", False)
        or citations
        or tool_counts.get("web_search", 0)
        or result_count
    )
    if not used_search:
        return "信息收集没有实际执行联网搜索。", False, None
    if not re.search(r"https?://", markdown):
        return "信息简报没有可验证的来源链接。", False, None
    if markdown.lstrip().startswith(("```", "# ")):
        return "信息简报格式无效。", False, None
    required_sections = (
        "## 今日值得关注",
        "## 与本周思考相关的探索",
        "## 可继续追踪",
    )
    if any(section not in markdown for section in required_sections):
        return "信息简报缺少必需章节。", False, None

    path = information_briefing_path(date)
    path.parent.mkdir(parents=True, exist_ok=True)
    final_content = (
        f"# {date:%Y-%m-%d} 每日信息简报\n\n"
        f"> 生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M}\n"
        f"> 定向研究：{len(targeted)} 项\n\n"
        f"{markdown.strip()}\n"
    )
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(final_content, encoding="utf-8")
    temp_path.replace(path)
    logger.info(
        "information_briefing_completed date=%s searches=%s results=%s",
        date.isoformat(),
        citations + tool_counts.get("web_search", 0),
        result_count,
    )
    return markdown, True, path
