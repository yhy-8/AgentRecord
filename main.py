"""AgentRecord 可执行入口与终端交互。"""

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
from analysis import (
    analysis_report_path,
    generate_analysis_report,
    install_system_automation,
    run_due_automatic_tasks,
    summarize_diary,
    uninstall_system_automation,
)


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

RECORD_MODE = "record"
REPORT_MODE = "report"
MODE_COMMANDS = {
    RECORD_MODE: ("/h", "/mode", "/v", "/ref", "/d", "/c"),
    REPORT_MODE: ("/h", "/mode", "/s", "/a", "/m"),
}


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


def show_help(mode: str = RECORD_MODE) -> None:
    if mode == REPORT_MODE:
        content = (
            "  [cyan]/h[/cyan]        → 显示报告模式帮助\n"
            "  [cyan]/mode[/cyan]     → 切换到记录模式\n"
            "  [cyan]/s [日期][/cyan] → 生成日记顶部总结（空=今天）\n"
            "  [cyan]/a [类型] [日期][/cyan] → 手动生成报告（daily/weekly/monthly）\n"
            "  [cyan]/m[/cyan]        → 永久切换总结和报告使用的模型"
        )
        title = "[bold]报告模式[/bold]"
    else:
        content = (
            "  普通文字      → 立即写入今日日记\n"
            "  [cyan]/h[/cyan]        → 显示记录模式帮助\n"
            "  [cyan]/mode[/cyan]     → 切换到报告模式\n"
            "  [cyan]/v [日期][/cyan] → 查看历史日记（[cyan]/v help[/cyan] 查看用法）\n"
            "  [cyan]/ref [类型] [筛选][/cyan] → 引用日记或分析报告并继续记录\n"
            "  [cyan]/d[/cyan]        → 删除今日最后一条记录\n"
            "  [cyan]/c[/cyan]        → 清空当前窗口"
        )
        title = "[bold]记录模式[/bold]"
    console.print(
        Panel(
            content,
            title=title,
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
    elif first in ("monthly", "month", "月报"):
        kind = "monthly"
        date_argument = arguments[1] if len(arguments) > 1 else ""
    else:
        date_argument = arguments[0]
    return kind, date_argument


def _handle_analysis(user_input: str, model_config: settings.ModelDict) -> None:
    kind, date_argument = _parse_analysis_arguments(user_input)
    if kind == "monthly" and re.fullmatch(r"\d{4}-\d{2}", date_argument):
        date_argument += "-01"
    date = journal.resolve_date(date_argument)
    if not date:
        console.print(f"[yellow][!][/yellow] 无法解析日期: {date_argument}")
        return
    anchor = datetime.datetime.strptime(date, "%Y-%m-%d").date()
    label = {"daily": "日报", "weekly": "周报", "monthly": "月报"}[kind]
    target_path = analysis_report_path(kind, anchor, origin="manual")
    if target_path and target_path.exists():
        confirmation = safe_input(
            f"该周期的手动{label}已存在，确认覆盖？[y/N] >> "
        ).strip().lower()
        if confirmation not in ("y", "yes", "是"):
            console.print("[dim]已取消，原报告保持不变。[/dim]")
            return
    console.print(f"[cyan][*][/cyan] 正在生成分析{label}...")
    report, success, report_path = generate_analysis_report(
        kind, anchor, model_config, origin="manual"
    )
    if success:
        console.print(Panel(report, title=f"[bold]分析{label}[/bold]", border_style="green"))
        console.print(f"[dim]报告已保存: {report_path}[/dim]")
    else:
        console.print(f"[red][!][/red] 报告生成失败: {report}")


_REFERENCE_KIND_ALIASES = {
    "1": "diary",
    "diary": "diary",
    "日记": "diary",
    "2": "daily",
    "daily": "daily",
    "日报": "daily",
    "3": "weekly",
    "weekly": "weekly",
    "周报": "weekly",
    "4": "monthly",
    "monthly": "monthly",
    "月报": "monthly",
}


def _handle_reference(user_input: str) -> None:
    arguments = user_input.split()[1:]
    kind_text = arguments[0].lower() if arguments else ""
    if not kind_text:
        kind_text = safe_input(
            "引用类型 [1=日记, 2=分析日报, 3=分析周报, 4=分析月报，空=取消] >> "
        ).strip()
        if not kind_text:
            return
    kind = _REFERENCE_KIND_ALIASES.get(kind_text)
    if not kind:
        console.print(f"[yellow][!][/yellow] 未知引用类型: {kind_text}")
        return

    keyword = arguments[1] if len(arguments) > 1 else ""
    sources = journal.list_reference_sources(kind, keyword=keyword)
    if not sources:
        suffix = f"（筛选: {keyword}）" if keyword else ""
        console.print(f"[yellow][!][/yellow] 没有可引用的文件{suffix}。")
        return

    choices = "\n".join(
        f"  [cyan]{index}[/cyan]. {label}"
        for index, (label, _) in enumerate(sources, 1)
    )
    console.print(Panel(choices, title="[bold]选择引用来源[/bold]", border_style="cyan"))
    selection = safe_input("选择编号 [空=取消] >> ").strip()
    if not selection:
        return
    try:
        selected_index = int(selection) - 1
        if selected_index < 0:
            raise IndexError
        label, source_path = sources[selected_index]
    except (ValueError, IndexError):
        console.print(f"[yellow][!][/yellow] 无效编号: {selection}")
        return

    note = safe_input("关联记录 [可留空] >> ").strip()
    submitted_at = datetime.datetime.now()
    journal.append_reference(label, source_path, note, submitted_at=submitted_at)
    console.print(f"[cyan][*][/cyan] 已引用: {label}")


def _handle_process_action(arguments: list[str]) -> bool:
    if "--run-automation" in arguments:
        run_due_automatic_tasks()
        return True
    if "--install-automation" in arguments:
        success, message = install_system_automation()
        console.print(f"[{'green' if success else 'red'}]{message}[/]")
        return True
    if "--uninstall-automation" in arguments:
        success, message = uninstall_system_automation()
        console.print(f"[{'green' if success else 'red'}]{message}[/]")
        return True
    return False


def _command_token(user_input: str) -> str:
    return user_input.split(maxsplit=1)[0]


def main() -> None:
    if _handle_process_action(sys.argv[1:]):
        return
    current_model = settings.ModelConfig.get_model()
    mode = RECORD_MODE
    console.print(Panel.fit("[bold]AgentRecord 思维记录系统[/bold]", border_style="cyan"))
    show_help(mode)
    console.print()

    while True:
        try:
            raw_input = safe_input(">> ")
            submitted_at = datetime.datetime.now()
            user_input = raw_input.strip()
        except (KeyboardInterrupt, EOFError):
            console.print("[dim]系统退出。[/dim]")
            break
        if not user_input:
            continue

        command = _command_token(user_input)
        if command == "/mode":
            mode = REPORT_MODE if mode == RECORD_MODE else RECORD_MODE
            show_help(mode)
            continue
        if command == "/h":
            show_help(mode)
            continue

        allowed_commands = MODE_COMMANDS[mode]
        known_commands = set(MODE_COMMANDS[RECORD_MODE] + MODE_COMMANDS[REPORT_MODE])
        if command in known_commands and command not in allowed_commands:
            target = "报告" if mode == RECORD_MODE else "记录"
            console.print(f"[yellow][!][/yellow] 请先用 /mode 切换到{target}模式。")
            continue

        if mode == REPORT_MODE:
            if command == "/m":
                next_model = settings.ModelConfig.next_after(current_model["name"])
                try:
                    current_model = settings.ModelConfig.select(next_model["name"])
                except OSError as error:
                    console.print(f"[red][!][/red] 模型切换失败: {error}")
                    continue
                console.print(
                    f"[cyan][*][/cyan] 模型已永久切换为: {current_model['name']}"
                )
            elif command == "/s" and (
                user_input == "/s" or user_input.startswith("/s ")
            ):
                _handle_summary(user_input, current_model)
            elif command == "/a" and (
                user_input == "/a" or user_input.startswith("/a ")
            ):
                _handle_analysis(user_input, current_model)
            else:
                console.print("[yellow][!][/yellow] 报告模式只接受当前帮助中的命令。")
            continue

        if user_input == "/c":
            console.clear()
        elif user_input == "/d":
            if journal.delete_last_record():
                console.print("[cyan][*][/cyan] 已删除今日最后一条记录。")
            else:
                console.print("[yellow][!][/yellow] 今日无记录可删除。")
        elif user_input == "/v" or user_input.startswith("/v "):
            _handle_view(user_input)
        elif user_input == "/ref" or user_input.startswith("/ref "):
            _handle_reference(user_input)
        else:
            journal.append_log(user_input, submitted_at=submitted_at)


if __name__ == "__main__":
    main()
