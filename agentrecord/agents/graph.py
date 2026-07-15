"""Central validation of graph-shaped Agent output."""

from collections.abc import Callable

from .base import AgentPipelineError, AgentSpec, confidence


MetadataValidator = Callable[[str, dict, set[str]], None]


def validate_graph_payload(
    spec: AgentSpec,
    payload: dict,
    *,
    allowed_source_ids: set[str],
    visible_node_ids: set[str],
    validate_metadata: MetadataValidator,
) -> tuple[list[dict], list[dict]]:
    raw_nodes = payload.get("nodes", [])
    raw_edges = payload.get("edges", [])
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise AgentPipelineError(f"{spec.name} 的 nodes 和 edges 必须是数组")

    nodes = []
    temporary_ids: set[str] = set()
    for raw_node in raw_nodes:
        if not isinstance(raw_node, dict):
            raise AgentPipelineError(f"{spec.name} 返回了非对象节点")
        temporary_id = str(raw_node.get("temp_id", "")).strip()
        node_type = str(raw_node.get("node_type", "")).strip()
        if not temporary_id or temporary_id in temporary_ids:
            raise AgentPipelineError(f"{spec.name} 返回空或重复的 temp_id")
        if node_type not in spec.writable_node_types:
            raise AgentPipelineError(f"{spec.name} 无权创建 {node_type} 节点")
        title = str(raw_node.get("title", "")).strip()
        body = str(raw_node.get("body", "")).strip()
        if not title or not body:
            raise AgentPipelineError(f"{spec.name} 节点缺少标题或正文")
        source_refs = raw_node.get("source_refs", [])
        if not isinstance(source_refs, list) or any(
            not isinstance(item, str) or item not in allowed_source_ids
            for item in source_refs
        ):
            raise AgentPipelineError(f"{spec.name} 节点包含未知来源")
        if node_type != "research" and not source_refs:
            raise AgentPipelineError(f"{spec.name} 的 {node_type} 节点缺少来源")
        supersedes_id = raw_node.get("supersedes_id") or None
        if supersedes_id and supersedes_id not in visible_node_ids:
            raise AgentPipelineError(f"{spec.name} 尝试替代不可见节点")
        metadata = raw_node.get("metadata", {})
        if not isinstance(metadata, dict):
            raise AgentPipelineError(f"{spec.name} 节点 metadata 必须是对象")
        validate_metadata(node_type, metadata, visible_node_ids)
        nodes.append(
            {
                "temp_id": temporary_id,
                "node_type": node_type,
                "title": title,
                "body": body,
                "confidence": confidence(raw_node.get("confidence", 0.5), "confidence"),
                "source_refs": source_refs,
                "supersedes_id": supersedes_id,
                "metadata": metadata,
            }
        )
        temporary_ids.add(temporary_id)

    allowed_edge_nodes = visible_node_ids | temporary_ids
    edges = []
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, dict):
            raise AgentPipelineError(f"{spec.name} 返回了非对象关系")
        source_id = str(raw_edge.get("source_id", "")).strip()
        target_id = str(raw_edge.get("target_id", "")).strip()
        relation_type = str(raw_edge.get("relation_type", "")).strip()
        if source_id not in allowed_edge_nodes or target_id not in allowed_edge_nodes:
            raise AgentPipelineError(f"{spec.name} 关系引用不可见节点")
        if relation_type not in spec.writable_relation_types:
            raise AgentPipelineError(f"{spec.name} 无权创建 {relation_type} 关系")
        edges.append(
            {
                "source_id": source_id,
                "target_id": target_id,
                "relation_type": relation_type,
                "weight": confidence(raw_edge.get("weight", 0.5), "weight"),
                "confidence": confidence(raw_edge.get("confidence", 0.5), "confidence"),
                "rationale": str(raw_edge.get("rationale", "")).strip(),
            }
        )
    return nodes, edges
