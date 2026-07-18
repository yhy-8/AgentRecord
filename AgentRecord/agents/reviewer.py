"""Independent quality gate for the two report sections."""

from .base import AgentPipelineError, AgentSpec


SPEC = AgentSpec(
    name="reviewer",
    purpose="分别审查整理回顾、人物画像和领域研究",
    can_read_raw=True,
    readable_node_types=frozenset(
        {"viewpoint", "principle", "ideal", "behavior_pattern", "interest"}
    ),
    writable_node_types=frozenset(),
    writable_relation_types=frozenset(),
    allowed_tools=frozenset(),
    instructions="""严格但只按实质问题检查事实、时期、身份、来源覆盖、因果越界、心理诊断、套话和行为教练倾向，不因措辞偏好或可选润色否决板块。
retrospective_review 模式必须把正文与 review_context 中的最小记录集合逐项对照，不能因为存在 [R-*] 格式就假定来源支持判断；还必须逐项决定 profile_entries 是否 accepted 或 rejected：只有记录直接支持、相对稳定、值得跨周期保留的观点、理念、理想、行为模式和关注领域才能接受。
research_review 模式检查外部来源是否真正支持正文，是否包含反例或边界，是否把探索性推断明确标为推断，以及是否避免替用户做最终判断。
pass 只表示板块正文是否可以按当前稿交付。画像候选被 rejected 本身不应令 pass=false；只有同一无依据判断也污染正文，或正文存在其他实质问题时才否决板块。pass=false 时 required_changes 或 unsupported_claims 必须给出能直接修改的具体意见；pass=true 时两者必须为空。
只返回 JSON：{"pass":true或false,"entry_decisions":[{"temp_id":"p1","status":"accepted|rejected","reason":"..."}],"unsupported_claims":["..."],"required_changes":["..."],"summary":"..."}。研究审查时 entry_decisions 为空数组。""",
)


def validate(
    payload: dict, *, expected_entry_ids: set[str] | None = None
) -> tuple[bool, dict[str, str], list[str]]:
    if not isinstance(payload.get("pass"), bool):
        raise AgentPipelineError("Reviewer 缺少布尔 pass")
    required = payload.get("required_changes", [])
    unsupported = payload.get("unsupported_claims", [])
    decisions = payload.get("entry_decisions", [])
    if not isinstance(required, list) or not isinstance(unsupported, list):
        raise AgentPipelineError("Reviewer 修改意见格式错误")
    if not isinstance(decisions, list):
        raise AgentPipelineError("Reviewer entry_decisions 必须是数组")
    expected = expected_entry_ids or set()
    normalized: dict[str, str] = {}
    for decision in decisions:
        if not isinstance(decision, dict):
            raise AgentPipelineError("Reviewer 画像决定必须是对象")
        temp_id = str(decision.get("temp_id", "")).strip()
        status = str(decision.get("status", "")).strip()
        reason = str(decision.get("reason", "")).strip()
        if temp_id not in expected or temp_id in normalized:
            raise AgentPipelineError("Reviewer 引用未知或重复画像条目")
        if status not in {"accepted", "rejected"} or not reason:
            raise AgentPipelineError("Reviewer 画像决定缺少有效状态或原因")
        normalized[temp_id] = status
    if set(normalized) != expected:
        raise AgentPipelineError("Reviewer 未审查全部人物画像条目")
    feedback = [str(item) for item in required + unsupported if str(item).strip()]
    if payload["pass"] and feedback:
        raise AgentPipelineError("Reviewer 通过时不应同时要求修改")
    if not payload["pass"] and not feedback:
        raise AgentPipelineError("Reviewer 否决时必须给出具体修改意见")
    return payload["pass"], normalized, feedback
