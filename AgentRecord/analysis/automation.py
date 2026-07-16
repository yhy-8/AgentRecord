"""Automatic due-task execution and operating-system scheduler integration."""

import datetime
import json
import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path

from .. import settings
from ..ai_client import automatic_request_mode
from .context import _existing_logs
from .information import generate_information_briefing
from .orchestrator import generate_analysis_report, summarize_diary
from .session_state import session_is_locked


logger = logging.getLogger(__name__)


class _AutomationLock:
    """A kernel-held cross-process lock released automatically on process death."""

    def __init__(self, path: Path, file_object):
        self.path = path
        self._file = file_object

    @classmethod
    def acquire(cls) -> "_AutomationLock | None":
        settings.ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
        path = settings.ANALYSIS_DIR / ".automation.lock"
        file_object = path.open("a+b")
        try:
            file_object.seek(0, os.SEEK_END)
            if file_object.tell() == 0:
                file_object.write(b"0")
                file_object.flush()
            file_object.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(file_object.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file_object.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            file_object.close()
            return None
        return cls(path, file_object)

    def release(self) -> None:
        if self._file.closed:
            return
        try:
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()


def _load_automation_state() -> dict:
    state_path = settings.ANALYSIS_DIR / ".automation-state.json"
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_automation_state(state: dict) -> None:
    settings.ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    state_path = settings.ANALYSIS_DIR / ".automation-state.json"
    temp_path = settings.ANALYSIS_DIR / ".automation-state.tmp"
    temp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temp_path.replace(state_path)


def _automation_model() -> settings.ModelDict:
    return settings.ModelConfig.get_model()


def _set_task_error(state: dict, task: str, message: str) -> None:
    state.setdefault("errors", {})[task] = (
        f"{datetime.datetime.now():%Y-%m-%d %H:%M} {message}"
    )


def _clear_task_error(state: dict, task: str) -> None:
    errors = state.get("errors", {})
    errors.pop(task, None)
    if not errors:
        state.pop("errors", None)


def _acquire_automation_lock() -> _AutomationLock | None:
    return _AutomationLock.acquire()


def _mark_lock_deferred(state: dict) -> None:
    state["last_deferred_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    state["deferred_reason"] = "会话已锁定；解锁后下一分钟内重新检查"
    _save_automation_state(state)


def _is_lock_deferral(message: str) -> bool:
    return "会话已锁定" in message or "锁屏" in message


def _run_daily_summaries(
    today: datetime.date, state: dict, model_config: settings.ModelDict
) -> None:
    yesterday = today - datetime.timedelta(days=1)
    last_daily_text = state.get("last_daily_date", "")
    if last_daily_text:
        try:
            current = datetime.date.fromisoformat(last_daily_text) + datetime.timedelta(days=1)
        except ValueError:
            current = yesterday
    else:
        current = yesterday
    while current <= yesterday:
        date_text = current.isoformat()
        path = settings.DIARY_DIR / f"{date_text}.md"
        if path.exists() and settings.CONFIG.get("automation", {}).get("daily_summary", True):
            message, success = summarize_diary(date_text, model_config)
        else:
            message, success = "", True
        if not success:
            if _is_lock_deferral(message):
                _mark_lock_deferred(state)
                break
            _set_task_error(
                state,
                "daily_summary",
                f"自动总结 {date_text} 失败: {message[:500]}",
            )
            _save_automation_state(state)
            break
        state["last_daily_date"] = date_text
        _clear_task_error(state, "daily_summary")
        _save_automation_state(state)
        current += datetime.timedelta(days=1)


def run_due_automatic_tasks() -> None:
    """执行到期的日总结、每日信息简报和闭合周期报告。"""
    automation = settings.CONFIG.get("automation", {})
    if not automation.get("enabled", True):
        return
    automation_lock = _acquire_automation_lock()
    if automation_lock is None:
        return
    state = _load_automation_state()
    state["last_check_started_at"] = datetime.datetime.now().isoformat(
        timespec="seconds"
    )
    _save_automation_state(state)
    try:
        if session_is_locked():
            _mark_lock_deferred(state)
            return
        with automatic_request_mode():
            model_config = _automation_model()
            _clear_task_error(state, "scheduler")
            state.pop("deferred_reason", None)
            now = datetime.datetime.now()
            today = now.date()
            if automation.get("daily_summary", True):
                _run_daily_summaries(today, state, model_config)

            if not session_is_locked() and automation.get("daily_information", True):
                _run_daily_information(now, state, model_config)
            if not session_is_locked() and automation.get("weekly_report", True):
                _run_weekly_reports(today, state, model_config, trigger="scheduled")
            if not session_is_locked() and automation.get("monthly_report", True):
                _run_monthly_reports(today, state, model_config, trigger="scheduled")
            if session_is_locked():
                _mark_lock_deferred(state)
    except Exception as error:
        state = _load_automation_state()
        _set_task_error(state, "scheduler", f"自动任务异常: {error}")
        _save_automation_state(state)
    finally:
        state = _load_automation_state()
        state["last_check_completed_at"] = datetime.datetime.now().isoformat(
            timespec="seconds"
        )
        _save_automation_state(state)
        automation_lock.release()


def _run_daily_information(
    now: datetime.datetime,
    state: dict,
    model_config: settings.ModelDict,
) -> None:
    time_text = str(
        settings.CONFIG.get("automation", {}).get("daily_information_time", "08:05")
    )
    try:
        scheduled_time = datetime.time.fromisoformat(time_text)
    except ValueError:
        scheduled_time = datetime.time(8, 5)
    last_date_text = state.get("last_information_date", "")
    if last_date_text:
        try:
            current = datetime.date.fromisoformat(last_date_text) + datetime.timedelta(
                days=1
            )
        except ValueError:
            current = now.date()
    else:
        current = now.date()
    last_due_date = (
        now.date()
        if now.time() >= scheduled_time
        else now.date() - datetime.timedelta(days=1)
    )
    while current <= last_due_date:
        date_text = current.isoformat()
        try:
            message, success, _ = generate_information_briefing(current, model_config)
        except Exception as error:
            _set_task_error(
                state,
                "daily_information",
                f"自动收集 {date_text} 信息异常: {error}",
            )
            _save_automation_state(state)
            break
        if not success:
            if _is_lock_deferral(message):
                _mark_lock_deferred(state)
                break
            _set_task_error(
                state,
                "daily_information",
                f"自动收集 {date_text} 信息失败: {message[:500]}",
            )
            _save_automation_state(state)
            break
        state["last_information_date"] = date_text
        _clear_task_error(state, "daily_information")
        _save_automation_state(state)
        current += datetime.timedelta(days=1)


def _run_weekly_reports(
    today: datetime.date,
    state: dict,
    model_config: settings.ModelDict,
    *,
    trigger: str = "scheduled",
) -> None:
    last_week_end = today - datetime.timedelta(days=today.weekday() + 1)
    state_week = state.get("last_week_end", "")
    if state_week:
        try:
            current_week_end = (
                datetime.datetime.strptime(state_week, "%Y-%m-%d").date()
                + datetime.timedelta(days=7)
            )
        except ValueError:
            current_week_end = last_week_end
    else:
        current_week_end = last_week_end

    while current_week_end <= last_week_end:
        current_week_start = current_week_end - datetime.timedelta(days=6)
        logs = _existing_logs(current_week_start, current_week_end)
        if logs:
            message, success, _ = generate_analysis_report(
                "weekly",
                current_week_start,
                model_config,
                origin="auto",
                trigger=trigger,
            )
        else:
            message = ""
            success = True
        if success:
            state["last_week_end"] = current_week_end.strftime("%Y-%m-%d")
            _clear_task_error(state, "weekly_report")
            _save_automation_state(state)
            current_week_end += datetime.timedelta(days=7)
        else:
            if _is_lock_deferral(message):
                _mark_lock_deferred(state)
                break
            _set_task_error(
                state,
                "weekly_report",
                f"自动生成截至 {current_week_end:%Y-%m-%d} 的周报失败: "
                f"{message[:500]}",
            )
            _save_automation_state(state)
            break


def _run_monthly_reports(
    today: datetime.date,
    state: dict,
    model_config: settings.ModelDict,
    *,
    trigger: str = "scheduled",
) -> None:
    this_month_start = today.replace(day=1)
    last_month_end = this_month_start - datetime.timedelta(days=1)
    state_month = state.get("last_month_end", "")
    if state_month:
        try:
            current_month_start = (
                datetime.date.fromisoformat(state_month) + datetime.timedelta(days=1)
            ).replace(day=1)
        except ValueError:
            current_month_start = last_month_end.replace(day=1)
    else:
        current_month_start = last_month_end.replace(day=1)

    while current_month_start <= last_month_end:
        next_month = (
            current_month_start.replace(day=28) + datetime.timedelta(days=4)
        ).replace(day=1)
        current_month_end = next_month - datetime.timedelta(days=1)
        logs = _existing_logs(current_month_start, current_month_end)
        if logs:
            message, success, _ = generate_analysis_report(
                "monthly",
                current_month_start,
                model_config,
                origin="auto",
                trigger=trigger,
            )
        else:
            message = ""
            success = True
        if success:
            state["last_month_end"] = current_month_end.strftime("%Y-%m-%d")
            _clear_task_error(state, "monthly_report")
            _save_automation_state(state)
            current_month_start = next_month
        else:
            if _is_lock_deferral(message):
                _mark_lock_deferred(state)
                break
            _set_task_error(
                state,
                "monthly_report",
                f"自动生成 {current_month_start:%Y-%m} 月报失败: {message[:500]}",
            )
            _save_automation_state(state)
            break


AUTOMATION_TASK_LABELS = {
    "daily_summary": "日总结",
    "daily_information": "每日信息简报",
    "weekly_report": "自动周报",
    "monthly_report": "自动月报",
}


def failed_automatic_tasks() -> list[tuple[str, str, str]]:
    """Return retryable failures as ``(task, label, error)`` tuples."""
    errors = _load_automation_state().get("errors", {})
    return [
        (task, AUTOMATION_TASK_LABELS[task], str(errors[task]))
        for task in AUTOMATION_TASK_LABELS
        if task in errors
    ]


def _retry_one_task(
    task: str,
    now: datetime.datetime,
    state: dict,
    model: settings.ModelDict,
) -> None:
    if task == "daily_summary":
        _run_daily_summaries(now.date(), state, model)
    elif task == "daily_information":
        _run_daily_information(now, state, model)
    elif task == "weekly_report":
        _run_weekly_reports(now.date(), state, model, trigger="retry")
    elif task == "monthly_report":
        _run_monthly_reports(now.date(), state, model, trigger="retry")


def retry_failed_automatic_tasks() -> tuple[str, bool]:
    """Retry every currently failed automatic task in an independent process."""
    automation_lock = _acquire_automation_lock()
    if automation_lock is None:
        return "另一个自动任务正在运行，请稍后重试。", False
    state = _load_automation_state()
    if session_is_locked():
        try:
            _mark_lock_deferred(state)
            return "会话已锁定，全部自动任务已延后。", False
        finally:
            automation_lock.release()
    tasks = [task for task in AUTOMATION_TASK_LABELS if task in state.get("errors", {})]
    if not tasks:
        automation_lock.release()
        return "当前没有失败的自动任务可重试。", True
    now = datetime.datetime.now()
    state["last_retry_started_at"] = now.isoformat(timespec="seconds")
    _save_automation_state(state)
    logger.info("automation_retry_started tasks=%s", ",".join(tasks))
    try:
        with automatic_request_mode():
            model = _automation_model()
            for task in tasks:
                if session_is_locked():
                    _mark_lock_deferred(state)
                    break
                _retry_one_task(task, now, state, model)
                state = _load_automation_state()
        state = _load_automation_state()
        remaining = [task for task in tasks if task in state.get("errors", {})]
        success = not remaining
        logger.info(
            "automation_retry_completed success=%s remaining=%s",
            success,
            ",".join(remaining),
        )
        if success:
            return "全部失败自动任务重试成功。", True
        labels = "、".join(AUTOMATION_TASK_LABELS[task] for task in remaining)
        return f"以下自动任务仍失败或被延后：{labels}", False
    except Exception as error:
        state = _load_automation_state()
        _set_task_error(
            state,
            "scheduler",
            f"后台重试全部自动任务异常: {error}",
        )
        _save_automation_state(state)
        logger.error(
            "automation_retry_failed error_type=%s",
            error.__class__.__name__,
        )
        return str(error), False
    finally:
        state = _load_automation_state()
        state["last_retry_completed_at"] = datetime.datetime.now().isoformat(
            timespec="seconds"
        )
        _save_automation_state(state)
        automation_lock.release()


def launch_automation_retry() -> tuple[bool, str]:
    """Start the all-failures retry in a detached process."""
    if not failed_automatic_tasks():
        return False, "当前没有失败的自动任务可重试。"
    command = _automation_command("--retry-automation")
    kwargs = {
        "cwd": str(settings.CONFIG_DIR),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if _is_windows():
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(command, **kwargs)
    except OSError as error:
        return False, f"启动后台重试进程失败: {error}"
    return True, "已在独立后台进程中重试全部失败自动任务。"


_CRON_MARKER = "# AgentRecord automation"
_WINDOWS_MINUTE_TASK_NAME = "AgentRecord Automation"
_WINDOWS_LOGON_TASK_NAME = "AgentRecord Automation Logon"


def _automation_command(action: str = "--run-automation") -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, action]
    return [sys.executable, str(settings.CONFIG_DIR / "main.py"), action]


def _is_windows() -> bool:
    return os.name == "nt"


def system_automation_status() -> tuple[bool, str]:
    """检查当前程序对应的系统自动任务是否完整安装。"""
    try:
        if _is_windows():
            results = [
                subprocess.run(
                    ["schtasks", "/Query", "/TN", task_name],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                for task_name in (
                    _WINDOWS_MINUTE_TASK_NAME,
                    _WINDOWS_LOGON_TASK_NAME,
                )
            ]
            installed_count = sum(result.returncode == 0 for result in results)
            if installed_count == len(results):
                return True, "系统自动任务已安装；解锁状态下每分钟检查。"
            if installed_count:
                return False, "系统自动任务安装不完整，请重新执行安装命令。"
            return False, "系统自动任务未安装；自动总结、信息简报和周期报告不会运行。"

        current = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=30
        )
        if current.returncode == 1:
            return False, "系统自动任务未安装；自动总结、信息简报和周期报告不会运行。"
        if current.returncode != 0:
            message = current.stderr.strip() or "无法读取当前 crontab。"
            return False, f"无法确认系统自动任务状态：{message}"
        lines = current.stdout.splitlines()
        has_startup = any(
            _CRON_MARKER in line and line.rstrip().endswith("startup")
            for line in lines
        )
        has_minute = any(
            _CRON_MARKER in line and line.rstrip().endswith("minute")
            for line in lines
        )
        if has_startup and has_minute:
            return True, "系统自动任务已安装；解锁状态下每分钟检查。"
        if has_startup or has_minute:
            return False, "系统自动任务安装不完整，请重新执行安装命令。"
        return False, "系统自动任务未安装；自动总结和周期报告不会运行。"
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"无法确认系统自动任务状态：{error}"


def automation_status_snapshot() -> dict:
    """汇总安装状态、最后检查、各类进度和当前失败。"""
    installed, install_message = system_automation_status()
    state = _load_automation_state()
    return {
        "installed": installed,
        "install_message": install_message,
        "last_check_started_at": state.get("last_check_started_at", ""),
        "last_check_completed_at": state.get("last_check_completed_at", ""),
        "last_retry_started_at": state.get("last_retry_started_at", ""),
        "last_retry_completed_at": state.get("last_retry_completed_at", ""),
        "last_deferred_at": state.get("last_deferred_at", ""),
        "deferred_reason": state.get("deferred_reason", ""),
        "last_daily_date": state.get("last_daily_date", ""),
        "last_information_date": state.get("last_information_date", ""),
        "last_week_end": state.get("last_week_end", ""),
        "last_month_end": state.get("last_month_end", ""),
        "errors": dict(state.get("errors", {})),
    }


def install_system_automation() -> tuple[bool, str]:
    """安装每分钟检查锁屏与到期任务的用户级系统任务。"""
    try:
        command = _automation_command()
        if _is_windows():
            task_command = subprocess.list2cmdline(command)
            schedules = (
                (_WINDOWS_MINUTE_TASK_NAME, "MINUTE"),
                (_WINDOWS_LOGON_TASK_NAME, "ONLOGON"),
            )
            for task_name, schedule in schedules:
                arguments = [
                    "schtasks",
                    "/Create",
                    "/TN",
                    task_name,
                    "/SC",
                    schedule,
                ]
                if schedule == "MINUTE":
                    arguments.extend(("/MO", "1"))
                arguments.extend(("/TR", task_command, "/F"))
                result = subprocess.run(
                    arguments,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    return (
                        False,
                        result.stderr.strip()
                        or result.stdout.strip()
                        or f"安装 {task_name} 失败。",
                    )
        else:
            current = subprocess.run(
                ["crontab", "-l"], capture_output=True, text=True, timeout=30
            )
            if current.returncode not in (0, 1):
                return False, current.stderr.strip() or "无法读取当前 crontab。"
            lines = [
                line
                for line in current.stdout.splitlines()
                if _CRON_MARKER not in line
            ]
            task_command = shlex.join(command)
            lines.extend(
                (
                    f"@reboot {task_command} {_CRON_MARKER} startup",
                    f"* * * * * {task_command} {_CRON_MARKER} minute",
                )
            )
            result = subprocess.run(
                ["crontab", "-"],
                input="\n".join(lines) + "\n",
                capture_output=True,
                text=True,
                timeout=30,
            )
        if result.returncode != 0:
            return False, result.stderr.strip() or result.stdout.strip() or "安装失败。"
        return True, "系统后台任务已安装：锁屏时延后，解锁后下一分钟内检查。"
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"安装系统后台任务失败: {error}"


def uninstall_system_automation() -> tuple[bool, str]:
    """卸载由 AgentRecord 创建的用户级系统任务。"""
    try:
        if _is_windows():
            results = [
                subprocess.run(
                    ["schtasks", "/Delete", "/TN", task_name, "/F"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                for task_name in (
                    _WINDOWS_MINUTE_TASK_NAME,
                    _WINDOWS_LOGON_TASK_NAME,
                )
            ]
            if all(result.returncode != 0 for result in results):
                result = results[0]
                return (
                    False,
                    result.stderr.strip()
                    or result.stdout.strip()
                    or "未找到可卸载的系统后台任务。",
                )
            result = next(
                result for result in results if result.returncode == 0
            )
        else:
            current = subprocess.run(
                ["crontab", "-l"], capture_output=True, text=True, timeout=30
            )
            if current.returncode == 1:
                return True, "系统后台任务未安装。"
            if current.returncode != 0:
                return False, current.stderr.strip() or "无法读取当前 crontab。"
            lines = [
                line
                for line in current.stdout.splitlines()
                if _CRON_MARKER not in line
            ]
            result = subprocess.run(
                ["crontab", "-"],
                input=("\n".join(lines) + "\n") if lines else "",
                capture_output=True,
                text=True,
                timeout=30,
            )
        if result.returncode != 0:
            return False, result.stderr.strip() or result.stdout.strip() or "卸载失败。"
        return True, "系统后台任务已卸载。"
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"卸载系统后台任务失败: {error}"
