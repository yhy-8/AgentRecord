import json
import re
import sys
import datetime
from typing import Any
import requests
import yaml
from pathlib import Path
import select
import os
import time

try:
    import msvcrt
    IS_WINDOWS = True
    # Enable ANSI escape sequence processing on Windows 10+ for cursor control
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    mode = ctypes.c_ulong()
    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
except ImportError:
    import termios
    import tty
    IS_WINDOWS = False

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

console = Console()

# ================= 加载配置文件 =================
ModelDict = dict[str, Any]


def _get_config_path() -> Path:
    """获取 config.yaml 路径，兼容 PyInstaller 打包后的路径。"""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).parent
    return base / "config.yaml"


def _load_config() -> dict:
    cp = _get_config_path()
    if cp.exists():
        with open(cp, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


_config = _load_config()


def _save_config():
    cp = _get_config_path()
    with open(cp, "w", encoding="utf-8") as f:
        yaml.safe_dump(_config, f, allow_unicode=True, default_flow_style=False)


# ================= 统一模型配置 =================
class ModelConfig:
    """统一管理所有可用模型及其 API 配置"""

    @classmethod
    def models(cls) -> list[ModelDict]:
        return _config.get("models", [])

    @classmethod
    def get_model(cls, name_or_index: str | int | None = None) -> ModelDict:
        models = cls.models()
        if not models:
            raise RuntimeError("config.yaml 中未配置任何模型")
        if name_or_index is None:
            return models[0]
        if isinstance(name_or_index, int):
            return models[name_or_index % len(models)]
        name_lower = name_or_index.lower()
        for m in models:
            if m["name"].lower() == name_lower:
                return m
        for m in models:
            if name_lower in m["name"].lower():
                return m
        raise KeyError(f"未找到匹配模型 '{name_or_index}'")

    @classmethod
    def index_of(cls, name: str) -> int:
        for i, m in enumerate(cls.models()):
            if m["name"] == name:
                return i
        return 0

    @classmethod
    def next_after(cls, name: str) -> ModelDict:
        models = cls.models()
        idx = cls.index_of(name)
        return models[(idx + 1) % len(models)]


# ================= 基础配置 =================
DIARY_DIR = Path(_config.get("diary_dir", "./AgentRecords"))
DIARY_DIR.mkdir(parents=True, exist_ok=True)


def resolve_date(arg: str) -> str:
    """解析日期参数，支持：
    - 空 → 今天
    - -N → N天前（-1 = 昨天）
    - today/yesterday/今天/昨天
    - last/prev/上一个 → 最近一个存在的记录
    - YYYY-MM-DD / YYYYMMDD → 完整日期
    - MM-DD / MMDD → 缩写（假定今年）
    """
    today = datetime.date.today()
    arg = arg.strip()

    if not arg:
        return today.strftime("%Y-%m-%d")

    if re.match(r'^-\d+$', arg):
        days = int(arg[1:])
        d = today - datetime.timedelta(days=days)
        return d.strftime("%Y-%m-%d")

    aliases = {'today': 0, '今天': 0, 'yesterday': 1, '昨天': 1}
    if arg.lower() in aliases:
        d = today - datetime.timedelta(days=aliases[arg.lower()])
        return d.strftime("%Y-%m-%d")

    if arg.lower() in ('last', 'prev', '上一个', '最近'):
        files = sorted(DIARY_DIR.glob("*.md"), reverse=True)
        today_str = today.strftime("%Y-%m-%d")
        for f in files:
            if f.stem < today_str:
                return f.stem
        return ""

    for fmt in ['%Y-%m-%d', '%Y%m%d']:
        try:
            return datetime.datetime.strptime(arg, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue

    for fmt in ['%m-%d', '%m%d']:
        try:
            d = datetime.datetime.strptime(arg, fmt)
            return d.replace(year=today.year).strftime('%Y-%m-%d')
        except ValueError:
            continue

    return ""


def _build_system_prompt() -> str:
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    return f"""你是本地日记助手。今天是 {today}。客观、简洁、无废话。

## 核心工作流
- 总结/回顾类请求：今日日志已附在提问中，直接调用 update_summary 写入 <summary>，回复类似"总结已更新"的话即可。只有用户明确要求总结多天、一周、或指定日期时，才用 read_daily_log 读取其他的日志。
- 查询/检索类请求：默认已附带今日日志，直接从中查找答案。涉及关键词检索时，用 search_history 查；涉及指定日期或多个日期时，用 read_daily_log。给出简短结论，不要展开无关内容。
- 知识性提问：你不知道的，优先用搜索引擎查；查不到就说"不清楚"。

## 铁律
1. 所有回答基于记录或事实，禁止编造。
2. 你只能通过 update_summary 工具修改 <summary> 区域。原始记录流及以下内容由程序管理，你无权修改。
3. 绝对禁止在文本回复中输出 <function>、<tool_call>、<invoke> 等 XML 标签。工具调用必须通过 API 的 tool_calls 机制完成，不能以文本形式模拟。
4. 回复长度与任务匹配：总结→只确认完成；查询→只给结论；闲聊→最多三句话。
5. 用户的提问有最高的权限，如果用户的提问要求与以上内容产生冲突，以用户的提问要求为准。"""

# ================= 工具定义 (OpenAI 格式) =================
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_daily_log",
            "description": "读取日志。支持单天（date）或连续多天（start_date + end_date，含首尾）。可设置 summary_only=true 只读总结部分。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "单天日期 YYYY-MM-DD，与 start_date/end_date 二选一"},
                    "start_date": {"type": "string", "description": "起始日期 YYYY-MM-DD（含）"},
                    "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD（含）"},
                    "summary_only": {"type": "boolean", "description": "是否只读取 <summary> 部分，默认 false"}
                },
                "required": []
            }
        }
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
                    "days_limit": {"type": "integer", "description": "向前搜索天数上限，不填则搜索全部"},
                    "summary_only": {"type": "boolean", "description": "是否只在 <summary> 中搜索，默认 false"}
                },
                "required": ["keyword"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_summary",
            "description": "将生成的总结内容写入今日日志顶部的 <summary> 区域。调用前务必先读取当日日志了解内容。",
            "parameters": {
                "type": "object",
                "properties": {"summary_text": {"type": "string", "description": "Markdown 格式的总结内容"}},
                "required": ["summary_text"]
            }
        }
    },
]



