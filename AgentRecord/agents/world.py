"""World Agent: conditional external verification and counterevidence."""

from .base import AgentPipelineError, AgentSpec
from .graph import validate_graph_payload


SPEC = AgentSpec(
    name="world",
    purpose="按需核查外部事实、时效性和反例",
    can_read_raw=False,
    readable_node_types=frozenset({"hypothesis", "insight"}),
    writable_node_types=frozenset({"research"}),
    writable_relation_types=frozenset({"supports", "challenges"}),
    allowed_tools=frozenset({"web_search"}),
    instructions="""只研究中控提供的问题。优先可靠和直接来源，记录查证时间；无法核实时如实说明。不得搜索或输出与研究问题无关的私人信息。
输出 JSON：{"nodes":[{"temp_id":"...","node_type":"research","title":"...","body":"...","confidence":0到1,"source_refs":[],"metadata":{"target_id":"候选节点ID","query":"...","checked_at":"YYYY-MM-DD","sources":[{"title":"...","url":"...","published":"..."}],"result":"supported|challenged|mixed|unverified"}}],"edges":[{"source_id":"研究节点ID","target_id":"候选节点ID","relation_type":"supports|challenges","weight":0到1,"confidence":0到1,"rationale":"..."}]}。""",
)


def _validate_metadata(node_type: str, metadata: dict, visible: set[str]) -> None:
    if metadata.get("target_id") not in visible:
        raise AgentPipelineError("World research 指向不可见候选")
    if metadata.get("result") not in {
        "supported",
        "challenged",
        "mixed",
        "unverified",
    }:
        raise AgentPipelineError("World research 缺少有效 result")
    sources = metadata.get("sources", [])
    if not isinstance(sources, list) or any(
        not isinstance(source, dict) for source in sources
    ):
        raise AgentPipelineError("World research sources 格式错误")
    if metadata["result"] != "unverified" and not any(
        str(source.get("url", "")).startswith(("http://", "https://"))
        for source in sources
    ):
        raise AgentPipelineError("World 已核查结论缺少外部来源 URL")


def validate(
    payload: dict, *, allowed_source_ids: set[str], visible_node_ids: set[str]
) -> tuple[list[dict], list[dict]]:
    return validate_graph_payload(
        SPEC,
        payload,
        allowed_source_ids=allowed_source_ids,
        visible_node_ids=visible_node_ids,
        validate_metadata=_validate_metadata,
    )
