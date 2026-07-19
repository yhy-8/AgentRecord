"""Select privacy-safe research topics from records and daily information."""

import re

from .base import AgentPipelineError, AgentSpec


SPEC = AgentSpec(
    name="research_planner",
    purpose="从周期记录与综合信息雷达中选择少量值得研究的公开领域问题",
    can_read_raw=True,
    readable_node_types=frozenset(),
    writable_node_types=frozenset(),
    writable_relation_types=frozenset(),
    allowed_tools=frozenset(),
    instructions="""为报告第二板块选择一至三个研究主题。主题可以来自记录中的观点、问题或兴趣，也可以来自周期内每日综合信息雷达；目标是拓宽视野，而不是给用户下行为指令。
只选择能够通过公开资料实质研究的领域问题。查询必须抽象化，不包含姓名、联系方式、长数字、本地路径或可识别私人细节。source_refs 只引用促成该主题的记录；纯新闻雷达主题可以为空。明确 origin 为 records、news 或 mixed。
只返回 JSON：{"topics":[{"topic_id":"Q001","title":"...","query":"适合公开搜索的查询","reason":"为何值得研究","origin":"records|news|mixed","source_refs":["R-..."]}]}。""",
)


def _sanitize(text: str, limit: int) -> str:
    value = re.sub(r"[\w.+-]+@[\w.-]+", "[email]", text)
    value = re.sub(r"(?<!\d)\d{7,}(?!\d)", "[number]", value)
    value = re.sub(
        r"(?:(?<!\w)[A-Za-z]:[\\/]|(?<![:/\w])/(?!/))[^\s]+",
        "[local-path]",
        value,
    )
    return value.strip()[:limit]


def validate(payload: dict, allowed_source_ids: set[str]) -> list[dict]:
    raw_topics = payload.get("topics", [])
    if not isinstance(raw_topics, list) or not 1 <= len(raw_topics) <= 3:
        raise AgentPipelineError("ResearchPlanner 必须返回一至三个主题")
    topics = []
    seen = set()
    for raw in raw_topics:
        if not isinstance(raw, dict):
            raise AgentPipelineError("研究主题必须是对象")
        topic_id = str(raw.get("topic_id", "")).strip()
        title = _sanitize(str(raw.get("title", "")), 200)
        query = _sanitize(str(raw.get("query", "")), 240)
        reason = _sanitize(str(raw.get("reason", "")), 500)
        origin = str(raw.get("origin", "")).strip()
        refs = raw.get("source_refs", [])
        if not re.fullmatch(r"Q\d{3}", topic_id) or topic_id in seen:
            raise AgentPipelineError("研究主题 ID 必须是唯一的 Q 三位数字")
        if not title or not query or not reason:
            raise AgentPipelineError("研究主题缺少标题、查询或理由")
        if origin not in {"records", "news", "mixed"}:
            raise AgentPipelineError("研究主题 origin 无效")
        if not isinstance(refs, list) or any(
            not isinstance(ref, str) or ref not in allowed_source_ids for ref in refs
        ):
            raise AgentPipelineError("研究主题包含未知记录来源")
        if origin in {"records", "mixed"} and not refs:
            raise AgentPipelineError("记录驱动研究主题必须引用记录")
        topics.append(
            {
                "topic_id": topic_id,
                "title": title,
                "query": query,
                "reason": reason,
                "origin": origin,
                "source_refs": list(dict.fromkeys(refs)),
            }
        )
        seen.add(topic_id)
    return topics
