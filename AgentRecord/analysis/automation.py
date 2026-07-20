"""Automatic due-task execution and operating-system scheduler integration."""

import datetime
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

from .. import journal, settings
from ..ai_client import (
    is_config_failure,
    is_network_failure,
    is_rate_limit_failure,
)
from .context import (
    _analysis_report_path,
    _existing_logs,
    _information_briefings,
    _monthly_supporting_reports,
    _period_records,
    _recent_summary_context,
    _referenced_source_context,
)
from .information import (
    _prior_week_briefings,
    _week_record_context,
    generate_information_briefing,
    information_briefing_path,
)
from .orchestrator import (
    generate_analysis_report,
    generate_daily_profile,
    summarize_diary,
)
from .store import AnalysisStore


logger = logging.getLogger(__name__)


_MAX_AUTOMATIC_CONTENT_FAILURES = 2
_CONTENT_FAILURE_POLICY_VERSION = 2


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
    temp_path = settings.ANALYSIS_DIR / f".automation-state.{uuid.uuid4().hex}.tmp"
    temp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temp_path.replace(state_path)


def _automation_model() -> settings.ModelDict:
    return settings.ModelConfig.get_model()


def _next_hour(now: datetime.datetime) -> datetime.datetime:
    return now.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(
        hours=1
    )


def _content_failure_key(task: str, now: datetime.datetime) -> str:
    """Hash the effective task input without persisting private content or keys."""
    try:
        model = _automation_model()
        model_signature = {
            key: model.get(key)
            for key in (
                "name",
                "model_id",
                "api_url",
                "search",
                "json_mode",
                "max_tokens",
            )
        }
        third_search = settings.CONFIG.get("third_search", {})
        search_signature = {
            key: third_search.get(key)
            for key in ("enabled", "api_url", "count", "timeout", "max_rounds")
        }
        today = now.date()
        payload: dict = {
            "policy_version": _CONTENT_FAILURE_POLICY_VERSION,
            "task": task,
            "model": model_signature,
            "third_search": search_signature,
        }
        if task == "daily_summary":
            date = today - datetime.timedelta(days=1)
            path = settings.DIARY_DIR / f"{date.isoformat()}.md"
            payload.update(
                target=date.isoformat(),
                diary=path.read_text(encoding="utf-8") if path.is_file() else "",
            )
        elif task == "daily_profile":
            date = today - datetime.timedelta(days=1)
            path = settings.DIARY_DIR / f"{date.isoformat()}.md"
            payload.update(
                target=date.isoformat(),
                diary=path.read_text(encoding="utf-8") if path.is_file() else "",
            )
        elif task == "daily_information":
            week_start = today - datetime.timedelta(days=today.weekday())
            prior_briefings, prior_queries = _prior_week_briefings(today)
            payload.update(
                target=today.isoformat(),
                week_start=week_start.isoformat(),
                records=_week_record_context(today),
                prior_briefings=prior_briefings,
                prior_queries=prior_queries,
            )
        elif task in {"weekly_report", "monthly_report"}:
            if task == "weekly_report":
                start, end = _latest_week_period(today)
                supporting_reports = "（周报不读取下级周期报告）"
            else:
                start, end = _latest_month_period(today)
                supporting_reports = _monthly_supporting_reports(start, end)
            logs = _existing_logs(start, end)
            payload.update(
                period={"start": start.isoformat(), "end": end.isoformat()},
                logs=logs,
                referenced_sources=_referenced_source_context(logs),
                recent_summaries=_recent_summary_context(start),
                supporting_reports=supporting_reports,
                information_leads=_information_briefings(start, end),
            )
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
    except (OSError, RuntimeError, KeyError, TypeError, ValueError):
        return ""


