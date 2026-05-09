from __future__ import annotations

import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import hashlib
from uuid import uuid4
import json
from pathlib import Path
from typing import Any, Optional, Protocol

from retrace.evidence import (
    PROMPT_SAFE_REDACTION_STATES,
    EvidenceItem,
    evidence_dedupe_key,
    evidence_items_from_replay_issue,
)
from retrace.failures import CanonicalFailure, canonical_failure_from_replay_issue
from retrace.repair import normalize_repair_task_status

FAILURE_TEST_COVERAGE_STATES = (
    "not_covered",
    "covered_unverified",
    "covered_passing",
    "covered_failing",
    "covered_flaky",
)

_SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _rollup_severity(values: list[str]) -> str:
    highest = "medium"
    highest_score = 0
    for value in values:
        severity = str(value or "medium").strip().lower()
        score = _SEVERITY_ORDER.get(severity, 2)
        if score > highest_score:
            highest = severity if severity in _SEVERITY_ORDER else "medium"
            highest_score = score
    return highest

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
    public_id TEXT NOT NULL,
    distinct_id TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    preview_json TEXT NOT NULL DEFAULT '{}',
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
    blob_backend TEXT NOT NULL DEFAULT '',
    blob_key TEXT NOT NULL DEFAULT '',
    event_count INTEGER NOT NULL DEFAULT 0,
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, environment_id, session_id, sequence)
);

CREATE TABLE IF NOT EXISTS replay_signals (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    detector TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    details_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, environment_id, session_id, detector, timestamp_ms, details_hash)
);

CREATE TABLE IF NOT EXISTS signal_definitions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    detector TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    run_mode TEXT NOT NULL DEFAULT 'replay_finalize',
    thresholds_json TEXT NOT NULL DEFAULT '{}',
    prompt_json TEXT NOT NULL DEFAULT '{}',
    custom_definition TEXT NOT NULL DEFAULT '',
    match_count INTEGER NOT NULL DEFAULT 0,
    last_match_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, environment_id, detector)
);

CREATE TABLE IF NOT EXISTS failures (
    id TEXT PRIMARY KEY,
    public_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_external_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'medium',
    confidence TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'new',
    affected_users INTEGER NOT NULL DEFAULT 0,
    affected_sessions INTEGER NOT NULL DEFAULT 0,
    first_seen_ms INTEGER NOT NULL DEFAULT 0,
    last_seen_ms INTEGER NOT NULL DEFAULT 0,
    related_deploy_sha TEXT NOT NULL DEFAULT '',
    related_pr_number INTEGER,
    linked_tests_json TEXT NOT NULL DEFAULT '[]',
    linked_repair_task_id TEXT NOT NULL DEFAULT '',
    linked_external_thread_id TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, environment_id, source_type, source_external_id)
);

CREATE INDEX IF NOT EXISTS idx_failures_scope_status
ON failures(project_id, environment_id, status, updated_at);

CREATE INDEX IF NOT EXISTS idx_failures_fingerprint
ON failures(project_id, environment_id, fingerprint);

CREATE TABLE IF NOT EXISTS failure_evidence (
    id TEXT PRIMARY KEY,
    failure_id TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    occurred_at_ms INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    redaction_state TEXT NOT NULL DEFAULT 'raw',
    payload_json TEXT NOT NULL DEFAULT '{}',
    artifact_path TEXT NOT NULL DEFAULT '',
    dedupe_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_failure_evidence_failure_time
ON failure_evidence(failure_id, occurred_at_ms, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_failure_evidence_dedupe
ON failure_evidence(failure_id, dedupe_key)
WHERE dedupe_key != '';

CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    public_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    group_key TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'open',
    failure_count INTEGER NOT NULL DEFAULT 0,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    repair_task_id TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, environment_id, group_key)
);

CREATE INDEX IF NOT EXISTS idx_incidents_scope_status
ON incidents(project_id, environment_id, status, updated_at);

CREATE TABLE IF NOT EXISTS incident_failures (
    incident_id TEXT NOT NULL,
    failure_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(incident_id, failure_id)
);

CREATE INDEX IF NOT EXISTS idx_incident_failures_failure
ON incident_failures(failure_id, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_incident_failures_one_incident_per_failure
ON incident_failures(failure_id);

CREATE TABLE IF NOT EXISTS deploy_markers (
    id TEXT PRIMARY KEY,
    public_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    sha TEXT NOT NULL,
    branch TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    deployed_at_ms INTEGER NOT NULL DEFAULT 0,
    changed_files_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, environment_id, sha)
);

CREATE INDEX IF NOT EXISTS idx_deploy_markers_scope_time
ON deploy_markers(project_id, environment_id, deployed_at_ms DESC);

CREATE TABLE IF NOT EXISTS failure_test_links (
    id TEXT PRIMARY KEY,
    failure_id TEXT NOT NULL,
    issue_id TEXT NOT NULL DEFAULT '',
    issue_public_id TEXT NOT NULL DEFAULT '',
    spec_id TEXT NOT NULL,
    spec_name TEXT NOT NULL DEFAULT '',
    spec_path TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    coverage_state TEXT NOT NULL DEFAULT 'covered_unverified',
    latest_run_id TEXT NOT NULL DEFAULT '',
    latest_run_status TEXT NOT NULL DEFAULT '',
    latest_run_classification TEXT NOT NULL DEFAULT '',
    latest_run_ok INTEGER,
    latest_run_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(failure_id, spec_id)
);

CREATE INDEX IF NOT EXISTS idx_failure_test_links_failure
ON failure_test_links(failure_id, coverage_state, updated_at);

CREATE INDEX IF NOT EXISTS idx_failure_test_links_issue
ON failure_test_links(issue_public_id, updated_at);

CREATE INDEX IF NOT EXISTS idx_failure_test_links_spec
ON failure_test_links(spec_id, updated_at);

CREATE TABLE IF NOT EXISTS repair_tasks (
    id TEXT PRIMARY KEY,
    public_id TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT '',
    environment_id TEXT NOT NULL DEFAULT '',
    failure_id TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT '',
    source_external_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    likely_files_json TEXT NOT NULL DEFAULT '[]',
    prompt_artifacts_json TEXT NOT NULL DEFAULT '[]',
    validation_commands_json TEXT NOT NULL DEFAULT '[]',
    branch TEXT NOT NULL DEFAULT '',
    pr_url TEXT NOT NULL DEFAULT '',
    risk_notes TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(failure_id, project_id, environment_id)
);

CREATE INDEX IF NOT EXISTS idx_repair_tasks_status
ON repair_tasks(status, updated_at);

CREATE INDEX IF NOT EXISTS idx_repair_tasks_source
ON repair_tasks(source_type, source_external_id, project_id, environment_id);

CREATE TABLE IF NOT EXISTS repair_task_evidence (
    repair_task_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'supporting',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(repair_task_id, evidence_id)
);

CREATE INDEX IF NOT EXISTS idx_repair_task_evidence_task
ON repair_task_evidence(repair_task_id, created_at);

CREATE TABLE IF NOT EXISTS replay_issues (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    public_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    fingerprint_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'new',
    priority TEXT NOT NULL DEFAULT 'medium',
    severity TEXT NOT NULL DEFAULT 'medium',
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    likely_cause TEXT NOT NULL DEFAULT '',
    reproduction_steps_json TEXT NOT NULL DEFAULT '[]',
    confidence TEXT NOT NULL DEFAULT 'medium',
    analysis_status TEXT NOT NULL DEFAULT '',
    analysis_model TEXT NOT NULL DEFAULT '',
    analysis_prompt_version TEXT NOT NULL DEFAULT '',
    analysis_created_at TEXT,
    analysis_error TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    signal_summary_json TEXT NOT NULL DEFAULT '{}',
    affected_count INTEGER NOT NULL DEFAULT 0,
    affected_users INTEGER NOT NULL DEFAULT 0,
    representative_session_id TEXT NOT NULL DEFAULT '',
    external_ticket_state TEXT NOT NULL DEFAULT '',
    external_ticket_url TEXT NOT NULL DEFAULT '',
    external_ticket_id TEXT NOT NULL DEFAULT '',
    canonical_failure_id TEXT NOT NULL DEFAULT '',
    distinct_id TEXT NOT NULL DEFAULT '',
    error_issue_ids_json TEXT NOT NULL DEFAULT '[]',
    trace_ids_json TEXT NOT NULL DEFAULT '[]',
    top_stack_frame TEXT NOT NULL DEFAULT '',
    error_tracking_url TEXT NOT NULL DEFAULT '',
    logs_url TEXT NOT NULL DEFAULT '',
    first_seen_ms INTEGER NOT NULL DEFAULT 0,
    last_seen_ms INTEGER NOT NULL DEFAULT 0,
    resolved_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, environment_id, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_replay_issues_public
ON replay_issues(project_id, environment_id, public_id);

CREATE TABLE IF NOT EXISTS replay_issue_sessions (
    issue_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'supporting',
    first_seen_ms INTEGER NOT NULL DEFAULT 0,
    last_seen_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(issue_id, session_id)
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
class SignalDefinitionRow:
    id: str
    project_id: str
    environment_id: str
    detector: str
    enabled: bool
    run_mode: str
    thresholds: dict[str, Any]
    prompt: dict[str, Any]
    custom_definition: str
    match_count: int
    last_match_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


@dataclass
class FailureRow:
    id: str
    public_id: str
    project_id: str
    environment_id: str
    source_type: str
    source_external_id: str
    fingerprint: str
    title: str
    summary: str
    severity: str
    confidence: str
    status: str
    affected_users: int
    affected_sessions: int
    first_seen_ms: int
    last_seen_ms: int
    related_deploy_sha: str
    related_pr_number: Optional[int]
    linked_tests: list[str]
    linked_repair_task_id: str
    linked_external_thread_id: str
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass
class EvidenceRow:
    id: str
    failure_id: str
    evidence_type: str
    occurred_at_ms: int
    source: str
    redaction_state: str
    payload: dict[str, Any]
    artifact_path: str
    dedupe_key: str
    created_at: datetime

    @property
    def safe_for_prompts(self) -> bool:
        return self.redaction_state in PROMPT_SAFE_REDACTION_STATES


@dataclass
class IncidentRow:
    id: str
    public_id: str
    project_id: str
    environment_id: str
    group_key: str
    title: str
    summary: str
    severity: str
    status: str
    failure_count: int
    evidence_count: int
    repair_task_id: str
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass
class DeployMarkerRow:
    id: str
    public_id: str
    project_id: str
    environment_id: str
    sha: str
    branch: str
    author: str
    deployed_at_ms: int
    changed_files: list[str]
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass
class FailureTestLinkRow:
    id: str
    failure_id: str
    issue_id: str
    issue_public_id: str
    spec_id: str
    spec_name: str
    spec_path: str
    source: str
    coverage_state: str
    latest_run_id: str
    latest_run_status: str
    latest_run_classification: str
    latest_run_ok: Optional[bool]
    latest_run_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


@dataclass
class RepairTaskRow:
    id: str
    public_id: str
    project_id: str
    environment_id: str
    failure_id: str
    source_type: str
    source_external_id: str
    title: str
    status: str
    likely_files: list[str]
    prompt_artifacts: list[dict[str, Any]]
    validation_commands: list[str]
    branch: str
    pr_url: str
    risk_notes: str
    metadata: dict[str, Any]
    evidence_ids: list[str]
    created_at: datetime
    updated_at: datetime


@dataclass
class ReplayBatchResult:
    session_row_id: str
    batch_id: str
    inserted: bool
    event_count: int
    processing_job_id: Optional[str] = None


@dataclass
class ReplayPlayback:
    session: sqlite3.Row
    batches: list[sqlite3.Row]
    events: list[dict[str, Any]]


@dataclass
class ReplayIssueUpsertResult:
    issue_id: str
    public_id: str
    inserted: bool
    previous_status: str = ""
    current_status: str = ""
    previous_resolved_at: str = ""

    @property
    def regressed(self) -> bool:
        return (
            self.previous_status == "resolved" and self.current_status == "regressed"
        )


@dataclass
class ProcessingJobUpdateResult:
    job_id: str
    updated: bool


class ReplayBlobStore(Protocol):
    backend: str

    def write_events(
        self,
        *,
        project_id: str,
        environment_id: str,
        session_id: str,
        sequence: int,
        events: list[dict[str, object]],
    ) -> str:
        ...

    def read_events(self, key: str) -> list[dict[str, Any]]:
        ...


class LocalReplayBlobStore:
    backend = "local_filesystem"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_part(value: object) -> str:
        raw = str(value or "").strip()
        safe = "".join(c if c.isalnum() or c in {"-", "_", "."} else "_" for c in raw)
        return safe or "default"

    def write_events(
        self,
        *,
        project_id: str,
        environment_id: str,
        session_id: str,
        sequence: int,
        events: list[dict[str, object]],
    ) -> str:
        key = "/".join(
            [
                self._safe_part(project_id),
                self._safe_part(environment_id),
                self._safe_part(session_id),
                f"{int(sequence):012d}.json",
            ]
        )
        path = (self.root / key).resolve()
        root = self.root.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("replay blob key escaped storage root") from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(events, separators=(",", ":")) + "\n", encoding="utf-8")
        return key

    def read_events(self, key: str) -> list[dict[str, Any]]:
        path = (self.root / key).resolve()
        root = self.root.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("replay blob key escaped storage root") from exc
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]


