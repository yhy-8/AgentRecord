"""Central validation of graph-shaped Agent output."""

import copy
from collections.abc import Callable

from .base import AgentPipelineError, AgentSpec, confidence


MetadataValidator = Callable[[str, dict, set[str]], None]


def replace_node_ids(value: object, replacements: dict[str, str]) -> object:
    """只替换结构化字段中完整匹配的节点 ID，不改写模型新建的 temp_id。"""
    if isinstance(value, str):
        return replacements.get(value, value)
    if isinstance(value, list):
        return [replace_node_ids(item, replacements) for item in value]
    if isinstance(value, tuple):
        return tuple(replace_node_ids(item, replacements) for item in value)
    if isinstance(value, dict):
        return {
            key: (
                item
                if key == "temp_id"
                else replace_node_ids(item, replacements)
            )
            for key, item in value.items()
        }
    return value


def inherit_source_refs(
    payload: dict,
    *,
    allowed_source_ids: set[str],
    visible_nodes: dict[str, dict],
) -> dict:
    """Expand visible knowledge-node references into original source IDs.

    Downstream Agents reason over knowledge node IDs.  The orchestrator, rather
    than the model, owns the deterministic provenance expansion back to R-* IDs.
    """
    normalized = copy.deepcopy(payload)
    raw_nodes = normalized.get("nodes", [])
    raw_edges = normalized.get("edges", [])
    if not isinstance(raw_nodes, list):
        return normalized

    linked_visible: dict[str, list[str]] = {}
    if isinstance(raw_edges, list):
        for edge in raw_edges:
            if not isinstance(edge, dict):
                continue
            source_id = str(edge.get("source_id", "")).strip()
            target_id = str(edge.get("target_id", "")).strip()
            if source_id in visible_nodes:
                linked_visible.setdefault(target_id, []).append(source_id)
            if target_id in visible_nodes:
                linked_visible.setdefault(source_id, []).append(target_id)

    def expand(reference: object) -> list[object]:
        if not isinstance(reference, str):
            return [reference]
        if reference in allowed_source_ids:
            return [reference]
        if reference in visible_nodes:
            return [
                source_id
                for source_id in visible_nodes[reference].get("source_refs", [])
                if source_id in allowed_source_ids
            ]
        return [reference]

    for node in raw_nodes:
        if not isinstance(node, dict):
            continue
        references = node.get("source_refs", [])
        if not isinstance(references, list):
            continue
        inherited = list(references)
        temporary_id = str(node.get("temp_id", "")).strip()
        inherited.extend(linked_visible.get(temporary_id, []))
        metadata = node.get("metadata", {})
        if isinstance(metadata, dict):
            for field in ("evidence_for", "evidence_against"):
                value = metadata.get(field, [])
                if isinstance(value, list):
                    inherited.extend(value)
            target_id = metadata.get("target_id")
            if isinstance(target_id, str):
                inherited.append(target_id)

        expanded: list[object] = []
        for reference in inherited:
            for source_id in expand(reference):
                if source_id not in expanded:
                    expanded.append(source_id)
        node["source_refs"] = expanded
    return normalized


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
        if not isinstance(source_refs, list):
            raise AgentPipelineError(f"{spec.name} 节点的 source_refs 必须是数组")
        unknown_source_count = sum(
            not isinstance(item, str) or item not in allowed_source_ids
            for item in source_refs
        )
        if unknown_source_count:
            raise AgentPipelineError(
                f"{spec.name} 节点包含 {unknown_source_count} 个未知来源"
            )
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
