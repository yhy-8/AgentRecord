"""日记总结、分析报告和后台调度。

这是未来各类分析 Agent 的接入层。当前先集中承载有实际用途的总结、日报、
周报、月报及编排逻辑；当 Extractor、Explorer、Cluster、World、Reviewer 等 Agent
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


def _referenced_source_context(logs: list[tuple[str, str]]) -> str:
    """读取本周期标准引用指向的 Markdown；拒绝日记和报告目录以外的路径。"""
    reference_pattern = re.compile(
        r"^\*\*\d{2}:\d{2} \[引用\]:\*\* \[[^\]]+\]\(<([^>]+)>\)",
        re.MULTILINE,
    )
    allowed_roots = (settings.DIARY_DIR.resolve(), settings.ANALYSIS_DIR.resolve())
    seen = set()
    sections = []

    for _, content in logs:
        for relative_path in reference_pattern.findall(content):
            source_path = (settings.DIARY_DIR / relative_path).resolve()
            if source_path in seen or source_path.suffix.lower() != ".md":
                continue
            if not any(source_path.is_relative_to(root) for root in allowed_roots):
                continue
            if not source_path.is_file():
                continue
            try:
                source_content = source_path.read_text(encoding="utf-8")
            except OSError:
                continue
            seen.add(source_path)
            sections.append(f"### {source_path.name}\n{source_content[:12000]}")
            if len(sections) == 10:
                return "\n\n".join(sections)
    return "\n\n".join(sections) or "（本周期没有可读取的显式引用来源）"


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
    if kind == "weekly":
        return (
            settings.ANALYSIS_DIR
            / "Weekly"
            / f"{start:%Y-%m-%d}_to_{end:%Y-%m-%d}.md"
        )
    return settings.ANALYSIS_DIR / "Monthly" / f"{start:%Y-%m}.md"


def _monthly_supporting_reports(start: datetime.date, end: datetime.date) -> str:
    """为月报读取与该月相交的周报，作为已完成分析而非用户原始观点。"""
    sections = []
    weekly_dir = settings.ANALYSIS_DIR / "Weekly"
    for path in sorted(weekly_dir.glob("*.md")):
        match = re.fullmatch(
            r"(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})", path.stem
        )
        if not match:
            continue
        try:
            report_start = datetime.date.fromisoformat(match.group(1))
            report_end = datetime.date.fromisoformat(match.group(2))
        except ValueError:
            continue
        if report_end < start or report_start > end:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        sections.append(f"### {path.name}\n{content[:12000]}")
    return "\n\n".join(sections) or "（没有可用的同期周报）"


def generate_analysis_report(
    kind: str,
    anchor: datetime.date,
    model_config: settings.ModelDict,
) -> tuple[str, bool, Path | None]:
    """生成日、周或月分析报告，并保存到独立目录。"""
    if kind == "daily":
        start = end = anchor
        report_name = f"{anchor:%Y-%m-%d} 分析日报"
    elif kind == "weekly":
        start = anchor - datetime.timedelta(days=anchor.weekday())
        end = start + datetime.timedelta(days=6)
        report_name = f"{start:%Y-%m-%d} 至 {end:%Y-%m-%d} 分析周报"
    elif kind == "monthly":
        start = anchor.replace(day=1)
        next_month = (start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        end = next_month - datetime.timedelta(days=1)
        report_name = f"{start:%Y年%m月} 分析月报"
    else:
        return f"未知报告类型: {kind}", False, None

    logs = _existing_logs(start, end)
    if not logs:
        return f"{start:%Y-%m-%d} 至 {end:%Y-%m-%d} 没有日记记录。", False, None

    period_logs = "\n\n---\n\n".join(
        f"# {date}\n{content}" for date, content in logs
    )
    referenced_sources = _referenced_source_context(logs)
    history = _recent_summary_context(start)
    period_focus = {
        "daily": "关注当天新出现的内容和变化，不必强行上升为长期结论。",
        "weekly": "关注一周内主题的聚合、推进、反复、转变及仍未解决的问题。",
        "monthly": "从更高层观察注意力分配、长期主题演化、判断得到的支持或挑战、反复模式，以及下个月最值得继续探索的少量方向。",
    }[kind]
    supporting_reports = (
        _monthly_supporting_reports(start, end)
        if kind == "monthly"
        else "（本报告不使用下级周期报告）"
    )
    prompt = f"""[程序分析报告任务]
