"""SQLite persistence for report runs and the long-lived personal profile.

The database is derived state. Markdown records remain the source of truth.
The final schema stores the compact profile model together with validated-stage
cache and request/search telemetry in Agent artifact payloads.
"""

import datetime
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

from .. import settings


PROFILE_CATEGORIES = {
    "viewpoint",
    "principle",
    "ideal",
    "behavior_pattern",
    "interest",
}
PROFILE_STATUSES = {"accepted", "rejected", "superseded"}
RUN_STATUSES = {"running", "completed", "failed"}
_SCHEMA_COLUMNS = {
    "analysis_runs": {
        "id", "kind", "period_start", "period_end", "origin", "trigger",
        "model_name", "status", "input_hash", "report_path", "error",
        "created_at", "completed_at",
    },
    "agent_artifacts": {
        "id", "run_id", "agent", "revision", "status", "payload_json",
        "error", "created_at",
    },
    "source_catalog": {
        "source_id", "relative_path", "source_date", "source_time",
        "record_index", "speaker", "tag", "content_hash", "excerpt",
        "last_seen_at",
    },
    "run_sources": {"run_id", "source_id"},
    "profile_entries": {
        "id", "run_id", "category", "title", "statement", "status",
        "confidence", "source_refs_json", "first_observed", "last_observed",
        "created_by", "supersedes_id", "created_at", "updated_at",
    },
    "profile_feedback": {
        "id", "entry_id", "action", "replacement_entry_id", "created_at",
    },
}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str) -> object:
    return json.loads(value) if value else {}


