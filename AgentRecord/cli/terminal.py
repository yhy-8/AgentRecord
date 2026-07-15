"""Cross-platform terminal input and Rich presentation."""

import ctypes
import os
import select
import shutil
import sys
import time
import unicodedata

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel


try:
    import msvcrt

    IS_WINDOWS = True

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
