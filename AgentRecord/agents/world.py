"""World Agent: conditional external verification and counterevidence."""

from .base import AgentPipelineError, AgentSpec
from .graph import validate_graph_payload


SPEC = AgentSpec(
    name="world",
    purpose="用外部知识核查并延伸候选观念、方法和点子",
    can_read_raw=False,
    readable_node_types=frozenset({"hypothesis", "insight"}),
    writable_node_types=frozenset({"research"}),
    writable_relation_types=frozenset({"supports", "challenges"}),
    allowed_tools=frozenset({"web_search"}),
    instructions="""只研究中控提供的问题。target_nodes 用于理解被研究的观念、方法或点子；调用 web_search 时只能使用中控已经去隐私的 research_queries，不得把节点中的私人细节加入搜索词。
外部研究同时承担两项职责：一是核查相关事实、时效性、支持证据和反例；二是寻找能延伸原想法的理论、概念、案例、相邻领域、适用边界和不同视角。优先可靠、直接和有解释力的来源，记录查证时间；不要堆砌链接。节点正文应综合说明查证结果、与目标想法的联系、限制，以及一至三个值得继续探索的方向。无法核实时如实说明，外部材料不得表述成用户已经认可的观点。
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
