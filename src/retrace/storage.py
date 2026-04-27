from __future__ import annotations

import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from uuid import uuid4
import json
from pathlib import Path
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    distinct_id TEXT,
    event_count INTEGER NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    sessions_scanned INTEGER DEFAULT 0,
    findings_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running',
    error TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS github_repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_full_name TEXT NOT NULL UNIQUE,
    default_branch TEXT NOT NULL,
    remote_url TEXT NOT NULL DEFAULT '',
    local_path TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT 'github',
    connected_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS report_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_path TEXT NOT NULL,
    finding_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    session_url TEXT NOT NULL,
    evidence_text TEXT NOT NULL DEFAULT '',
    distinct_id TEXT NOT NULL DEFAULT '',
    error_issue_ids_json TEXT NOT NULL DEFAULT '[]',
    trace_ids_json TEXT NOT NULL DEFAULT '[]',
    top_stack_frame TEXT NOT NULL DEFAULT '',
    error_tracking_url TEXT NOT NULL DEFAULT '',
    logs_url TEXT NOT NULL DEFAULT '',
    first_error_ts_ms INTEGER NOT NULL DEFAULT 0,
    last_error_ts_ms INTEGER NOT NULL DEFAULT 0,
    regression_state TEXT NOT NULL DEFAULT 'new',
    regression_occurrence_count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(report_path, finding_hash)
);

CREATE TABLE IF NOT EXISTS finding_regression_status (
    finding_hash TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'new',
    first_seen_report_path TEXT NOT NULL,
    last_seen_report_path TEXT NOT NULL,
    last_seen_report_seq INTEGER NOT NULL DEFAULT 0,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS code_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id INTEGER NOT NULL,
    repo_id INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    symbol TEXT,
    score REAL NOT NULL DEFAULT 0,
    rationale_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fix_prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id INTEGER NOT NULL,
    repo_id INTEGER NOT NULL,
    agent_target TEXT NOT NULL,
    prompt_markdown TEXT NOT NULL,
    prompt_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS organizations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(org_id, slug)
);

CREATE TABLE IF NOT EXISTS environments (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, slug)
);

CREATE TABLE IF NOT EXISTS project_members (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, email)
);

CREATE TABLE IF NOT EXISTS sdk_keys (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    name TEXT NOT NULL,
    prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    last4 TEXT NOT NULL,
    revoked_at TEXT,
    last_used_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS service_tokens (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    scopes_json TEXT NOT NULL DEFAULT '[]',
    revoked_at TEXT,
    last_used_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS replay_sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    stable_id TEXT NOT NULL,
    distinct_id TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, environment_id, stable_id)
);

CREATE TABLE IF NOT EXISTS replay_batches (
    id TEXT PRIMARY KEY,
    session_row_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    flush_type TEXT NOT NULL DEFAULT 'normal',
    payload_json TEXT NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 0,
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, environment_id, session_id, sequence)
);