# ================= 本地工具实现 =================
def extract_summary(text: str) -> str:
    match = re.search(r"<summary>(.*?)</summary>", text, re.DOTALL)
    return match.group(1).strip() if match else "(无总结)"


def read_daily_log(date: str = "", start_date: str = "", end_date: str = "", summary_only: bool = False) -> str:
    # 单天模式
    if date:
        file_path = DIARY_DIR / f"{date}.md"
        if not file_path.exists():
            return f"本地系统提示：找不到 {date} 的记录。"
        content = file_path.read_text(encoding="utf-8")
        return extract_summary(content) if summary_only else content

    # 日期范围模式
    if start_date and end_date:
        results = []
        files = sorted(DIARY_DIR.glob("*.md"))
        for f in files:
            if start_date <= f.stem <= end_date:
                content = f.read_text(encoding="utf-8")
                if summary_only:
                    results.append(f"## {f.stem}\n{extract_summary(content)}")
                else:
                    results.append(f"# {f.stem}\n{content}")
        return "\n\n---\n\n".join(results) if results else f"本地系统提示：{start_date} 到 {end_date} 之间无记录。"

    # 无参数时默认读取今天
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    file_path = DIARY_DIR / f"{today}.md"
    if not file_path.exists():
        return f"本地系统提示：找不到 {today} 的记录。"
    content = file_path.read_text(encoding="utf-8")
    return extract_summary(content) if summary_only else content


def search_history(keyword: str, days_limit: int = 0, summary_only: bool = False) -> str:
    files = sorted(DIARY_DIR.glob("*.md"), reverse=True)
    if days_limit and days_limit > 0:
        files = files[:days_limit]
    results = []
    for f in files:
        content = f.read_text(encoding="utf-8")
        search_target = extract_summary(content) if summary_only else content
        if keyword in search_target:
            lines = search_target.split('\n')
            matched = [line for line in lines if keyword in line]
            results.append(f"[{f.stem}] 匹配到:\n" + "\n".join(matched))
    return "\n\n".join(results) if results else f"本地系统提示：未找到关于 '{keyword}' 的记录。"


def get_today_file() -> Path:
    return DIARY_DIR / f"{datetime.datetime.now().strftime('%Y-%m-%d')}.md"


