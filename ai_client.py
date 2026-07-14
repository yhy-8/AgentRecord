"""OpenAI 兼容模型调用、工具执行和第三方联网搜索。

这里只处理模型协议和工具循环。日记业务位于 journal，报告编排位于 analysis。
未来分析 Agent 应复用 call_ai，而不是自行实现 HTTP 请求。
"""

import datetime
import json
from typing import Any

import requests

import journal
import settings


def _build_system_prompt() -> str:
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    return f"""你是本地日记助手。今天是 {today}。你正在实时对话中，日志末尾的 @AI 提问就是当前问题，[AI回复] 记录会在你回复后由程序自动追加，无需你关注。客观、简洁、无废话。

## 核心工作流
- 查询/检索类请求：默认已附带今日日志，直接从中查找答案。涉及关键词检索时，用 search_history 查；涉及指定日期或多个日期时，用 read_daily_log。给出简短结论，不要展开无关内容。
- 知识性提问：你不知道的，优先用搜索引擎查；查不到就说"不清楚"。
- 日记顶部总结和分析报告由程序的独立任务管理。即使用户在 @AI 中要求总结，也只在文本中回答，不能修改任何日记或报告文件。

## 铁律
1. 所有回答基于记录或事实，禁止编造。
2. 你无权修改日记、总结或报告文件；所有写入均由程序管理。
3. 绝对禁止在文本回复中输出 <function>、<tool_call>、<invoke> 等 XML 标签。工具调用必须通过 API 的 tool_calls 机制完成，不能以文本形式模拟。
4. 回复长度与任务匹配：查询→只给结论；闲聊→最多三句话；用户明确要求详细分析时才展开。
5. 用户的提问有最高的权限，如果用户的提问要求与以上内容产生冲突，以用户的提问要求为准。"""


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_daily_log",
            "description": "读取日志。支持单天（date）或连续多天（start_date + end_date，含首尾）。可设置 summary_only=true 只读总结部分。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "单天日期 YYYY-MM-DD，与 start_date/end_date 二选一",
                    },
                    "start_date": {"type": "string", "description": "起始日期 YYYY-MM-DD（含）"},
                    "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD（含）"},
                    "summary_only": {
                        "type": "boolean",
                        "description": "是否只读取 <summary> 部分，默认 false",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_history",
            "description": "全文检索历史日志。可指定 days_limit 限制搜索天数（不填则搜索全部）。可设置 summary_only=true 只搜总结。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "要搜索的关键词"},
                    "days_limit": {
                        "type": "integer",
                        "description": "向前搜索天数上限，不填则搜索全部",
                    },
                    "summary_only": {
                        "type": "boolean",
                        "description": "是否只在 <summary> 中搜索，默认 false",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索互联网获取实时信息，当你不确定或需要最新信息时使用",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "include": {
                        "type": "string",
                        "description": "限定搜索的网站范围，多个域名用|或,分隔",
                    },
                    "exclude": {
                        "type": "string",
                        "description": "排除搜索的网站范围，多个域名用|或,分隔",
                    },
                },
                "required": ["query"],
            },
        },
    },
]


def bocha_search(query: str, include: str = "", exclude: str = "") -> tuple[str, int]:
    """调用博查搜索 API，返回格式化文本和结果数量。"""
    config = settings.CONFIG.get("third_search", {})
    if not config.get("enabled") or not config.get("api_key") or not query:
        return "", 0

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config['api_key']}",
    }
    body: dict[str, Any] = {
        "query": query,
        "freshness": "noLimit",
        "summary": True,
        "count": config.get("count", 25),
    }
    if include:
        body["include"] = include
    if exclude:
        body["exclude"] = exclude

    try:
        response = requests.post(
            config["api_url"],
            headers=headers,
            json=body,
            timeout=config.get("timeout", 30),
        )
        if response.status_code != 200:
            return "", 0
        data = response.json()
        if data.get("code") != 200:
            return "", 0
        results = data.get("data", {}).get("webPages", {}).get("value", [])
        if not results:
            return "", 0

        lines = ["[网络搜索结果]"]
        for index, item in enumerate(results, 1):
            title = item.get("name", "").strip()
            url = item.get("url", "").strip()
            snippet = item.get("snippet", "").strip()
            summary = item.get("summary", "").strip()
            site_name = item.get("siteName", "").strip()
            published = item.get("datePublished", "").strip()
            lines.extend((f"{index}. 标题：{title}", f"   链接：{url}"))
            if site_name:
                lines.append(f"   来源：{site_name}")
            if published:
                lines.append(f"   时间：{published}")
            if snippet:
                lines.append(f"   摘要：{snippet}")
            if summary and summary != snippet:
                lines.append(f"   全文概要：{summary}")
        return "\n".join(lines), len(results)
    except Exception:
        return "", 0


