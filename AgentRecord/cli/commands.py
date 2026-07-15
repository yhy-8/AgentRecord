"""Record and report mode command handlers."""

import datetime
import re

from rich.markdown import Markdown
from rich.panel import Panel

from .. import journal, settings
from ..analysis import (
    analysis_report_path,
    automation_status_snapshot,
    generate_analysis_report,
    summarize_diary,
)
from ..analysis.store import AnalysisStore
from .report_jobs import manual_report_jobs
from .terminal import console, post_notification, safe_input, show_view_help


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


def _analysis_period_choices(kind: str, limit: int = 20) -> list[tuple[str, datetime.date]]:
    dates = []
    for path in settings.DIARY_DIR.glob("*.md"):
        try:
            dates.append(datetime.date.fromisoformat(path.stem))
        except ValueError:
            continue
    periods: dict[datetime.date, set[datetime.date]] = {}
    for date in dates:
        anchor = (
            date - datetime.timedelta(days=date.weekday())
            if kind == "weekly"
            else date.replace(day=1)
        )
        periods.setdefault(anchor, set()).add(date)

    today = datetime.date.today()
    choices = []
    for anchor, recorded_dates in sorted(periods.items(), reverse=True)[:limit]:
        if kind == "weekly":
            end = anchor + datetime.timedelta(days=6)
            period_text = f"{anchor:%Y-%m-%d} 至 {end:%Y-%m-%d}"
        else:
            next_month = (anchor.replace(day=28) + datetime.timedelta(days=4)).replace(
                day=1
            )
            end = next_month - datetime.timedelta(days=1)
            period_text = f"{anchor:%Y-%m}"
        closure = "进行中" if end >= today else "已闭合"
        origins = []
        for origin, label in (("manual", "手动"), ("auto", "自动")):
            report_path = analysis_report_path(kind, anchor, origin=origin)
            if report_path and report_path.exists():
                origins.append(label)
        report_status = "+".join(origins) + "已生成" if origins else "未生成"
        choices.append(
            (
                f"{period_text}  [dim]{closure} · {len(recorded_dates)} 天有记录 · {report_status}[/dim]",
                anchor,
            )
        )
    return choices


def _choose_analysis_period(kind: str) -> datetime.date | None:
    choices = _analysis_period_choices(kind)
    if not choices:
        console.print("[yellow][!][/yellow] 没有可生成报告的记录周期。")
        return None
    content = "\n".join(
        f"  [cyan]{index}[/cyan]. {label}"
        for index, (label, _) in enumerate(choices, 1)
    )
    label = "自然周" if kind == "weekly" else "自然月"
    console.print(Panel(content, title=f"[bold]选择{label}[/bold]", border_style="cyan"))
    selection = safe_input("选择编号 [空=取消] >> ").strip()
    if not selection:
        return None
    try:
        index = int(selection) - 1
        if index < 0:
            raise IndexError
        return choices[index][1]
    except (ValueError, IndexError):
        console.print(f"[yellow][!][/yellow] 无效编号: {selection}")
        return None


def _handle_analysis(user_input: str, model_config: settings.ModelDict) -> bool:
    kind, date_argument = _parse_analysis_arguments(user_input)
    if kind in ("weekly", "monthly") and not date_argument:
        anchor = _choose_analysis_period(kind)
        if anchor is None:
            return False
        date = anchor.isoformat()
    else:
        if kind == "monthly" and re.fullmatch(r"\d{4}-\d{2}", date_argument):
            date_argument += "-01"
        date = journal.resolve_date(date_argument)
    if not date:
        console.print(f"[yellow][!][/yellow] 无法解析日期: {date_argument}")
        return False
    anchor = datetime.datetime.strptime(date, "%Y-%m-%d").date()
    label = {"daily": "日报", "weekly": "周报", "monthly": "月报"}[kind]
    target_path = analysis_report_path(kind, anchor, origin="manual")
    if target_path and target_path.exists():
        confirmation = safe_input(
            f"该周期的手动{label}已存在，确认覆盖？[y/N] >> "
        ).strip().lower()
        if confirmation not in ("y", "yes", "是"):
            console.print("[dim]已取消，原报告保持不变。[/dim]")
            return False
    started = manual_report_jobs.start(
        kind,
        anchor,
        model_config,
        generate_analysis_report,
        post_notification,
    )
    if not started:
        running = manual_report_jobs.running_label() or "手动报告"
        console.print(f"[yellow][!][/yellow] {running}仍在后台生成，请完成后再试。")
        return False
    console.print(
        f"[cyan][*][/cyan] 分析{label}已转入后台；可以继续记录，"
        "完成前请不要关闭当前窗口。"
    )
    return True


def _handle_status() -> None:
    status = automation_status_snapshot()
    installed = "已安装" if status["installed"] else "未完整安装"
    lines = [
        f"  系统自动任务：{installed}",
        f"  安装详情：{status['install_message']}",
        f"  最后完成检查：{status['last_check_completed_at'] or '尚未记录'}",
        f"  日总结进度：{status['last_daily_date'] or '尚无'}",
        f"  信息简报进度：{status['last_information_date'] or '尚无'}",
        f"  周报进度：{status['last_week_end'] or '尚无'}",
        f"  月报进度：{status['last_month_end'] or '尚无'}",
    ]
    errors = status["errors"]
    if errors:
        lines.append("  [yellow]当前失败（下次每小时检查时会重试）：[/yellow]")
        lines.extend(f"    - {task}: {message}" for task, message in errors.items())
    else:
        lines.append("  当前失败：无")
    console.print(Panel("\n".join(lines), title="[bold]自动任务状态[/bold]", border_style="cyan"))


def _handle_feedback() -> None:
    store = AnalysisStore()
    nodes = store.feedback_candidates()
    if not nodes:
        console.print("[yellow][!][/yellow] 暂无可反馈的观念、假设或洞见节点。")
        return
    type_labels = {"theme": "观念", "hypothesis": "假设", "insight": "洞见"}
    content = "\n".join(
        f"  [cyan]{index}[/cyan]. [{type_labels.get(node['node_type'], node['node_type'])}] "
        f"{node['period_start']}  {node['title']}"
        for index, node in enumerate(nodes, 1)
    )
    console.print(Panel(content, title="[bold]选择要反馈的节点[/bold]", border_style="cyan"))
    selection = safe_input("选择编号 [空=取消] >> ").strip()
    if not selection:
        return
    try:
        index = int(selection) - 1
        if index < 0:
            raise IndexError
        node = nodes[index]
    except (ValueError, IndexError):
        console.print(f"[yellow][!][/yellow] 无效编号: {selection}")
        return
    console.print(Panel(node["body"], title=node["title"], border_style="cyan"))
    action_text = safe_input("操作 [1=认可, 2=否决, 3=修正，空=取消] >> ").strip()
    actions = {"1": "accept", "2": "reject", "3": "correct"}
    action = actions.get(action_text)
    if not action:
        if action_text:
            console.print(f"[yellow][!][/yellow] 无效操作: {action_text}")
        return
    title = body = ""
    if action == "correct":
        title = safe_input("新标题 [空=保留原标题] >> ").strip()
        body = safe_input("新内容 [空=保留原内容] >> ").strip()
    store.record_user_feedback(node["id"], action, title=title, body=body)
    console.print("[cyan][*][/cyan] 反馈已记录；将影响以后的分析，不会改写已有报告。")


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
