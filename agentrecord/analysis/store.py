"""SQLite storage for persistent analysis runs, artifacts, nodes, and relations.

This module does not read journals or call models.  It only persists data that the
analysis orchestrator has already validated.
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


SCHEMA_VERSION = 1
NODE_STATUSES = {"candidate", "accepted", "rejected", "superseded"}
RUN_STATUSES = {"running", "completed", "failed"}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str) -> object:
    return json.loads(value) if value else {}


class AnalysisStore:
    """Transactional access to the derived analysis database."""

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
        with self.transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"分析数据库版本 {version} 高于程序支持的 {SCHEMA_VERSION}"
                )
            if version == 0:
                connection.executescript(
                    """
                    CREATE TABLE analysis_runs (
                        id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        period_start TEXT NOT NULL,
                        period_end TEXT NOT NULL,
                        origin TEXT NOT NULL,
                        model_name TEXT NOT NULL,
                        status TEXT NOT NULL,
                        parent_run_id TEXT REFERENCES analysis_runs(id),
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

                    CREATE TABLE analysis_sources (
                        run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
                        source_id TEXT NOT NULL,
                        relative_path TEXT NOT NULL,
                        source_date TEXT NOT NULL,
                        source_time TEXT NOT NULL,
                        record_index INTEGER NOT NULL,
                        speaker TEXT NOT NULL,
                        tag TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        excerpt TEXT NOT NULL,
                        PRIMARY KEY(run_id, source_id)
                    );

                    CREATE TABLE knowledge_nodes (
                        id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
                        node_type TEXT NOT NULL,
                        title TEXT NOT NULL,
                        body TEXT NOT NULL,
                        status TEXT NOT NULL,
                        confidence REAL NOT NULL,
                        created_by TEXT NOT NULL,
                        source_refs_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        supersedes_id TEXT REFERENCES knowledge_nodes(id),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE knowledge_edges (
                        id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
                        source_id TEXT NOT NULL REFERENCES knowledge_nodes(id),
                        target_id TEXT NOT NULL REFERENCES knowledge_nodes(id),
                        relation_type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        weight REAL NOT NULL,
                        confidence REAL NOT NULL,
                        rationale TEXT NOT NULL,
                        created_by TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        supersedes_id TEXT REFERENCES knowledge_edges(id),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX idx_runs_period
                        ON analysis_runs(kind, period_start, period_end, origin, status);
                    CREATE INDEX idx_nodes_run_status
                        ON knowledge_nodes(run_id, status, node_type);
                    CREATE INDEX idx_nodes_active_history
                        ON knowledge_nodes(status, updated_at DESC);
                    CREATE INDEX idx_edges_run_status
                        ON knowledge_edges(run_id, status, relation_type);
                    """
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def start_run(
        self,
        kind: str,
        period_start: str,
        period_end: str,
        origin: str,
        model_name: str,
        input_hash: str,
    ) -> tuple[str, str | None]:
        run_id = uuid.uuid4().hex
        with self.transaction() as connection:
            previous = connection.execute(
                """
                SELECT id FROM analysis_runs
                WHERE kind = ? AND period_start = ? AND period_end = ?
                  AND origin = ? AND status = 'completed'
                ORDER BY completed_at DESC LIMIT 1
                """,
                (kind, period_start, period_end, origin),
            ).fetchone()
            parent_run_id = previous["id"] if previous else None
            connection.execute(
                """
                INSERT INTO analysis_runs(
                    id, kind, period_start, period_end, origin, model_name, status,
                    parent_run_id, input_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (
                    run_id,
                    kind,
                    period_start,
                    period_end,
                    origin,
                    model_name,
                    parent_run_id,
                    input_hash,
                    _now(),
                ),
            )
        return run_id, parent_run_id

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

    def save_sources(self, run_id: str, records: Sequence[dict]) -> None:
        """Persist source locations and hashes without copying complete journals."""
        with self.transaction() as connection:
            for record in records:
                text = str(record.get("text", ""))
                connection.execute(
                    """
                    INSERT INTO analysis_sources(
                        run_id, source_id, relative_path, source_date, source_time,
                        record_index, speaker, tag, content_hash, excerpt
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        record["source_id"],
                        record["path"],
                        record["date"],
                        record["time"],
                        int(record["record_index"]),
                        record.get("speaker", "user"),
                        record.get("tag", ""),
                        hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        text[:500],
                    ),
                )

    def sources_for_run(self, run_id: str) -> list[dict]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM analysis_sources WHERE run_id = ?
                ORDER BY source_date, source_time, record_index
                """,
                (run_id,),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def add_nodes(
        self, run_id: str, agent: str, nodes: Sequence[dict]
    ) -> dict[str, str]:
        """Insert candidate nodes and return temporary-to-persistent ID mapping."""
        id_map: dict[str, str] = {}
        with self.transaction() as connection:
            for node in nodes:
                temporary_id = str(node["temp_id"])
                if temporary_id in id_map:
                    raise ValueError(f"重复临时节点 ID: {temporary_id}")
                node_id = uuid.uuid4().hex
                supersedes_id = node.get("supersedes_id") or None
                revision = 1
                if supersedes_id:
                    previous = connection.execute(
                        "SELECT revision FROM knowledge_nodes WHERE id = ?",
                        (supersedes_id,),
                    ).fetchone()
                    if not previous:
                        raise ValueError(f"找不到被替代节点: {supersedes_id}")
                    revision = previous["revision"] + 1
                now = _now()
                connection.execute(
                    """
                    INSERT INTO knowledge_nodes(
                        id, run_id, node_type, title, body, status, confidence,
                        created_by, source_refs_json, metadata_json, revision,
                        supersedes_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node_id,
                        run_id,
                        node["node_type"],
                        node.get("title", "").strip(),
                        node.get("body", "").strip(),
                        float(node.get("confidence", 0.5)),
                        agent,
                        _json(node.get("source_refs", [])),
                        _json(node.get("metadata", {})),
                        revision,
                        supersedes_id,
                        now,
                        now,
                    ),
                )
                id_map[temporary_id] = node_id
        return id_map

    def add_edges(
        self,
        run_id: str,
        agent: str,
        edges: Sequence[dict],
        node_ids: dict[str, str] | None = None,
    ) -> list[str]:
        node_ids = node_ids or {}
        edge_ids = []
        with self.transaction() as connection:
            for edge in edges:
                source_id = node_ids.get(str(edge["source_id"]), str(edge["source_id"]))
                target_id = node_ids.get(str(edge["target_id"]), str(edge["target_id"]))
                for node_id in (source_id, target_id):
                    if not connection.execute(
                        "SELECT 1 FROM knowledge_nodes WHERE id = ?", (node_id,)
                    ).fetchone():
                        raise ValueError(f"关系引用未知节点: {node_id}")
                edge_id = uuid.uuid4().hex
                now = _now()
                connection.execute(
                    """
                    INSERT INTO knowledge_edges(
                        id, run_id, source_id, target_id, relation_type, status,
                        weight, confidence, rationale, created_by, revision,
                        supersedes_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?, 1, NULL, ?, ?)
                    """,
                    (
                        edge_id,
                        run_id,
                        source_id,
                        target_id,
                        edge["relation_type"],
                        float(edge.get("weight", 0.5)),
                        float(edge.get("confidence", 0.5)),
                        edge.get("rationale", "").strip(),
                        agent,
                        now,
                        now,
                    ),
                )
                edge_ids.append(edge_id)
        return edge_ids

    def apply_node_decisions(self, decisions: Sequence[dict]) -> None:
        with self.transaction() as connection:
            for decision in decisions:
                node_id = str(decision["node_id"])
                status = decision["status"]
                if status not in {"accepted", "rejected", "candidate"}:
                    raise ValueError(f"Reviewer 返回无效节点状态: {status}")
                node = connection.execute(
                    "SELECT supersedes_id FROM knowledge_nodes WHERE id = ?",
                    (node_id,),
                ).fetchone()
                if not node:
                    raise ValueError(f"Reviewer 引用未知节点: {node_id}")
                connection.execute(
                    "UPDATE knowledge_nodes SET status = ?, updated_at = ? WHERE id = ?",
                    (status, _now(), node_id),
                )
                if status == "accepted" and node["supersedes_id"]:
                    connection.execute(
                        """
                        UPDATE knowledge_nodes SET status = 'superseded', updated_at = ?
                        WHERE id = ? AND status = 'accepted'
                        """,
                        (_now(), node["supersedes_id"]),
                    )

    def accept_edges_for_run(self, run_id: str, accepted_node_ids: set[str]) -> None:
        """Accept edges whose endpoints survived review; reject all other candidates."""
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT id, source_id, target_id FROM knowledge_edges
                WHERE run_id = ? AND status = 'candidate'
                """,
                (run_id,),
            ).fetchall()
            now = _now()
            for row in rows:
                status = (
                    "accepted"
                    if row["source_id"] in accepted_node_ids
                    and row["target_id"] in accepted_node_ids
                    else "rejected"
                )
                connection.execute(
                    "UPDATE knowledge_edges SET status = ?, updated_at = ? WHERE id = ?",
                    (status, now, row["id"]),
                )

    def nodes_for_run(
        self,
        run_id: str,
        *,
        statuses: Sequence[str] | None = None,
        node_types: Sequence[str] | None = None,
    ) -> list[dict]:
        clauses = ["run_id = ?"]
        parameters: list[object] = [run_id]
        if statuses:
            clauses.append("status IN (%s)" % ",".join("?" for _ in statuses))
            parameters.extend(statuses)
        if node_types:
            clauses.append("node_type IN (%s)" % ",".join("?" for _ in node_types))
            parameters.extend(node_types)
        return self._select_nodes(" AND ".join(clauses), parameters)

    def edges_for_run(
        self, run_id: str, *, statuses: Sequence[str] | None = None
    ) -> list[dict]:
        where = "run_id = ?"
        parameters: list[object] = [run_id]
        if statuses:
            where += " AND status IN (%s)" % ",".join("?" for _ in statuses)
            parameters.extend(statuses)
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT * FROM knowledge_edges WHERE {where} ORDER BY created_at, id",
                parameters,
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def accepted_history(
        self,
        *,
        before: str,
        exclude_run_id: str | None = None,
        limit: int = 80,
    ) -> list[dict]:
        where = "status = 'accepted' AND updated_at < ?"
        parameters: list[object] = [before]
        if exclude_run_id:
            where += " AND run_id != ?"
            parameters.append(exclude_run_id)
        parameters.append(limit)
        return self._select_nodes(
            where + " ORDER BY updated_at DESC LIMIT ?", parameters, raw_suffix=True
        )

    def _select_nodes(
        self, where: str, parameters: Sequence[object], *, raw_suffix: bool = False
    ) -> list[dict]:
        order = "" if raw_suffix else " ORDER BY created_at, id"
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT * FROM knowledge_nodes WHERE {where}{order}", parameters
            ).fetchall()
        finally:
            connection.close()
        result = []
        for row in rows:
            item = dict(row)
            item["source_refs"] = _loads(item.pop("source_refs_json"))
            item["metadata"] = _loads(item.pop("metadata_json"))
            result.append(item)
        return result

    def run_record(self, run_id: str) -> dict | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM analysis_runs WHERE id = ?", (run_id,)
            ).fetchone()
        finally:
            connection.close()
        return dict(row) if row else None
