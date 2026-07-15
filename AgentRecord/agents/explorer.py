"""Explorer Agent: themes and evidence to hypotheses and insights."""

import re

from .base import AgentPipelineError, AgentSpec
from .graph import validate_graph_payload


SPEC = AgentSpec(
    name="explorer",
    purpose="提取并探索观念、思维模型、方法论、点子及其可能方向",
    can_read_raw=False,
    readable_node_types=frozenset(
        {"evidence", "theme", "hypothesis", "research", "insight"}
    ),
    writable_node_types=frozenset({"hypothesis", "insight"}),
    writable_relation_types=frozenset(
        {"supports", "challenges", "evolves_from", "contradicts"}
    ),
    allowed_tools=frozenset(),
    instructions="""优先少量、有依据且能提高下一轮思考质量的发现。主动识别用户记录中的观点、思维模型、判断原则、方法论和未充分展开的点子，而不只是概括发生了什么。沿有价值的方向提出联系、演化、矛盾、盲点、适用边界、相邻概念和可探索假设；明确区分用户原意、AI 推断和待外部查证内容。
为每个候选列出支持来源、反证或替代解释。外部研究既用于验证事实和观念，也用于寻找相关理论、案例、反例和相邻领域，从而延伸主题、拓宽视野。只要材料中存在可被外部知识实质拓展的观念、方法或点子，通常应选择 1 至 3 个最高价值方向，将对应节点设为 research_needed=true，并为每个节点给出至少一条不含私人细节的 research_queries；只有材料全是私人事实或流水记录、确无可研究问题时才可全部为 false 并返回空数组。不要自己声称已经查证。
source_refs 可以填写输入中的知识节点 ID，中控会展开其原始来源；不要编造来源 ID。
输出 JSON：{"nodes":[{"temp_id":"...","node_type":"hypothesis|insight","title":"...","body":"...","confidence":0到1,"source_refs":["输入节点ID"],"supersedes_id":null,"metadata":{"insight_type":"connection|evolution|contradiction|blind_spot|viewpoint|mental_model|methodology|extension","evidence_for":["节点ID"],"evidence_against":["节点ID"],"inference_level":"low|medium|high","why_it_matters":"...","research_needed":true}}],"edges":[{"source_id":"节点ID","target_id":"节点ID","relation_type":"supports|challenges|evolves_from|contradicts","weight":0到1,"confidence":0到1,"rationale":"..."}],"research_queries":[{"target_id":"候选节点ID","query":"去除私人细节后的查询","reason":"验证、反例或延伸目标"}]}。""",
)


def _validate_metadata(node_type: str, metadata: dict, visible: set[str]) -> None:
    if metadata.get("insight_type") not in {
        "connection",
        "evolution",
        "contradiction",
        "blind_spot",
        "viewpoint",
        "mental_model",
        "methodology",
        "extension",
    }:
        raise AgentPipelineError("Explorer 节点缺少有效 insight_type")
    if metadata.get("inference_level") not in {"low", "medium", "high"}:
        raise AgentPipelineError("Explorer 节点缺少有效 inference_level")
    for field in ("evidence_for", "evidence_against"):
        references = metadata.get(field, [])
        if not isinstance(references, list) or any(
            not isinstance(item, str) or item not in visible for item in references
        ):
            raise AgentPipelineError(f"Explorer 的 {field} 引用不可见节点")
    if not metadata.get("evidence_for"):
        raise AgentPipelineError("Explorer 节点没有支持证据")
    if not isinstance(metadata.get("research_needed"), bool):
        raise AgentPipelineError("Explorer 节点缺少布尔 research_needed")


def validate(
    payload: dict, *, allowed_source_ids: set[str], visible_node_ids: set[str]
) -> tuple[list[dict], list[dict]]:
    nodes, edges = validate_graph_payload(
        SPEC,
        payload,
        allowed_source_ids=allowed_source_ids,
        visible_node_ids=visible_node_ids,
        validate_metadata=_validate_metadata,
    )
    queries = payload.get("research_queries", [])
    if not isinstance(queries, list):
        raise AgentPipelineError("Explorer 的 research_queries 必须是数组")
    if len(queries) > 5:
        raise AgentPipelineError("Explorer 的 research_queries 最多五条")
    nodes_by_id = {node["temp_id"]: node for node in nodes}
    allowed_targets = set(nodes_by_id) | visible_node_ids
    query_targets = set()
    for item in queries:
        if not isinstance(item, dict):
            raise AgentPipelineError("Explorer 的研究问题必须是对象")
        target_id = str(item.get("target_id", "")).strip()
        query = str(item.get("query", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if target_id not in allowed_targets:
            raise AgentPipelineError("Explorer 的研究问题指向未知节点")
        if not query or not reason:
            raise AgentPipelineError("Explorer 的研究问题缺少 query 或 reason")
        query_targets.add(target_id)
    required_targets = {
        node["temp_id"]
        for node in nodes
        if node["metadata"].get("research_needed") is True
    }
    missing_targets = required_targets - query_targets
    if missing_targets:
        raise AgentPipelineError("Explorer 的研究节点缺少配套研究问题")
    inconsistent_targets = {
        target_id
        for target_id in query_targets & set(nodes_by_id)
        if nodes_by_id[target_id]["metadata"].get("research_needed") is not True
    }
    if inconsistent_targets:
        raise AgentPipelineError("Explorer 的研究问题目标未标记 research_needed")
    return nodes, edges


def clean_research_queries(payload: dict, visible_node_ids: set[str]) -> list[dict]:
    queries = payload.get("research_queries", [])
    if not isinstance(queries, list):
        raise AgentPipelineError("Explorer 的 research_queries 必须是数组")
    cleaned = []
    for item in queries[:5]:
        if not isinstance(item, dict):
            continue
        target_id = str(item.get("target_id", "")).strip()
        query = str(item.get("query", "")).strip()[:240]
        if target_id not in visible_node_ids or not query:
            continue
        query = re.sub(r"[\w.+-]+@[\w.-]+", "[email]", query)
        query = re.sub(r"(?<!\d)\d{7,}(?!\d)", "[number]", query)
        query = re.sub(r"(?:[A-Za-z]:\\|/home/|/Users/)[^\s]+", "[local-path]", query)
        cleaned.append(
            {
                "target_id": target_id,
                "query": query,
                "reason": str(item.get("reason", "")).strip(),
            }
        )
    return cleaned
