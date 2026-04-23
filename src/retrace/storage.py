from __future__ import annotations

import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
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
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(report_path, finding_hash)
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
            cols_repo = [r["name"] for r in conn.execute("PRAGMA table_info(github_repos)").fetchall()]
            if "local_path" not in cols_repo:
                conn.execute("ALTER TABLE github_repos ADD COLUMN local_path TEXT NOT NULL DEFAULT ''")
            cols_findings = [r["name"] for r in conn.execute("PRAGMA table_info(report_findings)").fetchall()]
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
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
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
                connected_at=datetime.fromisoformat(str(r["connected_at"]).replace("Z", "+00:00")),
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
            connected_at=datetime.fromisoformat(str(r["connected_at"]).replace("Z", "+00:00")),
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
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    last_error_ts_ms = excluded.last_error_ts_ms
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

    def list_report_findings(self, report_path: Optional[str] = None) -> list[ReportFindingRow]:
        with self._conn() as conn:
            if report_path is None:
                rows = conn.execute(
                    """
                    SELECT
                        id, report_path, finding_hash, title, severity, category, session_url, evidence_text,
                        distinct_id, error_issue_ids_json, trace_ids_json, top_stack_frame,
                        error_tracking_url, logs_url, first_error_ts_ms, last_error_ts_ms, created_at
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
                        error_tracking_url, logs_url, first_error_ts_ms, last_error_ts_ms, created_at
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
                created_at=datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00")),
            )
            for r in rows
        ]

    @staticmethod
    def _parse_string_list_json(raw: object) -> list[str]:
        try:
            parsed = json.loads(str(raw or "[]"))
        except Exception:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item) for item in parsed]

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
                [(finding_id, repo_id, fp, sym, score, rationale) for fp, sym, score, rationale in candidates],
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
                created_at=datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00")),
            )
            for r in rows
        ]