def init_file_if_not_exists():
    tf = get_today_file()
    if not tf.exists():
        template = (
            f"# {datetime.datetime.now().strftime('%Y-%m-%d')}\n\n"
            "<summary>\n暂无今日总结。\n</summary>\n\n"
            "---\n"
            "## 原始记录流\n\n"
        )
        tf.write_text(template, encoding="utf-8")


def append_log(content: str, tag: str = ""):
    init_file_if_not_exists()
    now = datetime.datetime.now().strftime("%H:%M")
    tf = get_today_file()
    with tf.open("a", encoding="utf-8") as f:
        if tag:
            f.write(f"**{now} {tag}:** {content}\n\n")
        else:
            f.write(f"**{now}:** {content}\n\n")


def read_last_at_query() -> tuple[str, bool, str]:
    """读取今日日志中最后一个 @AI 提问。
    返回 (query_text, is_answered, answer_or_empty)。
    """
    tf = get_today_file()
    if not tf.exists():
        return "", False, ""
    content = tf.read_text(encoding="utf-8")
    at_pattern = re.compile(r"\*\*(\d{2}:\d{2}) @AI(?:查阅)?:\*\* (.+?)(?=\n\*\*|\Z)", re.DOTALL)
    matches = list(at_pattern.finditer(content))
    if not matches:
        return "", False, ""
    last_match = matches[-1]
    query_text = last_match.group(2).strip()
    after_query = content[last_match.end():]
    reply_pattern = re.compile(
        r"\*\*\d{2}:\d{2} (?:\[AI回复]|\[AI查阅]) .+?:\*\* (.+?)(?=\n\*\*|\Z)", re.DOTALL
    )
    rm = reply_pattern.search(after_query)
    if rm:
        return query_text, True, rm.group(1).strip()
    return query_text, False, ""


def update_summary(summary_text: str) -> str:
    init_file_if_not_exists()
    tf = get_today_file()
    content = tf.read_text(encoding="utf-8")
    new_content = re.sub(
        r"<summary>.*?</summary>",
        f"<summary>\n{summary_text}\n</summary>",
        content,
        count=1,
        flags=re.DOTALL
    )
    tf.write_text(new_content, encoding="utf-8")
    return "总结已写入文档顶部。"


# ================= 工具调用分发 =================
def execute_tool(func_name: str, args: dict) -> str:
    if func_name == "read_daily_log":
        return read_daily_log(
            date=args.get("date", ""),
            start_date=args.get("start_date", ""),
            end_date=args.get("end_date", ""),
            summary_only=args.get("summary_only", False)
        )
    elif func_name == "search_history":
        return search_history(
            args.get("keyword", ""),
            args.get("days_limit", 0),
            args.get("summary_only", False)
        )
    elif func_name == "update_summary":
        return update_summary(args.get("summary_text", ""))
    else:
        return f"未知工具: {func_name}"


# ================= API 请求 =================
def call_gemini_api(prompt: str, model_cfg: ModelDict, search_enabled: bool = False, read_only: bool = False) -> tuple[str, bool, int, dict[str, int]]:
    api_url = model_cfg["api_url"]
    api_key = model_cfg["api_key"]
    model_name = model_cfg.get("model_id") or model_cfg["name"]

    url = f"{api_url}/{model_name}:generateContent?key={api_key}"
    active_tools = [t for t in TOOLS if not (read_only and t["function"]["name"] == "update_summary")]
    function_declarations = [t["function"] for t in active_tools]
    tools: list[dict] = [{"functionDeclarations": function_declarations}]
    if search_enabled:
        tools.insert(0, {"googleSearch": {}})

    payload: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": _build_system_prompt()}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": tools
    }

    web_searches = 0
    tool_calls: dict[str, int] = {}
    try:
        for _ in range(5):
            response = requests.post(url, json=payload, timeout=60)
            response.raise_for_status()
            data = response.json()
            candidate = data["candidates"][0]
            parts = candidate["content"]["parts"]

            # 提取网络搜索次数
            grounding = candidate.get("groundingMetadata", {})
            if grounding:
                queries = grounding.get("webSearchQueries", [])
                if queries:
                    web_searches = len(queries)
                else:
                    chunks = grounding.get("groundingChunks", [])
                    web_searches = len([c for c in chunks if "web" in c])

            # 检查是否有函数调用
            func_parts = [p for p in parts if "functionCall" in p]
            if not func_parts:
                text = parts[-1].get("text", "").strip()
                return text, True, web_searches, tool_calls

            # 执行函数调用
            func_responses = []
            for fp in func_parts:
                fc = fp["functionCall"]
                func_name = fc["name"]
                tool_calls[func_name] = tool_calls.get(func_name, 0) + 1
                args = fc.get("args", {})
                result = execute_tool(func_name, args)
                func_responses.append({
                    "functionResponse": {
                        "name": func_name,
                        "response": {"content": result}
                    }
                })

            payload["contents"].append({"role": "model", "parts": parts})
            payload["contents"].append({"role": "user", "parts": func_responses})

        return "接口异常: 函数调用超过最大轮次", False, web_searches, tool_calls
    except requests.RequestException as e:
        error_msg = str(e)
        if e.response is not None:
            error_msg += f" | {e.response.text}"
        return f"接口异常: {error_msg}", False, web_searches, tool_calls
    except Exception as e:
        return f"接口异常: {e}", False, web_searches, tool_calls