请基于给定原始日记和历史总结生成《{report_name}》。只输出报告 Markdown 正文，不要输出代码围栏或完成提示。

这不是日记内容摘要，而是个人思维智库的分析报告。请根据实际材料选择有价值的部分：
- 本报告的周期侧重点：{period_focus}
- 本周期重要的新想法、变化和注意力中心。
- 与历史思想之间少量但有依据的强关联。
- 重复出现的问题、观点演化、潜在矛盾和盲点。
- 必要时使用搜索能力核查外部事实或寻找反例；没有必要时不要为了丰富而搜索。
- 值得用户继续判断的少量问题。

要求：
- 明确区分用户表达、AI 回复、外部事实和你的推断。
- 关联历史思想时标注对应日期；弱关联不要写成确定结论。
- `[引用]` 后的文字是用户由该来源展开的新记录；引用来源正文只是上下文，不能当作用户已经认同其中全部内容。
- 不把每条记录都转成任务，不逐条复述，不编造。
- 输出一份可以脱离对话独立阅读的最终报告。

【本周期原始日记】
{period_logs}

【本周期显式引用的来源】
{referenced_sources}

【同期下级分析报告】
{supporting_reports}

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


def _set_task_error(state: dict, task: str, message: str) -> None:
    state.setdefault("errors", {})[task] = (
        f"{datetime.datetime.now():%Y-%m-%d %H:%M} {message}"
    )


def _clear_task_error(state: dict, task: str) -> None:
    errors = state.get("errors", {})
    errors.pop(task, None)
    if not errors:
        state.pop("errors", None)


def run_due_automatic_tasks() -> None:
    """补做日总结，并生成已经闭合的自然周周报和自然月月报。"""
    automation = settings.CONFIG.get("automation", {})
    if not automation.get("enabled", True):
        return
    if not _AUTOMATION_LOCK.acquire(blocking=False):
        return

    try:
        model_config = _automation_model()
        state = _load_automation_state()
        state.pop("last_error", None)
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
            if path.exists() and automation.get("daily_summary", True):
                _, success = summarize_diary(date_text, model_config)
            else:
                success = True
            if success:
                state["last_daily_date"] = date_text
                state.get("daily_progress", {}).pop(date_text, None)
                if not state.get("daily_progress"):
                    state.pop("daily_progress", None)
                _clear_task_error(state, "daily_summary")
                _save_automation_state(state)
                current += datetime.timedelta(days=1)
            else:
                _set_task_error(state, "daily_summary", f"自动总结 {date_text} 失败")
                _save_automation_state(state)
                break

        if automation.get("weekly_report", True):
            _run_weekly_reports(today, state, model_config)
        if automation.get("monthly_report", True):
            _run_monthly_reports(today, state, model_config)
    except Exception as error:
        state = _load_automation_state()
        _set_task_error(state, "scheduler", f"自动任务异常: {error}")
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
            _clear_task_error(state, "weekly_report")
            _save_automation_state(state)
            current_week_end += datetime.timedelta(days=7)
        else:
            _set_task_error(
                state,
                "weekly_report",
                f"自动生成截至 {current_week_end:%Y-%m-%d} 的周报失败",
            )
            _save_automation_state(state)
            break


def _run_monthly_reports(
    today: datetime.date,
    state: dict,
    model_config: settings.ModelDict,
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
            _, success, _ = generate_analysis_report(
                "monthly", current_month_start, model_config
            )
        else:
            success = True
        if success:
            state["last_month_end"] = current_month_end.strftime("%Y-%m-%d")
            _clear_task_error(state, "monthly_report")
            _save_automation_state(state)
            current_month_start = next_month
        else:
            _set_task_error(
                state,
                "monthly_report",
                f"自动生成 {current_month_start:%Y-%m} 月报失败",
            )
            _save_automation_state(state)
            break


def _automation_worker() -> None:
    while True:
        run_due_automatic_tasks()
        now = datetime.datetime.now()
        time.sleep(_next_automation_check_delay(now))


def _next_automation_check_delay(now: datetime.datetime) -> float:
    next_midnight = datetime.datetime.combine(
        now.date() + datetime.timedelta(days=1), datetime.time.min
    )
    seconds_to_midnight = (next_midnight - now).total_seconds() + 5
    return min(3600, max(1, seconds_to_midnight))


def start_automation_worker() -> None:
    if settings.CONFIG.get("automation", {}).get("enabled", True):
        threading.Thread(
            target=_automation_worker,
            name="agentrecord-automation",
            daemon=True,
        ).start()
