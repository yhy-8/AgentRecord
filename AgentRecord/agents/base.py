"""Shared Agent contract and model invocation without persistence access."""

import json
import re
import inspect
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class AgentSpec:
    name: str
    purpose: str
    can_read_raw: bool
    readable_node_types: frozenset[str]
    writable_node_types: frozenset[str]
    writable_relation_types: frozenset[str]
    allowed_tools: frozenset[str]
    instructions: str


class AgentPipelineError(RuntimeError):
    """A validated multi-agent analysis run could not be completed."""

    def __init__(
        self,
        message: str,
        *,
        response: str = "",
        telemetry: dict | None = None,
    ):
        super().__init__(message)
        self.response = response
        self.telemetry = telemetry or {}


class AgentOutputError(AgentPipelineError):
    """The model call succeeded, but its structured output could not be read."""


def _parse_json(text: str) -> dict:
    stripped = text.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*\n?(.*?)\n?```",
        stripped,
        re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as error:
        # Some OpenAI-compatible endpoints occasionally append a lone quote or
        # closing Markdown fence after an otherwise complete JSON object.  This
        # is unambiguous to recover, unlike extracting JSON from explanatory
        # prose or attempting to repair malformed content.
        if error.msg == "Extra data":
            try:
                value, end = json.JSONDecoder().raw_decode(stripped)
            except json.JSONDecodeError:
                value, end = None, 0
            trailing = stripped[end:].strip()
            if not (
                isinstance(value, dict)
                and re.fullmatch(r"(?:[`'\"}\]]|\s)*", trailing)
            ):
                raise AgentOutputError(
                    f"Agent JSON 无法解析: {error}", response=text
                ) from error
        else:
            raise AgentOutputError(
                f"Agent JSON 无法解析: {error}", response=text
            ) from error
    if not isinstance(value, dict):
        raise AgentOutputError("Agent JSON 顶层必须是对象", response=text)
    return value


def cited_source_ids(markdown: str) -> set[str]:
    """Return source IDs appearing inside Markdown citation brackets."""
    refs: set[str] = set()
    for citation in re.findall(r"\[([^\]\n]+)\]", markdown):
        refs.update(re.findall(r"R-\d{8}-\d{3}", citation))
        for match in re.finditer(
            r"R-(\d{8})-(\d{3})\s*(?:~|～|–|—|至)\s*"
            r"(?:(?:R-(\d{8})-)?(\d{3}))",
            citation,
        ):
            start_date, start_text, end_date, end_text = match.groups()
            if end_date and end_date != start_date:
                continue
            start_number = int(start_text)
            end_number = int(end_text)
            # A range is only shorthand within one diary.  Bound expansion so
            # malformed model output cannot create an enormous review context.
            if start_number <= end_number and end_number - start_number <= 200:
                refs.update(
                    f"R-{start_date}-{number:03d}"
                    for number in range(start_number, end_number + 1)
                )
    return refs


def _prompt(
    spec: AgentSpec,
    task: str,
    input_data: dict,
    revision_context: dict | None = None,
) -> str:
    permission_text = (
        f"可读原始记录：{'是' if spec.can_read_raw else '否'}；"
        f"可见节点：{', '.join(sorted(spec.readable_node_types)) or '无'}；"
        f"可创建节点：{', '.join(sorted(spec.writable_node_types)) or '无'}；"
        f"可创建关系：{', '.join(sorted(spec.writable_relation_types)) or '无'}；"
        f"可用工具：{', '.join(sorted(spec.allowed_tools)) or '无'}。"
    )
    prompt = f"""[程序 Agent 任务:{spec.name}]
你是 AgentRecord 的 {spec.name} Agent。{spec.purpose}。

【中控权限】
{permission_text}
未声明的读取、写入、关系和工具权限一律禁止。你只返回候选 JSON；中控负责数据库和文件写入。

【职责和输出契约】
{spec.instructions}

【本次任务】
{task}

【中控提供的输入 JSON】
{json.dumps(input_data, ensure_ascii=False)}"""
    if revision_context:
        prompt += f"""

【中控修订请求】
这是同一阶段的有限修订，不是新任务。保留原稿中正确且有依据的内容，只修正下列问题，然后重新输出完整结果；不要解释修改过程。
{json.dumps(revision_context, ensure_ascii=False)}"""
    return prompt + """

只输出一个符合契约的 JSON 对象，不要输出代码围栏、解释或完成提示。"""


def invoke_agent(
    spec: AgentSpec,
    task: str,
    input_data: dict,
    model_config: dict,
    call_model: Callable,
    *,
    revision_context: dict | None = None,
    allowed_search_queries: list[str] | None = None,
) -> dict:
    """Invoke one Agent with centrally supplied model access and tool permissions."""
    optional_kwargs = {
        "allowed_search_queries": allowed_search_queries,
        "structured_output": True,
    }
    try:
        parameters = inspect.signature(call_model).parameters.values()
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if not accepts_kwargs:
            accepted_names = {parameter.name for parameter in parameters}
            optional_kwargs = {
                key: value
                for key, value in optional_kwargs.items()
                if key in accepted_names
            }
    except (TypeError, ValueError):
        pass
    response = call_model(
        _prompt(spec, task, input_data, revision_context),
        model_config,
        allowed_tools=spec.allowed_tools,
        **optional_kwargs,
    )
    text, success, web_count, tool_counts, result_count = response
    from ..ai_client import response_telemetry

    telemetry = {
        "web_citations": web_count,
        "tool_calls": tool_counts,
        "search_results": result_count,
        **response_telemetry(response),
    }
    if not success:
        raise AgentPipelineError(
            f"{spec.name} 调用失败: {text}", response=text, telemetry=telemetry
        )
    try:
        payload = _parse_json(text)
    except AgentPipelineError as error:
        error.telemetry = telemetry
        raise
    payload["_telemetry"] = telemetry
    return payload


def confidence(value: object, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise AgentPipelineError(f"{field} 必须是 0 到 1 的数字") from error
    if not 0 <= number <= 1:
        raise AgentPipelineError(f"{field} 超出 0 到 1")
    return number