def call_openai_api(prompt: str, model_cfg: ModelDict, search_enabled: bool = False, read_only: bool = False) -> tuple[str, bool, int, dict[str, int]]:
    messages = [
        {"role": "system", "content": _build_system_prompt()},
        {"role": "user", "content": prompt}
    ]
    active_tools = [t for t in TOOLS if not (read_only and t["function"]["name"] == "update_summary")]
    payload: dict[str, Any] = {
        "model": model_cfg.get("model_id") or model_cfg["name"],
        "messages": messages,
        "tools": active_tools,
        "tool_choice": "auto"
    }

    if search_enabled:
        model_lower = (model_cfg.get("model_id") or model_cfg["name"]).lower()
        if "glm" in model_lower:
            payload["tools"] = payload["tools"] + [{"type": "web_search", "web_search": {"enable": True}}]
        elif "moonshot" in model_lower or "kimi" in model_lower:
            payload["tools"] = payload["tools"] + [{"type": "builtin_function", "function": {"name": "$web_search"}}]
        else:
            payload["web_search"] = True

    headers = {
        "Authorization": f"Bearer {model_cfg['api_key']}",
        "Content-Type": "application/json"
    }

    web_searches = 0
    tool_calls: dict[str, int] = {}
    try:
        for _ in range(5):
            resp = requests.post(model_cfg["api_url"], headers=headers, json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]

            # 提取网络搜索次数（citations 等自定义字段）
            citations = data.get("citations", [])
            if citations:
                web_searches += len(citations)

            tc = msg.get("tool_calls", [])
            if not tc:
                return msg["content"].strip(), True, web_searches, tool_calls

            for tool_call in tc:
                func_name = tool_call["function"]["name"]
                tool_calls[func_name] = tool_calls.get(func_name, 0) + 1

            messages.append(msg)
            for tool_call in tc:
                func_name = tool_call["function"]["name"]
                args = json.loads(tool_call["function"]["arguments"])
                res = execute_tool(func_name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "name": func_name,
                    "content": res
                })

            payload["messages"] = messages
            # 保留 tools 定义：DeepSeek 等模型需要每轮请求都带上 tools 才能继续调用

        return "接口异常: 工具调用超过最大轮次", False, web_searches, tool_calls
    except Exception as e:
        return f"接口异常: {e}", False, web_searches, tool_calls


def call_ai(prompt: str, model_cfg: ModelDict, read_only: bool = False) -> tuple[str, bool, int, dict[str, int]]:
    search_enabled = model_cfg.get("search", False)
    if model_cfg["type"] == "gemini":
        return call_gemini_api(prompt, model_cfg, search_enabled, read_only)
    else:
        return call_openai_api(prompt, model_cfg, search_enabled, read_only)


# ================= 查阅模式轻记录 =================
def format_stats(web_n: int, tool_dict: dict[str, int]) -> str:
    """格式化统计信息。无搜索能力的模型不显示搜索次数；工具调用显示具体名称和次数。"""
    parts = []
    if web_n:
        parts.append(f"网络搜索 {web_n} 次")
    if tool_dict:
        detail = ", ".join(f"{name} {n}次" for name, n in tool_dict.items())
        parts.append(f"本地工具调用: {detail}")
    return f"[*] {'; '.join(parts)}" if parts else ""


