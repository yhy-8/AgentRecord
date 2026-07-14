"""日记总结、分析报告和后台调度。

这是未来各类分析 Agent 的接入层。当前先集中承载有实际用途的总结、日报、
周报及编排逻辑；当 Extractor、Explorer、Cluster、World、Reviewer 等 Agent
拥有独立状态和足够实现后，再从本模块拆成 agents 包，避免空壳文件。
"""

import datetime
import json
import re
import threading
import time
from pathlib import Path

import journal
import settings
from ai_client import call_ai


def _log_without_summary(content: str) -> str:
    return re.sub(
        r"<summary>.*?</summary>",
        "<summary>（已省略）</summary>",
        content,
        count=1,
        flags=re.DOTALL,
    )


def summarize_diary(date: str, model_config: settings.ModelDict) -> tuple[str, bool]:
    """生成指定日期的日记总结，并写回原文件的 <summary> 区域。"""
    file_path = settings.DIARY_DIR / f"{date}.md"
    if not file_path.exists():
        return f"找不到 {date} 的记录。", False

    content = _log_without_summary(file_path.read_text(encoding="utf-8"))
    prompt = f"""[程序日记总结任务]
请总结 {date} 的日记。只输出要写入 <summary> 的 Markdown 正文，不要输出标题、标签、代码围栏或完成提示。

要求：
- 概括当天的重要事件、想法、决定、问题和进展，不要逐条复述流水账。
- 区分用户记录与 AI 回复；AI 回复只能作为咨询结果，不能当作用户已经认同的观点。
- 保留重要的具体信息，禁止编造。
- 内容为空或信息很少时如实简短说明。

【{date} 原始日记】
{content}"""
    summary, success, _, _, _ = call_ai(prompt, model_config)
    if not success:
        return summary, False

    result = journal.update_summary_for_date(date, summary)
    if not result.endswith("总结已写入文档顶部。"):
        return result, False
    return summary, True


def _date_span(start: datetime.date, end: datetime.date) -> list[str]:
    dates = []
    current = start
    while current <= end:
        dates.append(current.strftime("%Y-%m-%d"))
        current += datetime.timedelta(days=1)
    return dates


def _existing_logs(start: datetime.date, end: datetime.date) -> list[tuple[str, str]]:
    logs = []
    for date in _date_span(start, end):
        path = settings.DIARY_DIR / f"{date}.md"
        if path.exists():
            logs.append((date, _log_without_summary(path.read_text(encoding="utf-8"))))
    return logs


def _recent_summary_context(before: datetime.date, days: int = 30) -> str:
    start = before - datetime.timedelta(days=days)
    sections = []
    for date in _date_span(start, before - datetime.timedelta(days=1)):
        path = settings.DIARY_DIR / f"{date}.md"
        if not path.exists():
            continue
        summary = journal.extract_summary(path.read_text(encoding="utf-8"))
        if summary not in ("(无总结)", "暂无今日总结。"):
            sections.append(f"### {date}\n{summary}")
    return "\n\n".join(sections) or "（没有可用的历史总结）"


def _analysis_report_path(kind: str, start: datetime.date, end: datetime.date) -> Path:
    if kind == "daily":
        return settings.ANALYSIS_DIR / "Daily" / f"{start:%Y-%m-%d}.md"
    return (
        settings.ANALYSIS_DIR
        / "Weekly"
        / f"{start:%Y-%m-%d}_to_{end:%Y-%m-%d}.md"
    )


def generate_analysis_report(
    kind: str,
    anchor: datetime.date,
    model_config: settings.ModelDict,
) -> tuple[str, bool, Path | None]:
    """生成日分析报告或周分析报告，并保存到独立目录。"""
    if kind == "daily":
        start = end = anchor
        report_name = f"{anchor:%Y-%m-%d} 分析日报"
    elif kind == "weekly":
        start = anchor - datetime.timedelta(days=anchor.weekday())
        end = start + datetime.timedelta(days=6)
        report_name = f"{start:%Y-%m-%d} 至 {end:%Y-%m-%d} 分析周报"
    else:
        return f"未知报告类型: {kind}", False, None

    logs = _existing_logs(start, end)
    if not logs:
        return f"{start:%Y-%m-%d} 至 {end:%Y-%m-%d} 没有日记记录。", False, None

    period_logs = "\n\n---\n\n".join(
        f"# {date}\n{content}" for date, content in logs
    )
    history = _recent_summary_context(start)
    prompt = f"""[程序分析报告任务]
请基于给定原始日记和历史总结生成《{report_name}》。只输出报告 Markdown 正文，不要输出代码围栏或完成提示。

这不是日记内容摘要，而是个人思维智库的分析报告。请根据实际材料选择有价值的部分：
- 本周期重要的新想法、变化和注意力中心。
- 与历史思想之间少量但有依据的强关联。
- 重复出现的问题、观点演化、潜在矛盾和盲点。
- 必要时使用搜索能力核查外部事实或寻找反例；没有必要时不要为了丰富而搜索。
- 值得用户继续判断的少量问题。

要求：
- 明确区分用户表达、AI 回复、外部事实和你的推断。
- 关联历史思想时标注对应日期；弱关联不要写成确定结论。
- 不把每条记录都转成任务，不逐条复述，不编造。
- 输出一份可以脱离对话独立阅读的最终报告。

【本周期原始日记】
{period_logs}

【本周期之前 30 天的日记总结】
{history}"""
    report, success, _, _, _ = call_ai(prompt, model_config)
    if not success:
        return report, False, None

    report_path = _analysis_report_path(kind, start, end)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    title = f"# {report_name}\n\n"
    metadata = (
        f"> 生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M}\n"
        f"> 原始日记范围：{start:%Y-%m-%d} 至 {end:%Y-%m-%d}\n\n"
    )
    temp_path = report_path.with_suffix(report_path.suffix + ".tmp")
    temp_path.write_text(title + metadata + report.strip() + "\n", encoding="utf-8")
    temp_path.replace(report_path)
    return report, True, report_path


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


