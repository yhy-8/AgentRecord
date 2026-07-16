"""Best-effort desktop lock detection for automatic network work."""

import ctypes
import os
import subprocess
import sys


def _windows_locked() -> bool | None:
    try:
        user32 = ctypes.windll.user32
        desktop = user32.OpenInputDesktop(0, False, 0x0100)
        if not desktop:
            return True
        try:
            return not bool(user32.SwitchDesktop(desktop))
        finally:
            user32.CloseDesktop(desktop)
    except (AttributeError, OSError):
        return None


def _linux_locked() -> bool | None:
    try:
        listed = subprocess.run(
            ["loginctl", "list-sessions", "--no-legend"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if listed.returncode != 0:
        return None
    uid = str(os.getuid())
    session_ids = []
    for line in listed.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[1] == uid:
            session_ids.append(fields[0])
    if not session_ids:
        return None

    states: list[tuple[bool, bool]] = []
    for session_id in session_ids:
        try:
            active = subprocess.run(
                ["loginctl", "show-session", session_id, "-p", "Active", "--value"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            locked = subprocess.run(
                [
                    "loginctl",
                    "show-session",
                    session_id,
                    "-p",
                    "LockedHint",
                    "--value",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if active.returncode == 0 and locked.returncode == 0:
            states.append(
                (
                    active.stdout.strip().lower() == "yes",
                    locked.stdout.strip().lower() == "yes",
                )
            )
    if not states:
        return None
    if any(active and not locked for active, locked in states):
        return False
    if any(locked for _, locked in states):
        return True
    return False


def session_is_locked() -> bool:
    """Return False when lock state cannot be determined, avoiding permanent stalls."""
    forced = os.environ.get("AGENTRECORD_SESSION_LOCKED")
    if forced in {"0", "1"}:
        return forced == "1"
    if sys.platform == "win32":
        state = _windows_locked()
    elif sys.platform.startswith("linux"):
        state = _linux_locked()
    else:
        state = None
    return bool(state)