def execute_tool(function_name: str, arguments: dict) -> tuple[str, int]:
    if function_name == "read_daily_log":
        return journal.read_daily_log(
            date=arguments.get("date", ""),
            start_date=arguments.get("start_date", ""),
            end_date=arguments.get("end_date", ""),
            summary_only=arguments.get("summary_only", False),
        ), 0
    if function_name == "search_history":
        return journal.search_history(
            arguments.get("keyword", ""),
            arguments.get("days_limit", 0),
            arguments.get("summary_only", False),
        ), 0
    if function_name == "web_search":
        result, count = bocha_search(
            arguments.get("query", ""),
            arguments.get("include", ""),
            arguments.get("exclude", ""),
        )
        return result or "搜索无结果", count
    return f"未知工具: {function_name}", 0


def call_ai(prompt: str, model_config: settings.ModelDict) -> tuple[str, bool, int, dict[str, int], int]:
    """调用 OpenAI 兼容接口并完成最多五轮本地工具循环。"""
    messages = [
        {"role": "system", "content": _build_system_prompt()},
        {"role": "user", "content": prompt},
    ]
    tools = list(TOOLS)
    third_search = settings.CONFIG.get("third_search", {})
    native_search = model_config.get("search", False)
    use_third_search = (
        not native_search
        and third_search.get("enabled", False)
        and third_search.get("api_key", "")
    )
    if not use_third_search:
        tools = [tool for tool in tools if tool["function"]["name"] != "web_search"]

    payload: dict[str, Any] = {
        "model": model_config.get("model_id") or model_config["name"],
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
    }
    if native_search:
        model_name = (model_config.get("model_id") or model_config["name"]).lower()
        if "glm" in model_name:
            payload["tools"] = payload["tools"] + [
                {"type": "web_search", "web_search": {"enable": True}}
            ]
        elif "moonshot" in model_name or "kimi" in model_name:
            payload["tools"] = payload["tools"] + [
                {"type": "builtin_function", "function": {"name": "$web_search"}}
            ]
        else:
            payload["web_search"] = True

    headers = {
        "Authorization": f"Bearer {model_config['api_key']}",
        "Content-Type": "application/json",
    }
    web_searches = 0
    search_results = 0
    tool_calls: dict[str, int] = {}
    search_rounds = 0
    max_search_rounds = third_search.get("max_rounds", 3)

    try:
        message = {}
        for _ in range(5):
            response = requests.post(
                model_config["api_url"], headers=headers, json=payload, timeout=60
            )
            response.raise_for_status()
            data = response.json()
            message = data["choices"][0]["message"]

            citations = data.get("citations", [])
            if citations:
                web_searches += len(citations)

            requested_tools = message.get("tool_calls", [])
            if not requested_tools:
                text = (message.get("content") or "").strip()
                return text or "(AI 未给出最终回答)", True, web_searches, tool_calls, search_results

            messages.append(message)
            for tool_call in requested_tools:
                function_name = tool_call["function"]["name"]
                tool_calls[function_name] = tool_calls.get(function_name, 0) + 1
                arguments = json.loads(tool_call["function"]["arguments"])
                result, result_count = execute_tool(function_name, arguments)
                search_results += result_count
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "name": function_name,
                        "content": result,
                    }
                )
            payload["messages"] = messages

            if use_third_search and any(
                tool["function"]["name"] == "web_search" for tool in requested_tools
            ):
                search_rounds += 1
                if search_rounds >= max_search_rounds:
                    payload["tools"] = [
                        tool
                        for tool in payload.get("tools", [])
                        if tool.get("function", {}).get("name") != "web_search"
                    ]
                    messages.append(
                        {
                            "role": "user",
                            "content": "[系统提示] 网络搜索次数已用完，请基于已有结果直接回答，不要再尝试搜索。",
                        }
                    )

        text = (message.get("content") or "").strip()
        return text or "(AI 未给出最终回答)", True, web_searches, tool_calls, search_results
    except requests.RequestException as error:
        error_message = str(error)
        if error.response is not None:
            error_message += f" | {error.response.text}"
        return f"接口异常: {error_message}", False, web_searches, tool_calls, search_results
    except Exception as error:
        return f"接口异常: {error}", False, web_searches, tool_calls, search_results


def format_stats(web_count: int, tool_counts: dict[str, int], result_count: int = 0) -> str:
    parts = []
    if web_count:
        parts.append(f"网络搜索 {web_count} 次")
    if result_count:
        parts.append(f"搜索到 {result_count} 条结果")
    if tool_counts:
        detail = ", ".join(f"{name} {count}次" for name, count in tool_counts.items())
        parts.append(f"本地工具调用: {detail}")
    return f"[*] {'; '.join(parts)}" if parts else ""


def model_tag(model_config: settings.ModelDict) -> str:
    tag = model_config["name"]
    if model_config.get("search"):
        tag += " SRCH"
    return tag