def _model_tag(model_cfg: ModelDict) -> str:
    """构建日志标签用的模型标识：name + SRCH（如有搜索能力）"""
    tag = model_cfg['name']
    if model_cfg.get('search'):
        tag += ' SRCH'
    return tag


def generate_review_summary(answer: str, model_cfg: ModelDict) -> str:
    """调用 AI 将查阅结果总结为一句话。"""
    summary_prompt = (
        f"用户进行了一次查阅，AI 的回答是：「{answer[:2000]}」\n\n"
        "请用一句话总结这次查阅得到的结论。直接说结论，不要重复用户的问题，不要以\"查阅了\"开头。只输出结论本身。"
    )
    try:
        summary, ok, _, _ = call_ai(summary_prompt, model_cfg, read_only=True)
        return summary if ok else f"得到了相关回答。"
    except requests.RequestException:
        return f"得到了相关回答。"


def _redraw_line(prompt: str, chars: list[str]) -> None:
    sys.stdout.buffer.write(b'\x1b[u')
    sys.stdout.buffer.write(b'\x1b[0J')
    sys.stdout.write(prompt)
    for ch in chars:
        sys.stdout.write(ch)
    sys.stdout.flush()


def _safe_input_unix(prompt: str) -> str:
    sys.stdout.buffer.write(b'\x1b[s')
    sys.stdout.write(prompt)
    sys.stdout.flush()

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    try:
        tty.setraw(fd)
        chars: list[str] = []

        while True:
            b = os.read(fd, 1)
            if not b:
                break

            if b == b'\r':
                sys.stdout.write('\r\n')
                sys.stdout.flush()
                break
            elif b in (b'\x7f', b'\x08'):
                if chars:
                    chars.pop()
                    _redraw_line(prompt, chars)
            elif b == b'\x03':
                sys.stdout.write('^C\r\n')
                sys.stdout.flush()
                raise KeyboardInterrupt()
            elif b == b'\x04':
                if not chars:
                    sys.stdout.write('\r\n')
                    sys.stdout.flush()
                    raise EOFError()
            elif b == b'\x1b':
                # Drain escape sequence via raw fd (no Python buffering)
                while select.select([fd], [], [], 0.05)[0]:
                    os.read(fd, 16)
                continue
            elif b[0] < 0x20 or b[0] == 0x7f:
                pass
            else:
                if b[0] & 0x80 == 0:
                    trail = 0
                elif b[0] & 0xE0 == 0xC0:
                    trail = 1
                elif b[0] & 0xF0 == 0xE0:
                    trail = 2
                elif b[0] & 0xF8 == 0xF0:
                    trail = 3
                else:
                    continue

                char_bytes = b
                for _ in range(trail):
                    char_bytes += os.read(fd, 1)

                try:
                    char = char_bytes.decode('utf-8')
                    chars.append(char)
                    sys.stdout.write(char)
                    sys.stdout.flush()
                except UnicodeDecodeError:
                    pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

    return ''.join(chars)


def _safe_input_windows(prompt: str) -> str:
    sys.stdout.buffer.write(b'\x1b[s')
    sys.stdout.write(prompt)
    sys.stdout.flush()
    chars: list[str] = []

    while True:
        ch = msvcrt.getwch()
        if ch == '\r' or ch == '\n':
            sys.stdout.write('\r\n')
            sys.stdout.flush()
            break
        elif ch == '\x08':
            if chars:
                chars.pop()
                # 2. 放弃传统的 '\b \b'，改用跨平台兼容的整行重绘
                _redraw_line(prompt, chars)
        elif ch == '\x03':
            sys.stdout.write('^C\r\n')
            sys.stdout.flush()
            raise KeyboardInterrupt()
        elif ch == '\x1a':
            if not chars:
                sys.stdout.write('^Z\r\n')
                sys.stdout.flush()
                raise EOFError()
        elif ch in ('\x00', '\xe0'):
            # Extended key prefix — consume the scan code and ignore
            msvcrt.getwch()
            continue
        elif ch == '\x1b':
            # ANSI escape sequence — drain remaining bytes
            time.sleep(0.03)
            while msvcrt.kbhit():
                msvcrt.getwch()
            continue
        elif ord(ch) < 0x20 or ord(ch) == 0x7f:
            # Other control character or DEL — silently ignore
            continue
        else:
            chars.append(ch)
            sys.stdout.write(ch)
            sys.stdout.flush()

    return ''.join(chars)