def _set_task_error(state: dict, task: str, message: str) -> None:
    now = datetime.datetime.now()
    state.setdefault("errors", {})[task] = f"{now:%Y-%m-%d %H:%M} {message}"
    if task in AUTOMATION_TASK_LABELS:
        if is_config_failure(message):
            state.setdefault("retry_kind", {})[task] = "blocked"
            retry_after = state.get("retry_after", {})
            retry_after.pop(task, None)
            if not retry_after:
                state.pop("retry_after", None)
        else:
            network_error = is_network_failure(message)
            rate_limited = is_rate_limit_failure(message)
            retry_at = (
                now + datetime.timedelta(minutes=5)
                if network_error or rate_limited
                else _next_hour(now)
            )
            state.setdefault("retry_after", {})[task] = retry_at.isoformat(
                timespec="seconds"
            )
            if network_error:
                retry_kind = "network"
            elif rate_limited:
                retry_kind = "rate_limit"
            else:
                failure_key = _content_failure_key(task, now)
                previous_key = state.get("failure_keys", {}).get(task)
                try:
                    previous_count = int(
                        state.get("failure_counts", {}).get(task, 0)
                    )
                except (TypeError, ValueError):
                    previous_count = 0
                failure_count = (
                    previous_count + 1
                    if failure_key and failure_key == previous_key
                    else 1
                )
                state.setdefault("failure_counts", {})[task] = failure_count
                if failure_key:
                    state.setdefault("failure_keys", {})[task] = failure_key
                if failure_count >= _MAX_AUTOMATIC_CONTENT_FAILURES:
                    retry_kind = "content_blocked"
                    state.get("retry_after", {}).pop(task, None)
                    if not state.get("retry_after"):
                        state.pop("retry_after", None)
                else:
                    retry_kind = "hourly"
            state.setdefault("retry_kind", {})[task] = retry_kind


def _clear_task_error(state: dict, task: str) -> None:
    errors = state.get("errors", {})
    errors.pop(task, None)
    if not errors:
        state.pop("errors", None)
    retry_after = state.get("retry_after", {})
    retry_after.pop(task, None)
    if not retry_after:
        state.pop("retry_after", None)
    retry_kind = state.get("retry_kind", {})
    retry_kind.pop(task, None)
    if not retry_kind:
        state.pop("retry_kind", None)
    failure_counts = state.get("failure_counts", {})
    failure_counts.pop(task, None)
    if not failure_counts:
        state.pop("failure_counts", None)
    failure_keys = state.get("failure_keys", {})
    failure_keys.pop(task, None)
    if not failure_keys:
        state.pop("failure_keys", None)


def _acquire_automation_lock() -> _AutomationLock | None:
    return _AutomationLock.acquire()


def _remove_legacy_progress(state: dict) -> None:
    for key in (
        "last_daily_date",
        "last_information_date",
        "last_week_end",
        "last_month_end",
        "last_deferred_at",
        "deferred_reason",
    ):
        state.pop(key, None)


def _diary_summary_needs_generation(path: Path) -> bool:
    try:
        summary = journal.extract_summary(path.read_text(encoding="utf-8")).strip()
    except OSError:
        return False
    return summary in {"", "(无总结)", "暂无今日总结。"}


def _set_current_task(state: dict, task: str, detail: str) -> None:
    state["current_task"] = task
    state["current_task_detail"] = detail
    state["current_task_started_at"] = datetime.datetime.now().isoformat(
        timespec="seconds"
    )
    _save_automation_state(state)


