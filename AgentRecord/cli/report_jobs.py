"""Single-window background execution for manually requested reports."""

import datetime
import logging
import threading
from collections.abc import Callable
from pathlib import Path

from .. import settings


GenerateReport = Callable[
    [str, datetime.date, settings.ModelDict],
    tuple[str, bool, Path | None],
]
Notify = Callable[[str, str | None], None]

logger = logging.getLogger(__name__)


class ManualReportJobs:
    """Own at most one manual report thread for one interactive window."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._label = ""

    def start(
        self,
        kind: str,
        anchor: datetime.date,
        model_config: settings.ModelDict,
        generate_report: GenerateReport,
        notify: Notify,
    ) -> bool:
        label = {"weekly": "分析周报", "monthly": "分析月报"}[kind]
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            thread = threading.Thread(
                target=self._run,
                args=(kind, anchor, dict(model_config), label, generate_report, notify),
                name=f"AgentRecord-{kind}-report",
                daemon=False,
            )
            self._thread = thread
            self._label = label
            thread.start()
        return True

    def _run(
        self,
        kind: str,
        anchor: datetime.date,
        model_config: settings.ModelDict,
        label: str,
        generate_report: GenerateReport,
        notify: Notify,
    ) -> None:
        logger.info("manual_report_started kind=%s period_anchor=%s", kind, anchor)
        notification = f"{label}后台任务异常。"
        style = "red"
        try:
            message, success, report_path = generate_report(
                kind, anchor, model_config, origin="manual"
            )
            if success:
                notification = f"{label}已完成，报告已保存：{report_path}"
                style = "green"
                logger.info("manual_report_completed kind=%s", kind)
            else:
                notification = f"{label}生成失败：{message}"
                style = "red"
                logger.warning("manual_report_failed kind=%s", kind)
        except Exception as error:
            notification = f"{label}后台任务异常：{error.__class__.__name__}"
            style = "red"
            logger.error(
                "manual_report_crashed kind=%s error_type=%s",
                kind,
                error.__class__.__name__,
            )
        finally:
            try:
                notify(notification, style)
            finally:
                with self._lock:
                    self._thread = None
                    self._label = ""

    def running_label(self) -> str:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return ""
            return self._label

    def wait(self, timeout: float | None = None) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)


manual_report_jobs = ManualReportJobs()
