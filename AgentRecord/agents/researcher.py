"""Web-enabled Agent for the report's domain research section."""

import re
from urllib.parse import parse_qsl, unquote, urlsplit

from .base import AgentPipelineError, AgentSpec, cited_source_ids


SPEC = AgentSpec(
    name="researcher",
    purpose="对中控选定的公开领域问题进行查证、探索与推演",
    can_read_raw=False,
    readable_node_types=frozenset(),
    writable_node_types=frozenset(),
    writable_relation_types=frozenset(),
    allowed_tools=frozenset({"web_search"}),
    instructions="""逐项研究 research_topics。每一次输出（包括格式修订和 Reviewer 退回后的修订）都必须重新调用 web_search；调用时只能逐字使用中控给出的 query，不得尝试还原私人背景。优先一手、权威、可核查来源，同时寻找反例、适用边界、相邻概念和不同视角。
生成报告第二板块正文：它是一份领域研究，而不是新闻链接堆砌或行为建议。记录驱动主题应保留中控给出的 [R-...] 来源标记，外部事实必须就近使用 Markdown 链接。明确区分记录为何引出问题、外部资料说明什么、AI 进行了什么有限推演。探索性推断可以大胆，但必须显式标注不确定性。
只使用工具结果中“链接：”字段真实出现的 URL，并将同一 URL 原样放入正文 Markdown 链接和 sources；中控修订请求中的 verified_source_options 是本次运行此前轮次已审计的真实工具结果，也可以继续使用。摘要正文里出现的网址不算可用来源，不得凭记忆补全、改写或推测链接。sources 只列正文实际引用的链接，不要附加“备选来源”。只返回 JSON：{"markdown":"不含一、二级标题的第二板块正文","sources":[{"topic_id":"Q001","title":"...","url":"https://...","published":"..."}]}。""",
)


_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}


def canonical_url(url: str) -> tuple[str, str, str, tuple[tuple[str, str], ...]]:
    """Return a comparison key while preserving the delivered URL verbatim."""
    parts = urlsplit(url.strip())
    query = tuple(
        sorted(
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_")
            and key.casefold() not in _TRACKING_QUERY_KEYS
        )
    )
    path = unquote(parts.path).rstrip("/") or "/"
    return parts.scheme.casefold(), parts.netloc.casefold(), path, query


def markdown_urls(markdown: str) -> set[tuple[str, str, str, tuple[tuple[str, str], ...]]]:
    targets = re.findall(
        r"\]\(\s*(https?://(?:[^()\s]|\([^()\s]*\))+)\s*\)", markdown
    )
    return {canonical_url(target) for target in targets}


def validate(
    payload: dict, topics: list[dict], allowed_source_ids: set[str]
) -> tuple[str, list[dict]]:
    markdown = payload.get("markdown", "")
    if not isinstance(markdown, str) or not markdown.strip():
        raise AgentPipelineError("Researcher markdown 为空或格式错误")
    if re.search(r"^#{1,2}\s", markdown, re.MULTILINE) or "```" in markdown:
        raise AgentPipelineError("领域研究包含一、二级标题或代码围栏")
    cited = cited_source_ids(markdown)
    if cited - allowed_source_ids:
        raise AgentPipelineError("领域研究引用未知记录来源")
    missing_record_topics = [
        topic["topic_id"]
        for topic in topics
        if topic["origin"] in {"records", "mixed"}
        and not cited.intersection(topic["source_refs"])
    ]
    if missing_record_topics:
        raise AgentPipelineError(
            "领域研究没有逐项标明记录驱动主题的来源: "
            + "、".join(missing_record_topics)
        )
    raw_sources = payload.get("sources", [])
    if not isinstance(raw_sources, list):
        raise AgentPipelineError("Researcher sources 必须是数组")
    topic_ids = {topic["topic_id"] for topic in topics}
    sources = []
    for raw in raw_sources:
        if not isinstance(raw, dict):
            raise AgentPipelineError("领域研究来源必须是对象")
        topic_id = str(raw.get("topic_id", "")).strip()
        title = str(raw.get("title", "")).strip()
        url = str(raw.get("url", "")).strip()
        url_key = canonical_url(url)
        if (
            topic_id not in topic_ids
            or not title
            or url_key[0] not in {"http", "https"}
            or not url_key[1]
        ):
            raise AgentPipelineError("领域研究来源缺少有效主题、标题或 URL")
        sources.append(
            {
                "topic_id": topic_id,
                "title": title,
                "url": url,
                "published": str(raw.get("published", "")).strip(),
            }
        )
    linked_urls = markdown_urls(markdown)
    if not sources or not linked_urls:
        raise AgentPipelineError("领域研究没有可验证的外部来源")
    declared_urls = {canonical_url(source["url"]) for source in sources}
    if linked_urls - declared_urls:
        raise AgentPipelineError("领域研究正文包含未列入 sources 的外部链接")
    # ``sources`` is audit metadata rather than delivered report content.  Models
    # sometimes append unused alternatives here; discard those harmless extras
    # instead of rejecting an otherwise cited draft.  Topic coverage below still
    # requires at least one source that the report actually links.
    sources = [
        source
        for source in sources
        if canonical_url(source["url"]) in linked_urls
    ]
    covered_topic_ids = {source["topic_id"] for source in sources}
    if covered_topic_ids != topic_ids:
        raise AgentPipelineError("领域研究正文没有为每个中控选题就近引用来源")
    return markdown.strip(), sources