def _failure_retry_is_due(
    state: dict, task: str, now: datetime.datetime
) -> bool:
    if task not in state.get("errors", {}):
        return True
    if state.get("retry_kind", {}).get(task) in {"blocked", "content_blocked"}:
        return False
    retry_text = str(state.get("retry_after", {}).get(task, ""))
    if retry_text:
        try:
            return now >= datetime.datetime.fromisoformat(retry_text)
        except ValueError:
            return False

    error_text = str(state.get("errors", {}).get(task, ""))
    try:
        failed_at = datetime.datetime.strptime(error_text[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return False
    return now >= _next_hour(failed_at)


def _hour_key(now: datetime.datetime) -> str:
    return now.strftime("%Y-%m-%dT%H")


def _daily_information_scheduled_time() -> datetime.time:
    time_text = str(
        settings.CONFIG.get("automation", {}).get("daily_information_time", "08:05")
    )
    try:
        return datetime.time.fromisoformat(time_text)
    except ValueError:
        return datetime.time(8, 5)


def _latest_week_period(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    end = today - datetime.timedelta(days=today.weekday() + 1)
    return end - datetime.timedelta(days=6), end


def _latest_month_period(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    end = today.replace(day=1) - datetime.timedelta(days=1)
    return end.replace(day=1), end


def _task_missing(task: str, now: datetime.datetime) -> bool:
    today = now.date()
    if task == "daily_summary":
        yesterday = today - datetime.timedelta(days=1)
        path = settings.DIARY_DIR / f"{yesterday.isoformat()}.md"
        return path.exists() and _diary_summary_needs_generation(path)
    if task == "daily_profile":
        yesterday = today - datetime.timedelta(days=1)
        logs = _existing_logs(yesterday, yesterday)
        if not _period_records(logs):
            return False
        return not AnalysisStore.has_completed_run(
            "daily_profile",
            yesterday.isoformat(),
            yesterday.isoformat(),
        )
    if task == "daily_information":
        if now.time() < _daily_information_scheduled_time():
            return False
        return not information_briefing_path(today).exists()
    if task == "weekly_report":
        start, end = _latest_week_period(today)
        path = _analysis_report_path("weekly", start, end, "auto")
        return bool(_existing_logs(start, end)) and not path.exists()
    if task == "monthly_report":
        start, end = _latest_month_period(today)
        path = _analysis_report_path("monthly", start, end, "auto")
        return bool(_existing_logs(start, end)) and not path.exists()
    return False


def _task_artifact_status(task: str, now: datetime.datetime) -> str:
    today = now.date()
    if task == "daily_summary":
        yesterday = today - datetime.timedelta(days=1)
        path = settings.DIARY_DIR / f"{yesterday.isoformat()}.md"
        if not path.exists():
            return f"{yesterday} 无日记"
        return f"{yesterday} {'缺失' if _diary_summary_needs_generation(path) else '已存在'}"
    if task == "daily_profile":
        yesterday = today - datetime.timedelta(days=1)
        logs = _existing_logs(yesterday, yesterday)
        if not _period_records(logs):
            return f"{yesterday} 无日记"
        return f"{yesterday} {'缺失' if _task_missing(task, now) else '已更新'}"
    if task == "daily_information":
        if now.time() < _daily_information_scheduled_time():
            return f"{today} 未到生成时间"
        return f"{today} {'缺失' if _task_missing(task, now) else '已存在'}"
    if task == "weekly_report":
        start, end = _latest_week_period(today)
        if not _existing_logs(start, end):
            return f"{start} 至 {end} 无记录"
        return f"{start} 至 {end} {'缺失' if _task_missing(task, now) else '已存在'}"
    if task == "monthly_report":
        start, end = _latest_month_period(today)
        if not _existing_logs(start, end):
            return f"{start:%Y-%m} 无记录"
        return f"{start:%Y-%m} {'缺失' if _task_missing(task, now) else '已存在'}"
    return "未知"


def _task_should_run(
    state: dict,
    task: str,
    now: datetime.datetime,
    *,
    initial_detection_due: bool,
) -> bool:
    if task in state.get("errors", {}):
        if state.get("retry_kind", {}).get(task) == "content_blocked":
            previous_key = str(state.get("failure_keys", {}).get(task, ""))
            current_key = _content_failure_key(task, now)
            if not current_key or current_key == previous_key:
                return False
            _clear_task_error(state, task)
            if not initial_detection_due:
                return False
        if not _failure_retry_is_due(state, task, now):
            return False
    elif not initial_detection_due:
        return False
    if _task_missing(task, now):
        return True
    _clear_task_error(state, task)
    return False


def _run_daily_summaries(
    today: datetime.date, state: dict, model_config: settings.ModelDict
) -> None:
    yesterday = today - datetime.timedelta(days=1)
    date_text = yesterday.isoformat()
    path = settings.DIARY_DIR / f"{date_text}.md"
    if not path.exists() or not _diary_summary_needs_generation(path):
        _clear_task_error(state, "daily_summary")
        _save_automation_state(state)
        return
    _set_current_task(state, "daily_summary", f"正在总结 {date_text} 日记")
    message, success = summarize_diary(date_text, model_config)
    if success:
        _clear_task_error(state, "daily_summary")
    else:
        _set_task_error(
            state,
            "daily_summary",
            f"自动总结 {date_text} 失败: {message[:500]}",
        )
    _save_automation_state(state)


def run_due_automatic_tasks() -> None:
    """执行到期的日总结、每日信息简报和闭合周期报告。"""
    automation = settings.CONFIG.get("automation", {})
    if not automation.get("enabled", True):
        return
    automation_lock = _acquire_automation_lock()
    if automation_lock is None:
        return
    state = _load_automation_state()
    _remove_legacy_progress(state)
    now = datetime.datetime.now()
    state["last_check_started_at"] = now.isoformat(timespec="seconds")
    _save_automation_state(state)
    try:
        hourly_detection_due = state.get("last_detection_hour") != _hour_key(now)
        daily_information_due = (
            now.time() >= _daily_information_scheduled_time()
            and _task_missing("daily_information", now)
        )
        due_tasks = []
        task_settings = (
            ("daily_summary", "daily_summary", hourly_detection_due),
            ("daily_profile", "daily_profile", hourly_detection_due),
            (
                "daily_information",
                "daily_information",
                hourly_detection_due or daily_information_due,
            ),
            ("weekly_report", "weekly_report", hourly_detection_due),
            ("monthly_report", "monthly_report", hourly_detection_due),
        )
        for task, config_key, initial_due in task_settings:
            if not automation.get(config_key, True):
                _clear_task_error(state, task)
                continue
            if _task_should_run(
                state, task, now, initial_detection_due=initial_due
            ):
                due_tasks.append(task)
            elif task in state.get("errors", {}):
                # A recorded predecessor failure is a dependency barrier even
                # while its own retry deadline has not arrived.
                break
        _save_automation_state(state)

        if due_tasks:
            model_config = _automation_model()
            today = now.date()
            halt = False
            if "daily_summary" in due_tasks and not halt:
                _run_daily_summaries(today, state, model_config)
                halt = "daily_summary" in state.get("errors", {})
            if "daily_profile" in due_tasks and not halt:
                trigger = (
                    "retry"
                    if "daily_profile" in state.get("errors", {})
                    else "scheduled"
                )
                _run_daily_profile(
                    today, state, model_config, trigger=trigger
                )
                halt = "daily_profile" in state.get("errors", {})
            if "daily_information" in due_tasks and not halt:
                _run_daily_information(now, state, model_config)
                halt = "daily_information" in state.get("errors", {})
            if "weekly_report" in due_tasks and not halt:
                trigger = (
                    "retry"
                    if "weekly_report" in state.get("errors", {})
                    else "scheduled"
                )
                _run_weekly_reports(today, state, model_config, trigger=trigger)
                halt = "weekly_report" in state.get("errors", {})
            if "monthly_report" in due_tasks and not halt:
                trigger = (
                    "retry"
                    if "monthly_report" in state.get("errors", {})
                    else "scheduled"
                )
                _run_monthly_reports(today, state, model_config, trigger=trigger)
        if hourly_detection_due:
            # This is only a scheduler watermark. Writing it after the work means
            # a killed process is detected again on the next minute invocation.
            state["last_detection_hour"] = _hour_key(now)
        _clear_task_error(state, "scheduler")
        _save_automation_state(state)
    except Exception as error:
        state = _load_automation_state()
        _set_task_error(state, "scheduler", f"自动任务异常: {error}")
        _save_automation_state(state)
    finally:
        state = _load_automation_state()
        state.pop("current_task", None)
        state.pop("current_task_detail", None)
        state.pop("current_task_started_at", None)
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
    date = now.date()
    date_text = date.isoformat()
    if now.time() < _daily_information_scheduled_time() or information_briefing_path(
        date
    ).exists():
        _clear_task_error(state, "daily_information")
        _save_automation_state(state)
        return
    try:
        _set_current_task(state, "daily_information", f"正在收集 {date_text} 信息")
        message, success, _ = generate_information_briefing(date, model_config)
    except Exception as error:
        message, success = f"接口异常: {error}", False
    if success:
        _clear_task_error(state, "daily_information")
    else:
        _set_task_error(
            state,
            "daily_information",
            f"自动收集 {date_text} 信息失败: {message[:500]}",
        )
    _save_automation_state(state)


def _run_daily_profile(
    today: datetime.date,
    state: dict,
    model_config: settings.ModelDict,
    *,
    trigger: str = "scheduled",
) -> None:
    date = today - datetime.timedelta(days=1)
    date_text = date.isoformat()
    logs = _existing_logs(date, date)
    completed = AnalysisStore.has_completed_run(
        "daily_profile", date_text, date_text
    )
    if not _period_records(logs) or completed:
        _clear_task_error(state, "daily_profile")
        _save_automation_state(state)
        return
    try:
        _set_current_task(
            state, "daily_profile", f"正在更新 {date_text} 人物画像"
        )
        message, success = generate_daily_profile(
            date, model_config, trigger=trigger
        )
    except Exception as error:
        message, success = f"接口异常: {error}", False
    if success:
        _clear_task_error(state, "daily_profile")
    else:
        _set_task_error(
            state,
            "daily_profile",
            f"自动更新 {date_text} 人物画像失败: {message[:500]}",
        )
    _save_automation_state(state)


def _run_weekly_reports(
    today: datetime.date,
    state: dict,
    model_config: settings.ModelDict,
    *,
    trigger: str = "scheduled",
) -> None:
    start, end = _latest_week_period(today)
    path = _analysis_report_path("weekly", start, end, "auto")
    if not _existing_logs(start, end) or path.exists():
        _clear_task_error(state, "weekly_report")
        _save_automation_state(state)
        return
    _set_current_task(
        state,
        "weekly_report",
        f"正在生成 {start:%Y-%m-%d} 至 {end:%Y-%m-%d} 自动周报",
    )
    message, success, _ = generate_analysis_report(
        "weekly", start, model_config, origin="auto", trigger=trigger
    )
    if success:
        _clear_task_error(state, "weekly_report")
    else:
        _set_task_error(
            state,
            "weekly_report",
            f"自动生成截至 {end:%Y-%m-%d} 的周报失败: {message[:500]}",
        )
    _save_automation_state(state)


def _run_monthly_reports(
    today: datetime.date,
    state: dict,
    model_config: settings.ModelDict,
    *,
    trigger: str = "scheduled",
) -> None:
    start, end = _latest_month_period(today)
    path = _analysis_report_path("monthly", start, end, "auto")
    if not _existing_logs(start, end) or path.exists():
        _clear_task_error(state, "monthly_report")
        _save_automation_state(state)
        return
    _set_current_task(state, "monthly_report", f"正在生成 {start:%Y-%m} 自动月报")
    message, success, _ = generate_analysis_report(
        "monthly", start, model_config, origin="auto", trigger=trigger
    )
    if success:
        _clear_task_error(state, "monthly_report")
    else:
        _set_task_error(
            state,
            "monthly_report",
            f"自动生成 {start:%Y-%m} 月报失败: {message[:500]}",
        )
    _save_automation_state(state)


AUTOMATION_TASK_LABELS = {
    "daily_summary": "日总结",
    "daily_profile": "每日人物画像",
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
    elif task == "daily_profile":
        _run_daily_profile(now.date(), state, model, trigger="retry")
    elif task == "daily_information":
        _run_daily_information(now, state, model)
    elif task == "weekly_report":
        _run_weekly_reports(now.date(), state, model, trigger="retry")
    elif task == "monthly_report":
        _run_monthly_reports(now.date(), state, model, trigger="retry")


def retry_failed_automatic_tasks() -> tuple[str, bool]:
    """Retry current failures in dependency order until one still fails."""
    automation_lock = _acquire_automation_lock()
    if automation_lock is None:
        return "另一个自动任务正在运行，请稍后重试。", False
    state = _load_automation_state()
    tasks = [task for task in AUTOMATION_TASK_LABELS if task in state.get("errors", {})]
    if not tasks:
        automation_lock.release()
        return "当前没有失败的自动任务可重试。", True
    now = datetime.datetime.now()
    state["last_retry_started_at"] = now.isoformat(timespec="seconds")
    _save_automation_state(state)
    logger.info("automation_retry_started tasks=%s", ",".join(tasks))
    try:
        model = _automation_model()
        for task in tasks:
            _retry_one_task(task, now, state, model)
            state = _load_automation_state()
            if task in state.get("errors", {}):
                break
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
        return f"以下自动任务仍失败：{labels}", False
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
        state.pop("current_task", None)
        state.pop("current_task_detail", None)
        state.pop("current_task_started_at", None)
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
    return True, "已在独立后台进程中按依赖顺序重试失败自动任务。"


_CRON_MARKER = "# AgentRecord automation"
_WINDOWS_MINUTE_TASK_NAME = "AgentRecord Automation"
_WINDOWS_LOGON_TASK_NAME = "AgentRecord Automation Logon"
_WINDOWS_BACKGROUND_EXECUTABLE = "AgentRecordBackground.exe"


def _automation_command(action: str = "--run-automation") -> list[str]:
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
        if _is_windows():
            background = executable.with_name(_WINDOWS_BACKGROUND_EXECUTABLE)
            if background.is_file():
                executable = background
        return [str(executable), action]
    executable = Path(sys.executable)
    if _is_windows():
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.is_file():
            executable = pythonw
    return [str(executable), str(settings.CONFIG_DIR / "main.py"), action]


def _is_windows() -> bool:
    return os.name == "nt"


def system_automation_status() -> tuple[bool, str]:
    """检查当前程序对应的系统自动任务是否完整安装。"""
    try:
        if _is_windows():
            results = [
                subprocess.run(
                    ["schtasks", "/Query", "/TN", task_name, "/XML"],
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
                commands = [
                    re_match.group(1).strip()
                    for result in results
                    if (
                        re_match := re.search(
                            r"<(?:\w+:)?Command>(.*?)</(?:\w+:)?Command>",
                            result.stdout,
                            re.IGNORECASE | re.DOTALL,
                        )
                    )
                ]
                allowed_names = {
                    _WINDOWS_BACKGROUND_EXECUTABLE.casefold(),
                    "pythonw.exe",
                }
                if len(commands) != len(results) or any(
                    Path(command).name.casefold() not in allowed_names
                    for command in commands
                ):
                    return False, "系统自动任务仍指向有窗口的旧入口，请重新安装。"
                return True, "系统自动任务已安装；每分钟唤醒调度器检查到期任务。"
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
            return True, "系统自动任务已安装；每分钟唤醒调度器检查到期任务。"
        if has_startup or has_minute:
            return False, "系统自动任务安装不完整，请重新执行安装命令。"
        return False, "系统自动任务未安装；自动总结、信息简报和周期报告不会运行。"
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"无法确认系统自动任务状态：{error}"


def automation_status_snapshot() -> dict:
    """汇总安装状态、真实产物状态、调度时间和当前失败。"""
    installed, install_message = system_automation_status()
    state = _load_automation_state()
    now = datetime.datetime.now()
    return {
        "installed": installed,
        "install_message": install_message,
        "last_check_started_at": state.get("last_check_started_at", ""),
        "last_check_completed_at": state.get("last_check_completed_at", ""),
        "last_retry_started_at": state.get("last_retry_started_at", ""),
        "last_retry_completed_at": state.get("last_retry_completed_at", ""),
        "current_task": state.get("current_task", ""),
        "current_task_detail": state.get("current_task_detail", ""),
        "current_task_started_at": state.get("current_task_started_at", ""),
        "daily_summary_status": _task_artifact_status("daily_summary", now),
        "daily_profile_status": _task_artifact_status("daily_profile", now),
        "daily_information_status": _task_artifact_status("daily_information", now),
        "weekly_report_status": _task_artifact_status("weekly_report", now),
        "monthly_report_status": _task_artifact_status("monthly_report", now),
        "last_detection_hour": state.get("last_detection_hour", ""),
        "retry_after": dict(state.get("retry_after", {})),
        "retry_kind": dict(state.get("retry_kind", {})),
        "failure_counts": dict(state.get("failure_counts", {})),
        "errors": dict(state.get("errors", {})),
    }


def install_system_automation() -> tuple[bool, str]:
    """安装每分钟唤醒调度器的用户级系统任务。"""
    try:
        command = _automation_command()
        if _is_windows():
            if Path(command[0]).name.casefold() not in {
                _WINDOWS_BACKGROUND_EXECUTABLE.casefold(),
                "pythonw.exe",
            }:
                return (
                    False,
                    f"缺少无窗口后台入口 {_WINDOWS_BACKGROUND_EXECUTABLE}，"
                    "请将它与 AgentRecord.exe 放在同一目录后重试。",
                )
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
        return True, "系统后台任务已安装：每分钟唤醒，并在重启后补检到期任务。"
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"安装系统后台任务失败: {error}"


def uninstall_system_automation() -> tuple[bool, str]:
    """卸载由 AgentRecord 创建的用户级系统任务。"""
    try:
        if _is_windows():
            delete_results = [
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
            remaining = [
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
            if any(result.returncode == 0 for result in remaining):
                result = next(
                    (result for result in delete_results if result.returncode != 0),
                    next(result for result in remaining if result.returncode == 0),
                )
                return (
                    False,
                    result.stderr.strip()
                    or result.stdout.strip()
                    or "系统后台任务未能完全卸载。",
                )
            result = subprocess.CompletedProcess([], 0, "", "")
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
