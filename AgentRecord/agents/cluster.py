"""Cluster Agent: evidence to themes and temporal trajectories."""

from .base import AgentPipelineError, AgentSpec
from .graph import validate_graph_payload


SPEC = AgentSpec(
    name="cluster",
    purpose="把证据组织成主题和时间轨迹",
    can_read_raw=False,
    readable_node_types=frozenset({"evidence", "theme"}),
    writable_node_types=frozenset({"theme"}),
    writable_relation_types=frozenset(
        {"member_of", "evolves_from", "splits_from", "merges_into"}
    ),
    allowed_tools=frozenset(),
    instructions="""只建立材料支持的主题，避免把单条偶然记录包装成长期趋势。可以结合已接受历史主题判断新出现、持续、增强、衰退、分叉或合并。
source_refs 可以填写输入中的证据/主题节点 ID，中控会展开其原始来源；不要编造来源 ID。
输出 JSON：{"nodes":[{"temp_id":"...","node_type":"theme","title":"...","body":"...","confidence":0到1,"source_refs":["输入节点ID"],"supersedes_id":null,"metadata":{"trajectory":"new|continuing|growing|weakening|split|merged"}}],"edges":[{"source_id":"节点ID","target_id":"节点ID","relation_type":"member_of|evolves_from|splits_from|merges_into","weight":0到1,"confidence":0到1,"rationale":"..."}]}。""",
)


def _validate_metadata(node_type: str, metadata: dict, visible: set[str]) -> None:
    if metadata.get("trajectory") not in {
        "new",
        "continuing",
        "growing",
        "weakening",
        "split",
        "merged",
    }:
        raise AgentPipelineError("Cluster theme 缺少有效 trajectory")


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
