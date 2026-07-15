"""原始日记的读取、追加、查找和总结区域更新。

每天一个 Markdown 文件及其内容格式由本模块统一维护。分析代码只能通过
这里提供的接口写入日记，避免未来 Agent 直接改动原始记录流。
"""

import datetime
import os
import re
from pathlib import Path

import settings


def resolve_date(arg: str) -> str:
    """解析常用日期参数，返回 YYYY-MM-DD，无法解析时返回空字符串。"""
    today = datetime.date.today()
    arg = arg.strip()

    if not arg:
        return today.strftime("%Y-%m-%d")

    if re.match(r"^-\d+$", arg):
        days = int(arg[1:])
        return (today - datetime.timedelta(days=days)).strftime("%Y-%m-%d")

    aliases = {"today": 0, "今天": 0, "yesterday": 1, "昨天": 1}
    if arg.lower() in aliases:
        return (today - datetime.timedelta(days=aliases[arg.lower()])).strftime("%Y-%m-%d")

    if arg.lower() in ("last", "prev", "上一个", "最近"):
        files = sorted(settings.DIARY_DIR.glob("*.md"), reverse=True)
        today_text = today.strftime("%Y-%m-%d")
        for file in files:
            if file.stem < today_text:
                return file.stem
        return ""

    for date_format in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.datetime.strptime(arg, date_format).strftime("%Y-%m-%d")
        except ValueError:
            continue

    for date_format in ("%m-%d", "%m%d"):
        try:
            date = datetime.datetime.strptime(arg, date_format)
            return date.replace(year=today.year).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def extract_summary(text: str) -> str:
    match = re.search(r"<summary>(.*?)</summary>", text, re.DOTALL)
    return match.group(1).strip() if match else "(无总结)"


def read_daily_log(
    date: str = "",
    start_date: str = "",
    end_date: str = "",
    summary_only: bool = False,
) -> str:
    if date:
        file_path = settings.DIARY_DIR / f"{date}.md"
        if not file_path.exists():
            return f"本地系统提示：找不到 {date} 的记录。"
        content = file_path.read_text(encoding="utf-8")
        return extract_summary(content) if summary_only else content

    if start_date and end_date:
        results = []
        for file in sorted(settings.DIARY_DIR.glob("*.md")):
            if start_date <= file.stem <= end_date:
                content = file.read_text(encoding="utf-8")
                if summary_only:
                    results.append(f"## {file.stem}\n{extract_summary(content)}")
                else:
                    results.append(f"# {file.stem}\n{content}")
        return (
            "\n\n---\n\n".join(results)
            if results
            else f"本地系统提示：{start_date} 到 {end_date} 之间无记录。"
        )

    today = datetime.datetime.now().strftime("%Y-%m-%d")
    file_path = settings.DIARY_DIR / f"{today}.md"
    if not file_path.exists():
        return f"本地系统提示：找不到 {today} 的记录。"
    content = file_path.read_text(encoding="utf-8")
    return extract_summary(content) if summary_only else content


def search_history(keyword: str, days_limit: int = 0, summary_only: bool = False) -> str:
    files = sorted(settings.DIARY_DIR.glob("*.md"), reverse=True)
    if days_limit > 0:
        files = files[:days_limit]

    results = []
    for file in files:
        content = file.read_text(encoding="utf-8")
        search_target = extract_summary(content) if summary_only else content
        if keyword in search_target:
            matched = [line for line in search_target.split("\n") if keyword in line]
            results.append(f"[{file.stem}] 匹配到:\n" + "\n".join(matched))
    return (
        "\n\n".join(results)
        if results
        else f"本地系统提示：未找到关于 '{keyword}' 的记录。"
    )


def _diary_file_for(submitted_at: datetime.datetime) -> Path:
    return settings.DIARY_DIR / f"{submitted_at:%Y-%m-%d}.md"


def get_today_file() -> Path:
    return _diary_file_for(datetime.datetime.now())


def init_file_if_not_exists(
    submitted_at: datetime.datetime | None = None,
) -> Path:
    """使用同一个提交时间确定文件路径和文件头，并返回该路径。"""
    submitted_at = submitted_at or datetime.datetime.now()
    file_path = _diary_file_for(submitted_at)
    if file_path.exists():
        return file_path
    template = (
        f"# {submitted_at:%Y-%m-%d}\n\n"
        "<summary>\n暂无今日总结。\n</summary>\n\n"
        "---\n"
        "## 原始记录流\n\n"
    )
    file_path.write_text(template, encoding="utf-8")
    return file_path


