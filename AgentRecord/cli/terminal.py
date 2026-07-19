"""Cross-platform terminal input and Rich presentation."""

import ctypes
import os
import queue
import select
import shutil
import sys
import time
import unicodedata

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel


def _configure_utf8_stdio() -> None:
    """Keep Windows CI and legacy consoles from encoding Chinese as cp1252."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


_configure_utf8_stdio()


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
_NOTIFICATIONS: queue.SimpleQueue[tuple[str, str | None]] = queue.SimpleQueue()

RECORD_MODE = "record"
REPORT_MODE = "report"


def post_notification(message: str, style: str | None = None) -> None:
    """Queue a worker-thread notification for rendering by the input thread."""
    _NOTIFICATIONS.put((message, style))


def _pending_notifications() -> list[tuple[str, str | None]]:
    notifications = []
    while True:
        try:
            notifications.append(_NOTIFICATIONS.get_nowait())
        except queue.Empty:
            return notifications


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

    move_up = f"\x1b[{old_rows - 1}A" if old_rows > 1 else ""
    # Do not mix TextIOWrapper writes with direct ``buffer`` writes.  On
    # Windows their buffering order is not guaranteed: the erase sequence can
    # otherwise run before the carriage return and leave the final cell of a
    # double-width Chinese character on screen.
    sys.stdout.write(f"{move_up}\r\x1b[0J{new_text}")
    sys.stdout.flush()


def _show_notifications(prompt: str, characters: list[str]) -> None:
    notifications = _pending_notifications()
    if not notifications:
        return
    current_width = _display_width(prompt) + sum(
        _display_width(character) for character in characters
    )
    terminal_width = shutil.get_terminal_size().columns or 80
    current_rows = max(1, (current_width + terminal_width - 1) // terminal_width)
    move_up = f"\x1b[{current_rows - 1}A" if current_rows > 1 else ""
    sys.stdout.write(f"{move_up}\r\x1b[0J")
    sys.stdout.flush()
    for message, style in notifications:
        console.print(message, style=style, markup=False)
    sys.stdout.write(prompt + "".join(characters))
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
            if not select.select([file_descriptor], [], [], 0.1)[0]:
                _show_notifications(prompt, characters)
                continue
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
        if not msvcrt.kbhit():
            _show_notifications(prompt, characters)
            time.sleep(0.05)
            continue
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
            "  [cyan]/status[/cyan]   → 查看自动任务产物、调度与失败状态\n"
            "  [cyan]/s [日期][/cyan] → 生成日记顶部总结（空=今天）\n"
            "  [cyan]/a weekly [日期][/cyan] → 后台生成分析周报（空=快速选择自然周）\n"
            "  [cyan]/a monthly [日期][/cyan] → 后台生成分析月报（空=快速选择自然月）\n"
            "  [cyan]/retry[/cyan]    → 独立后台重试全部失败自动任务（产物仍为自动）\n"
            "  [cyan]/f[/cyan]        → 认可、否决或修正最近的人物画像条目\n"
            "  [cyan]/m[/cyan]        → 永久切换总结和报告使用的模型"
        )
        title = "[bold]报告模式[/bold]"
    else:
        content = (
            "  普通文字      → 立即写入今日日记\n"
            "  [cyan]/h[/cyan]        → 显示记录模式帮助\n"
            "  [cyan]/mode[/cyan]     → 切换到报告模式\n"
            "  [cyan]/v [日期][/cyan] → 查看历史日记（[cyan]/v help[/cyan] 查看用法）\n"
            "  [cyan]/ref [日期][/cyan] → 按日期选择日记并继续记录\n"
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
