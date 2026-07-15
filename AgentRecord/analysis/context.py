"""Report input preparation, source resolution, and period paths."""

import datetime
import json
import re
from pathlib import Path

from .. import journal, settings


def _log_without_summary(content: str) -> str:
    return re.sub(
        r"<summary>.*?</summary>",
        "<summary>（已省略）</summary>",
        content,
        count=1,
        flags=re.DOTALL,
    )


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


def _analysis_report_path(
    kind: str, start: datetime.date, end: datetime.date, origin: str
) -> Path:
    suffix = {"manual": "manual", "auto": "auto"}[origin]
    if kind == "daily":
        return settings.ANALYSIS_DIR / "Daily" / f"{start:%Y-%m-%d}_{suffix}.md"
    if kind == "weekly":
        return (
            settings.ANALYSIS_DIR
            / "Weekly"
            / f"{start:%Y-%m-%d}_to_{end:%Y-%m-%d}_{suffix}.md"
        )
    return settings.ANALYSIS_DIR / "Monthly" / f"{start:%Y-%m}_{suffix}.md"


def analysis_report_path(
    kind: str, anchor: datetime.date, origin: str = "manual"
) -> Path | None:
    """返回报告确定路径，供生成前确认是否覆盖。"""
    if origin not in ("manual", "auto"):
        return None
    if kind == "daily":
        start = end = anchor
    elif kind == "weekly":
        start = anchor - datetime.timedelta(days=anchor.weekday())
        end = start + datetime.timedelta(days=6)
    elif kind == "monthly":
        start = anchor.replace(day=1)
        next_month = (start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        end = next_month - datetime.timedelta(days=1)
    else:
        return None
    return _analysis_report_path(kind, start, end, origin)


def _monthly_supporting_reports(start: datetime.date, end: datetime.date) -> str:
    """为月报读取与该月相交的周报，作为已完成分析而非用户原始观点。"""
    sections = []
    weekly_dir = settings.ANALYSIS_DIR / "Weekly"
    for path in sorted(weekly_dir.glob("*.md")):
        match = re.fullmatch(
            r"(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})_(?:manual|auto)",
            path.stem,
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


def _information_briefings(
    start: datetime.date, end: datetime.date, max_characters: int = 30000
) -> str:
    """读取周期内的每日信息简报，仅作为需要重新查证的外部线索。"""
    directory = settings.ANALYSIS_DIR / "Information"
    sections = []
    size = 0
    for path in sorted(directory.glob("*.md"), reverse=True):
        try:
            date = datetime.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if not start <= date <= end:
            continue
        try:
            content = path.read_text(encoding="utf-8")[:8000]
        except OSError:
            continue
        section = f"### {path.name}\n{content}"
        if size + len(section) > max_characters:
            break
        sections.append(section)
        size += len(section)
    sections.reverse()
    return "\n\n".join(sections) or "（本周期没有可用的每日信息简报）"


_RECORD_PATTERN = re.compile(
    r"^\*\*(\d{2}:\d{2})(?: ([^\n]*?))?:\*\*\s?(.*?)"
    r"(?=^\*\*\d{2}:\d{2}(?: [^\n]*?)?:\*\*|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _period_records(logs: list[tuple[str, str]]) -> list[dict]:
    """Parse immutable report input into addressable journal records."""
    records = []
    for date, content in logs:
        for index, match in enumerate(_RECORD_PATTERN.finditer(content), 1):
            tag = (match.group(2) or "").strip()
            speaker = "quoted_ai" if "[AI回复]" in tag else "user"
            records.append(
                {
                    "source_id": f"R-{date.replace('-', '')}-{index:03d}",
                    "path": f"{date}.md",
                    "date": date,
                    "time": match.group(1),
                    "record_index": index,
                    "tag": tag,
                    "speaker": speaker,
                    "text": match.group(3).strip(),
                }
            )
    return records


def _record_chunks(records: list[dict], max_characters: int = 24000) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_size = 0
    for record in records:
        size = len(json.dumps(record, ensure_ascii=False))
        if current and current_size + size > max_characters:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(record)
        current_size += size
    if current:
        chunks.append(current)
    return chunks
