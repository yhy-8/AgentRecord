"""Report Agent: final Markdown synthesis and deterministic validation."""

import re

from .base import AgentPipelineError, AgentSpec


SPEC = AgentSpec(
    name="report",
    purpose="把审查通过的材料组织为可独立阅读的 Markdown 报告",
    can_read_raw=False,
    readable_node_types=frozenset(
        {"evidence", "theme", "hypothesis", "research", "insight"}
    ),
    writable_node_types=frozenset(),
    writable_relation_types=frozenset(),
    allowed_tools=frozenset(),
    instructions="""只使用中控给出的 accepted 节点，不新增未经审查的核心结论。报告的价值不只是总结发生了什么：优先提炼记录中有依据的观点、思维模型、判断方式、方法论和点子，说明它们如何形成、变化、彼此关联以及可能的边界。
对于 accepted research，必须把有价值的外部核查、反例、相邻概念和探索方向整合进对应主题，明确区分“用户记录”“AI 推断”“外部资料”，并用来源标题和 URL 标识外部材料。延伸应帮助用户看到原记录之外的可能方向，但不能把外部理论写成用户立场，也不能为了显得深刻制造弱关联。
选择少量强发现，说明依据、解释、反证或其他解释、为何值得关注、可以向哪里继续探索以及留给用户判断的问题。用 [R-日期-序号] 标注日记来源；避免逐条复述和任务清单，没有材料的章节不要生成。
输出 JSON：{"markdown":"不含一级标题、代码围栏和生成提示的报告正文"}。""",
)


def markdown_from_payload(payload: dict) -> str:
    markdown = payload.get("markdown", "")
    if not isinstance(markdown, str):
        raise AgentPipelineError("Report 的 markdown 必须是字符串")
    return markdown


def validation_errors(markdown: str, source_ids: set[str]) -> list[str]:
    errors = []
    if not markdown.strip():
        errors.append("报告正文为空")
    if re.search(r"^#\s", markdown, re.MULTILINE):
        errors.append("报告正文包含一级标题")
    if "```" in markdown:
        errors.append("报告正文包含代码围栏")
    cited = set(re.findall(r"\[(R-\d{8}-\d{3})\]", markdown))
    unknown = cited - source_ids
    if unknown:
        errors.append("报告引用未知来源: " + ", ".join(sorted(unknown)))
    if not cited:
        errors.append("报告没有引用任何原始记录")
    return errors


def source_appendix(markdown: str, records: list[dict]) -> str:
    cited = set(re.findall(r"\[(R-\d{8}-\d{3})\]", markdown))
    lines = ["## 来源索引"]
    for record in records:
        if record["source_id"] in cited:
            lines.append(
                f"- [{record['source_id']}] {record['date']} {record['time']} "
                f"— `{record['path']}` 第 {record['record_index']} 条记录"
            )
    return "\n".join(lines)
