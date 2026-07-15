"""Explorer Agent: themes and evidence to hypotheses and insights."""

import re

from .base import AgentPipelineError, AgentSpec
from .graph import validate_graph_payload


SPEC = AgentSpec(
    name="explorer",
    purpose="提出强关联、演化、矛盾、盲点和候选洞见",
    can_read_raw=False,
    readable_node_types=frozenset(
        {"evidence", "theme", "hypothesis", "research", "insight"}
    ),
    writable_node_types=frozenset({"hypothesis", "insight"}),
    writable_relation_types=frozenset(
        {"supports", "challenges", "evolves_from", "contradicts"}
    ),
    allowed_tools=frozenset(),
    instructions="""优先少量、有依据且能提高下一轮思考质量的发现。明确区分记录事实和推断，为每个候选列出支持来源、反证或替代解释。需要外部核查时给出不含私人细节的 research_queries；不要自己声称已经查证。
source_refs 可以填写输入中的知识节点 ID，中控会展开其原始来源；不要编造来源 ID。
输出 JSON：{"nodes":[{"temp_id":"...","node_type":"hypothesis|insight","title":"...","body":"...","confidence":0到1,"source_refs":["输入节点ID"],"supersedes_id":null,"metadata":{"insight_type":"connection|evolution|contradiction|blind_spot","evidence_for":["节点ID"],"evidence_against":["节点ID"],"inference_level":"low|medium|high","why_it_matters":"...","research_needed":true}}],"edges":[{"source_id":"节点ID","target_id":"节点ID","relation_type":"supports|challenges|evolves_from|contradicts","weight":0到1,"confidence":0到1,"rationale":"..."}],"research_queries":[{"target_id":"候选节点ID","query":"去除私人细节后的查询","reason":"..."}]}。""",
)


def _validate_metadata(node_type: str, metadata: dict, visible: set[str]) -> None:
    if metadata.get("insight_type") not in {
        "connection",
        "evolution",
        "contradiction",
        "blind_spot",
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
    return validate_graph_payload(
        SPEC,
        payload,
        allowed_source_ids=allowed_source_ids,
        visible_node_ids=visible_node_ids,
        validate_metadata=_validate_metadata,
    )


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
