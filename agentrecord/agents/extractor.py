"""Extractor Agent: raw journal records to source-grounded evidence."""

from .base import AgentPipelineError, AgentSpec
from .graph import validate_graph_payload


SPEC = AgentSpec(
    name="extractor",
    purpose="从记录中提取带来源的证据节点",
    can_read_raw=True,
    readable_node_types=frozenset(),
    writable_node_types=frozenset({"evidence"}),
    writable_relation_types=frozenset(),
    allowed_tools=frozenset(),
    instructions="""逐条识别事件、想法、判断、决定、问题和计划。不要概括成宏大主题，不要补充记录中没有的信息。
每个节点必须引用输入中存在的 source_id，并在 metadata.kind 中写 event、idea、claim、decision、question 或 plan；metadata.speaker 只能是 user、quoted_ai 或 referenced_report。
输出 JSON：{"nodes":[{"temp_id":"...","node_type":"evidence","title":"...","body":"...","confidence":0到1,"source_refs":["R-..."],"metadata":{"kind":"idea","speaker":"user"}}]}。""",
)


def _validate_metadata(node_type: str, metadata: dict, visible: set[str]) -> None:
    if metadata.get("kind") not in {
        "event",
        "idea",
        "claim",
        "decision",
        "question",
        "plan",
    }:
        raise AgentPipelineError("Extractor evidence 缺少有效 kind")
    if metadata.get("speaker") not in {"user", "quoted_ai", "referenced_report"}:
        raise AgentPipelineError("Extractor evidence 缺少有效 speaker")


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