def safe_input(prompt: str = "") -> str:
    if IS_WINDOWS:
        return _safe_input_windows(prompt)
    else:
        return _safe_input_unix(prompt)


def show_view_help():
    console.print(Panel(
        "  [cyan]/v[/cyan]              → 今天（同: [dim]today, 今天[/dim]）\n"
        "  [cyan]/v -1[/cyan]           → 昨天（[dim]-N = N天前[/dim]；同: [dim]yesterday, 昨天[/dim]）\n"
        "  [cyan]/v last[/cyan]         → 最近一个有记录的日期\n"
        "  [cyan]/v 5-8[/cyan]          → 今年5月8日（MM-DD 或 MMDD）\n"
        "  [cyan]/v 2026-05-03[/cyan]   → 完整日期（YYYY-MM-DD 或 YYYYMMDD）",
        title="[bold]/v 用法[/bold]",
        border_style="cyan"
    ))


def show_help():
    console.print(Panel(
        "  [cyan]/h[/cyan]        → 显示此帮助\n"
        "  [cyan]/m[/cyan]        → 切换到下一个模型\n"
        "  [cyan]/o[/cyan]        → 切换 记录/查阅 模式\n"
        "  [cyan]/v [日期][/cyan] → 查看历史日记（空=今天, [cyan]/v help[/cyan] 查看所有用法）\n"
        "  [cyan]/r[/cyan]        → 重试今日最后一个未回答的 @提问\n"
        "  [cyan]/c[/cyan]        → 清空当前窗口\n"
        "  [cyan]/d[/cyan]        → 删除今日最后一条记录\n"
        "  [cyan]@[内容][/cyan]   → 呼叫AI解答或执行任务（如 @总结今日内容）",
        title="[bold]命令手册[/bold]",
        border_style="cyan"
    ))


def delete_last_record() -> bool:
    """删除今日日志中最后一条记录。返回是否成功删除。"""
    tf = get_today_file()
    if not tf.exists():
        return False
    content = tf.read_text(encoding="utf-8")
    pattern = re.compile(r"^\*\*\d{2}:\d{2}", re.MULTILINE)
    matches = list(pattern.finditer(content))
    if not matches:
        return False
    last_match = matches[-1]
    start = last_match.start()
    if start > 0 and content[start - 1] == '\n':
        start -= 1
    new_content = content[:start].rstrip() + "\n\n"
    tf.write_text(new_content, encoding="utf-8")
    return True