class AnalysisStore:
    """Transactional access to disposable analysis state and durable feedback."""

    def __init__(self, path: Path | None = None):
        self.path = path or settings.ANALYSIS_DIR / ".analysis.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        # Inspect the physical schema before enabling WAL so an incompatible
        # database is rejected without modification. No schema version or
        # migration state is stored.
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    """
                )
            }
            if tables:
                if tables != set(_SCHEMA_COLUMNS):
                    raise RuntimeError(
                        "分析数据库结构不符合当前程序。"
                        "本项目不提供数据库迁移或兼容；请确认无需保留后，"
                        f"手动删除 {self.path} 及同名 -wal、-shm 文件再启动。"
                    )
                for table, expected_columns in _SCHEMA_COLUMNS.items():
                    actual_columns = {
                        row[1]
                        for row in connection.execute(
                            f"PRAGMA table_info({table})"
                        )
                    }
                    if actual_columns != expected_columns:
                        raise RuntimeError(
                            f"分析数据库表 {table} 结构不符合当前程序。"
                            "本项目不提供数据库迁移或兼容；请手动删除"
                            "数据库主文件及同名 -wal、-shm 文件再启动。"
                        )
        finally:
            connection.close()

        if tables:
            return

        with self.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE analysis_runs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    report_path TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE agent_artifacts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
                    agent TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, agent, revision)
                );

                CREATE TABLE source_catalog (
                    source_id TEXT PRIMARY KEY,
                    relative_path TEXT NOT NULL,
                    source_date TEXT NOT NULL,
                    source_time TEXT NOT NULL,
                    record_index INTEGER NOT NULL,
                    speaker TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    excerpt TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE run_sources (
                    run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
                    source_id TEXT NOT NULL REFERENCES source_catalog(source_id),
                    PRIMARY KEY(run_id, source_id)
                );

                CREATE TABLE profile_entries (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    source_refs_json TEXT NOT NULL,
                    first_observed TEXT NOT NULL,
                    last_observed TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    supersedes_id TEXT REFERENCES profile_entries(id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE profile_feedback (
                    id TEXT PRIMARY KEY,
                    entry_id TEXT NOT NULL REFERENCES profile_entries(id),
                    action TEXT NOT NULL,
                    replacement_entry_id TEXT REFERENCES profile_entries(id),
                    created_at TEXT NOT NULL
                );

                CREATE INDEX idx_runs_period
                    ON analysis_runs(kind, period_start, period_end, origin, status);
                CREATE INDEX idx_profile_active
                    ON profile_entries(status, last_observed, updated_at DESC);
                CREATE INDEX idx_profile_run
                    ON profile_entries(run_id, status, category);
                CREATE INDEX idx_profile_feedback
                    ON profile_feedback(entry_id, created_at DESC);
                """
            )

    def start_run(
        self,
        kind: str,
        period_start: str,
        period_end: str,
        origin: str,
        model_name: str,
        input_hash: str,
        *,
        trigger: str | None = None,
    ) -> str:
        if kind not in {"weekly", "monthly"}:
            raise ValueError(f"不支持的报告类型: {kind}")
        if origin not in {"manual", "auto"}:
            raise ValueError(f"不支持的报告来源: {origin}")
        trigger = trigger or ("manual" if origin == "manual" else "scheduled")
        if trigger not in {"manual", "scheduled", "retry"}:
            raise ValueError(f"不支持的触发方式: {trigger}")
        run_id = uuid.uuid4().hex
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO analysis_runs(
                    id, kind, period_start, period_end, origin, trigger, model_name,
                    status, input_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
                """,
                (
                    run_id,
                    kind,
                    period_start,
                    period_end,
                    origin,
                    trigger,
                    model_name,
                    input_hash,
                    _now(),
                ),
            )
        return run_id

    def complete_run(self, run_id: str, report_path: Path) -> None:
        self._finish_run(run_id, "completed", report_path=str(report_path))

    def fail_run(self, run_id: str, error: str) -> None:
        self._finish_run(run_id, "failed", error=error)

    def _finish_run(
        self,
        run_id: str,
        status: str,
        *,
        report_path: str | None = None,
        error: str | None = None,
    ) -> None:
        if status not in RUN_STATUSES:
            raise ValueError(f"无效运行状态: {status}")
        with self.transaction() as connection:
            if status == "completed":
                stale = connection.execute(
                    """
                    SELECT child.supersedes_id
                    FROM profile_entries AS child
                    JOIN profile_entries AS parent ON parent.id = child.supersedes_id
                    WHERE child.run_id = ? AND child.status = 'accepted'
                      AND child.supersedes_id IS NOT NULL
                      AND parent.status != 'accepted'
                    LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                if stale:
                    raise RuntimeError(
                        "报告生成期间人物画像已被其他操作更新，本次候选不能覆盖新状态"
                    )
                connection.execute(
                    """
                    UPDATE profile_entries
                    SET status = 'superseded', updated_at = ?
                    WHERE status = 'accepted' AND id IN (
                        SELECT supersedes_id
                        FROM profile_entries
                        WHERE run_id = ? AND status = 'accepted'
                          AND supersedes_id IS NOT NULL
                    )
                    """,
                    (_now(), run_id),
                )
            connection.execute(
                """
                UPDATE analysis_runs
                SET status = ?, report_path = ?, error = ?, completed_at = ?
                WHERE id = ?
                """,
                (status, report_path, error, _now(), run_id),
            )

    def save_artifact(
        self,
        run_id: str,
        agent: str,
        payload: dict,
        *,
        status: str = "completed",
        error: str | None = None,
    ) -> str:
        artifact_id = uuid.uuid4().hex
        with self.transaction() as connection:
            revision = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) + 1
                FROM agent_artifacts WHERE run_id = ? AND agent = ?
                """,
                (run_id, agent),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO agent_artifacts(
                    id, run_id, agent, revision, status, payload_json, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    run_id,
                    agent,
                    revision,
                    status,
                    _json(payload),
                    error,
                    _now(),
                ),
            )
        return artifact_id

    def reusable_artifact(
        self,
        input_hash: str,
        kind: str,
        period_start: str,
        period_end: str,
        origin: str,
        model_name: str,
        agent: str,
    ) -> tuple[str, dict] | None:
        """Return the latest fully validated stage from an equivalent failed run."""
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT a.run_id, a.payload_json
                FROM agent_artifacts AS a
                JOIN analysis_runs AS r ON r.id = a.run_id
                WHERE r.input_hash = ? AND r.kind = ?
                  AND r.period_start = ? AND r.period_end = ?
                  AND r.origin = ? AND r.model_name = ?
                  AND r.status = 'failed'
                  AND a.agent = ? AND a.status = 'completed'
                ORDER BY r.completed_at DESC, a.revision DESC
                LIMIT 1
                """,
                (
                    input_hash,
                    kind,
                    period_start,
                    period_end,
                    origin,
                    model_name,
                    agent,
                ),
            ).fetchone()
        finally:
            connection.close()
        if not row:
            return None
        payload = _loads(row["payload_json"])
        return (row["run_id"], payload) if isinstance(payload, dict) else None

    def save_sources(self, run_id: str, records: Sequence[dict]) -> None:
        now = _now()
        with self.transaction() as connection:
            for record in records:
                text = str(record.get("text", ""))
                values = (
                    record["source_id"],
                    record["path"],
                    record["date"],
                    record["time"],
                    int(record["record_index"]),
                    record.get("speaker", "user"),
                    record.get("tag", ""),
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    text[:500],
                    now,
                )
                connection.execute(
                    """
                    INSERT INTO source_catalog(
                        source_id, relative_path, source_date, source_time,
                        record_index, speaker, tag, content_hash, excerpt, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        relative_path=excluded.relative_path,
                        source_date=excluded.source_date,
                        source_time=excluded.source_time,
                        record_index=excluded.record_index,
                        speaker=excluded.speaker,
                        tag=excluded.tag,
                        content_hash=excluded.content_hash,
                        excerpt=excluded.excerpt,
                        last_seen_at=excluded.last_seen_at
                    """,
                    values,
                )
                connection.execute(
                    "INSERT OR IGNORE INTO run_sources(run_id, source_id) VALUES (?, ?)",
                    (run_id, record["source_id"]),
                )

    def source_records(self, source_ids: Sequence[str]) -> list[dict]:
        if not source_ids:
            return []
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM source_catalog WHERE source_id IN (%s) "
                "ORDER BY source_date, source_time, record_index"
                % ",".join("?" for _ in source_ids),
                list(source_ids),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def active_profiles(self, period_end: str, limit: int = 80) -> list[dict]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT p.* FROM profile_entries AS p
                JOIN analysis_runs AS r ON r.id = p.run_id
                WHERE p.status = 'accepted' AND p.last_observed <= ?
                  AND r.status = 'completed'
                ORDER BY p.updated_at DESC LIMIT ?
                """,
                (period_end, limit),
            ).fetchall()
        finally:
            connection.close()
        return [self._profile_dict(row) for row in rows]

    def save_profile_entries(
        self,
        run_id: str,
        entries: Sequence[dict],
        decisions: dict[str, str],
    ) -> dict[str, str]:
        id_map: dict[str, str] = {}
        with self.transaction() as connection:
            now = _now()
            for entry in entries:
                temp_id = str(entry["temp_id"])
                status = decisions.get(temp_id, "rejected")
                if status not in {"accepted", "rejected"}:
                    raise ValueError(f"无效画像决定: {status}")
                entry_id = uuid.uuid4().hex
                supersedes_id = entry.get("supersedes_id") or None
                connection.execute(
                    """
                    INSERT INTO profile_entries(
                        id, run_id, category, title, statement, status, confidence,
                        source_refs_json, first_observed, last_observed, created_by,
                        supersedes_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'retrospective', ?, ?, ?)
                    """,
                    (
                        entry_id,
                        run_id,
                        entry["category"],
                        entry["title"],
                        entry["statement"],
                        status,
                        float(entry["confidence"]),
                        _json(entry["source_refs"]),
                        entry["first_observed"],
                        entry["last_observed"],
                        supersedes_id,
                        now,
                        now,
                    ),
                )
                id_map[temp_id] = entry_id
        return id_map

    def feedback_candidates(self, limit: int = 20) -> list[dict]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT p.*, r.period_start, r.period_end
                FROM profile_entries AS p
                JOIN analysis_runs AS r ON r.id = p.run_id
                WHERE p.status = 'accepted' AND r.status = 'completed'
                ORDER BY r.completed_at DESC, p.updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            connection.close()
        return [self._profile_dict(row) for row in rows]

    def record_user_feedback(
        self,
        entry_id: str,
        action: str,
        *,
        title: str = "",
        body: str = "",
    ) -> str | None:
        if action not in {"accept", "reject", "correct"}:
            raise ValueError(f"未知反馈操作: {action}")
        with self.transaction() as connection:
            entry = connection.execute(
                "SELECT * FROM profile_entries WHERE id = ?", (entry_id,)
            ).fetchone()
            if not entry:
                raise ValueError(f"找不到人物画像条目: {entry_id}")
            now = _now()
            replacement_id = None
            if action == "reject":
                connection.execute(
                    "UPDATE profile_entries SET status='rejected', updated_at=? WHERE id=?",
                    (now, entry_id),
                )
            else:
                replacement_id = uuid.uuid4().hex
                connection.execute(
                    """
                    INSERT INTO profile_entries(
                        id, run_id, category, title, statement, status, confidence,
                        source_refs_json, first_observed, last_observed, created_by,
                        supersedes_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'accepted', 1.0, ?, ?, ?, 'user', ?, ?, ?)
                    """,
                    (
                        replacement_id,
                        entry["run_id"],
                        entry["category"],
                        title.strip() or entry["title"],
                        body.strip() or entry["statement"],
                        entry["source_refs_json"],
                        entry["first_observed"],
                        entry["last_observed"],
                        entry_id,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE profile_entries SET status='superseded', updated_at=? WHERE id=?",
                    (now, entry_id),
                )
            connection.execute(
                """
                INSERT INTO profile_feedback(
                    id, entry_id, action, replacement_entry_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (uuid.uuid4().hex, entry_id, action, replacement_id, now),
            )
        return replacement_id

    @staticmethod
    def _profile_dict(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["source_refs"] = _loads(item.pop("source_refs_json"))
        item["body"] = item["statement"]
        item["node_type"] = item["category"]
        return item
