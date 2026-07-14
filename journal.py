"""原始日记的读取、追加、查找和总结区域更新。

每天一个 Markdown 文件及其内容格式由本模块统一维护。分析代码只能通过
这里提供的接口写入日记，避免未来 Agent 直接改动原始记录流。
"""

import datetime
import re

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


def get_today_file():
    return settings.DIARY_DIR / f"{datetime.datetime.now():%Y-%m-%d}.md"


def init_file_if_not_exists() -> None:
    file_path = get_today_file()
    if file_path.exists():
        return
    template = (
        f"# {datetime.datetime.now():%Y-%m-%d}\n\n"
        "<summary>\n暂无今日总结。\n</summary>\n\n"
        "---\n"
        "## 原始记录流\n\n"
    )
    file_path.write_text(template, encoding="utf-8")


def append_log(content: str, tag: str = "") -> None:
    init_file_if_not_exists()
    now = datetime.datetime.now().strftime("%H:%M")
    with get_today_file().open("a", encoding="utf-8") as file:
        if tag:
            file.write(f"**{now} {tag}:** {content}\n\n")
        else:
            file.write(f"**{now}:** {content}\n\n")


def read_last_at_query() -> tuple[str, bool, str]:
    """读取今日最后一个 @AI 提问及其回答状态。"""
    file_path = get_today_file()
    if not file_path.exists():
        return "", False, ""
    content = file_path.read_text(encoding="utf-8")
    query_pattern = re.compile(
        r"\*\*(\d{2}:\d{2}) @AI:\*\* (.+?)(?=\n\*\*|\Z)", re.DOTALL
    )
    matches = list(query_pattern.finditer(content))
    if not matches:
        return "", False, ""

    last_match = matches[-1]
    query_text = last_match.group(2).strip()
    after_query = content[last_match.end():]
    reply_pattern = re.compile(
        r"\*\*\d{2}:\d{2} \[AI回复] .+?:\*\* (.+?)(?=\n\*\*|\Z)", re.DOTALL
    )
    reply = reply_pattern.search(after_query)
    if reply:
        return query_text, True, reply.group(1).strip()
    return query_text, False, ""


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