class Storage:
    def __init__(self, path: Path, replay_blob_dir: Optional[Path] = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.replay_blob_store: ReplayBlobStore | None = (
            LocalReplayBlobStore(replay_blob_dir) if replay_blob_dir is not None else None
        )

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
            cols_replay_sessions = [
                r["name"]
                for r in conn.execute("PRAGMA table_info(replay_sessions)").fetchall()
            ]
            if "public_id" not in cols_replay_sessions:
                conn.execute(
                    "ALTER TABLE replay_sessions ADD COLUMN public_id TEXT NOT NULL DEFAULT ''"
                )
            if "preview_json" not in cols_replay_sessions:
                conn.execute(
                    "ALTER TABLE replay_sessions ADD COLUMN preview_json TEXT NOT NULL DEFAULT '{}'"
                )
            cols_replay_batches = [
                r["name"]
                for r in conn.execute("PRAGMA table_info(replay_batches)").fetchall()
            ]
            if "blob_backend" not in cols_replay_batches:
                conn.execute(
                    "ALTER TABLE replay_batches ADD COLUMN blob_backend TEXT NOT NULL DEFAULT ''"
                )
            if "blob_key" not in cols_replay_batches:
                conn.execute(
                    "ALTER TABLE replay_batches ADD COLUMN blob_key TEXT NOT NULL DEFAULT ''"
                )
            cols_replay_issues = [
                r["name"]
                for r in conn.execute("PRAGMA table_info(replay_issues)").fetchall()
            ]
            replay_issue_columns = {
                "fingerprint_version": "INTEGER NOT NULL DEFAULT 1",
                "affected_users": "INTEGER NOT NULL DEFAULT 0",
                "representative_session_id": "TEXT NOT NULL DEFAULT ''",
                "external_ticket_state": "TEXT NOT NULL DEFAULT ''",
                "external_ticket_url": "TEXT NOT NULL DEFAULT ''",
                "external_ticket_id": "TEXT NOT NULL DEFAULT ''",
                "canonical_failure_id": "TEXT NOT NULL DEFAULT ''",
                "analysis_status": "TEXT NOT NULL DEFAULT ''",
                "analysis_model": "TEXT NOT NULL DEFAULT ''",
                "analysis_prompt_version": "TEXT NOT NULL DEFAULT ''",
                "analysis_created_at": "TEXT",
                "analysis_error": "TEXT NOT NULL DEFAULT ''",
                "evidence_json": "TEXT NOT NULL DEFAULT '{}'",
                "distinct_id": "TEXT NOT NULL DEFAULT ''",
                "error_issue_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "trace_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "top_stack_frame": "TEXT NOT NULL DEFAULT ''",
                "error_tracking_url": "TEXT NOT NULL DEFAULT ''",
                "logs_url": "TEXT NOT NULL DEFAULT ''",
            }
            for column, ddl in replay_issue_columns.items():
                if column not in cols_replay_issues:
                    conn.execute(f"ALTER TABLE replay_issues ADD COLUMN {column} {ddl}")
            conn.execute(
                """
                UPDATE replay_issues
                SET status = 'new'
                WHERE status = 'open'
                """
            )
            cols_replay_issue_sessions = [
                r["name"]
                for r in conn.execute("PRAGMA table_info(replay_issue_sessions)").fetchall()
            ]
            if "role" not in cols_replay_issue_sessions:
                conn.execute(
                    "ALTER TABLE replay_issue_sessions ADD COLUMN role TEXT NOT NULL DEFAULT 'supporting'"
                )
            cols_failure_test_links = [
                r["name"]
                for r in conn.execute("PRAGMA table_info(failure_test_links)").fetchall()
            ]
            if "latest_run_classification" not in cols_failure_test_links:
                conn.execute(
                    "ALTER TABLE failure_test_links ADD COLUMN latest_run_classification TEXT NOT NULL DEFAULT ''"
                )
            cols_repair_tasks = [
                r["name"]
                for r in conn.execute("PRAGMA table_info(repair_tasks)").fetchall()
            ]
            if "project_id" not in cols_repair_tasks:
                conn.execute(
                    "ALTER TABLE repair_tasks ADD COLUMN project_id TEXT NOT NULL DEFAULT ''"
                )
            if "environment_id" not in cols_repair_tasks:
                conn.execute(
                    "ALTER TABLE repair_tasks ADD COLUMN environment_id TEXT NOT NULL DEFAULT ''"
                )
            rows = conn.execute(
                """
                SELECT id, project_id, environment_id, stable_id
                FROM replay_sessions
                WHERE public_id IS NULL OR public_id = ''
                """
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE replay_sessions SET public_id = ? WHERE id = ?",
                    (
                        self.make_replay_public_id(
                            str(row["project_id"]),
                            str(row["environment_id"]),
                            str(row["stable_id"]),
                        ),
                        row["id"],
                    ),
                )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_replay_sessions_public
                ON replay_sessions(project_id, environment_id, public_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_replay_batches_blob
                ON replay_batches(blob_backend, blob_key)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_signal_definitions_scope
                ON signal_definitions(project_id, environment_id, enabled)
                """
            )
            self._backfill_failure_test_links(conn)

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}_{uuid4().hex}"

    @staticmethod
    def _public_id(prefix: str, *parts: object) -> str:
        raw = "\x1f".join(str(p) for p in parts)
        return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"

    @classmethod
    def make_replay_public_id(
        cls, project_id: str, environment_id: str, session_id: str
    ) -> str:
        return cls._public_id("rpl", project_id, environment_id, session_id)

    @classmethod
    def make_issue_public_id(
        cls, project_id: str, environment_id: str, fingerprint: str
    ) -> str:
        return cls._public_id("bug", project_id, environment_id, fingerprint)

    @staticmethod
    def _replay_preview(events: list[dict[str, object]]) -> dict[str, object]:
        preview: dict[str, object] = {"event_count": len(events)}
        timestamps = [
            int(event["timestamp"])
            for event in events
            if isinstance(event.get("timestamp"), int)
        ]
        if timestamps:
            preview["first_timestamp_ms"] = min(timestamps)
            preview["last_timestamp_ms"] = max(timestamps)
        for event in events:
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            href = data.get("href")
            if event.get("type") == 4 and isinstance(href, str) and href:
                preview["url"] = href
                break
        return preview

    @staticmethod
    def _merge_replay_preview(
        existing: dict[str, Any],
        incoming: dict[str, object],
        *,
        event_count: int,
    ) -> dict[str, object]:
        merged: dict[str, object] = {**existing, "event_count": int(event_count)}
        for key, reducer in (
            ("first_timestamp_ms", min),
            ("last_timestamp_ms", max),
        ):
            old = existing.get(key)
            new = incoming.get(key)
            if isinstance(old, int) and isinstance(new, int):
                merged[key] = reducer(old, new)
            elif isinstance(new, int):
                merged[key] = new
            elif isinstance(old, int):
                merged[key] = old
        if not merged.get("url") and incoming.get("url"):
            merged["url"] = str(incoming["url"])
        return merged

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

    @staticmethod
    def _now_iso_microseconds() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

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

    def ensure_signal_definitions(
        self,
        *,
        project_id: str,
        environment_id: str,
        detector_names: list[str],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            for detector in detector_names:
                clean = str(detector).strip()
                if not clean:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO signal_definitions
                    (id, project_id, environment_id, detector, enabled, run_mode,
                     thresholds_json, prompt_json, custom_definition, updated_at)
                    VALUES (?, ?, ?, ?, 1, 'replay_finalize', '{}', '{}', '', ?)
                    """,
                    (
                        self._id("sigdef"),
                        project_id,
                        environment_id,
                        clean,
                        now,
                    ),
                )

    def upsert_signal_definition(
        self,
        *,
        project_id: str,
        environment_id: str,
        detector: str,
        enabled: bool = True,
        run_mode: str = "replay_finalize",
        thresholds: Optional[dict[str, Any]] = None,
        prompt: Optional[dict[str, Any]] = None,
        custom_definition: str = "",
    ) -> str:
        now = datetime.now(timezone.utc).isoformat()
        definition_id = self._id("sigdef")
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO signal_definitions
                (id, project_id, environment_id, detector, enabled, run_mode,
                 thresholds_json, prompt_json, custom_definition, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, environment_id, detector) DO UPDATE SET
                    enabled = excluded.enabled,
                    run_mode = excluded.run_mode,
                    thresholds_json = excluded.thresholds_json,
                    prompt_json = excluded.prompt_json,
                    custom_definition = excluded.custom_definition,
                    updated_at = excluded.updated_at
                """,
                (
                    definition_id,
                    project_id,
                    environment_id,
                    str(detector).strip(),
                    1 if enabled else 0,
                    str(run_mode).strip() or "replay_finalize",
                    json.dumps(thresholds or {}, sort_keys=True),
                    json.dumps(prompt or {}, sort_keys=True),
                    str(custom_definition or ""),
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT id FROM signal_definitions
                WHERE project_id = ? AND environment_id = ? AND detector = ?
                """,
                (project_id, environment_id, str(detector).strip()),
            ).fetchone()
        assert row is not None
        return str(row["id"])

    def list_signal_definitions(
        self,
        *,
        project_id: str,
        environment_id: str,
        enabled: Optional[bool] = None,
    ) -> list[SignalDefinitionRow]:
        params: list[object] = [project_id, environment_id]
        where = "project_id = ? AND environment_id = ?"
        if enabled is not None:
            where += " AND enabled = ?"
            params.append(1 if enabled else 0)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM signal_definitions
                WHERE {where}
                ORDER BY detector
                """,
                params,
            ).fetchall()
        return [self._signal_definition_from_row(row) for row in rows]

    def record_signal_definition_matches(
        self,
        *,
        project_id: str,
        environment_id: str,
        detector_counts: dict[str, int],
    ) -> None:
        if not detector_counts:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            for detector, count in detector_counts.items():
                if count <= 0:
                    continue
                conn.execute(
                    """
                    UPDATE signal_definitions
                    SET match_count = match_count + ?,
                        last_match_at = ?,
                        updated_at = ?
                    WHERE project_id = ? AND environment_id = ? AND detector = ?
                    """,
                    (
                        int(count),
                        now,
                        now,
                        project_id,
                        environment_id,
                        detector,
                    ),
                )

    def _signal_definition_from_row(self, row: sqlite3.Row) -> SignalDefinitionRow:
        return SignalDefinitionRow(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            environment_id=str(row["environment_id"]),
            detector=str(row["detector"]),
            enabled=bool(row["enabled"]),
            run_mode=str(row["run_mode"]),
            thresholds=self._safe_json_obj(row["thresholds_json"]),
            prompt=self._safe_json_obj(row["prompt_json"]),
            custom_definition=str(row["custom_definition"] or ""),
            match_count=int(row["match_count"] or 0),
            last_match_at=self._dt(row["last_match_at"]),
            created_at=self._dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=self._dt(row["updated_at"]) or datetime.now(timezone.utc),
        )

    def upsert_failure(self, failure: CanonicalFailure) -> str:
        now = datetime.now(timezone.utc).isoformat()
        failure_id = self._id("flr")
        with self._conn() as conn:
            return self._upsert_failure(
                conn,
                failure=failure,
                now=now,
                failure_id=failure_id,
            )

    def _upsert_failure(
        self,
        conn: sqlite3.Connection,
        *,
        failure: CanonicalFailure,
        now: str,
        failure_id: str,
    ) -> str:
        existing = conn.execute(
            """
            SELECT linked_tests_json
            FROM failures
            WHERE project_id = ? AND environment_id = ?
              AND source_type = ? AND source_external_id = ?
            """,
            (
                failure.project_id,
                failure.environment_id,
                failure.source_type,
                failure.source_external_id,
            ),
        ).fetchone()
        linked_tests = self._merge_string_lists(
            self._parse_string_list_json(existing["linked_tests_json"])
            if existing is not None
            else [],
            failure.linked_tests,
        )
        conn.execute(
            """
            INSERT INTO failures
            (id, public_id, project_id, environment_id, source_type, source_external_id,
             fingerprint, title, summary, severity, confidence, status, affected_users,
             affected_sessions, first_seen_ms, last_seen_ms, related_deploy_sha,
             related_pr_number, linked_tests_json, linked_repair_task_id,
             linked_external_thread_id, metadata_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, environment_id, source_type, source_external_id)
            DO UPDATE SET
                public_id = excluded.public_id,
                fingerprint = excluded.fingerprint,
                title = excluded.title,
                summary = excluded.summary,
                severity = excluded.severity,
                confidence = excluded.confidence,
                status = excluded.status,
                affected_users = excluded.affected_users,
                affected_sessions = excluded.affected_sessions,
                first_seen_ms = CASE
                    WHEN failures.first_seen_ms = 0 THEN excluded.first_seen_ms
                    WHEN excluded.first_seen_ms = 0 THEN failures.first_seen_ms
                    ELSE MIN(failures.first_seen_ms, excluded.first_seen_ms)
                END,
                last_seen_ms = MAX(failures.last_seen_ms, excluded.last_seen_ms),
                related_deploy_sha = excluded.related_deploy_sha,
                related_pr_number = excluded.related_pr_number,
                linked_tests_json = excluded.linked_tests_json,
                linked_repair_task_id = CASE
                    WHEN excluded.linked_repair_task_id != '' THEN excluded.linked_repair_task_id
                    ELSE failures.linked_repair_task_id
                END,
                linked_external_thread_id = excluded.linked_external_thread_id,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                failure_id,
                failure.public_id,
                failure.project_id,
                failure.environment_id,
                failure.source_type,
                failure.source_external_id,
                failure.fingerprint,
                failure.title,
                failure.summary,
                failure.severity,
                failure.confidence,
                failure.status,
                int(failure.affected_users),
                int(failure.affected_sessions),
                int(failure.first_seen_ms),
                int(failure.last_seen_ms),
                failure.related_deploy_sha,
                failure.related_pr_number,
                json.dumps(linked_tests, sort_keys=True),
                failure.linked_repair_task_id,
                failure.linked_external_thread_id,
                json.dumps(failure.metadata, sort_keys=True),
                now,
            ),
        )
        row = conn.execute(
            """
            SELECT id
            FROM failures
            WHERE project_id = ? AND environment_id = ?
              AND source_type = ? AND source_external_id = ?
            """,
            (
                failure.project_id,
                failure.environment_id,
                failure.source_type,
                failure.source_external_id,
            ),
        ).fetchone()
        assert row is not None
        persisted_failure_id = str(row["id"])
        self._refresh_incidents_for_failure(conn, failure_id=persisted_failure_id)
        return persisted_failure_id

    def get_failure(
        self,
        *,
        project_id: str,
        environment_id: str,
        failure_id: str,
    ) -> Optional[FailureRow]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM failures
                WHERE project_id = ? AND environment_id = ?
                  AND (id = ? OR public_id = ?)
                """,
                (project_id, environment_id, failure_id, failure_id),
            ).fetchone()
        return self._failure_from_row(row) if row is not None else None

    def find_failure_by_source(
        self,
        *,
        project_id: str,
        environment_id: str,
        source_type: str,
        source_external_id: str,
    ) -> Optional[FailureRow]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM failures
                WHERE project_id = ? AND environment_id = ?
                  AND source_type = ? AND source_external_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (project_id, environment_id, source_type, source_external_id),
            ).fetchone()
        return self._failure_from_row(row) if row is not None else None

    def list_failures(
        self,
        *,
        project_id: str,
        environment_id: str,
        source_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[FailureRow]:
        params: list[object] = [project_id, environment_id]
        where = "project_id = ? AND environment_id = ?"
        if source_type is not None:
            where += " AND source_type = ?"
            params.append(source_type)
        if status is not None:
            where += " AND status = ?"
            params.append(status)
        params.append(max(1, min(int(limit), 500)))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM failures
                WHERE {where}
                ORDER BY updated_at DESC, public_id
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._failure_from_row(row) for row in rows]

    def _failure_from_row(self, row: sqlite3.Row) -> FailureRow:
        return FailureRow(
            id=str(row["id"]),
            public_id=str(row["public_id"]),
            project_id=str(row["project_id"]),
            environment_id=str(row["environment_id"]),
            source_type=str(row["source_type"]),
            source_external_id=str(row["source_external_id"]),
            fingerprint=str(row["fingerprint"]),
            title=str(row["title"]),
            summary=str(row["summary"]),
            severity=str(row["severity"]),
            confidence=str(row["confidence"]),
            status=str(row["status"]),
            affected_users=int(row["affected_users"] or 0),
            affected_sessions=int(row["affected_sessions"] or 0),
            first_seen_ms=int(row["first_seen_ms"] or 0),
            last_seen_ms=int(row["last_seen_ms"] or 0),
            related_deploy_sha=str(row["related_deploy_sha"] or ""),
            related_pr_number=(
                int(row["related_pr_number"])
                if row["related_pr_number"] is not None
                else None
            ),
            linked_tests=self._parse_string_list_json(row["linked_tests_json"]),
            linked_repair_task_id=str(row["linked_repair_task_id"] or ""),
            linked_external_thread_id=str(row["linked_external_thread_id"] or ""),
            metadata=dict(self._safe_json_obj(row["metadata_json"])),
            created_at=self._dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=self._dt(row["updated_at"]) or datetime.now(timezone.utc),
        )

    def append_failure_evidence(self, evidence: EvidenceItem) -> str:
        with self._conn() as conn:
            return self._append_failure_evidence(conn, evidence=evidence)

    def _append_failure_evidence(
        self,
        conn: sqlite3.Connection,
        *,
        evidence: EvidenceItem,
    ) -> str:
        if evidence.redaction_state not in {"raw", "redacted", "sensitive"}:
            raise ValueError("invalid evidence redaction_state")
        try:
            payload_json = json.dumps(evidence.payload, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("evidence payload must be JSON-serializable") from exc
        evidence_id = self._id("ev")
        created_at = self._now_iso_microseconds()
        conn.execute(
            """
            INSERT OR IGNORE INTO failure_evidence
            (id, failure_id, evidence_type, occurred_at_ms, source, redaction_state,
             payload_json, artifact_path, dedupe_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence_id,
                evidence.failure_id,
                evidence.evidence_type,
                int(evidence.occurred_at_ms),
                evidence.source,
                evidence.redaction_state,
                payload_json,
                evidence.artifact_path,
                evidence.dedupe_key,
                created_at,
            ),
        )
        if evidence.dedupe_key:
            row = conn.execute(
                """
                SELECT id
                FROM failure_evidence
                WHERE failure_id = ? AND dedupe_key = ?
                """,
                (evidence.failure_id, evidence.dedupe_key),
            ).fetchone()
            assert row is not None
            self._refresh_incidents_for_failure(
                conn,
                failure_id=evidence.failure_id,
            )
            return str(row["id"])
        self._refresh_incidents_for_failure(conn, failure_id=evidence.failure_id)
        return evidence_id

    def list_failure_evidence(
        self,
        *,
        failure_id: str,
        include_sensitive: bool = True,
    ) -> list[EvidenceRow]:
        where = "failure_id = ?"
        params: list[object] = [failure_id]
        if not include_sensitive:
            placeholders = ",".join("?" for _ in PROMPT_SAFE_REDACTION_STATES)
            where += f" AND redaction_state IN ({placeholders})"
            params.extend(PROMPT_SAFE_REDACTION_STATES)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM failure_evidence
                WHERE {where}
                ORDER BY
                    occurred_at_ms,
                    replace(replace(created_at, ' ', 'T'), '+00:00', 'Z'),
                    id
                """,
                params,
            ).fetchall()
        return [self._evidence_from_row(row) for row in rows]

    def get_failure_by_id(self, failure_id: str) -> Optional[FailureRow]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM failures
                WHERE id = ? OR public_id = ?
                """,
                (failure_id, failure_id),
            ).fetchone()
        return self._failure_from_row(row) if row is not None else None

    def upsert_incident(
        self,
        *,
        project_id: str,
        environment_id: str,
        group_key: str,
        title: str,
        summary: str = "",
        severity: str = "medium",
        status: str = "open",
        metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        now = datetime.now(timezone.utc).isoformat()
        incident_id = self._id("inc")
        public_id = self._public_id("inc", project_id, environment_id, group_key)
        try:
            metadata_json = json.dumps(metadata or {}, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("incident metadata must be JSON-serializable") from exc
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO incidents
                (id, public_id, project_id, environment_id, group_key, title, summary,
                 severity, status, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, environment_id, group_key) DO UPDATE SET
                    title = excluded.title,
                    summary = excluded.summary,
                    severity = excluded.severity,
                    status = excluded.status,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    incident_id,
                    public_id,
                    project_id,
                    environment_id,
                    group_key,
                    title,
                    summary,
                    severity,
                    status,
                    metadata_json,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT id
                FROM incidents
                WHERE project_id = ? AND environment_id = ? AND group_key = ?
                """,
                (project_id, environment_id, group_key),
            ).fetchone()
            assert row is not None
            return str(row["id"])

    def link_failure_to_incident(self, *, incident_id: str, failure_id: str) -> None:
        with self._conn() as conn:
            incident = conn.execute(
                """
                SELECT id, project_id, environment_id
                FROM incidents
                WHERE id = ? OR public_id = ?
                """,
                (incident_id, incident_id),
            ).fetchone()
            if incident is None:
                raise ValueError(f"unknown incident_id: {incident_id}")
            failure = conn.execute(
                """
                SELECT id, project_id, environment_id
                FROM failures
                WHERE id = ? OR public_id = ?
                """,
                (failure_id, failure_id),
            ).fetchone()
            if failure is None:
                raise ValueError(f"unknown failure_id: {failure_id}")
            if (
                str(incident["project_id"]) != str(failure["project_id"])
                or str(incident["environment_id"]) != str(failure["environment_id"])
            ):
                raise ValueError("incident and failure must belong to the same workspace")
            existing_link = conn.execute(
                """
                SELECT incident_id
                FROM incident_failures
                WHERE failure_id = ?
                """,
                (str(failure["id"]),),
            ).fetchone()
            if existing_link is not None:
                linked_incident_id = str(existing_link["incident_id"])
                if linked_incident_id != str(incident["id"]):
                    raise ValueError("failure is already linked to another incident")
                self._refresh_incident_rollup(
                    conn,
                    incident_id=linked_incident_id,
                )
                return
            conn.execute(
                """
                INSERT OR IGNORE INTO incident_failures
                (incident_id, failure_id)
                VALUES (?, ?)
                """,
                (str(incident["id"]), str(failure["id"])),
            )
            self._refresh_incident_rollup(conn, incident_id=str(incident["id"]))

    def move_failure_to_incident(self, *, incident_id: str, failure_id: str) -> None:
        with self._conn() as conn:
            incident = conn.execute(
                """
                SELECT id, project_id, environment_id
                FROM incidents
                WHERE id = ? OR public_id = ?
                """,
                (incident_id, incident_id),
            ).fetchone()
            if incident is None:
                raise ValueError(f"unknown incident_id: {incident_id}")
            failure = conn.execute(
                """
                SELECT id, project_id, environment_id
                FROM failures
                WHERE id = ? OR public_id = ?
                """,
                (failure_id, failure_id),
            ).fetchone()
            if failure is None:
                raise ValueError(f"unknown failure_id: {failure_id}")
            if (
                str(incident["project_id"]) != str(failure["project_id"])
                or str(incident["environment_id"]) != str(failure["environment_id"])
            ):
                raise ValueError("incident and failure must belong to the same workspace")
            old_links = conn.execute(
                """
                SELECT incident_id
                FROM incident_failures
                WHERE failure_id = ?
                """,
                (str(failure["id"]),),
            ).fetchall()
            conn.execute(
                """
                DELETE FROM incident_failures
                WHERE failure_id = ?
                """,
                (str(failure["id"]),),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO incident_failures
                (incident_id, failure_id)
                VALUES (?, ?)
                """,
                (str(incident["id"]), str(failure["id"])),
            )
            for row in old_links:
                self._refresh_incident_rollup(
                    conn,
                    incident_id=str(row["incident_id"]),
                )
            self._refresh_incident_rollup(conn, incident_id=str(incident["id"]))

    def get_incident(self, incident_id: str) -> Optional[IncidentRow]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM incidents
                WHERE id = ? OR public_id = ?
                """,
                (incident_id, incident_id),
            ).fetchone()
        return self._incident_from_row(row) if row is not None else None

    def find_incident_by_group(
        self,
        *,
        project_id: str,
        environment_id: str,
        group_key: str,
    ) -> Optional[IncidentRow]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM incidents
                WHERE project_id = ? AND environment_id = ? AND group_key = ?
                """,
                (project_id, environment_id, group_key),
            ).fetchone()
        return self._incident_from_row(row) if row is not None else None

    def list_incidents(
        self,
        *,
        project_id: str,
        environment_id: str,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[IncidentRow]:
        params: list[object] = [project_id, environment_id]
        where = "project_id = ? AND environment_id = ?"
        if status is not None:
            where += " AND status = ?"
            params.append(status)
        params.append(max(1, min(int(limit), 500)))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM incidents
                WHERE {where}
                ORDER BY updated_at DESC, public_id
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._incident_from_row(row) for row in rows]

    def list_incident_failures(self, *, incident_id: str) -> list[FailureRow]:
        with self._conn() as conn:
            incident = conn.execute(
                """
                SELECT id
                FROM incidents
                WHERE id = ? OR public_id = ?
                """,
                (incident_id, incident_id),
            ).fetchone()
            if incident is None:
                return []
            rows = conn.execute(
                """
                SELECT f.*
                FROM incident_failures inf
                JOIN failures f ON f.id = inf.failure_id
                WHERE inf.incident_id = ?
                ORDER BY f.updated_at DESC, f.public_id
                """,
                (str(incident["id"]),),
            ).fetchall()
        return [self._failure_from_row(row) for row in rows]

    def list_incident_evidence(
        self,
        *,
        incident_id: str,
        include_sensitive: bool = True,
    ) -> list[EvidenceRow]:
        resolved_incident_id = self._resolve_incident_id(incident_id)
        if not resolved_incident_id:
            return []
        where = "inf.incident_id = ?"
        params: list[object] = [resolved_incident_id]
        if not include_sensitive:
            placeholders = ",".join("?" for _ in PROMPT_SAFE_REDACTION_STATES)
            where += f" AND ev.redaction_state IN ({placeholders})"
            params.extend(PROMPT_SAFE_REDACTION_STATES)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT ev.*
                FROM incident_failures inf
                JOIN failure_evidence ev ON ev.failure_id = inf.failure_id
                WHERE {where}
                ORDER BY
                    ev.occurred_at_ms,
                    replace(replace(ev.created_at, ' ', 'T'), '+00:00', 'Z'),
                    ev.id
                """,
                params,
            ).fetchall()
        return [self._evidence_from_row(row) for row in rows]

    def _resolve_incident_id(self, incident_id: str) -> str:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT id
                FROM incidents
                WHERE id = ? OR public_id = ?
                """,
                (incident_id, incident_id),
            ).fetchone()
        return str(row["id"]) if row is not None else ""

    def set_incident_repair_task(self, *, incident_id: str, repair_task_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            incident = conn.execute(
                """
                SELECT id, project_id, environment_id
                FROM incidents
                WHERE id = ? OR public_id = ?
                """,
                (incident_id, incident_id),
            ).fetchone()
            if incident is None:
                raise ValueError(f"unknown incident_id: {incident_id}")
            repair_task = conn.execute(
                """
                SELECT id, project_id, environment_id
                FROM repair_tasks
                WHERE id = ? OR public_id = ?
                """,
                (repair_task_id, repair_task_id),
            ).fetchone()
            if repair_task is None:
                raise ValueError(f"unknown repair_task_id: {repair_task_id}")
            if (
                str(incident["project_id"]) != str(repair_task["project_id"])
                or str(incident["environment_id"]) != str(repair_task["environment_id"])
            ):
                raise ValueError(
                    "incident and repair task must belong to the same workspace"
                )
            conn.execute(
                """
                UPDATE incidents
                SET repair_task_id = ?, updated_at = ?
                WHERE id = ? OR public_id = ?
                """,
                (str(repair_task["id"]), now, str(incident["id"]), str(incident["id"])),
            )

    def _incident_from_row(self, row: sqlite3.Row) -> IncidentRow:
        return IncidentRow(
            id=str(row["id"]),
            public_id=str(row["public_id"]),
            project_id=str(row["project_id"]),
            environment_id=str(row["environment_id"]),
            group_key=str(row["group_key"]),
            title=str(row["title"] or ""),
            summary=str(row["summary"] or ""),
            severity=str(row["severity"] or "medium"),
            status=str(row["status"] or "open"),
            failure_count=int(row["failure_count"] or 0),
            evidence_count=int(row["evidence_count"] or 0),
            repair_task_id=str(row["repair_task_id"] or ""),
            metadata=dict(self._safe_json_obj(row["metadata_json"])),
            created_at=self._dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=self._dt(row["updated_at"]) or datetime.now(timezone.utc),
        )

    def _refresh_incident_rollup(
        self,
        conn: sqlite3.Connection,
        *,
        incident_id: str,
    ) -> None:
        rows = conn.execute(
            """
            SELECT severity, updated_at
            FROM incident_failures inf
            JOIN failures f ON f.id = inf.failure_id
            WHERE inf.incident_id = ?
            """,
            (incident_id,),
        ).fetchall()
        evidence_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM incident_failures inf
            JOIN failure_evidence ev ON ev.failure_id = inf.failure_id
            WHERE inf.incident_id = ?
            """,
            (incident_id,),
        ).fetchone()
        severity = _rollup_severity([str(row["severity"] or "") for row in rows])
        conn.execute(
            """
            UPDATE incidents
            SET failure_count = ?,
                evidence_count = ?,
                severity = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(rows),
                int(evidence_count["count"] if evidence_count is not None else 0),
                severity,
                datetime.now(timezone.utc).isoformat(),
                incident_id,
            ),
        )

    def _refresh_incidents_for_failure(
        self,
        conn: sqlite3.Connection,
        *,
        failure_id: str,
    ) -> None:
        rows = conn.execute(
            """
            SELECT incident_id
            FROM incident_failures
            WHERE failure_id = ?
            """,
            (failure_id,),
        ).fetchall()
        for row in rows:
            self._refresh_incident_rollup(
                conn,
                incident_id=str(row["incident_id"]),
            )

    def record_deploy_marker(
        self,
        *,
        project_id: str,
        environment_id: str,
        sha: str,
        branch: str = "",
        author: str = "",
        deployed_at_ms: int = 0,
        changed_files: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        clean_sha = sha.strip()
        if not clean_sha:
            raise ValueError("sha is required")
        now = datetime.now(timezone.utc).isoformat()
        deploy_id = self._id("dep")
        public_id = self._public_id("dep", project_id, environment_id, clean_sha)
        changed_files_was_omitted = changed_files is None
        metadata_was_omitted = metadata is None
        clean_changed_files = self._merge_string_lists(changed_files or [])
        try:
            metadata_json = json.dumps(metadata or {}, sort_keys=True)
            changed_files_json = json.dumps(clean_changed_files, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("deploy marker metadata must be JSON-serializable") from exc
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO deploy_markers
                (id, public_id, project_id, environment_id, sha, branch, author,
                 deployed_at_ms, changed_files_json, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, environment_id, sha) DO UPDATE SET
                    branch = excluded.branch,
                    author = excluded.author,
                    deployed_at_ms = excluded.deployed_at_ms,
                    changed_files_json = CASE
                        WHEN ? THEN deploy_markers.changed_files_json
                        ELSE excluded.changed_files_json
                    END,
                    metadata_json = CASE
                        WHEN ? THEN deploy_markers.metadata_json
                        ELSE excluded.metadata_json
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    deploy_id,
                    public_id,
                    project_id,
                    environment_id,
                    clean_sha,
                    branch.strip(),
                    author.strip(),
                    int(deployed_at_ms),
                    changed_files_json,
                    metadata_json,
                    now,
                    now,
                    int(changed_files_was_omitted),
                    int(metadata_was_omitted),
                ),
            )
            row = conn.execute(
                """
                SELECT id
                FROM deploy_markers
                WHERE project_id = ? AND environment_id = ? AND sha = ?
                """,
                (project_id, environment_id, clean_sha),
            ).fetchone()
            assert row is not None
            return str(row["id"])

    def get_deploy_marker(self, deploy_id: str) -> Optional[DeployMarkerRow]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM deploy_markers
                WHERE id = ? OR public_id = ? OR sha = ?
                ORDER BY deployed_at_ms DESC
                LIMIT 1
                """,
                (deploy_id, deploy_id, deploy_id),
            ).fetchone()
        return self._deploy_marker_from_row(row) if row is not None else None

    def get_deploy_marker_by_sha(
        self,
        *,
        project_id: str,
        environment_id: str,
        sha: str,
    ) -> Optional[DeployMarkerRow]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM deploy_markers
                WHERE project_id = ? AND environment_id = ? AND sha = ?
                """,
                (project_id, environment_id, sha),
            ).fetchone()
        return self._deploy_marker_from_row(row) if row is not None else None

    def list_deploy_markers(
        self,
        *,
        project_id: str,
        environment_id: str,
        limit: int = 50,
    ) -> list[DeployMarkerRow]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM deploy_markers
                WHERE project_id = ? AND environment_id = ?
                ORDER BY deployed_at_ms DESC, updated_at DESC
                LIMIT ?
                """,
                (project_id, environment_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._deploy_marker_from_row(row) for row in rows]

    def nearest_deploy_marker(
        self,
        *,
        project_id: str,
        environment_id: str,
        at_ms: int,
    ) -> Optional[DeployMarkerRow]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM deploy_markers
                WHERE project_id = ? AND environment_id = ?
                  AND deployed_at_ms <= ?
                ORDER BY deployed_at_ms DESC, updated_at DESC
                LIMIT 1
                """,
                (project_id, environment_id, max(0, int(at_ms))),
            ).fetchone()
        return self._deploy_marker_from_row(row) if row is not None else None

    def update_failure_deploy(self, *, failure_id: str, deploy_sha: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE failures
                SET related_deploy_sha = ?, updated_at = ?
                WHERE id = ? OR public_id = ?
                """,
                (deploy_sha.strip(), now, failure_id, failure_id),
            )
            row = conn.execute(
                """
                SELECT id
                FROM failures
                WHERE id = ? OR public_id = ?
                """,
                (failure_id, failure_id),
            ).fetchone()
            if row is not None:
                self._refresh_incidents_for_failure(
                    conn,
                    failure_id=str(row["id"]),
                )

    def _deploy_marker_from_row(self, row: sqlite3.Row) -> DeployMarkerRow:
        return DeployMarkerRow(
            id=str(row["id"]),
            public_id=str(row["public_id"]),
            project_id=str(row["project_id"]),
            environment_id=str(row["environment_id"]),
            sha=str(row["sha"]),
            branch=str(row["branch"] or ""),
            author=str(row["author"] or ""),
            deployed_at_ms=int(row["deployed_at_ms"] or 0),
            changed_files=self._parse_string_list_json(row["changed_files_json"]),
            metadata=dict(self._safe_json_obj(row["metadata_json"])),
            created_at=self._dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=self._dt(row["updated_at"]) or datetime.now(timezone.utc),
        )

    def upsert_failure_with_evidence_and_repair_task(
        self,
        *,
        failure: CanonicalFailure,
        evidence_items: list[EvidenceItem],
        repair_task: dict[str, Any],
    ) -> tuple[str, list[str], str]:
        now = datetime.now(timezone.utc).isoformat()
        failure_id = self._id("flr")
        with self._conn() as conn:
            persisted_failure_id = self._upsert_failure(
                conn,
                failure=failure,
                now=now,
                failure_id=failure_id,
            )
            normalized_evidence = [
                EvidenceItem(
                    failure_id=persisted_failure_id,
                    evidence_type=item.evidence_type,
                    occurred_at_ms=item.occurred_at_ms,
                    source=item.source,
                    redaction_state=item.redaction_state,
                    payload=item.payload,
                    artifact_path=item.artifact_path,
                    dedupe_key=evidence_dedupe_key(
                        failure_id=persisted_failure_id,
                        evidence_type=item.evidence_type,
                        source=item.source,
                        occurred_at_ms=item.occurred_at_ms,
                        payload=item.payload,
                    ),
                )
                for item in evidence_items
            ]
            evidence_ids = [
                self._append_failure_evidence(conn, evidence=item)
                for item in normalized_evidence
            ]
            repair_task_id = self._upsert_repair_task_in_conn(
                conn,
                failure_id=persisted_failure_id,
                title=str(repair_task.get("title") or ""),
                source_type=str(repair_task.get("source_type") or ""),
                source_external_id=str(repair_task.get("source_external_id") or ""),
                status=str(repair_task.get("status") or "open"),
                likely_files=list(repair_task.get("likely_files") or []),
                prompt_artifacts=list(repair_task.get("prompt_artifacts") or []),
                validation_commands=list(
                    repair_task.get("validation_commands") or []
                ),
                branch=str(repair_task.get("branch") or ""),
                pr_url=str(repair_task.get("pr_url") or ""),
                risk_notes=str(repair_task.get("risk_notes") or ""),
                metadata=dict(repair_task.get("metadata") or {}),
                evidence_ids=evidence_ids,
                replace_supporting=False,
            )
            return persisted_failure_id, evidence_ids, repair_task_id

    def upsert_repair_task(
        self,
        *,
        failure_id: str,
        title: str,
        source_type: str = "",
        source_external_id: str = "",
        status: str = "open",
        likely_files: Optional[list[str]] = None,
        prompt_artifacts: Optional[list[dict[str, Any]]] = None,
        validation_commands: Optional[list[str]] = None,
        branch: str = "",
        pr_url: str = "",
        risk_notes: str = "",
        metadata: Optional[dict[str, Any]] = None,
        evidence_ids: Optional[list[str]] = None,
    ) -> str:
        failure_id = failure_id.strip()
        if not failure_id:
            raise ValueError("failure_id is required")
        now = datetime.now(timezone.utc).isoformat()
        task_id = self._id("rpr")
        public_id = self._public_id("rpr", failure_id)
        normalized_status = normalize_repair_task_status(status)
        clean_likely_files = self._merge_string_lists(likely_files or [])
        clean_validation = self._merge_string_lists(validation_commands or [])
        clean_evidence_ids = self._merge_string_lists(evidence_ids or [])
        try:
            prompt_json = json.dumps(prompt_artifacts or [], sort_keys=True)
            metadata_json = json.dumps(metadata or {}, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("repair task metadata must be JSON-serializable") from exc

        with self._conn() as conn:
            return self._upsert_repair_task_in_conn(
                conn,
                failure_id=failure_id,
                title=title,
                source_type=source_type,
                source_external_id=source_external_id,
                status=normalized_status,
                likely_files=clean_likely_files,
                prompt_artifacts=json.loads(prompt_json),
                validation_commands=clean_validation,
                branch=branch,
                pr_url=pr_url,
                risk_notes=risk_notes,
                metadata=json.loads(metadata_json),
                evidence_ids=clean_evidence_ids,
                now=now,
                task_id=task_id,
                public_id=public_id,
            )

    def _upsert_repair_task_in_conn(
        self,
        conn: sqlite3.Connection,
        *,
        failure_id: str,
        title: str,
        source_type: str = "",
        source_external_id: str = "",
        status: str = "open",
        likely_files: Optional[list[str]] = None,
        prompt_artifacts: Optional[list[dict[str, Any]]] = None,
        validation_commands: Optional[list[str]] = None,
        branch: str = "",
        pr_url: str = "",
        risk_notes: str = "",
        metadata: Optional[dict[str, Any]] = None,
        evidence_ids: Optional[list[str]] = None,
        now: str = "",
        task_id: str = "",
        public_id: str = "",
        replace_supporting: bool = True,
    ) -> str:
        failure_id = failure_id.strip()
        if not failure_id:
            raise ValueError("failure_id is required")
        now = now or datetime.now(timezone.utc).isoformat()
        task_id = task_id or self._id("rpr")
        public_id = public_id or self._public_id("rpr", failure_id)
        normalized_status = normalize_repair_task_status(status)
        clean_likely_files = self._merge_string_lists(likely_files or [])
        clean_validation = self._merge_string_lists(validation_commands or [])
        clean_evidence_ids = self._merge_string_lists(evidence_ids or [])
        try:
            prompt_json = json.dumps(prompt_artifacts or [], sort_keys=True)
            metadata_json = json.dumps(metadata or {}, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("repair task metadata must be JSON-serializable") from exc
        failure_row = conn.execute(
            "SELECT id, project_id, environment_id FROM failures WHERE id = ?",
            (failure_id,),
        ).fetchone()
        if failure_row is None:
            raise ValueError(f"unknown failure_id: {failure_id}")
        task_project_id = str(failure_row["project_id"])
        task_environment_id = str(failure_row["environment_id"])
        conn.execute(
            """
            INSERT INTO repair_tasks
            (id, public_id, project_id, environment_id, failure_id, source_type,
             source_external_id, title, status, likely_files_json, prompt_artifacts_json,
             validation_commands_json, branch, pr_url, risk_notes, metadata_json,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(failure_id, project_id, environment_id) DO UPDATE SET
                source_type = excluded.source_type,
                source_external_id = excluded.source_external_id,
                title = excluded.title,
                status = excluded.status,
                likely_files_json = excluded.likely_files_json,
                prompt_artifacts_json = excluded.prompt_artifacts_json,
                validation_commands_json = excluded.validation_commands_json,
                branch = excluded.branch,
                pr_url = excluded.pr_url,
                risk_notes = excluded.risk_notes,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                task_id,
                public_id,
                task_project_id,
                task_environment_id,
                failure_id,
                source_type.strip(),
                source_external_id.strip(),
                title.strip() or "Repair task",
                normalized_status,
                json.dumps(clean_likely_files, sort_keys=True),
                prompt_json,
                json.dumps(clean_validation, sort_keys=True),
                branch.strip(),
                pr_url.strip(),
                risk_notes.strip(),
                metadata_json,
                now,
                now,
            ),
        )
        row = conn.execute(
            """
            SELECT id
            FROM repair_tasks
            WHERE failure_id = ? AND project_id = ? AND environment_id = ?
            """,
            (failure_id, task_project_id, task_environment_id),
        ).fetchone()
        assert row is not None
        persisted_task_id = str(row["id"])
        if replace_supporting:
            conn.execute(
                """
                DELETE FROM repair_task_evidence
                WHERE repair_task_id = ? AND role = 'supporting'
                """,
                (persisted_task_id,),
            )
        for evidence_id in clean_evidence_ids:
            evidence_row = conn.execute(
                """
                SELECT 1
                FROM failure_evidence
                WHERE id = ? AND failure_id = ?
                """,
                (evidence_id, failure_id),
            ).fetchone()
            if evidence_row is None:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO repair_task_evidence
                (repair_task_id, evidence_id, role)
                VALUES (?, ?, 'supporting')
                """,
                (persisted_task_id, evidence_id),
            )
        conn.execute(
            """
            UPDATE failures
            SET linked_repair_task_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (persisted_task_id, now, failure_id),
        )
        return persisted_task_id

    def get_repair_task(self, repair_task_id: str) -> Optional[RepairTaskRow]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM repair_tasks
                WHERE id = ? OR public_id = ?
                """,
                (repair_task_id, repair_task_id),
            ).fetchone()
            if row is None:
                return None
            evidence_rows = conn.execute(
                """
                SELECT evidence_id
                FROM repair_task_evidence
                WHERE repair_task_id = ?
                ORDER BY created_at, evidence_id
                """,
                (str(row["id"]),),
            ).fetchall()
        return self._repair_task_from_row(
            row,
            evidence_ids=[str(item["evidence_id"]) for item in evidence_rows],
        )

    def list_repair_tasks(
        self,
        *,
        project_id: Optional[str] = None,
        environment_id: Optional[str] = None,
        failure_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[RepairTaskRow]:
        where: list[str] = []
        params: list[object] = []
        if project_id is not None:
            where.append("project_id = ?")
            params.append(project_id)
        if environment_id is not None:
            where.append("environment_id = ?")
            params.append(environment_id)
        if failure_id is not None:
            where.append("failure_id = ?")
            params.append(failure_id)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        clause = " AND ".join(where) if where else "1 = 1"
        params.append(max(1, min(int(limit), 500)))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM repair_tasks
                WHERE {clause}
                ORDER BY updated_at DESC, public_id
                LIMIT ?
                """,
                params,
            ).fetchall()
            evidence_by_task: dict[str, list[str]] = {}
            for row in rows:
                evidence_rows = conn.execute(
                    """
                    SELECT evidence_id
                    FROM repair_task_evidence
                    WHERE repair_task_id = ?
                    ORDER BY created_at, evidence_id
                    """,
                    (str(row["id"]),),
                ).fetchall()
                evidence_by_task[str(row["id"])] = [
                    str(item["evidence_id"]) for item in evidence_rows
                ]
        return [
            self._repair_task_from_row(
                row,
                evidence_ids=evidence_by_task.get(str(row["id"]), []),
            )
            for row in rows
        ]

    def upsert_failure_test_link(
        self,
        *,
        failure_id: str,
        spec_id: str,
        issue_id: str = "",
        issue_public_id: str = "",
        spec_name: str = "",
        spec_path: str = "",
        source: str = "manual",
    ) -> str:
        if not failure_id.strip():
            raise ValueError("failure_id is required")
        if not spec_id.strip():
            raise ValueError("spec_id is required")
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            failure_row = conn.execute(
                "SELECT 1 FROM failures WHERE id = ?",
                (failure_id.strip(),),
            ).fetchone()
            if failure_row is None:
                raise ValueError(f"unknown failure_id: {failure_id}")
            link_id = self._id("ftl")
            conn.execute(
                """
                INSERT INTO failure_test_links
                (id, failure_id, issue_id, issue_public_id, spec_id, spec_name,
                 spec_path, source, coverage_state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'covered_unverified', ?, ?)
                ON CONFLICT(failure_id, spec_id) DO UPDATE SET
                    issue_id = CASE
                        WHEN excluded.issue_id != '' THEN excluded.issue_id
                        ELSE failure_test_links.issue_id
                    END,
                    issue_public_id = CASE
                        WHEN excluded.issue_public_id != '' THEN excluded.issue_public_id
                        ELSE failure_test_links.issue_public_id
                    END,
                    spec_name = CASE
                        WHEN excluded.spec_name != '' THEN excluded.spec_name
                        ELSE failure_test_links.spec_name
                    END,
                    spec_path = CASE
                        WHEN excluded.spec_path != '' THEN excluded.spec_path
                        ELSE failure_test_links.spec_path
                    END,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    link_id,
                    failure_id.strip(),
                    issue_id.strip(),
                    issue_public_id.strip(),
                    spec_id.strip(),
                    spec_name.strip(),
                    spec_path.strip(),
                    source.strip() or "manual",
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT id
                FROM failure_test_links
                WHERE failure_id = ? AND spec_id = ?
                """,
                (failure_id.strip(), spec_id.strip()),
            ).fetchone()
            assert row is not None
            self._add_linked_test_to_failure(conn, failure_id=failure_id, spec_id=spec_id)
            return str(row["id"])

    def list_failure_test_links(
        self,
        *,
        failure_id: Optional[str] = None,
        issue_public_id: Optional[str] = None,
        spec_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[FailureTestLinkRow]:
        where: list[str] = []
        params: list[object] = []
        if failure_id is not None:
            where.append("failure_id = ?")
            params.append(failure_id)
        if issue_public_id is not None:
            where.append("issue_public_id = ?")
            params.append(issue_public_id)
        if spec_id is not None:
            where.append("spec_id = ?")
            params.append(spec_id)
        params.append(max(1, min(int(limit), 500)))
        clause = " AND ".join(where) if where else "1 = 1"
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM failure_test_links
                WHERE {clause}
                ORDER BY updated_at DESC, created_at DESC, id
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._failure_test_link_from_row(row) for row in rows]

    def coverage_state_for_failure(self, failure_id: str) -> str:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT coverage_state
                FROM failure_test_links
                WHERE failure_id = ?
                """,
                (failure_id,),
            ).fetchall()
        if not rows:
            return "not_covered"
        states = {str(row["coverage_state"]) for row in rows}
        for state in (
            "covered_failing",
            "covered_flaky",
            "covered_passing",
            "covered_unverified",
        ):
            if state in states:
                return state
        return "covered_unverified"

    def update_failure_test_link_run(
        self,
        *,
        spec_id: str,
        run_result: object,
        link_id: str = "",
    ) -> list[FailureTestLinkRow]:
        spec_id = spec_id.strip()
        link_id = link_id.strip()
        if not link_id:
            raise ValueError("link_id is required for updates")
        coverage_state = self._coverage_state_from_run_result(run_result)
        run_id = str(getattr(run_result, "run_id", "") or "")
        run_status = str(getattr(run_result, "status", "") or "")
        run_classification = str(
            getattr(run_result, "failure_classification", "") or ""
        )
        run_ok = 1 if bool(getattr(run_result, "ok", False)) else 0
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cursor = conn.execute(
                """
                UPDATE failure_test_links
                SET coverage_state = ?,
                    latest_run_id = ?,
                    latest_run_status = ?,
                    latest_run_classification = ?,
                    latest_run_ok = ?,
                    latest_run_at = ?,
                    updated_at = ?
                WHERE id = ? AND spec_id = ?
                """,
                (
                    coverage_state,
                    run_id,
                    run_status,
                    run_classification,
                    run_ok,
                    now,
                    now,
                    link_id,
                    spec_id,
                ),
            )
            if int(cursor.rowcount) == 0:
                raise ValueError(
                    f"unknown failure_test_link id={link_id} spec_id={spec_id}"
                )
            rows = conn.execute(
                """
                SELECT *
                FROM failure_test_links
                WHERE id = ?
                ORDER BY updated_at DESC, id
                """,
                (link_id,),
            ).fetchall()
        return [self._failure_test_link_from_row(row) for row in rows]

    def _backfill_failure_test_links(self, conn: sqlite3.Connection) -> None:
        migrated = conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            ("failure_test_links_backfill_v1",),
        ).fetchone()
        if migrated is not None:
            return
        rows = conn.execute(
            """
            SELECT id, linked_tests_json
            FROM failures
            WHERE linked_tests_json IS NOT NULL
              AND linked_tests_json != ''
              AND linked_tests_json != '[]'
            """
        ).fetchall()
        now = datetime.now(timezone.utc).isoformat()
        for row in rows:
            for link in self._legacy_failure_test_links(row["linked_tests_json"]):
                spec_id = link["spec_id"]
                conn.execute(
                    """
                    INSERT INTO failure_test_links
                    (id, failure_id, spec_id, spec_name, spec_path, source,
                     coverage_state, latest_run_id, latest_run_status,
                     latest_run_classification, latest_run_ok, latest_run_at,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'legacy', ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(failure_id, spec_id) DO NOTHING
                    """,
                    (
                        self._id("ftl"),
                        str(row["id"]),
                        spec_id,
                        link["spec_name"],
                        link["spec_path"],
                        link["coverage_state"],
                        link["latest_run_id"],
                        link["latest_run_status"],
                        link["latest_run_classification"],
                        link["latest_run_ok"],
                        link["latest_run_at"],
                        now,
                        now,
                    ),
                )
        conn.execute(
            """
            INSERT INTO meta (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            ("failure_test_links_backfill_v1", now),
        )

    @classmethod
    def _legacy_failure_test_links(cls, raw: object) -> list[dict[str, object]]:
        try:
            parsed = json.loads(str(raw or "[]"))
        except Exception:
            return []
        if not isinstance(parsed, list):
            return []
        links: list[dict[str, object]] = []
        for item in parsed:
            if isinstance(item, dict):
                spec_id = str(item.get("spec_id") or item.get("id") or "").strip()
                state = str(
                    item.get("coverage_state") or "covered_unverified"
                ).strip()
                if state not in FAILURE_TEST_COVERAGE_STATES:
                    state = "covered_unverified"
                latest_run_ok = item.get("latest_run_ok")
                if latest_run_ok is not None:
                    latest_run_ok = 1 if bool(latest_run_ok) else 0
                link = {
                    "spec_id": spec_id,
                    "spec_name": str(item.get("spec_name") or item.get("name") or ""),
                    "spec_path": str(item.get("spec_path") or item.get("path") or ""),
                    "coverage_state": state,
                    "latest_run_id": str(item.get("latest_run_id") or ""),
                    "latest_run_status": str(item.get("latest_run_status") or ""),
                    "latest_run_classification": str(
                        item.get("latest_run_classification") or ""
                    ),
                    "latest_run_ok": latest_run_ok,
                    "latest_run_at": item.get("latest_run_at"),
                }
            else:
                spec_id = str(item or "").strip()
                link = {
                    "spec_id": spec_id,
                    "spec_name": "",
                    "spec_path": "",
                    "coverage_state": "covered_unverified",
                    "latest_run_id": "",
                    "latest_run_status": "",
                    "latest_run_classification": "",
                    "latest_run_ok": None,
                    "latest_run_at": None,
                }
            if spec_id:
                links.append(link)
        return links

    @staticmethod
    def _coverage_state_from_run_result(run_result: object) -> str:
        state: str
        if bool(getattr(run_result, "flaky", False)):
            state = "covered_flaky"
        elif bool(getattr(run_result, "ok", False)):
            state = "covered_passing"
        else:
            status = str(getattr(run_result, "status", "") or "").lower()
            state = "covered_flaky" if "flaky" in status else "covered_failing"
        assert state in FAILURE_TEST_COVERAGE_STATES
        return state

    def _add_linked_test_to_failure(
        self,
        conn: sqlite3.Connection,
        *,
        failure_id: str,
        spec_id: str,
    ) -> None:
        row = conn.execute(
            "SELECT linked_tests_json FROM failures WHERE id = ?",
            (failure_id.strip(),),
        ).fetchone()
        if row is None:
            return
        spec_id = spec_id.strip()
        linked = self._merge_string_lists(
            self._parse_string_list_json(row["linked_tests_json"]),
            [spec_id],
        )
        if linked != self._parse_string_list_json(row["linked_tests_json"]):
            conn.execute(
                """
                UPDATE failures
                SET linked_tests_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(linked, sort_keys=True),
                    datetime.now(timezone.utc).isoformat(),
                    failure_id.strip(),
                ),
            )

    @staticmethod
    def _merge_string_lists(*values: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for value_list in values:
            for value in value_list:
                item = str(value or "").strip()
                if item and item not in seen:
                    seen.add(item)
                    merged.append(item)
        return merged

    def _failure_test_link_from_row(self, row: sqlite3.Row) -> FailureTestLinkRow:
        latest_run_ok: Optional[bool]
        if row["latest_run_ok"] is None:
            latest_run_ok = None
        else:
            latest_run_ok = bool(row["latest_run_ok"])
        return FailureTestLinkRow(
            id=str(row["id"]),
            failure_id=str(row["failure_id"]),
            issue_id=str(row["issue_id"] or ""),
            issue_public_id=str(row["issue_public_id"] or ""),
            spec_id=str(row["spec_id"]),
            spec_name=str(row["spec_name"] or ""),
            spec_path=str(row["spec_path"] or ""),
            source=str(row["source"] or ""),
            coverage_state=str(row["coverage_state"] or "covered_unverified"),
            latest_run_id=str(row["latest_run_id"] or ""),
            latest_run_status=str(row["latest_run_status"] or ""),
            latest_run_classification=str(row["latest_run_classification"] or ""),
            latest_run_ok=latest_run_ok,
            latest_run_at=self._dt(row["latest_run_at"]),
            created_at=self._dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=self._dt(row["updated_at"]) or datetime.now(timezone.utc),
        )

    def _repair_task_from_row(
        self,
        row: sqlite3.Row,
        *,
        evidence_ids: list[str],
    ) -> RepairTaskRow:
        return RepairTaskRow(
            id=str(row["id"]),
            public_id=str(row["public_id"]),
            project_id=str(row["project_id"] or ""),
            environment_id=str(row["environment_id"] or ""),
            failure_id=str(row["failure_id"]),
            source_type=str(row["source_type"] or ""),
            source_external_id=str(row["source_external_id"] or ""),
            title=str(row["title"] or ""),
            status=str(row["status"] or "open"),
            likely_files=self._parse_string_list_json(row["likely_files_json"]),
            prompt_artifacts=self._parse_dict_list_json(row["prompt_artifacts_json"]),
            validation_commands=self._parse_string_list_json(
                row["validation_commands_json"]
            ),
            branch=str(row["branch"] or ""),
            pr_url=str(row["pr_url"] or ""),
            risk_notes=str(row["risk_notes"] or ""),
            metadata=dict(self._safe_json_obj(row["metadata_json"])),
            evidence_ids=evidence_ids,
            created_at=self._dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=self._dt(row["updated_at"]) or datetime.now(timezone.utc),
        )

    def _backfill_replay_issue_evidence(
        self,
        conn: sqlite3.Connection,
        *,
        failure_id: str,
        issue_public_id: str,
        evidence: dict[str, Any],
    ) -> None:
        for item in evidence_items_from_replay_issue(
            failure_id=failure_id,
            issue_public_id=issue_public_id,
            evidence=evidence,
        ):
            self._append_failure_evidence(conn, evidence=item)

    def _evidence_from_row(self, row: sqlite3.Row) -> EvidenceRow:
        return EvidenceRow(
            id=str(row["id"]),
            failure_id=str(row["failure_id"]),
            evidence_type=str(row["evidence_type"]),
            occurred_at_ms=int(row["occurred_at_ms"] or 0),
            source=str(row["source"] or ""),
            redaction_state=str(row["redaction_state"] or "raw"),
            payload=dict(self._safe_json_obj(row["payload_json"])),
            artifact_path=str(row["artifact_path"] or ""),
            dedupe_key=str(row["dedupe_key"] or ""),
            created_at=self._dt(row["created_at"]) or datetime.now(timezone.utc),
        )

    @staticmethod
    def _parse_dict_list_json(raw: object) -> list[dict[str, Any]]:
        try:
            parsed = json.loads(str(raw or "[]"))
        except Exception:
            return []
        if not isinstance(parsed, list):
            return []
        return [item for item in parsed if isinstance(item, dict)]

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
        preview = self._replay_preview(events)
        blob_backend = ""
        blob_key = ""
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
                (id, project_id, environment_id, stable_id, public_id, distinct_id, started_at,
                 last_seen_at, event_count, metadata_json, preview_json, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_row_id,
                    project_id,
                    environment_id,
                    session_id,
                    self.make_replay_public_id(project_id, environment_id, session_id),
                    distinct_id or "",
                    now,
                    now,
                    0,
                    json.dumps(metadata or {}),
                    json.dumps(preview, sort_keys=True),
                    "completed" if clean_flush == "final" else "active",
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT id, metadata_json, preview_json, event_count FROM replay_sessions
                WHERE project_id = ? AND environment_id = ? AND stable_id = ?
                """,
                (project_id, environment_id, session_id),
            ).fetchone()
            assert row is not None
            session_row_id = str(row["id"])
            existing_metadata = self._safe_json_obj(row["metadata_json"])
            merged_metadata = {**existing_metadata, **(metadata or {})}
            existing_preview = self._safe_json_obj(row["preview_json"])

            batch_id = self._id("rb")
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO replay_batches
                (id, session_row_id, project_id, environment_id, session_id, sequence,
                 flush_type, payload_json, blob_backend, blob_key, event_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    "",
                    "",
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
                if self.replay_blob_store is not None:
                    blob_backend = self.replay_blob_store.backend
                    blob_key = self.replay_blob_store.write_events(
                        project_id=project_id,
                        environment_id=environment_id,
                        session_id=session_id,
                        sequence=sequence,
                        events=events,
                    )
                    conn.execute(
                        """
                        UPDATE replay_batches
                        SET blob_backend = ?, blob_key = ?
                        WHERE id = ?
                        """,
                        (blob_backend, blob_key, batch_id),
                    )
                merged_preview = self._merge_replay_preview(
                    existing_preview,
                    preview,
                    event_count=int(row["event_count"] or 0) + event_count,
                )
                conn.execute(
                    """
                    UPDATE replay_sessions
                    SET last_seen_at = ?,
                        event_count = event_count + ?,
                        distinct_id = COALESCE(NULLIF(?, ''), distinct_id),
                        metadata_json = ?,
                        preview_json = ?,
                        status = CASE WHEN ? = 'final' THEN 'completed' ELSE status END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        event_count,
                        distinct_id or "",
                        json.dumps(merged_metadata),
                        json.dumps(merged_preview, sort_keys=True),
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
        project_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        where: list[str] = []
        params: list[str] = []
        if kind is not None:
            where.append("kind = ?")
            params.append(kind)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        if project_id is not None:
            where.append("project_id = ?")
            params.append(project_id)
        query = "SELECT * FROM processing_jobs"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY created_at, id LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._conn() as conn:
            return conn.execute(query, params).fetchall()

    def claim_processing_job(self, job_id: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE processing_jobs
                SET status = 'running',
                    attempts = attempts + 1,
                    updated_at = ?
                WHERE id = ? AND status IN ('queued', 'failed')
                """,
                (now, job_id),
            )
            return int(cur.rowcount) > 0

    def finish_processing_job(
        self,
        *,
        job_id: str,
        status: str,
        error: str = "",
    ) -> ProcessingJobUpdateResult:
        if status not in {"succeeded", "failed"}:
            raise ValueError("invalid processing job status")
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE processing_jobs
                SET status = ?, last_error = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (status, error, now, job_id),
            )
            return ProcessingJobUpdateResult(
                job_id=job_id,
                updated=int(cur.rowcount) > 0,
            )

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

    def get_replay_session_by_public_id(
        self,
        *,
        project_id: str,
        environment_id: str,
        replay_id: str,
    ) -> Optional[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT *
                FROM replay_sessions
                WHERE project_id = ? AND environment_id = ? AND public_id = ?
                """,
                (project_id, environment_id, replay_id),
            ).fetchone()

    def list_replay_sessions(
        self,
        *,
        project_id: str,
        environment_id: str,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        params: list[object] = [project_id, environment_id]
        where = "project_id = ? AND environment_id = ?"
        if status is not None:
            where += " AND status = ?"
            params.append(status)
        params.append(max(1, min(int(limit), 500)))
        with self._conn() as conn:
            return conn.execute(
                f"""
                SELECT *
                FROM replay_sessions
                WHERE {where}
                ORDER BY last_seen_at DESC, created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

    def list_recent_replay_sessions(self, *, limit: int = 100) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT *
                FROM replay_sessions
                ORDER BY last_seen_at DESC, created_at DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()

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

    def _events_for_replay_batch(self, batch: sqlite3.Row) -> list[dict[str, Any]]:
        blob_key = ""
        blob_backend = ""
        try:
            blob_key = str(batch["blob_key"] or "")
            blob_backend = str(batch["blob_backend"] or "")
        except (IndexError, KeyError):
            blob_key = ""
            blob_backend = ""
        if (
            blob_key
            and self.replay_blob_store is not None
            and blob_backend == self.replay_blob_store.backend
        ):
            try:
                return self.replay_blob_store.read_events(blob_key)
            except Exception:
                pass

        payload = self._safe_json_obj(batch["payload_json"])
        batch_events = payload.get("events")
        if not isinstance(batch_events, list):
            return []
        return [event for event in batch_events if isinstance(event, dict)]

    def _count_distinct_users_for_sessions(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        environment_id: str,
        session_ids: list[str],
    ) -> int:
        if not session_ids:
            return 0
        placeholders = ",".join("?" for _ in session_ids)
        rows = conn.execute(
            f"""
            SELECT stable_id, distinct_id
            FROM replay_sessions
            WHERE project_id = ? AND environment_id = ? AND stable_id IN ({placeholders})
            """,
            [project_id, environment_id, *session_ids],
        ).fetchall()
        if not rows:
            return len(set(session_ids))
        users = {
            str(row["distinct_id"])
            for row in rows
            if str(row["distinct_id"] or "").strip()
        }
        anonymous_sessions = {
            str(row["stable_id"])
            for row in rows
            if not str(row["distinct_id"] or "").strip()
        }
        return len(users) + len(anonymous_sessions)

    def get_replay_playback(
        self,
        *,
        project_id: str,
        environment_id: str,
        session_id: Optional[str] = None,
        replay_id: Optional[str] = None,
    ) -> Optional[ReplayPlayback]:
        if not session_id and not replay_id:
            raise ValueError("session_id or replay_id is required")
        session = (
            self.get_replay_session(
                project_id=project_id,
                environment_id=environment_id,
                session_id=session_id,
            )
            if session_id
            else self.get_replay_session_by_public_id(
                project_id=project_id,
                environment_id=environment_id,
                replay_id=str(replay_id),
            )
        )
        if session is None:
            return None
        batches = self.list_replay_batches(
            project_id=project_id,
            environment_id=environment_id,
            session_id=str(session["stable_id"]),
        )
        events: list[dict[str, Any]] = []
        for batch in batches:
            events.extend(self._events_for_replay_batch(batch))
        return ReplayPlayback(session=session, batches=batches, events=events)

    def upsert_replay_signals(
        self,
        *,
        project_id: str,
        environment_id: str,
        signals: list[object],
    ) -> int:
        inserted = 0
        with self._conn() as conn:
            for signal in signals:
                session_id = str(getattr(signal, "session_id"))
                detector = str(getattr(signal, "detector"))
                timestamp_ms = int(getattr(signal, "timestamp_ms"))
                url = str(getattr(signal, "url", "") or "")
                details = getattr(signal, "details", {}) or {}
                details_json = json.dumps(details, sort_keys=True, separators=(",", ":"))
                details_hash = hashlib.sha256(details_json.encode("utf-8")).hexdigest()
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO replay_signals
                    (id, project_id, environment_id, session_id, detector, timestamp_ms,
                     url, details_json, details_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._id("sig"),
                        project_id,
                        environment_id,
                        session_id,
                        detector,
                        timestamp_ms,
                        url,
                        details_json,
                        details_hash,
                    ),
                )
                inserted += int(cur.rowcount)
        return inserted

    def list_replay_signals(
        self,
        *,
        project_id: str,
        environment_id: str,
        session_id: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        where = "project_id = ? AND environment_id = ?"
        params: list[object] = [project_id, environment_id]
        if session_id is not None:
            where += " AND session_id = ?"
            params.append(session_id)
        with self._conn() as conn:
            return conn.execute(
                f"""
                SELECT *
                FROM replay_signals
                WHERE {where}
                ORDER BY session_id, timestamp_ms, detector
                """,
                params,
            ).fetchall()

    def upsert_replay_issue(
        self,
        *,
        project_id: str,
        environment_id: str,
        fingerprint: str,
        session_ids: list[str],
        signal_summary: dict[str, int],
        first_seen_ms: int,
        last_seen_ms: int,
        title: str = "",
        summary: str = "",
        likely_cause: str = "",
        reproduction_steps: Optional[list[str]] = None,
        severity: str = "medium",
        priority: str = "medium",
        confidence: str = "medium",
        fingerprint_version: int = 1,
        analysis_status: str = "",
        analysis_model: str = "",
        analysis_prompt_version: str = "",
        analysis_created_at: str = "",
        analysis_error: str = "",
        evidence: Optional[dict[str, Any]] = None,
        distinct_id: str = "",
        error_issue_ids: Optional[list[str]] = None,
        trace_ids: Optional[list[str]] = None,
        top_stack_frame: str = "",
        error_tracking_url: str = "",
        logs_url: str = "",
    ) -> ReplayIssueUpsertResult:
        now = datetime.now(timezone.utc).isoformat()
        public_id = self.make_issue_public_id(project_id, environment_id, fingerprint)
        issue_id = self._id("ri")
        clean_session_ids = list(dict.fromkeys(str(s) for s in session_ids if str(s)))
        representative_session_id = clean_session_ids[0] if clean_session_ids else ""
        with self._conn() as conn:
            prior_row = conn.execute(
                """
                SELECT status, resolved_at FROM replay_issues
                WHERE project_id = ? AND environment_id = ? AND fingerprint = ?
                """,
                (project_id, environment_id, fingerprint),
            ).fetchone()
            previous_status = str(prior_row["status"]) if prior_row else ""
            previous_resolved_at = str(prior_row["resolved_at"] or "") if prior_row else ""
            conn.execute(
                """
                INSERT INTO replay_issues
                (id, project_id, environment_id, public_id, fingerprint,
                 fingerprint_version, status, priority, severity, title, summary,
                 likely_cause, reproduction_steps_json, confidence, analysis_status,
                 analysis_model, analysis_prompt_version, analysis_created_at,
                 analysis_error, evidence_json, signal_summary_json, affected_count,
                 affected_users, representative_session_id,
                 distinct_id, error_issue_ids_json, trace_ids_json,
                 top_stack_frame, error_tracking_url, logs_url,
                 first_seen_ms, last_seen_ms, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, environment_id, fingerprint) DO UPDATE SET
                    status = CASE
                        WHEN replay_issues.status = 'resolved' THEN 'regressed'
                        WHEN replay_issues.status = 'new' THEN 'ongoing'
                        WHEN replay_issues.status = 'unresolved' THEN 'ongoing'
                        WHEN replay_issues.status = 'ongoing' THEN 'ongoing'
                        WHEN replay_issues.status = 'regressed' THEN 'ongoing'
                        WHEN replay_issues.status = 'ticket_created' THEN 'ticket_created'
                        WHEN replay_issues.status = 'ignored' THEN 'ignored'
                        ELSE replay_issues.status
                    END,
                    fingerprint_version = excluded.fingerprint_version,
                    priority = excluded.priority,
                    severity = excluded.severity,
                    title = excluded.title,
                    summary = excluded.summary,
                    likely_cause = excluded.likely_cause,
                    reproduction_steps_json = excluded.reproduction_steps_json,
                    confidence = excluded.confidence,
                    analysis_status = excluded.analysis_status,
                    analysis_model = excluded.analysis_model,
                    analysis_prompt_version = excluded.analysis_prompt_version,
                    analysis_created_at = excluded.analysis_created_at,
                    analysis_error = excluded.analysis_error,
                    evidence_json = excluded.evidence_json,
                    signal_summary_json = excluded.signal_summary_json,
                    affected_count = excluded.affected_count,
                    affected_users = excluded.affected_users,
                    distinct_id = CASE
                        WHEN excluded.distinct_id != '' THEN excluded.distinct_id
                        ELSE replay_issues.distinct_id
                    END,
                    error_issue_ids_json = CASE
                        WHEN excluded.error_issue_ids_json NOT IN ('', '[]')
                            THEN excluded.error_issue_ids_json
                        ELSE replay_issues.error_issue_ids_json
                    END,
                    trace_ids_json = CASE
                        WHEN excluded.trace_ids_json NOT IN ('', '[]')
                            THEN excluded.trace_ids_json
                        ELSE replay_issues.trace_ids_json
                    END,
                    top_stack_frame = CASE
                        WHEN excluded.top_stack_frame != '' THEN excluded.top_stack_frame
                        ELSE replay_issues.top_stack_frame
                    END,
                    error_tracking_url = CASE
                        WHEN excluded.error_tracking_url != '' THEN excluded.error_tracking_url
                        ELSE replay_issues.error_tracking_url
                    END,
                    logs_url = CASE
                        WHEN excluded.logs_url != '' THEN excluded.logs_url
                        ELSE replay_issues.logs_url
                    END,
                    representative_session_id = COALESCE(
                        NULLIF(replay_issues.representative_session_id, ''),
                        excluded.representative_session_id
                    ),
                    first_seen_ms = MIN(replay_issues.first_seen_ms, excluded.first_seen_ms),
                    last_seen_ms = MAX(replay_issues.last_seen_ms, excluded.last_seen_ms),
                    resolved_at = CASE
                        WHEN replay_issues.status = 'resolved' THEN NULL
                        ELSE replay_issues.resolved_at
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    issue_id,
                    project_id,
                    environment_id,
                    public_id,
                    fingerprint,
                    int(fingerprint_version),
                    priority,
                    severity,
                    title,
                    summary,
                    likely_cause,
                    json.dumps(reproduction_steps or []),
                    confidence,
                    analysis_status,
                    analysis_model,
                    analysis_prompt_version,
                    analysis_created_at or None,
                    analysis_error,
                    json.dumps(evidence or {}, sort_keys=True),
                    json.dumps(signal_summary, sort_keys=True),
                    len(clean_session_ids),
                    self._count_distinct_users_for_sessions(
                        conn,
                        project_id=project_id,
                        environment_id=environment_id,
                        session_ids=clean_session_ids,
                    ),
                    representative_session_id,
                    distinct_id,
                    json.dumps(error_issue_ids or []),
                    json.dumps(trace_ids or []),
                    top_stack_frame,
                    error_tracking_url,
                    logs_url,
                    int(first_seen_ms),
                    int(last_seen_ms),
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT id, public_id, status FROM replay_issues
                WHERE project_id = ? AND environment_id = ? AND fingerprint = ?
                """,
                (project_id, environment_id, fingerprint),
            ).fetchone()
            assert row is not None
            inserted = str(row["id"]) == issue_id
            issue_id = str(row["id"])
            public_id = str(row["public_id"])
            current_status = str(row["status"])
            for sid in clean_session_ids:
                conn.execute(
                    """
                    INSERT INTO replay_issue_sessions
                    (issue_id, project_id, environment_id, session_id, role,
                     first_seen_ms, last_seen_ms)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(issue_id, session_id) DO UPDATE SET
                        role = CASE
                            WHEN replay_issue_sessions.role = 'representative' THEN 'representative'
                            ELSE excluded.role
                        END,
                        first_seen_ms = MIN(replay_issue_sessions.first_seen_ms, excluded.first_seen_ms),
                        last_seen_ms = MAX(replay_issue_sessions.last_seen_ms, excluded.last_seen_ms)
                    """,
                    (
                        issue_id,
                        project_id,
                        environment_id,
                        sid,
                        "representative" if sid == representative_session_id else "supporting",
                        int(first_seen_ms),
                        int(last_seen_ms),
                    ),
                )
            conn.execute(
                """
                UPDATE replay_issues
                SET affected_count = (
                    SELECT COUNT(*) FROM replay_issue_sessions WHERE issue_id = ?
                ),
                    affected_users = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    issue_id,
                    self._count_distinct_users_for_sessions(
                        conn,
                        project_id=project_id,
                        environment_id=environment_id,
                        session_ids=[
                            str(row["session_id"])
                            for row in conn.execute(
                                """
                                SELECT session_id
                                FROM replay_issue_sessions
                                WHERE issue_id = ?
                                """,
                                (issue_id,),
                            ).fetchall()
                        ],
                    ),
                    now,
                    issue_id,
                ),
            )
            issue_row = conn.execute(
                """
                SELECT *
                FROM replay_issues
                WHERE id = ?
                """,
                (issue_id,),
            ).fetchone()
            assert issue_row is not None
            canonical_failure_id = self._upsert_failure(
                conn,
                failure=canonical_failure_from_replay_issue(dict(issue_row)),
                now=now,
                failure_id=self._id("flr"),
            )
            conn.execute(
                """
                UPDATE replay_issues
                SET canonical_failure_id = ?
                WHERE id = ?
                """,
                (canonical_failure_id, issue_id),
            )
            self._backfill_replay_issue_evidence(
                conn,
                failure_id=canonical_failure_id,
                issue_public_id=public_id,
                evidence=dict(self._safe_json_obj(issue_row["evidence_json"])),
            )
        return ReplayIssueUpsertResult(
            issue_id=issue_id,
            public_id=public_id,
            inserted=inserted,
            previous_status=previous_status,
            current_status=current_status,
            previous_resolved_at=previous_resolved_at,
        )

    def list_replay_issues(
        self,
        *,
        project_id: str,
        environment_id: str,
        status: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        where = "project_id = ? AND environment_id = ?"
        params: list[object] = [project_id, environment_id]
        if status is not None:
            where += " AND status = ?"
            params.append(status)
        with self._conn() as conn:
            return conn.execute(
                f"""
                SELECT *
                FROM replay_issues
                WHERE {where}
                ORDER BY updated_at DESC, public_id
                """,
                params,
            ).fetchall()

    def get_replay_issue(
        self,
        *,
        project_id: str,
        environment_id: str,
        issue_id: str,
    ) -> Optional[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT *
                FROM replay_issues
                WHERE project_id = ? AND environment_id = ?
                  AND (id = ? OR public_id = ?)
                """,
                (project_id, environment_id, issue_id, issue_id),
            ).fetchone()

    def find_replay_issue(self, issue_id: str) -> Optional[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT *
                FROM replay_issues
                WHERE id = ? OR public_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (issue_id, issue_id),
            ).fetchone()

    def list_replay_issue_sessions(self, issue_id: str) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT *
                FROM replay_issue_sessions
                WHERE issue_id = ?
                ORDER BY
                    CASE role WHEN 'representative' THEN 0 ELSE 1 END,
                    created_at,
                    session_id
                """,
                (issue_id,),
            ).fetchall()

    def list_replay_issue_sessions_for_issues(
        self, issue_ids: list[str]
    ) -> list[sqlite3.Row]:
        clean_ids = [str(issue_id).strip() for issue_id in issue_ids if str(issue_id).strip()]
        if not clean_ids:
            return []
        placeholders = ",".join("?" for _ in clean_ids)
        with self._conn() as conn:
            return conn.execute(
                f"""
                SELECT
                    ris.*,
                    rs.public_id AS replay_public_id,
                    rs.stable_id AS replay_stable_id
                FROM replay_issue_sessions ris
                LEFT JOIN replay_issues ri
                  ON ri.id = ris.issue_id
                LEFT JOIN replay_sessions rs
                  ON rs.project_id = ri.project_id
                 AND rs.environment_id = ri.environment_id
                 AND rs.stable_id = ris.session_id
                WHERE ris.issue_id IN ({placeholders})
                ORDER BY
                    ris.issue_id,
                    CASE ris.role WHEN 'representative' THEN 0 ELSE 1 END,
                    ris.created_at,
                    ris.session_id
                """,
                clean_ids,
            ).fetchall()

    def list_recent_replay_issues(self, *, limit: int = 100) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT *
                FROM replay_issues
                ORDER BY updated_at DESC, public_id
                LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()

    def list_ticketed_replay_issues(
        self,
        *,
        project_id: str,
        environment_id: str,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        """Return open replay issues that have an upstream ticket attached.

        Used by the ticket-sync workflow to poll Linear/GitHub for state
        without re-resolving every replay issue.
        """
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT *
                FROM replay_issues
                WHERE project_id = ?
                  AND environment_id = ?
                  AND external_ticket_id IS NOT NULL
                  AND external_ticket_id != ''
                  AND status != 'resolved'
                  AND status != 'ignored'
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (project_id, environment_id, max(1, int(limit))),
            ).fetchall()

    def transition_replay_issue(
        self,
        issue_id: str,
        *,
        status: str,
        external_ticket_id: str = "",
        external_ticket_url: str = "",
    ) -> bool:
        allowed = {
            "new",
            "unresolved",
            "ticket_created",
            "resolved",
            "ongoing",
            "regressed",
            "ignored",
        }
        if status not in allowed:
            raise ValueError(f"invalid replay issue status: {status}")
        now = datetime.now(timezone.utc).isoformat()
        resolved_at = now if status == "resolved" else None
        external_state = "created" if status == "ticket_created" else ""
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE replay_issues
                SET status = ?,
                    external_ticket_state = COALESCE(NULLIF(?, ''), external_ticket_state),
                    external_ticket_id = COALESCE(NULLIF(?, ''), external_ticket_id),
                    external_ticket_url = COALESCE(NULLIF(?, ''), external_ticket_url),
                    resolved_at = CASE WHEN ? = 'resolved' THEN ? ELSE NULL END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    external_state,
                    external_ticket_id,
                    external_ticket_url,
                    status,
                    resolved_at,
                    now,
                    issue_id,
                ),
            )
            return int(cur.rowcount) > 0

    def resolve_replay_issue(self, issue_id: str) -> bool:
        return self.transition_replay_issue(issue_id, status="resolved")

    def mark_replay_issue_unresolved(self, issue_id: str) -> bool:
        return self.transition_replay_issue(issue_id, status="unresolved")

    def ignore_replay_issue(self, issue_id: str) -> bool:
        return self.transition_replay_issue(issue_id, status="ignored")

    def mark_replay_issue_ticket_created(
        self,
        issue_id: str,
        *,
        external_ticket_id: str,
        external_ticket_url: str,
    ) -> bool:
        return self.transition_replay_issue(
            issue_id,
            status="ticket_created",
            external_ticket_id=external_ticket_id,
            external_ticket_url=external_ticket_url,
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
