"""Reviewer Agent: candidate decisions and final report quality gate."""

from .base import AgentPipelineError, AgentSpec, confidence


SPEC = AgentSpec(
    name="reviewer",
    purpose="审查候选节点和最终报告",
    can_read_raw=False,
    readable_node_types=frozenset(
        {"evidence", "theme", "hypothesis", "research", "insight"}
    ),
    writable_node_types=frozenset(),
    writable_relation_types=frozenset(),
    allowed_tools=frozenset(),
    instructions="""严格检查来源可追溯性、证据强度、来源身份混淆、因果越界、弱关联和套话。宁可拒绝无价值候选，也不要为了丰富报告而放行。
候选节点使用 N001 形式的本次运行短别名。node_id 必须从中控给出的 valid_node_ids 原样复制，不得缩写、改写或使用其他 ID。
候选审查输出 JSON：{"decisions":[{"node_id":"N001","status":"accepted|rejected|candidate","reason":"...","confidence":0到1}],"revision_guidance":"..."}。
报告审查输出 JSON：{"pass":true或false,"unsupported_claims":["..."],"required_changes":["..."],"summary":"..."}。""",
)


def validate_candidate_review(payload: dict, candidate_ids: set[str]) -> list[dict]:
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        raise AgentPipelineError("Reviewer 缺少 decisions 数组")
    seen = set()
    normalized = []
    for decision in decisions:
        if not isinstance(decision, dict):
            raise AgentPipelineError("Reviewer 决定必须是对象")
        node_id = str(decision.get("node_id", "")).strip()
        status = decision.get("status")
        if node_id not in candidate_ids or node_id in seen:
            raise AgentPipelineError("Reviewer 引用了未知或重复节点")
        if status not in ("accepted", "rejected", "candidate"):
            raise AgentPipelineError("Reviewer 返回无效状态")
        reason = str(decision.get("reason", "")).strip()
        if not reason:
            raise AgentPipelineError("Reviewer 决定缺少原因")
        normalized.append(
            {
                "node_id": node_id,
                "status": status,
                "reason": reason,
                "confidence": confidence(
                    decision.get("confidence", 0.5), "confidence"
                ),
            }
        )
        seen.add(node_id)
    if seen != candidate_ids:
        raise AgentPipelineError("Reviewer 未审查全部候选节点")
    return normalized


def validate_report_review(payload: dict) -> tuple[bool, list[str]]:
    if not isinstance(payload.get("pass"), bool):
        raise AgentPipelineError("Reviewer 报告审查缺少布尔 pass")
    required_changes = payload.get("required_changes", [])
    unsupported = payload.get("unsupported_claims", [])
    if not isinstance(required_changes, list) or not isinstance(unsupported, list):
        raise AgentPipelineError("Reviewer 报告审查字段格式错误")
    feedback = [str(item) for item in required_changes + unsupported]
    return payload["pass"], feedback