def append_log(
    content: str,
    tag: str = "",
    submitted_at: datetime.datetime | None = None,
) -> None:
    """按回车提交时间追加记录；一次写入只使用一个时间值。"""
    submitted_at = submitted_at or datetime.datetime.now()
    file_path = init_file_if_not_exists(submitted_at)
    submitted_time = submitted_at.strftime("%H:%M")
    with file_path.open("a", encoding="utf-8") as file:
        if tag:
            file.write(f"**{submitted_time} {tag}:** {content}\n\n")
        else:
            file.write(f"**{submitted_time}:** {content}\n\n")


REFERENCE_KINDS = {
    "diary": ("日记", lambda: settings.DIARY_DIR),
    "daily": ("分析日报", lambda: settings.ANALYSIS_DIR / "Daily"),
    "weekly": ("分析周报", lambda: settings.ANALYSIS_DIR / "Weekly"),
    "monthly": ("分析月报", lambda: settings.ANALYSIS_DIR / "Monthly"),
}

REPORT_STEM_PATTERNS = {
    "daily": re.compile(r"^(\d{4}-\d{2}-\d{2})_(manual|auto)$"),
    "weekly": re.compile(
        r"^(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})_(manual|auto)$"
    ),
    "monthly": re.compile(r"^(\d{4}-\d{2})_(manual|auto)$"),
}


def list_reference_sources(
    kind: str, keyword: str = "", limit: int = 20
) -> list[tuple[str, Path]]:
    """列出可引用的日记或报告，按文件名倒序返回。"""
    if kind not in REFERENCE_KINDS:
        return []
    type_label, directory_factory = REFERENCE_KINDS[kind]
    files = sorted(directory_factory().glob("*.md"), reverse=True)
    if kind != "diary":
        pattern = REPORT_STEM_PATTERNS[kind]
        files = [path for path in files if pattern.fullmatch(path.stem)]
    if keyword:
        files = [path for path in files if keyword in path.stem]
    if limit > 0:
        files = files[:limit]

    sources = []
    for path in files:
        if kind == "diary":
            period = path.stem
            label = type_label
        else:
            match = REPORT_STEM_PATTERNS[kind].fullmatch(path.stem)
            if not match:
                continue
            origin = match.groups()[-1]
            period_parts = match.groups()[:-1]
            period = " 至 ".join(period_parts)
            label = f"{'手动' if origin == 'manual' else '自动'}{type_label}"
        sources.append((f"{label} | {period}", path))
    return sources


def append_reference(
    label: str,
    source_path: Path,
    note: str = "",
    submitted_at: datetime.datetime | None = None,
) -> None:
    """把来源及可选的新想法作为一条带时间的标准引用记录追加到今日日记。"""
    submitted_at = submitted_at or datetime.datetime.now()
    diary_path = _diary_file_for(submitted_at)
    relative_path = os.path.relpath(source_path, diary_path.parent)
    portable_path = Path(relative_path).as_posix()
    content = f"[{label}](<{portable_path}>)"
    if note.strip():
        content += f"\n\n{note.strip()}"
    append_log(content, "[引用]", submitted_at=submitted_at)


def update_summary_for_date(date: str, summary_text: str) -> str:
    file_path = settings.DIARY_DIR / f"{date}.md"
    if not file_path.exists():
        return f"找不到 {date} 的记录。"
    content = file_path.read_text(encoding="utf-8")
    if not re.search(r"<summary>.*?</summary>", content, re.DOTALL):
        return f"{date} 的记录缺少 <summary> 区域。"

    new_content = re.sub(
        r"<summary>.*?</summary>",
        f"<summary>\n{summary_text}\n</summary>",
        content,
        count=1,
        flags=re.DOTALL,
    )
    temp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    temp_path.write_text(new_content, encoding="utf-8")
    temp_path.replace(file_path)
    return f"{date} 的总结已写入文档顶部。"


def delete_last_record() -> bool:
    file_path = get_today_file()
    if not file_path.exists():
        return False
    content = file_path.read_text(encoding="utf-8")
    matches = list(re.finditer(r"^\*\*\d{2}:\d{2}", content, re.MULTILINE))
    if not matches:
        return False
    start = matches[-1].start()
    if start > 0 and content[start - 1] == "\n":
        start -= 1
    file_path.write_text(content[:start].rstrip() + "\n\n", encoding="utf-8")
    return True
