"""Shared Agent contract and model invocation without persistence access."""

import json
import re
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


def _parse_json(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, count=1)
        stripped = re.sub(r"\s*```$", "", stripped, count=1)
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise AgentPipelineError("Agent 没有返回 JSON 对象", response=text)
        try:
            value = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as error:
            raise AgentPipelineError(
                f"Agent JSON 无法解析: {error}", response=text
            ) from error
    if not isinstance(value, dict):
        raise AgentPipelineError("Agent JSON 顶层必须是对象", response=text)
    return value


def _prompt(spec: AgentSpec, task: str, input_data: dict) -> str:
    permission_text = (
        f"可读原始记录：{'是' if spec.can_read_raw else '否'}；"
        f"可见节点：{', '.join(sorted(spec.readable_node_types)) or '无'}；"
        f"可创建节点：{', '.join(sorted(spec.writable_node_types)) or '无'}；"
        f"可创建关系：{', '.join(sorted(spec.writable_relation_types)) or '无'}；"
        f"可用工具：{', '.join(sorted(spec.allowed_tools)) or '无'}。"
    )
    return f"""[程序 Agent 任务:{spec.name}]
你是 AgentRecord 的 {spec.name} Agent。{spec.purpose}。

【中控权限】
{permission_text}
未声明的读取、写入、关系和工具权限一律禁止。你只返回候选 JSON；中控负责数据库和文件写入。

【职责和输出契约】
{spec.instructions}

【本次任务】
{task}

【中控提供的输入 JSON】
{json.dumps(input_data, ensure_ascii=False)}

只输出一个符合契约的 JSON 对象，不要输出代码围栏、解释或完成提示。"""


def invoke_agent(
    spec: AgentSpec,
    task: str,
    input_data: dict,
    model_config: dict,
    call_model: Callable,
) -> dict:
    """Invoke one Agent with centrally supplied model access and tool permissions."""
    text, success, web_count, tool_counts, result_count = call_model(
        _prompt(spec, task, input_data),
        model_config,
        allowed_tools=spec.allowed_tools,
    )
    telemetry = {
        "web_citations": web_count,
        "tool_calls": tool_counts,
        "search_results": result_count,
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