CREATE TABLE IF NOT EXISTS processing_jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    payload_json TEXT NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(kind, subject_id)
);
"""


@dataclass
class SessionMeta:
    id: str
    project_id: str
    started_at: datetime
    duration_ms: int
    distinct_id: Optional[str]
    event_count: int


@dataclass
class RunRow:
    id: int
    started_at: datetime
    finished_at: Optional[datetime]
    sessions_scanned: int
    findings_count: int
    status: str
    error: Optional[str]


@dataclass
class GitHubRepoRow:
    id: int
    repo_full_name: str
    default_branch: str
    remote_url: str
    local_path: str
    provider: str
    connected_at: datetime


@dataclass
class ReportFindingRow:
    id: int
    report_path: str
    finding_hash: str
    title: str
    severity: str
    category: str
    session_url: str
    evidence_text: str
    distinct_id: str
    error_issue_ids: list[str]
    trace_ids: list[str]
    top_stack_frame: str
    error_tracking_url: str
    logs_url: str
    first_error_ts_ms: int
    last_error_ts_ms: int
    regression_state: str
    regression_occurrence_count: int
    created_at: datetime


@dataclass
class FixPromptRow:
    id: int
    finding_id: int
    repo_id: int
    agent_target: str
    prompt_markdown: str
    prompt_json: str
    created_at: datetime


@dataclass
class WorkspaceIds:
    org_id: str
    project_id: str
    environment_id: str


@dataclass
class SDKKeyRow:
    id: str
    project_id: str
    environment_id: str
    name: str
    prefix: str
    key_hash: str
    last4: str
    revoked_at: Optional[datetime]
    last_used_at: Optional[datetime]
    created_at: datetime


@dataclass
class ServiceTokenRow:
    id: str
    project_id: str
    name: str
    token_hash: str
    scopes: list[str]
    revoked_at: Optional[datetime]
    last_used_at: Optional[datetime]
    created_at: datetime


@dataclass
class ReplayBatchResult:
    session_row_id: str
    batch_id: str
    inserted: bool
    event_count: int
    processing_job_id: Optional[str] = None


class Storage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # Lightweight migrations for existing DBs.
            cols_repo = [
                r["name"]
                for r in conn.execute("PRAGMA table_info(github_repos)").fetchall()
            ]
            if "local_path" not in cols_repo:
                conn.execute(
                    "ALTER TABLE github_repos ADD COLUMN local_path TEXT NOT NULL DEFAULT ''"
                )
            cols_findings = [
                r["name"]
                for r in conn.execute("PRAGMA table_info(report_findings)").fetchall()
            ]
            if "evidence_text" not in cols_findings:
                conn.execute(
                    "ALTER TABLE report_findings ADD COLUMN evidence_text TEXT NOT NULL DEFAULT ''"
                )
            if "distinct_id" not in cols_findings:
                conn.execute(
                    "ALTER TABLE report_findings ADD COLUMN distinct_id TEXT NOT NULL DEFAULT ''"
                )
            if "error_issue_ids_json" not in cols_findings:
                conn.execute(
                    "ALTER TABLE report_findings ADD COLUMN error_issue_ids_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "trace_ids_json" not in cols_findings:
                conn.execute(
                    "ALTER TABLE report_findings ADD COLUMN trace_ids_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "top_stack_frame" not in cols_findings:
                conn.execute(
                    "ALTER TABLE report_findings ADD COLUMN top_stack_frame TEXT NOT NULL DEFAULT ''"
                )
            if "error_tracking_url" not in cols_findings:
                conn.execute(
                    "ALTER TABLE report_findings ADD COLUMN error_tracking_url TEXT NOT NULL DEFAULT ''"
                )
            if "logs_url" not in cols_findings:
                conn.execute(
                    "ALTER TABLE report_findings ADD COLUMN logs_url TEXT NOT NULL DEFAULT ''"
                )
            if "first_error_ts_ms" not in cols_findings:
                conn.execute(
                    "ALTER TABLE report_findings ADD COLUMN first_error_ts_ms INTEGER NOT NULL DEFAULT 0"
                )
            if "last_error_ts_ms" not in cols_findings:
                conn.execute(
                    "ALTER TABLE report_findings ADD COLUMN last_error_ts_ms INTEGER NOT NULL DEFAULT 0"
                )
            if "regression_state" not in cols_findings:
                conn.execute(
                    "ALTER TABLE report_findings ADD COLUMN regression_state TEXT NOT NULL DEFAULT 'new'"
                )
            if "regression_occurrence_count" not in cols_findings:
                conn.execute(
                    "ALTER TABLE report_findings ADD COLUMN regression_occurrence_count INTEGER NOT NULL DEFAULT 1"
                )
            cols_regression = [
                r["name"]
                for r in conn.execute("PRAGMA table_info(finding_regression_status)").fetchall()
            ]
            if "last_seen_report_seq" not in cols_regression:
                conn.execute(
                    "ALTER TABLE finding_regression_status ADD COLUMN last_seen_report_seq INTEGER NOT NULL DEFAULT 0"
                )

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}_{uuid4().hex}"

    @staticmethod
    def _slug(value: str) -> str:
        out = "".join(
            c.lower() if c.isalnum() else "-"
            for c in str(value or "").strip()
        ).strip("-")
        while "--" in out:
            out = out.replace("--", "-")
        return out or "default"

    @staticmethod
    def _dt(value: object) -> Optional[datetime]:
        if not value:
            return None
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    def ensure_workspace(
        self,
        *,
        org_name: str = "Local",
        project_name: str = "Default",
        environment_name: str = "production",
    ) -> WorkspaceIds:
        """Create or return a local cloud-style org/project/environment tuple."""
        org_slug = self._slug(org_name)
        project_slug = self._slug(project_name)
        env_slug = self._slug(environment_name)
        org_id = f"org_{org_slug}"

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO organizations (id, name)
                VALUES (?, ?)
                ON CONFLICT(id) DO UPDATE SET name = excluded.name
                """,
                (org_id, org_name.strip() or "Local"),
            )
            row = conn.execute(
                "SELECT id FROM projects WHERE org_id = ? AND slug = ?",
                (org_id, project_slug),
            ).fetchone()
            if row is None:
                project_id = self._id("proj")
                conn.execute(
                    """
                    INSERT INTO projects (id, org_id, name, slug)
                    VALUES (?, ?, ?, ?)
                    """,
                    (project_id, org_id, project_name.strip() or "Default", project_slug),
                )
            else:
                project_id = str(row["id"])

            row = conn.execute(
                "SELECT id FROM environments WHERE project_id = ? AND slug = ?",
                (project_id, env_slug),
            ).fetchone()
            if row is None:
                environment_id = self._id("env")
                conn.execute(
                    """
                    INSERT INTO environments (id, project_id, name, slug)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        environment_id,
                        project_id,
                        environment_name.strip() or "production",
                        env_slug,
                    ),
                )
            else:
                environment_id = str(row["id"])

        return WorkspaceIds(
            org_id=org_id,
            project_id=project_id,
            environment_id=environment_id,
        )

    def create_sdk_key(
        self,
        *,
        project_id: str,
        environment_id: str,
        name: str,
        key_hash: str,
        prefix: str,
        last4: str,
    ) -> str:
        key_id = self._id("sdk")
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO sdk_keys
                (id, project_id, environment_id, name, prefix, key_hash, last4)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key_id,
                    project_id,
                    environment_id,
                    name.strip() or "SDK key",
                    prefix,
                    key_hash,
                    last4,
                ),
            )
        return key_id

    def add_project_member(
        self,
        *,
        project_id: str,
        email: str,
        role: str = "member",
    ) -> str:
        member_id = self._id("mem")
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO project_members (id, project_id, email, role)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id, email) DO UPDATE SET role = excluded.role
                """,
                (
                    member_id,
                    project_id,
                    email.strip().lower(),
                    role.strip() or "member",
                ),
            )
            row = conn.execute(
                """
                SELECT id FROM project_members
                WHERE project_id = ? AND email = ?
                """,
                (project_id, email.strip().lower()),
            ).fetchone()
        assert row is not None
        return str(row["id"])

    def list_project_members(self, project_id: str) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT id, project_id, email, role, created_at
                FROM project_members
                WHERE project_id = ?
                ORDER BY email
                """,
                (project_id,),
            ).fetchall()

    def get_sdk_key_by_hash(self, key_hash: str) -> Optional[SDKKeyRow]:
        with self._conn() as conn:
            r = conn.execute(
                """
                SELECT id, project_id, environment_id, name, prefix, key_hash, last4,
                       revoked_at, last_used_at, created_at
                FROM sdk_keys
                WHERE key_hash = ?
                """,
                (key_hash,),
            ).fetchone()
        if not r:
            return None
        return SDKKeyRow(
            id=str(r["id"]),
            project_id=str(r["project_id"]),
            environment_id=str(r["environment_id"]),
            name=str(r["name"]),
            prefix=str(r["prefix"]),
            key_hash=str(r["key_hash"]),
            last4=str(r["last4"]),
            revoked_at=self._dt(r["revoked_at"]),
            last_used_at=self._dt(r["last_used_at"]),
            created_at=self._dt(r["created_at"]) or datetime.now(timezone.utc),
        )

    def touch_sdk_key(self, key_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE sdk_keys SET last_used_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), key_id),
            )

    def create_service_token(
        self,
        *,
        project_id: str,
        name: str,
        token_hash: str,
        scopes: list[str],
    ) -> str:
        token_id = self._id("svc")
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO service_tokens
                (id, project_id, name, token_hash, scopes_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    token_id,
                    project_id,
                    name.strip() or "Service token",
                    token_hash,
                    json.dumps([str(s) for s in scopes]),
                ),
            )
        return token_id

    def get_service_token_by_hash(self, token_hash: str) -> Optional[ServiceTokenRow]:
        with self._conn() as conn:
            r = conn.execute(
                """
                SELECT id, project_id, name, token_hash, scopes_json, revoked_at,
                       last_used_at, created_at
                FROM service_tokens
                WHERE token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
        if not r:
            return None
        return ServiceTokenRow(
            id=str(r["id"]),
            project_id=str(r["project_id"]),
            name=str(r["name"]),
            token_hash=str(r["token_hash"]),
            scopes=self._parse_string_list_json(r["scopes_json"]),
            revoked_at=self._dt(r["revoked_at"]),
            last_used_at=self._dt(r["last_used_at"]),
            created_at=self._dt(r["created_at"]) or datetime.now(timezone.utc),
        )

    def touch_service_token(self, token_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE service_tokens SET last_used_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), token_id),
            )

    def revoke_service_token(self, token_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE service_tokens
                SET revoked_at = COALESCE(revoked_at, ?)
                WHERE id = ?
                """,
                (datetime.now(timezone.utc).isoformat(), token_id),
            )
            return int(cur.rowcount) > 0

    def revoke_sdk_key(self, key_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE sdk_keys
                SET revoked_at = COALESCE(revoked_at, ?)
                WHERE id = ?
                """,
                (datetime.now(timezone.utc).isoformat(), key_id),
            )
            return int(cur.rowcount) > 0

    def insert_replay_batch(
        self,
        *,
        project_id: str,
        environment_id: str,
        session_id: str,
        sequence: int,
        events: list[dict[str, object]],
        flush_type: str,
        distinct_id: str = "",
        metadata: Optional[dict[str, object]] = None,
    ) -> ReplayBatchResult:
        now = datetime.now(timezone.utc).isoformat()
        clean_flush = flush_type if flush_type in {"normal", "final"} else "normal"
        event_count = len(events)
        payload_json = json.dumps(
            {
                "sessionId": session_id,
                "sequence": int(sequence),
                "flushType": clean_flush,
                "distinctId": distinct_id,
                "metadata": metadata or {},
                "events": events,
            },
            separators=(",", ":"),
        )

        with self._conn() as conn:
            processing_job_id: Optional[str] = None
            session_row_id = self._id("rs")
            conn.execute(
                """
                INSERT OR IGNORE INTO replay_sessions
                (id, project_id, environment_id, stable_id, distinct_id, started_at,
                 last_seen_at, event_count, metadata_json, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_row_id,
                    project_id,
                    environment_id,
                    session_id,
                    distinct_id or "",
                    now,
                    now,
                    0,
                    json.dumps(metadata or {}),
                    "completed" if clean_flush == "final" else "active",
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT id, metadata_json FROM replay_sessions
                WHERE project_id = ? AND environment_id = ? AND stable_id = ?
                """,
                (project_id, environment_id, session_id),
            ).fetchone()
            assert row is not None
            session_row_id = str(row["id"])
            existing_metadata = self._safe_json_obj(row["metadata_json"])
            merged_metadata = {**existing_metadata, **(metadata or {})}

            batch_id = self._id("rb")
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO replay_batches
                (id, session_row_id, project_id, environment_id, session_id, sequence,
                 flush_type, payload_json, event_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    session_row_id,
                    project_id,
                    environment_id,
                    session_id,
                    int(sequence),
                    clean_flush,
                    payload_json,
                    event_count,
                ),
            )
            inserted = int(cur.rowcount) > 0
            if not inserted:
                existing = conn.execute(
                    """
                    SELECT id, event_count FROM replay_batches
                    WHERE project_id = ? AND environment_id = ? AND session_id = ? AND sequence = ?
                    """,
                    (project_id, environment_id, session_id, int(sequence)),
                ).fetchone()
                assert existing is not None
                batch_id = str(existing["id"])
                event_count = int(existing["event_count"])
            else:
                conn.execute(
                    """
                    UPDATE replay_sessions
                    SET last_seen_at = ?,
                        event_count = event_count + ?,
                        distinct_id = COALESCE(NULLIF(?, ''), distinct_id),
                        metadata_json = ?,
                        status = CASE WHEN ? = 'final' THEN 'completed' ELSE status END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        event_count,
                        distinct_id or "",
                        json.dumps(merged_metadata),
                        clean_flush,
                        now,
                        session_row_id,
                    ),
                )
                if clean_flush == "final":
                    processing_job_id = self.enqueue_processing_job(
                        project_id=project_id,
                        environment_id=environment_id,
                        kind="replay.finalize",
                        subject_id=session_row_id,
                        payload={
                            "session_id": session_id,
                            "batch_id": batch_id,
                            "sequence": int(sequence),
                        },
                        conn=conn,
                    )

        return ReplayBatchResult(
            session_row_id=session_row_id,
            batch_id=batch_id,
            inserted=inserted,
            event_count=event_count,
            processing_job_id=processing_job_id,
        )

    def enqueue_processing_job(
        self,
        *,
        project_id: str,
        environment_id: str,
        kind: str,
        subject_id: str,
        payload: dict[str, object],
        conn: Optional[sqlite3.Connection] = None,
    ) -> str:
        job_id = self._id("job")
        now = datetime.now(timezone.utc).isoformat()

        def _insert(c: sqlite3.Connection) -> str:
            c.execute(
                """
                INSERT OR IGNORE INTO processing_jobs
                (id, project_id, environment_id, kind, subject_id, status, payload_json, updated_at)
                VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    job_id,
                    project_id,
                    environment_id,
                    kind,
                    subject_id,
                    json.dumps(payload),
                    now,
                ),
            )
            row = c.execute(
                """
                SELECT id FROM processing_jobs
                WHERE kind = ? AND subject_id = ?
                """,
                (kind, subject_id),
            ).fetchone()
            assert row is not None
            return str(row["id"])

        if conn is not None:
            return _insert(conn)
        with self._conn() as owned_conn:
            return _insert(owned_conn)

    def list_processing_jobs(
        self,
        *,
        kind: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        where: list[str] = []
        params: list[str] = []
        if kind is not None:
            where.append("kind = ?")
            params.append(kind)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        query = "SELECT * FROM processing_jobs"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY created_at, id"
        with self._conn() as conn:
            return conn.execute(query, params).fetchall()

    def get_replay_session(
        self,
        *,
        project_id: str,
        environment_id: str,
        session_id: str,
    ) -> Optional[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT *
                FROM replay_sessions
                WHERE project_id = ? AND environment_id = ? AND stable_id = ?
                """,
                (project_id, environment_id, session_id),
            ).fetchone()

    def list_replay_batches(
        self,
        *,
        project_id: str,
        environment_id: str,
        session_id: str,
    ) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT *
                FROM replay_batches
                WHERE project_id = ? AND environment_id = ? AND session_id = ?
                ORDER BY sequence
                """,
                (project_id, environment_id, session_id),
            ).fetchall()

    def upsert_session(self, s: SessionMeta) -> None:
        if s.started_at.tzinfo is None:
            raise ValueError("SessionMeta.started_at must be timezone-aware")
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO sessions (id, project_id, started_at, duration_ms, distinct_id, event_count)
                VALUES (:id, :project_id, :started_at, :duration_ms, :distinct_id, :event_count)
                ON CONFLICT(id) DO UPDATE SET
                    duration_ms = excluded.duration_ms,
                    event_count = excluded.event_count
                """,
                {**asdict(s), "started_at": s.started_at.isoformat()},
            )

    def get_session(self, sid: str) -> Optional[SessionMeta]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, project_id, started_at, duration_ms, distinct_id, event_count FROM sessions WHERE id = ?",
                (sid,),
            ).fetchone()
        if not row:
            return None
        return SessionMeta(
            id=row["id"],
            project_id=row["project_id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            duration_ms=row["duration_ms"],
            distinct_id=row["distinct_id"],
            event_count=row["event_count"],
        )

    def get_last_run_cursor(self) -> Optional[datetime]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'last_run_cursor'"
            ).fetchone()
        if not row:
            return None
        return datetime.fromisoformat(row["value"])

    def set_last_run_cursor(self, ts: datetime) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO meta (key, value) VALUES ('last_run_cursor', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (ts.isoformat(),),
            )

    def start_run(self) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at) VALUES (?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            rid = cur.lastrowid
            assert rid is not None
            return rid

    def finish_run(
        self,
        run_id: int,
        *,
        sessions_scanned: int,
        findings_count: int,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE runs SET finished_at = ?, sessions_scanned = ?, findings_count = ?, status = ?, error = ?
                WHERE id = ?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    sessions_scanned,
                    findings_count,
                    status,
                    error,
                    run_id,
                ),
            )

    def get_run(self, run_id: int) -> Optional[RunRow]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        return RunRow(
            id=row["id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"])
            if row["finished_at"]
            else None,
            sessions_scanned=row["sessions_scanned"],
            findings_count=row["findings_count"],
            status=row["status"],
            error=row["error"],
        )

    def upsert_github_repo(
        self,
        *,
        repo_full_name: str,
        default_branch: str,
        remote_url: str = "",
        local_path: str = "",
        provider: str = "github",
    ) -> int:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO github_repos (repo_full_name, default_branch, remote_url, local_path, provider)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(repo_full_name) DO UPDATE SET
                    default_branch = excluded.default_branch,
                    remote_url = excluded.remote_url,
                    local_path = excluded.local_path,
                    provider = excluded.provider
                """,
                (repo_full_name, default_branch, remote_url, local_path, provider),
            )
            row = conn.execute(
                "SELECT id FROM github_repos WHERE repo_full_name = ?",
                (repo_full_name,),
            ).fetchone()
        assert row is not None
        return int(row["id"])

    def list_github_repos(self) -> list[GitHubRepoRow]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, repo_full_name, default_branch, remote_url, local_path, provider, connected_at
                FROM github_repos
                ORDER BY repo_full_name
                """
            ).fetchall()
        return [
            GitHubRepoRow(
                id=int(r["id"]),
                repo_full_name=str(r["repo_full_name"]),
                default_branch=str(r["default_branch"]),
                remote_url=str(r["remote_url"]),
                local_path=str(r["local_path"]),
                provider=str(r["provider"]),
                connected_at=datetime.fromisoformat(
                    str(r["connected_at"]).replace("Z", "+00:00")
                ),
            )
            for r in rows
        ]

    def get_github_repo(self, repo_full_name: str) -> Optional[GitHubRepoRow]:
        with self._conn() as conn:
            r = conn.execute(
                """
                SELECT id, repo_full_name, default_branch, remote_url, local_path, provider, connected_at
                FROM github_repos
                WHERE repo_full_name = ?
                """,
                (repo_full_name,),
            ).fetchone()
        if not r:
            return None
        return GitHubRepoRow(
            id=int(r["id"]),
            repo_full_name=str(r["repo_full_name"]),
            default_branch=str(r["default_branch"]),
            remote_url=str(r["remote_url"]),
            local_path=str(r["local_path"]),
            provider=str(r["provider"]),
            connected_at=datetime.fromisoformat(
                str(r["connected_at"]).replace("Z", "+00:00")
            ),
        )

    def delete_github_repo(self, repo_full_name: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM github_repos WHERE repo_full_name = ?",
                (repo_full_name,),
            )
            return int(cur.rowcount)

    def upsert_report_finding(
        self,
        *,
        report_path: str,
        finding_hash: str,
        title: str,
        severity: str,
        category: str,
        session_url: str,
        evidence_text: str = "",
        distinct_id: str = "",
        error_issue_ids: Optional[list[str]] = None,
        trace_ids: Optional[list[str]] = None,
        top_stack_frame: str = "",
        error_tracking_url: str = "",
        logs_url: str = "",
        first_error_ts_ms: int = 0,
        last_error_ts_ms: int = 0,
        regression_state: str = "new",
        regression_occurrence_count: int = 1,
    ) -> int:
        error_ids = json.dumps(error_issue_ids or [])
        trace_json = json.dumps(trace_ids or [])
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO report_findings
                (
                    report_path, finding_hash, title, severity, category, session_url, evidence_text,
                    distinct_id, error_issue_ids_json, trace_ids_json, top_stack_frame, error_tracking_url,
                    logs_url, first_error_ts_ms, last_error_ts_ms
                    , regression_state, regression_occurrence_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_path, finding_hash) DO UPDATE SET
                    title = excluded.title,
                    severity = excluded.severity,
                    category = excluded.category,
                    session_url = excluded.session_url,
                    evidence_text = excluded.evidence_text,
                    distinct_id = excluded.distinct_id,
                    error_issue_ids_json = excluded.error_issue_ids_json,
                    trace_ids_json = excluded.trace_ids_json,
                    top_stack_frame = excluded.top_stack_frame,
                    error_tracking_url = excluded.error_tracking_url,
                    logs_url = excluded.logs_url,
                    first_error_ts_ms = excluded.first_error_ts_ms,
                    last_error_ts_ms = excluded.last_error_ts_ms,
                    regression_state = excluded.regression_state,
                    regression_occurrence_count = excluded.regression_occurrence_count
                """,
                (
                    report_path,
                    finding_hash,
                    title,
                    severity,
                    category,
                    session_url,
                    evidence_text,
                    distinct_id,
                    error_ids,
                    trace_json,
                    top_stack_frame,
                    error_tracking_url,
                    logs_url,
                    int(first_error_ts_ms),
                    int(last_error_ts_ms),
                    str(regression_state or "new"),
                    int(regression_occurrence_count or 1),
                ),
            )
            row = conn.execute(
                """
                SELECT id FROM report_findings
                WHERE report_path = ? AND finding_hash = ?
                """,
                (report_path, finding_hash),
            ).fetchone()
        assert row is not None
        return int(row["id"])

    def list_report_findings(
        self, report_path: Optional[str] = None
    ) -> list[ReportFindingRow]:
        with self._conn() as conn:
            if report_path is None:
                rows = conn.execute(
                    """
                    SELECT
                        id, report_path, finding_hash, title, severity, category, session_url, evidence_text,
                        distinct_id, error_issue_ids_json, trace_ids_json, top_stack_frame,
                        error_tracking_url, logs_url, first_error_ts_ms, last_error_ts_ms,
                        regression_state, regression_occurrence_count, created_at
                    FROM report_findings
                    ORDER BY id
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT
                        id, report_path, finding_hash, title, severity, category, session_url, evidence_text,
                        distinct_id, error_issue_ids_json, trace_ids_json, top_stack_frame,
                        error_tracking_url, logs_url, first_error_ts_ms, last_error_ts_ms,
                        regression_state, regression_occurrence_count, created_at
                    FROM report_findings
                    WHERE report_path = ?
                    ORDER BY id
                    """,
                    (report_path,),
                ).fetchall()
        return [
            ReportFindingRow(
                id=int(r["id"]),
                report_path=str(r["report_path"]),
                finding_hash=str(r["finding_hash"]),
                title=str(r["title"]),
                severity=str(r["severity"]),
                category=str(r["category"]),
                session_url=str(r["session_url"]),
                evidence_text=str(r["evidence_text"]),
                distinct_id=str(r["distinct_id"] or ""),
                error_issue_ids=self._parse_string_list_json(r["error_issue_ids_json"]),
                trace_ids=self._parse_string_list_json(r["trace_ids_json"]),
                top_stack_frame=str(r["top_stack_frame"] or ""),
                error_tracking_url=str(r["error_tracking_url"] or ""),
                logs_url=str(r["logs_url"] or ""),
                first_error_ts_ms=int(r["first_error_ts_ms"] or 0),
                last_error_ts_ms=int(r["last_error_ts_ms"] or 0),
                regression_state=str(r["regression_state"] or "new"),
                regression_occurrence_count=int(r["regression_occurrence_count"] or 1),
                created_at=datetime.fromisoformat(
                    str(r["created_at"]).replace("Z", "+00:00")
                ),
            )
            for r in rows
        ]

    def reconcile_regression_states(
        self,
        *,
        report_path: str,
        finding_hashes: list[str],
    ) -> dict[str, tuple[str, int]]:
        now = datetime.now(timezone.utc).isoformat()
        unique_hashes = list(dict.fromkeys(finding_hashes))
        result: dict[str, tuple[str, int]] = {}

        # Derive current report sequence from timestamp in path or use current timestamp
        current_report_seq = int(datetime.now(timezone.utc).timestamp() * 1000)

        with self._conn() as conn:
            for h in unique_hashes:
                row = conn.execute(
                    """
                    SELECT status, occurrence_count, last_seen_report_seq, last_seen_report_path
                    FROM finding_regression_status
                    WHERE finding_hash = ?
                    """,
                    (h,),
                ).fetchone()
                if row is None:
                    status = "new"
                    occ = 1
                    conn.execute(
                        """
                        INSERT INTO finding_regression_status
                        (finding_hash, status, first_seen_report_path, last_seen_report_path, last_seen_report_seq, occurrence_count, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (h, status, report_path, report_path, current_report_seq, occ, now),
                    )
                else:
                    prev_status = str(row["status"] or "new")
                    prev_occ = int(row["occurrence_count"] or 0)
                    prev_report_path = row["last_seen_report_path"]

                    # Skip update if re-importing the same report
                    if prev_report_path == report_path:
                        result[h] = (prev_status, prev_occ)
                        continue

                    if prev_status == "resolved":
                        status = "regressed"
                    elif prev_status in {"new", "ongoing", "regressed"}:
                        status = "ongoing"
                    else:
                        status = "ongoing"
                    occ = prev_occ + 1
                    conn.execute(
                        """
                        UPDATE finding_regression_status
                        SET status = ?, last_seen_report_path = ?, last_seen_report_seq = ?, occurrence_count = ?, updated_at = ?
                        WHERE finding_hash = ?
                        """,
                        (status, report_path, current_report_seq, occ, now, h),
                    )
                result[h] = (status, occ)

            # Any previously active finding not present in this report becomes resolved,
            # but only if the last seen sequence is less than current (prevents older reports from resolving newer findings).
            conn.execute(
                """
                UPDATE finding_regression_status
                SET status = 'resolved', updated_at = ?
                WHERE status IN ('new','ongoing','regressed')
                  AND last_seen_report_seq < ?
                  AND finding_hash NOT IN (SELECT DISTINCT value FROM json_each(?))
                """,
                (now, current_report_seq, json.dumps(unique_hashes)),
            )

            # Sync this report rows with computed states.
            for h, (status, occ) in result.items():
                conn.execute(
                    """
                    UPDATE report_findings
                    SET regression_state = ?, regression_occurrence_count = ?
                    WHERE report_path = ? AND finding_hash = ?
                    """,
                    (status, occ, report_path, h),
                )

            # Remove stale findings for this report_path that are no longer present.
            if unique_hashes:
                # Build parameterized query for NOT IN clause
                placeholders = ",".join("?" * len(unique_hashes))
                conn.execute(
                    f"""
                    DELETE FROM report_findings
                    WHERE report_path = ? AND finding_hash NOT IN ({placeholders})
                    """,
                    [report_path] + unique_hashes,
                )
            else:
                # If no hashes in result, delete all rows for this report_path
                conn.execute(
                    """
                    DELETE FROM report_findings
                    WHERE report_path = ?
                    """,
                    (report_path,),
                )
        return result

    @staticmethod
    def _parse_string_list_json(raw: object) -> list[str]:
        try:
            parsed = json.loads(str(raw or "[]"))
        except Exception:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item) for item in parsed]

    @staticmethod
    def _safe_json_obj(raw: object) -> dict[str, object]:
        try:
            parsed = json.loads(str(raw or "{}"))
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def replace_code_candidates(
        self,
        *,
        finding_id: int,
        repo_id: int,
        candidates: list[tuple[str, Optional[str], float, str]],
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM code_candidates WHERE finding_id = ? AND repo_id = ?",
                (finding_id, repo_id),
            )
            conn.executemany(
                """
                INSERT INTO code_candidates (finding_id, repo_id, file_path, symbol, score, rationale_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (finding_id, repo_id, fp, sym, score, rationale)
                    for fp, sym, score, rationale in candidates
                ],
            )

    def list_code_candidates(
        self,
        *,
        finding_id: int,
        repo_id: int,
    ) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT file_path, symbol, score, rationale_json
                FROM code_candidates
                WHERE finding_id = ? AND repo_id = ?
                ORDER BY score DESC, file_path
                """,
                (finding_id, repo_id),
            ).fetchall()

    def replace_fix_prompts(
        self,
        *,
        finding_id: int,
        repo_id: int,
        prompts: list[tuple[str, str, str]],
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM fix_prompts WHERE finding_id = ? AND repo_id = ?",
                (finding_id, repo_id),
            )
            conn.executemany(
                """
                INSERT INTO fix_prompts (finding_id, repo_id, agent_target, prompt_markdown, prompt_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                [(finding_id, repo_id, target, md, pj) for target, md, pj in prompts],
            )

    def list_fix_prompts(
        self,
        *,
        finding_id: int,
        repo_id: Optional[int] = None,
    ) -> list[FixPromptRow]:
        with self._conn() as conn:
            if repo_id is None:
                rows = conn.execute(
                    """
                    SELECT id, finding_id, repo_id, agent_target, prompt_markdown, prompt_json, created_at
                    FROM fix_prompts
                    WHERE finding_id = ?
                    ORDER BY id
                    """,
                    (finding_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, finding_id, repo_id, agent_target, prompt_markdown, prompt_json, created_at
                    FROM fix_prompts
                    WHERE finding_id = ? AND repo_id = ?
                    ORDER BY id
                    """,
                    (finding_id, repo_id),
                ).fetchall()
        return [
            FixPromptRow(
                id=int(r["id"]),
                finding_id=int(r["finding_id"]),
                repo_id=int(r["repo_id"]),
                agent_target=str(r["agent_target"]),
                prompt_markdown=str(r["prompt_markdown"]),
                prompt_json=str(r["prompt_json"]),
                created_at=datetime.fromisoformat(
                    str(r["created_at"]).replace("Z", "+00:00")
                ),
            )
            for r in rows
        ]
