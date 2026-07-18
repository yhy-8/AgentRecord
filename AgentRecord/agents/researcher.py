"""Web-enabled Agent for the report's domain research section."""

import re

from .base import AgentPipelineError, AgentSpec, cited_source_ids


SPEC = AgentSpec(
    name="researcher",
    purpose="对中控选定的公开领域问题进行查证、探索与推演",
    can_read_raw=False,
    readable_node_types=frozenset(),
    writable_node_types=frozenset(),
    writable_relation_types=frozenset(),
    allowed_tools=frozenset({"web_search"}),
    instructions="""逐项研究 research_topics。调用 web_search 时只能使用中控给出的 query，不得尝试还原私人背景。优先一手、权威、可核查来源，同时寻找反例、适用边界、相邻概念和不同视角。
生成报告第二板块正文：它是一份领域研究，而不是新闻链接堆砌或行为建议。记录驱动主题应保留中控给出的 [R-...] 来源标记，外部事实必须就近使用 Markdown 链接。明确区分记录为何引出问题、外部资料说明什么、AI 进行了什么有限推演。探索性推断可以大胆，但必须显式标注不确定性。
只返回 JSON：{"markdown":"不含一、二级标题的第二板块正文","sources":[{"topic_id":"Q001","title":"...","url":"https://...","published":"..."}]}。""",
)


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
    required_refs = {
        ref
        for topic in topics
        if topic["origin"] in {"records", "mixed"}
        for ref in topic["source_refs"]
    }
    if required_refs and not cited & required_refs:
        raise AgentPipelineError("领域研究没有标明记录驱动主题的来源")
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
        if topic_id not in topic_ids or not title or not url.startswith(("http://", "https://")):
            raise AgentPipelineError("领域研究来源缺少有效主题、标题或 URL")
        sources.append(
            {
                "topic_id": topic_id,
                "title": title,
                "url": url,
                "published": str(raw.get("published", "")).strip(),
            }
        )
    if not sources or not re.search(r"https?://", markdown):
        raise AgentPipelineError("领域研究没有可验证的外部来源")
    covered_topic_ids = {source["topic_id"] for source in sources}
    if covered_topic_ids != topic_ids:
        raise AgentPipelineError("领域研究没有为每个中控选题提供来源")
    missing_links = [source["url"] for source in sources if source["url"] not in markdown]
    if missing_links:
        raise AgentPipelineError("领域研究 sources 中的 URL 没有在正文就近引用")
    return markdown.strip(), sources
