"""终端输入、命令解析和结果展示。"""

import datetime
import os
import re
import select
import shutil
import sys
import time
import unicodedata

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

import journal
import settings
from ai_client import call_ai, format_stats, model_tag
from analysis import generate_analysis_report, start_automation_worker, summarize_diary


try:
    import msvcrt

    IS_WINDOWS = True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-11)
    mode = ctypes.c_ulong()
    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
except ImportError:
    import termios
    import tty

    IS_WINDOWS = False


console = Console()


def _display_width(text: str) -> int:
    width = 0
    for character in text:
        east_asian_width = unicodedata.east_asian_width(character)
        width += 2 if east_asian_width in ("W", "F") else 1
    return width


def _redraw_line(prompt: str, characters: list[str], popped: str = "") -> None:
    new_text = prompt + "".join(characters)
    terminal_width = shutil.get_terminal_size().columns or 80
    new_width = _display_width(prompt) + sum(
        _display_width(character) for character in characters
    )
    old_width = new_width + (_display_width(popped) if popped else 1)
    old_rows = max(1, (old_width + terminal_width - 1) // terminal_width)

    if old_rows > 1:
        sys.stdout.write(f"\x1b[{old_rows - 1}A")
    sys.stdout.write("\r")
    sys.stdout.buffer.write(b"\x1b[0J")
    sys.stdout.write(new_text)
    sys.stdout.flush()


def _safe_input_unix(prompt: str) -> str:
    sys.stdout.write(prompt)
    sys.stdout.flush()
    file_descriptor = sys.stdin.fileno()
    old_settings = termios.tcgetattr(file_descriptor)

    try:
        tty.setraw(file_descriptor)
        characters: list[str] = []
        while True:
            byte = os.read(file_descriptor, 1)
            if not byte:
                break
            if byte == b"\r":
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                break
            if byte in (b"\x7f", b"\x08"):
                if characters:
                    _redraw_line(prompt, characters, characters.pop())
                continue
            if byte == b"\x03":
                sys.stdout.write("^C\r\n")
                sys.stdout.flush()
                raise KeyboardInterrupt()
            if byte == b"\x04":
                if not characters:
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    raise EOFError()
                continue
            if byte == b"\x1b":
                while select.select([file_descriptor], [], [], 0.05)[0]:
                    os.read(file_descriptor, 16)
                continue
            if byte[0] < 0x20 or byte[0] == 0x7F:
                continue

            if byte[0] & 0x80 == 0:
                trailing_bytes = 0
            elif byte[0] & 0xE0 == 0xC0:
                trailing_bytes = 1
            elif byte[0] & 0xF0 == 0xE0:
                trailing_bytes = 2
            elif byte[0] & 0xF8 == 0xF0:
                trailing_bytes = 3
            else:
                continue

            character_bytes = byte
            for _ in range(trailing_bytes):
                character_bytes += os.read(file_descriptor, 1)
            try:
                character = character_bytes.decode("utf-8")
            except UnicodeDecodeError:
                continue
            characters.append(character)
            sys.stdout.write(character)
            sys.stdout.flush()
    finally:
        termios.tcsetattr(file_descriptor, termios.TCSADRAIN, old_settings)
    return "".join(characters)


def _safe_input_windows(prompt: str) -> str:
    sys.stdout.write(prompt)
    sys.stdout.flush()
    characters: list[str] = []

    while True:
        character = msvcrt.getwch()
        if character in ("\r", "\n"):
            sys.stdout.write("\r\n")
            sys.stdout.flush()
            break
        if character == "\x08":
            if characters:
                _redraw_line(prompt, characters, characters.pop())
            continue
        if character == "\x03":
            sys.stdout.write("^C\r\n")
            sys.stdout.flush()
            raise KeyboardInterrupt()
        if character == "\x1a":
            if not characters:
                sys.stdout.write("^Z\r\n")
                sys.stdout.flush()
                raise EOFError()
            continue
        if character in ("\x00", "\xe0"):
            msvcrt.getwch()
            continue
        if character == "\x1b":
            time.sleep(0.03)
            while msvcrt.kbhit():
                msvcrt.getwch()
            continue
        if ord(character) < 0x20 or ord(character) == 0x7F:
            continue
        characters.append(character)
        sys.stdout.write(character)
        sys.stdout.flush()
    return "".join(characters)


def safe_input(prompt: str = "") -> str:
    return _safe_input_windows(prompt) if IS_WINDOWS else _safe_input_unix(prompt)


def show_view_help() -> None:
    console.print(
        Panel(
            "  [cyan]/v[/cyan]              → 今天（同: [dim]today, 今天[/dim]）\n"
            "  [cyan]/v -1[/cyan]           → 昨天（[dim]-N = N天前[/dim]）\n"
            "  [cyan]/v last[/cyan]         → 最近一个有记录的日期\n"
            "  [cyan]/v 5-8[/cyan]          → 今年5月8日（MM-DD 或 MMDD）\n"
            "  [cyan]/v 2026-05-03[/cyan]   → 完整日期（YYYY-MM-DD 或 YYYYMMDD）",
            title="[bold]/v 用法[/bold]",
            border_style="cyan",
        )
    )


def show_help() -> None:
    console.print(
        Panel(
            "  [cyan]/h[/cyan]        → 显示此帮助\n"
            "  [cyan]/m[/cyan]        → 切换到下一个模型\n"
            "  [cyan]/v [日期][/cyan] → 查看历史日记（[cyan]/v help[/cyan] 查看用法）\n"
            "  [cyan]/s [日期][/cyan] → 生成日记顶部总结（空=今天）\n"
            "  [cyan]/a [类型] [日期][/cyan] → 生成分析报告（daily/weekly）\n"
            "  [cyan]/r[/cyan]        → 重试今日最后一个未回答的 @AI 提问\n"
            "  [cyan]/c[/cyan]        → 清空当前窗口\n"
            "  [cyan]/d[/cyan]        → 删除今日最后一条记录\n"
            "  [cyan]@[内容][/cyan]   → 呼叫AI解答或执行任务（完整记录）",
            title="[bold]命令手册[/bold]",
            border_style="cyan",
        )
    )


def _handle_view(user_input: str) -> None:
    argument = user_input[3:].strip() if user_input.startswith("/v ") else ""
    if argument.lower() == "help":
        show_view_help()
        return
    date = journal.resolve_date(argument)
    if not date:
        console.print(f"[yellow][!][/yellow] 无法解析日期: {argument}")
        return
    file_path = settings.DIARY_DIR / f"{date}.md"
    if not file_path.exists():
        console.print(f"[yellow][!][/yellow] 找不到 {date} 的记录。")
        return
    content = re.sub(r"</?summary>", "", file_path.read_text(encoding="utf-8"))
    console.print(
        Panel(Markdown(content), title=f"[bold]{date}[/bold]", border_style="cyan")
    )


def _handle_summary(user_input: str, model_config: settings.ModelDict) -> None:
    argument = user_input[3:].strip() if user_input.startswith("/s ") else ""
    date = journal.resolve_date(argument)
    if not date:
        console.print(f"[yellow][!][/yellow] 无法解析日期: {argument}")
        return
    console.print(f"[cyan][*][/cyan] 正在生成 {date} 的日记总结...")
    summary, success = summarize_diary(date, model_config)
    if success:
        console.print(
            Panel(summary, title=f"[bold]{date} 日记总结[/bold]", border_style="green")
        )
    else:
        console.print(f"[red][!][/red] 总结失败: {summary}")


def _parse_analysis_arguments(user_input: str) -> tuple[str, str]:
    arguments = user_input.split()[1:]
    kind = "daily"
    date_argument = ""
    if not arguments:
        return kind, date_argument

    first = arguments[0].lower()
    if first in ("daily", "day", "日报"):
        date_argument = arguments[1] if len(arguments) > 1 else ""
    elif first in ("weekly", "week", "周报"):
        kind = "weekly"
        date_argument = arguments[1] if len(arguments) > 1 else ""
    else:
        date_argument = arguments[0]
    return kind, date_argument


def _handle_analysis(user_input: str, model_config: settings.ModelDict) -> None:
    kind, date_argument = _parse_analysis_arguments(user_input)
    date = journal.resolve_date(date_argument)
    if not date:
        console.print(f"[yellow][!][/yellow] 无法解析日期: {date_argument}")
        return
    anchor = datetime.datetime.strptime(date, "%Y-%m-%d").date()
    label = "日报" if kind == "daily" else "周报"
    console.print(f"[cyan][*][/cyan] 正在生成分析{label}...")
    report, success, report_path = generate_analysis_report(kind, anchor, model_config)
    if success:
        console.print(Panel(report, title=f"[bold]分析{label}[/bold]", border_style="green"))
        console.print(f"[dim]报告已保存: {report_path}[/dim]")
    else:
        console.print(f"[red][!][/red] 报告生成失败: {report}")


def _ask_ai(query: str, model_config: settings.ModelDict, retry: bool = False) -> None:
    if not retry:
        journal.append_log(query, "@AI")
    journal.init_file_if_not_exists()
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    today_log = journal.get_today_file().read_text(encoding="utf-8")
    prompt = f"【今日记录（{today}）】\n{today_log}\n\n【用户提问】\n{query}"
    answer, success, web_count, tool_counts, result_count = call_ai(prompt, model_config)
    if not success:
        label = "重试失败" if retry else "请求失败"
        console.print(f"[red][!][/red] {label}: {answer}")
        return

    console.print(Panel(answer, title="[bold]AI 输出[/bold]", border_style="green"))
    stats = format_stats(web_count, tool_counts, result_count)
    if stats:
        console.print(f"[dim]{stats}[/dim]")
    journal.append_log(answer, f"[AI回复] {model_tag(model_config)}")


def _handle_retry(model_config: settings.ModelDict) -> None:
    query, answered, previous_answer = journal.read_last_at_query()
    if not query:
        console.print("[yellow][!][/yellow] 今日日志中没有 @AI 提问。")
        return
    if answered:
        console.print(f"[cyan][*][/cyan] 最后一个 @AI 提问已被回答：\n\n{previous_answer}\n")
        return
    console.print(
        f"[cyan][*][/cyan] 重试提问: {query[:80]}{'...' if len(query) > 80 else ''}"
    )
    console.print("[cyan][*][/cyan] AI 思考/检索中...")
    _ask_ai(query, model_config, retry=True)


def main() -> None:
    current_model = settings.ModelConfig.get_model()
    console.print(Panel.fit("[bold]Agent 日记系统[/bold]", border_style="cyan"))
    console.print(
        f"  可用模型: [dim]{', '.join(model['name'] for model in settings.ModelConfig.models())}[/dim]"
    )
    show_help()
    console.print()
    start_automation_worker()

    while True:
        try:
            search_label = " SRCH" if current_model.get("search") else ""
            user_input = safe_input(
                f"[{current_model['name']}{search_label}] >> "
            ).strip()
        except (KeyboardInterrupt, EOFError):
            console.print("[dim]系统退出。[/dim]")
            break
        if not user_input:
            continue

        if user_input == "/h":
            show_help()
        elif user_input == "/m":
            current_model = settings.ModelConfig.next_after(current_model["name"])
            console.print(f"[cyan][*][/cyan] 模型已切换为: {current_model['name']}")
        elif user_input == "/c":
            console.clear()
        elif user_input == "/d":
            if journal.delete_last_record():
                console.print("[cyan][*][/cyan] 已删除今日最后一条记录。")
            else:
                console.print("[yellow][!][/yellow] 今日无记录可删除。")
        elif user_input == "/v" or user_input.startswith("/v "):
            _handle_view(user_input)
        elif user_input == "/s" or user_input.startswith("/s "):
            _handle_summary(user_input, current_model)
        elif user_input == "/a" or user_input.startswith("/a "):
            _handle_analysis(user_input, current_model)
        elif user_input == "/r":
            _handle_retry(current_model)
        elif user_input.startswith("@"):
            query = user_input[1:].strip()
            if not query:
                console.print("[yellow][!][/yellow] 请输入提问内容。")
                continue
            console.print("[cyan][*][/cyan] AI 思考/检索中...")
            _ask_ai(query, current_model)
        else:
            journal.append_log(user_input)
