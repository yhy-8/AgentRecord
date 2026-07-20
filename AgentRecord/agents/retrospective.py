"""Agent and validation contract for the report's retrospective section."""

import re

from .base import AgentPipelineError, AgentSpec, cited_source_ids, confidence


PROFILE_CATEGORIES = {
    "viewpoint",
    "principle",
    "ideal",
    "behavior_pattern",
    "interest",
}


SPEC = AgentSpec(
    name="retrospective",
    purpose="整理周期事实，并更新可追溯的人物观点与行为画像",
    can_read_raw=True,
    readable_node_types=frozenset(PROFILE_CATEGORIES),
    writable_node_types=frozenset(PROFILE_CATEGORIES),
    writable_relation_types=frozenset(),
    allowed_tools=frozenset(),
    instructions="""生成报告第一板块“整理与回顾”的正文，并提出少量值得长期保存的人物画像更新。
正文必须忠实回顾本周期做过什么、关注点如何分配，以及观点、理念、理想或行为模式出现了怎样的变化。行为分析属于事实整理的一部分，但不得把时间先后写成因果，不得心理诊断，不得给出行为教练式命令。每个事实或判断所在段落都必须就近引用 [R-YYYYMMDD-NNN]。
历史画像只用于比较此前状态；不得使用晚于报告周期结束的内容。新的画像条目只保存相对稳定或反复出现的内容，不保存一次性事件、任务、外部事实或 AI 自己的建议。supersedes_id 只能复制输入中的 P 三位短别名；没有明确变化时为 null。
只返回 JSON：{"markdown":"不含一、二级标题的第一板块正文","profile_entries":[{"temp_id":"p1","category":"viewpoint|principle|ideal|behavior_pattern|interest","title":"...","statement":"...","confidence":0到1,"source_refs":["R-..."],"supersedes_id":null}]}。""",
)


def section_errors(markdown: str, allowed_source_ids: set[str]) -> list[str]:
    errors = []
    if not markdown.strip():
        errors.append("整理与回顾正文为空")
        return errors
    if re.search(r"^#{1,2}\s", markdown, re.MULTILINE):
        errors.append("整理与回顾包含一、二级标题")
    if "```" in markdown:
        errors.append("整理与回顾包含代码围栏")
    cited = cited_source_ids(markdown)
    unknown = cited - allowed_source_ids
    if unknown:
        errors.append("整理与回顾引用未知来源: " + ", ".join(sorted(unknown)))
    for paragraph in re.split(r"\n\s*\n", markdown.strip()):
        content = paragraph.strip()
        if not content or content.startswith("### "):
            continue
        if not cited_source_ids(content):
            preview = re.sub(r"\s+", " ", content)[:160]
            errors.append(f"整理与回顾存在没有来源引用的段落：{preview}")
            break
    return errors


def validate(
    payload: dict,
    *,
    allowed_source_ids: set[str],
    current_source_ids: set[str],
    visible_profile_ids: set[str],
    visible_profiles: dict[str, dict] | None = None,
) -> tuple[str, list[dict]]:
    markdown = payload.get("markdown", "")
    if not isinstance(markdown, str):
        raise AgentPipelineError("Retrospective markdown 必须是字符串")
    errors = section_errors(markdown, allowed_source_ids)
    if errors:
        raise AgentPipelineError("；".join(errors))
    raw_entries = payload.get("profile_entries", [])
    if not isinstance(raw_entries, list) or len(raw_entries) > 12:
        raise AgentPipelineError("Retrospective profile_entries 必须是不超过 12 项的数组")
    entries = []
    seen = set()
    superseded = set()
    seen_signatures = set()
    existing_signatures = {
        (
            str(profile.get("category", "")).strip(),
            re.sub(r"\s+", "", str(profile.get("title", ""))).casefold(),
            re.sub(r"\s+", "", str(profile.get("statement", ""))).casefold(),
        ): profile_id
        for profile_id, profile in (visible_profiles or {}).items()
    }
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise AgentPipelineError("人物画像条目必须是对象")
        temp_id = str(raw.get("temp_id", "")).strip()
        category = str(raw.get("category", "")).strip()
        title = str(raw.get("title", "")).strip()
        statement = str(raw.get("statement", "")).strip()
        refs = raw.get("source_refs", [])
        supersedes_id = raw.get("supersedes_id") or None
        if not temp_id or temp_id in seen:
            raise AgentPipelineError("人物画像 temp_id 为空或重复")
        if category not in PROFILE_CATEGORIES:
            raise AgentPipelineError("人物画像 category 无效")
        if not title or not statement:
            raise AgentPipelineError("人物画像缺少标题或陈述")
        if not isinstance(refs, list) or any(
            not isinstance(ref, str) or ref not in allowed_source_ids for ref in refs
        ):
            raise AgentPipelineError("人物画像包含未知来源")
        if not set(refs) & current_source_ids:
            raise AgentPipelineError("人物画像更新必须有本周期来源")
        if category == "behavior_pattern" and len(set(refs)) < 2:
            raise AgentPipelineError("行为模式必须由至少两条不同记录共同支持")
        if supersedes_id and supersedes_id not in visible_profile_ids:
            raise AgentPipelineError("人物画像尝试替代不可见条目")
        if supersedes_id and supersedes_id in superseded:
            raise AgentPipelineError("一次报告不能用多个候选替代同一人物画像")
        signature = (
            category,
            re.sub(r"\s+", "", title).casefold(),
            re.sub(r"\s+", "", statement).casefold(),
        )
        if signature in seen_signatures:
            raise AgentPipelineError("一次报告不能创建重复的人物画像候选")
        existing_id = existing_signatures.get(signature)
        if existing_id and supersedes_id != existing_id:
            raise AgentPipelineError("人物画像与现有条目重复，必须明确替代原条目")
        entries.append(
            {
                "temp_id": temp_id,
                "category": category,
                "title": title,
                "statement": statement,
                "confidence": confidence(raw.get("confidence", 0.5), "confidence"),
                "source_refs": list(dict.fromkeys(refs)),
                "supersedes_id": supersedes_id,
            }
        )
        seen.add(temp_id)
        seen_signatures.add(signature)
        if supersedes_id:
            superseded.add(supersedes_id)
    return markdown.strip(), entries
