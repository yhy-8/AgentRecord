"""Process entry point shared by scripts, module execution, and PyInstaller."""

import ctypes
import logging
import sys


logger = logging.getLogger(__name__)


def _hide_background_console() -> None:
    """Hide only the Windows packaged background-task console."""
    if sys.platform != "win32" or "--run-automation" not in sys.argv:
        return
    try:
        window = ctypes.windll.kernel32.GetConsoleWindow()
        if window:
            ctypes.windll.user32.ShowWindow(window, 0)
    except (AttributeError, OSError):
        pass


_hide_background_console()


def _handle_process_action(arguments: list[str]) -> bool:
    from ..analysis import (
        install_system_automation,
        run_due_automatic_tasks,
        uninstall_system_automation,
    )

    if "--run-automation" in arguments:
        run_due_automatic_tasks()
        return True
    from .terminal import console

    if "--install-automation" in arguments:
        success, message = install_system_automation()
        console.print(f"[{'green' if success else 'red'}]{message}[/]")
        return True
    if "--uninstall-automation" in arguments:
        success, message = uninstall_system_automation()
        console.print(f"[{'green' if success else 'red'}]{message}[/]")
        return True
    return False


def main() -> None:
    from ..logging_config import configure_logging

    configure_logging()
    action = next(
        (argument for argument in sys.argv[1:] if argument.startswith("--")),
        "interactive",
    )
    logger.info("application_started action=%s", action)
    if _handle_process_action(sys.argv[1:]):
        return
    from .app import run_interactive

    run_interactive()