_AUTOMATION_LOCK = threading.Lock()


def _automation_model() -> settings.ModelDict:
    automation = settings.CONFIG.get("automation", {})
    model_name = automation.get("model")
    return (
        settings.ModelConfig.get_model(model_name)
        if model_name
        else settings.ModelConfig.get_model()
    )


def run_due_automatic_tasks() -> None:
    """补做截至昨天的自动总结/日报，并生成已结束自然周的周报。"""
    automation = settings.CONFIG.get("automation", {})
    if not automation.get("enabled", True):
        return
    if not _AUTOMATION_LOCK.acquire(blocking=False):
        return

    try:
        model_config = _automation_model()
        state = _load_automation_state()
        today = datetime.date.today()
        yesterday = today - datetime.timedelta(days=1)

        last_daily_text = state.get("last_daily_date", "")
        if last_daily_text:
            try:
                current = (
                    datetime.datetime.strptime(last_daily_text, "%Y-%m-%d").date()
                    + datetime.timedelta(days=1)
                )
            except ValueError:
                current = yesterday
        else:
            current = yesterday

        while current <= yesterday:
            date_text = current.strftime("%Y-%m-%d")
            path = settings.DIARY_DIR / f"{date_text}.md"
            progress = state.setdefault("daily_progress", {}).setdefault(date_text, {})
            task_ok = True

            if (
                path.exists()
                and automation.get("daily_summary", True)
                and not progress.get("summary")
            ):
                _, success = summarize_diary(date_text, model_config)
                task_ok = task_ok and success
                if success:
                    progress["summary"] = True
                    _save_automation_state(state)

            if (
                path.exists()
                and automation.get("daily_report", True)
                and not progress.get("report")
            ):
                _, success, _ = generate_analysis_report("daily", current, model_config)
                task_ok = task_ok and success
                if success:
                    progress["report"] = True
                    _save_automation_state(state)

            summary_done = (
                not path.exists()
                or not automation.get("daily_summary", True)
                or progress.get("summary")
            )
            report_done = (
                not path.exists()
                or not automation.get("daily_report", True)
                or progress.get("report")
            )
            if task_ok and summary_done and report_done:
                state["last_daily_date"] = date_text
                state.get("daily_progress", {}).pop(date_text, None)
                state.pop("last_error", None)
                _save_automation_state(state)
                current += datetime.timedelta(days=1)
            else:
                state["last_error"] = (
                    f"{datetime.datetime.now():%Y-%m-%d %H:%M} "
                    f"自动处理 {date_text} 失败"
                )
                _save_automation_state(state)
                break

        if automation.get("weekly_report", True):
            _run_weekly_reports(today, state, model_config)
    except Exception as error:
        state = _load_automation_state()
        state["last_error"] = (
            f"{datetime.datetime.now():%Y-%m-%d %H:%M} 自动任务异常: {error}"
        )
        _save_automation_state(state)
    finally:
        _AUTOMATION_LOCK.release()


def _run_weekly_reports(
    today: datetime.date,
    state: dict,
    model_config: settings.ModelDict,
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
            _, success, _ = generate_analysis_report(
                "weekly", current_week_start, model_config
            )
        else:
            success = True
        if success:
            state["last_week_end"] = current_week_end.strftime("%Y-%m-%d")
            state.pop("last_error", None)
            _save_automation_state(state)
            current_week_end += datetime.timedelta(days=7)
        else:
            state["last_error"] = (
                f"{datetime.datetime.now():%Y-%m-%d %H:%M} "
                f"自动生成截至 {current_week_end:%Y-%m-%d} 的周报失败"
            )
            _save_automation_state(state)
            break


def _automation_worker() -> None:
    while True:
        run_due_automatic_tasks()
        now = datetime.datetime.now()
        next_midnight = datetime.datetime.combine(
            now.date() + datetime.timedelta(days=1), datetime.time.min
        )
        seconds_to_midnight = max(60, (next_midnight - now).total_seconds() + 5)
        time.sleep(min(3600, seconds_to_midnight))


def start_automation_worker() -> None:
    if settings.CONFIG.get("automation", {}).get("enabled", True):
        threading.Thread(
            target=_automation_worker,
            name="agentrecord-automation",
            daemon=True,
        ).start()