# ================= 主循环 =================
def main():
    current_cfg = ModelConfig.get_model()
    current_mode = "记录"

    console.print(Panel.fit("[bold]Agent 日记系统[/bold]", border_style="cyan"))
    console.print(f"  可用模型: [dim]{', '.join(m['name'] for m in ModelConfig.models())}[/dim]")
    show_help()
    console.print()

    while True:
        try:
            srch = " SRCH" if current_cfg.get("search") else ""
            prompt_prefix = f"[{current_cfg['name']}{srch} | {current_mode}] >> "
            user_input = safe_input(prompt_prefix).strip()
        except (KeyboardInterrupt, EOFError):
            console.print("[dim]系统退出。[/dim]")
            break

        if not user_input:
            continue

        # --- /h 命令 ---
        if user_input == "/h":
            show_help()
            continue

        # --- /m 命令（切换模型） ---
        if user_input == "/m":
            current_cfg = ModelConfig.next_after(current_cfg["name"])
            console.print(f"[cyan][*][/cyan] 模型已切换为: {current_cfg['name']}")
            continue

        # --- /o 命令（切换模式） ---
        if user_input == "/o":
            current_mode = "查阅" if current_mode == "记录" else "记录"
            console.print(f"[cyan][*][/cyan] 模式已切换为: {current_mode}模式")
            continue

        # --- /c 命令（清屏） ---
        if user_input == "/c":
            console.clear()
            continue

        # --- /d 命令（删除最后一条记录） ---
        if user_input == "/d":
            if delete_last_record():
                console.print("[cyan][*][/cyan] 已删除今日最后一条记录。")
            else:
                console.print("[yellow][!][/yellow] 今日无记录可删除。")
            continue

        # --- /v 命令 ---
        if user_input == "/v" or user_input.startswith("/v "):
            arg = user_input[3:].strip() if user_input.startswith("/v ") else ""
            if arg.lower() == "help":
                show_view_help()
                continue
            date_str = resolve_date(arg)
            if not date_str:
                console.print(f"[yellow][!][/yellow] 无法解析日期: {arg}")
                continue
            file_path = DIARY_DIR / f"{date_str}.md"
            if not file_path.exists():
                console.print(f"[yellow][!][/yellow] 找不到 {date_str} 的记录。")
                continue
            content = file_path.read_text(encoding="utf-8")
            content = re.sub(r'</?summary>', '', content)
            console.print(Panel(
                Markdown(content),
                title=f"[bold]{date_str}[/bold]",
                border_style="cyan"
            ))
            continue

        # --- /r 命令 ---
        if user_input == "/r":
            last_query, answered, prev_ans = read_last_at_query()
            if not last_query:
                console.print("[yellow][!][/yellow] 今日日志中没有 @AI 提问。")
                continue
            if answered:
                console.print(f"[cyan][*][/cyan] 最后一个 @AI 提问已被回答：\n\n{prev_ans}\n")
                continue
            console.print(f"[cyan][*][/cyan] 重试提问: {last_query[:80]}{'...' if len(last_query) > 80 else ''}")
            console.print("[cyan][*][/cyan] AI 思考/检索中...")
            init_file_if_not_exists()
            today_log = get_today_file().read_text(encoding="utf-8")
            retry_prompt = f"【今日记录（{datetime.datetime.now().strftime('%Y-%m-%d')}）】\n{today_log}\n\n【用户提问】\n{last_query}"
            ans, success, web_n, tool_dict = call_ai(retry_prompt, current_cfg, read_only=(current_mode == "查阅"))

            if success:
                console.print(Panel(f"{ans}", title="[bold]AI 输出[/bold]", border_style="green"))
                stats = format_stats(web_n, tool_dict)
                if stats:
                    console.print(f"[dim]{stats}[/dim]")
                tag = f"[AI回复] {_model_tag(current_cfg)}"
                append_log(ans, tag)
            else:
                console.print(f"[red][!][/red] 重试失败: {ans}")
            continue

        # --- @AI 提问 ---
        if user_input.startswith("@"):
            query = user_input[1:].strip()
            if not query:
                console.print("[yellow][!][/yellow] 请输入提问内容。")
                continue

            console.print("[cyan][*][/cyan] AI 思考/检索中...")

            # 记录用户提问（查阅模式用独立标签，保证 /r 可读取）
            user_tag = "@AI查阅" if current_mode == "查阅" else "@AI"
            append_log(query, user_tag)

            # 自动附带今日记录
            init_file_if_not_exists()
            today_log = get_today_file().read_text(encoding="utf-8")
            prompt = f"【今日记录（{datetime.datetime.now().strftime('%Y-%m-%d')}）】\n{today_log}\n\n【用户提问】\n{query}"

            ans, success, web_n, tool_dict = call_ai(prompt, current_cfg, read_only=(current_mode == "查阅"))

            if success:
                console.print(Panel(f"{ans}", title="[bold]AI 输出[/bold]", border_style="green"))
                stats = format_stats(web_n, tool_dict)
                if stats:
                    console.print(f"[dim]{stats}[/dim]")

                if current_mode == "查阅":
                    # 查阅模式：AI 生成一句话总结后记录
                    console.print("[cyan][*][/cyan] 查阅模式，正在生成轻记录...")
                    review_summary = generate_review_summary(ans, current_cfg)
                    append_log(review_summary, f"[AI查阅] {_model_tag(current_cfg)}")
                else:
                    append_log(ans, f"[AI回复] {_model_tag(current_cfg)}")
            else:
                console.print(f"[red][!][/red] 请求失败: {ans}")
            continue

        # --- 普通文本输入 ---
        if current_mode == "记录":
            append_log(user_input)
        else:
            console.print("[yellow][!][/yellow] 查阅模式下普通文本不会被记录。请使用 @ 提问，或输入 /o 切换回记录模式。")


if __name__ == "__main__":
    main()
