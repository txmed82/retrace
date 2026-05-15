"""Storage class - main database access layer."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from uuid import uuid4
import json
from pathlib import Path
from typing import Any, Optional

from retrace.evidence import (
    PROMPT_SAFE_REDACTION_STATES,
    EvidenceItem,
    evidence_dedupe_key,
    evidence_items_from_replay_issue,
)
from retrace.failures import (
    CanonicalFailure,
    canonical_failure_from_replay_issue,
    normalize_failure_status,
)
from retrace.repair import normalize_repair_task_status

from .helpers import (
    _id,
    _public_id,
    _dt,
    _now_iso_microseconds,
    _safe_json_obj,
    _merge_string_lists,
    _parse_string_list_json,
    _parse_dict_list_json,
    _replay_preview,
    _merge_replay_preview,
    _slug,
    _SEVERITY_ORDER,
    _rollup_severity,
    _string_values,
    _normalize_github_review_run_status,
    _normalize_app_error_incident_status,
    _retention_interval,
    FAILURE_TEST_COVERAGE_STATES,
    INGEST_RATE_LIMIT_RETENTION_SECONDS,
    INGEST_RATE_LIMIT_MAX_IDENTITIES_PER_BUCKET,
    APP_ERROR_FAILURE_STATUS_BY_INCIDENT_STATUS,
)
from .schema import SCHEMA
from .models import (
    SessionMeta,
    RunRow,
    GitHubRepoRow,
    ReportFindingRow,
    FixPromptRow,
    WorkspaceIds,
    SDKKeyRow,
    ServiceTokenRow,
    SignalDefinitionRow,
    FailureRow,
    EvidenceRow,
    IncidentRow,
    IncidentLifecycleEventRow,
    AppErrorAlertRuleRow,
    AlertRouteRow,
    RateLimitDecision,
    AppErrorRetentionPruneResult,
    DeployMarkerRow,
    SourceMapRow,
    OtelEventRow,
    FailureTestLinkRow,
    RepairTaskRow,
    GitHubReviewRunRow,
    ReplayBatchResult,
    ReplayPlayback,
    ReplayIssueUpsertResult,
    ProcessingJobUpdateResult,
)
from .blob import ReplayBlobStore, LocalReplayBlobStore
from .repositories.incidents import IncidentRepository
from .repositories.incidents import IncidentRepository

class Storage:
    def __init__(
        self,
        path: Path | str,
        replay_blob_dir: Optional[Path] = None,
    ):
        # P1.5: route to a Backend (`SqliteBackend` or `PostgresBackend`).
        # The 290+ existing `conn.execute(...)` call sites keep their
        # sqlite-flavored SQL -- `WrappedConnection` translates at
        # execute time (see `retrace.sql_dialect`).
        from retrace.storage_backend import (
            ParsedDsn,
            SqliteBackend,
            backend_from_url,
            parse_storage_url,
        )

        if isinstance(path, str) and "://" in path:
            dsn = parse_storage_url(path)
            self._backend = backend_from_url(path)
            if dsn.is_postgres():
                # `self.path` stays defined for back-compat callers; it
                # points at the database name so existing diagnostics
                # don't crash on `str(self.path)`.
                self.path = Path(f"postgres:{dsn.database}")
            else:
                self.path = Path(dsn.path)
        else:
            # Bare path → SQLite (back-compat).
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._backend = SqliteBackend(ParsedDsn(scheme="sqlite", path=str(self.path)))

        self.replay_blob_store: ReplayBlobStore | None = (
            LocalReplayBlobStore(replay_blob_dir) if replay_blob_dir is not None else None
        )

        # Repositories (P1.0 Migration)
        self.incidents = IncidentRepository(self)

        # Repositories (P1.0 Migration)
        self.incidents = IncidentRepository(self)

    @property
    def backend_name(self) -> str:
        """`"sqlite"` or `"postgres"` -- useful for tests + diagnostics."""
        return self._backend.name

    def _conn(self):
        """Return a connection wrapped with the sqlite3.Connection
        surface -- sqlite3-flavored SQL still works against Postgres
        via the dialect translator in `retrace.sql_dialect`."""
        return self._backend.connect()

    def init_schema(self) -> None:
        from retrace.sql_schema import translate_schema

        schema_sql = translate_schema(SCHEMA, dialect=self._backend.name)
        with self._conn() as conn:
            conn.executescript(schema_sql)
            if self._backend.name != "sqlite":
                # Postgres path: lightweight `PRAGMA table_info`-based
                # migrations below don't apply -- a fresh Postgres install
                # gets the current schema from the CREATE TABLE block.
                # SQLite-side, they catch up old DBs.
                return
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
            cols_alert_rules = [
                r["name"]
                for r in conn.execute(
                    "PRAGMA table_info(app_error_alert_rules)"
                ).fetchall()
            ]
            if "precedence" not in cols_alert_rules:
                conn.execute(
                    "ALTER TABLE app_error_alert_rules ADD COLUMN precedence INTEGER NOT NULL DEFAULT 0"
                )
            # P3.5: backfill the cost-visibility columns onto old DBs.
            # Default 0 / 0.0 means historical rows show as "unknown
            # cost" in the summary CLI (which is honest -- we didn't
            # capture tokens for those runs).
            cols_llm_reviews = [
                r["name"]
                for r in conn.execute(
                    "PRAGMA table_info(llm_pr_reviews)"
                ).fetchall()
            ]
            if "input_tokens" not in cols_llm_reviews:
                conn.execute(
                    "ALTER TABLE llm_pr_reviews ADD COLUMN input_tokens INTEGER NOT NULL DEFAULT 0"
                )
            if "output_tokens" not in cols_llm_reviews:
                conn.execute(
                    "ALTER TABLE llm_pr_reviews ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0"
                )
            if "estimated_cost_usd" not in cols_llm_reviews:
                conn.execute(
                    "ALTER TABLE llm_pr_reviews ADD COLUMN estimated_cost_usd REAL NOT NULL DEFAULT 0.0"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS incident_lifecycle_events (
                    id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    environment_id TEXT NOT NULL,
                    from_status TEXT NOT NULL DEFAULT '',
                    to_status TEXT NOT NULL,
                    actor_type TEXT NOT NULL DEFAULT '',
                    actor_id TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_incident_lifecycle_events_incident_time
                ON incident_lifecycle_events(incident_id, created_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_incident_lifecycle_events_scope_time
                ON incident_lifecycle_events(project_id, environment_id, created_at)
                """
            )
            conn.execute("DROP INDEX IF EXISTS idx_app_error_alert_rules_scope")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_app_error_alert_rules_eval
                ON app_error_alert_rules(
                    project_id,
                    environment_id,
                    enabled,
                    precedence DESC,
                    created_at ASC,
                    id ASC
                )
                """
            )
            self._backfill_failure_trace_map(conn)
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
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_github_review_runs_repo_pr
                ON github_review_runs(repo_full_name, pr_number, updated_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_github_review_runs_status
                ON github_review_runs(status, updated_at)
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_github_review_runs_comment
                ON github_review_runs(repo_full_name, pr_number, comment_id)
                WHERE comment_id != ''
                """
            )
            self._backfill_failure_test_links(conn)

    @staticmethod
    def _id(prefix: str) -> str:
        return _id(prefix)

    @staticmethod
    def _public_id(prefix: str, *parts: object) -> str:
        return _public_id(prefix, *parts)

    @staticmethod
    def _dt(value: object) -> Optional[datetime]:
        return _dt(value)

    @staticmethod
    def _now_iso_microseconds() -> str:
        return _now_iso_microseconds()

    @staticmethod
    def _safe_json_obj(raw: object) -> dict[str, object]:
        return _safe_json_obj(raw)

    @staticmethod
    def _parse_string_list_json(raw: object) -> list[str]:
        return _parse_string_list_json(raw)

    @staticmethod
    def _parse_dict_list_json(raw: object) -> list[dict[str, Any]]:
        return _parse_dict_list_json(raw)

    @staticmethod
    def _merge_string_lists(*values: list[str]) -> list[str]:
        return _merge_string_lists(*values)

    @staticmethod
    def _slug(value: str) -> str:
        return _slug(value)

    @staticmethod
    def make_replay_public_id(project_id: str, environment_id: str, session_id: str) -> str:
        return _public_id("rpl", project_id, environment_id, session_id)

    @staticmethod
    def make_issue_public_id(project_id: str, environment_id: str, fingerprint: str) -> str:
        return _public_id("bug", project_id, environment_id, fingerprint)

    def upsert_incident(self, *, project_id: str, environment_id: str, group_key: str, title: str, summary: str = "", severity: str = "medium", status: str = "open", metadata: Optional[dict[str, Any]] = None, reopen_resolved: bool = False) -> str:
        return self.incidents.upsert_incident(project_id=project_id, environment_id=environment_id, group_key=group_key, title=title, summary=summary, severity=severity, status=status, metadata=metadata, reopen_resolved=reopen_resolved)

    def link_failure_to_incident(self, *, incident_id: str, failure_id: str) -> None:
        return self.incidents.link_failure_to_incident(incident_id=incident_id, failure_id=failure_id)

    def move_failure_to_incident(self, *, incident_id: str, failure_id: str) -> None:
        return self.incidents.move_failure_to_incident(incident_id=incident_id, failure_id=failure_id)

    def get_incident(self, incident_id: str) -> Optional[IncidentRow]:
        return self.incidents.get_incident(incident_id)

    def find_incident_by_group(self, *, project_id: str, environment_id: str, group_key: str) -> Optional[IncidentRow]:
        return self.incidents.find_incident_by_group(project_id=project_id, environment_id=environment_id, group_key=group_key)

    def list_incidents(self, *, project_id: str, environment_id: str, status: Optional[str] = None, limit: int = 100) -> list[IncidentRow]:
        return self.incidents.list_incidents(project_id=project_id, environment_id=environment_id, status=status, limit=limit)

    def list_incident_failures(self, *, incident_id: str) -> list[FailureRow]:
        return self.incidents.list_incident_failures(incident_id=incident_id)

    def list_incident_failures_for_incidents(self, *, incident_ids: list[str]) -> dict[str, list[FailureRow]]:
        return self.incidents.list_incident_failures_for_incidents(incident_ids=incident_ids)

    def list_incident_evidence(self, *, incident_id: str, include_sensitive: bool = True) -> list[EvidenceRow]:
        return self.incidents.list_incident_evidence(incident_id=incident_id, include_sensitive=include_sensitive)

    def transition_app_error_incident(self, *, project_id: str, environment_id: str, incident_id: str, status: str, actor_type: str = "service_token", actor_id: str = "", reason: str = "", metadata: Optional[dict[str, Any]] = None) -> IncidentRow:
        return self.incidents.transition_app_error_incident(project_id=project_id, environment_id=environment_id, incident_id=incident_id, status=status, actor_type=actor_type, actor_id=actor_id, reason=reason, metadata=metadata)

    def list_incident_lifecycle_events(self, *, incident_id: str, limit: int = 100) -> list[IncidentLifecycleEventRow]:
        return self.incidents.list_incident_lifecycle_events(incident_id=incident_id, limit=limit)

    def _append_incident_lifecycle_event(self, conn: sqlite3.Connection, *, incident_id: str, project_id: str, environment_id: str, from_status: str, to_status: str, actor_type: str, actor_id: str, reason: str, metadata: Optional[dict[str, Any]] = None, metadata_json: str = "", created_at: str = "") -> None:
        return self.incidents._append_incident_lifecycle_event(conn, incident_id=incident_id, project_id=project_id, environment_id=environment_id, from_status=from_status, to_status=to_status, actor_type=actor_type, actor_id=actor_id, reason=reason, metadata=metadata, metadata_json=metadata_json, created_at=created_at)

    def _resolve_incident_id(self, incident_id: str) -> str:
        return self.incidents._resolve_incident_id(incident_id)

    def set_incident_repair_task(self, *, incident_id: str, repair_task_id: str) -> None:
        return self.incidents.set_incident_repair_task(incident_id=incident_id, repair_task_id=repair_task_id)

    def _incident_from_row(self, row: sqlite3.Row) -> IncidentRow:
        return self.incidents._incident_from_row(row)

    def _incident_lifecycle_event_from_row(self, row: sqlite3.Row) -> IncidentLifecycleEventRow:
        return self.incidents._incident_lifecycle_event_from_row(row)

    def upsert_app_error_alert_rule(self, *, project_id: str, environment_id: str, name: str, enabled: bool = True, precedence: int = 0, action: str = "alert", min_severity: str = "", provider: str = "", title_contains: str = "", fingerprint_contains: str = "", route_contains: str = "", metadata: Optional[dict[str, Any]] = None) -> str:
        return self.incidents.upsert_app_error_alert_rule(project_id=project_id, environment_id=environment_id, name=name, enabled=enabled, precedence=precedence, action=action, min_severity=min_severity, provider=provider, title_contains=title_contains, fingerprint_contains=fingerprint_contains, route_contains=route_contains, metadata=metadata)

    def list_app_error_alert_rules(self, *, project_id: str, environment_id: str, enabled: Optional[bool] = None, limit: int = 100, offset: int = 0) -> list[AppErrorAlertRuleRow]:
        return self.incidents.list_app_error_alert_rules(project_id=project_id, environment_id=environment_id, enabled=enabled, limit=limit, offset=offset)

    def delete_app_error_alert_rule(self, *, project_id: str, environment_id: str, name: str) -> bool:
        return self.incidents.delete_app_error_alert_rule(project_id=project_id, environment_id=environment_id, name=name)

    def _app_error_alert_rule_from_row(self, row: sqlite3.Row) -> AppErrorAlertRuleRow:
        return self.incidents._app_error_alert_rule_from_row(row)

    def upsert_alert_route(self, *, project_id: str, environment_id: str, name: str, target_kind: str, target_url: str, target_secret: str = "", rule_name: str = "", min_severity: str = "", dedup_window_seconds: int = 300, enabled: bool = True) -> AlertRouteRow:
        return self.incidents.upsert_alert_route(project_id=project_id, environment_id=environment_id, name=name, target_kind=target_kind, target_url=target_url, target_secret=target_secret, rule_name=rule_name, min_severity=min_severity, dedup_window_seconds=dedup_window_seconds, enabled=enabled)

    def list_alert_routes(self, *, project_id: str, environment_id: str, enabled: Optional[bool] = None, rule_name: Optional[str] = None) -> list[AlertRouteRow]:
        return self.incidents.list_alert_routes(project_id=project_id, environment_id=environment_id, enabled=enabled, rule_name=rule_name)

    def get_alert_route(self, *, project_id: str, environment_id: str, name: str) -> Optional[AlertRouteRow]:
        return self.incidents.get_alert_route(project_id=project_id, environment_id=environment_id, name=name)

    def delete_alert_route(self, *, project_id: str, environment_id: str, name: str) -> bool:
        return self.incidents.delete_alert_route(project_id=project_id, environment_id=environment_id, name=name)

    def record_alert_dispatch(self, *, route_id: str, project_id: str, environment_id: str, fingerprint: str, status: str, target_kind: str, target_url: str, payload: dict, error: str = "") -> int:
        return self.incidents.record_alert_dispatch(route_id=route_id, project_id=project_id, environment_id=environment_id, fingerprint=fingerprint, status=status, target_kind=target_kind, target_url=target_url, payload=payload, error=error)

    def recent_alert_dispatch_for(self, *, route_id: str, fingerprint: str, within_seconds: int) -> Optional[sqlite3.Row]:
        return self.incidents.recent_alert_dispatch_for(route_id=route_id, fingerprint=fingerprint, within_seconds=within_seconds)

    def list_recent_alert_dispatches(self, *, project_id: str, environment_id: str, limit: int = 50) -> list[sqlite3.Row]:
        return self.incidents.list_recent_alert_dispatches(project_id=project_id, environment_id=environment_id, limit=limit)

    def _alert_route_from_row(self, row: sqlite3.Row) -> AlertRouteRow:
        return self.incidents._alert_route_from_row(row)

    def _refresh_incident_rollup(self, conn: sqlite3.Connection, *, incident_id: str) -> None:
        return self.incidents._refresh_incident_rollup(conn, incident_id=incident_id)

    def _refresh_incidents_for_failure(self, conn: sqlite3.Connection, *, failure_id: str) -> None:
        return self.incidents._refresh_incidents_for_failure(conn, failure_id=failure_id)

    def _replace_failure_trace_map(self, conn: sqlite3.Connection, *, failure_id: str, metadata: dict[str, Any]) -> None:
        return self.incidents._replace_failure_trace_map(conn, failure_id=failure_id, metadata=metadata)

    def _backfill_failure_trace_map(self, conn: sqlite3.Connection) -> None:
        return self.incidents._backfill_failure_trace_map(conn)

    def record_deploy_marker(self, *, project_id: str, environment_id: str, sha: str, branch: str = "", author: str = "", deployed_at_ms: int = 0, changed_files: Optional[list[str]] = None, metadata: Optional[dict[str, Any]] = None) -> str:
        return self.incidents.record_deploy_marker(project_id=project_id, environment_id=environment_id, sha=sha, branch=branch, author=author, deployed_at_ms=deployed_at_ms, changed_files=changed_files, metadata=metadata)

    def get_deploy_marker(self, deploy_id: str) -> Optional[DeployMarkerRow]:
        return self.incidents.get_deploy_marker(deploy_id)

    def get_deploy_marker_by_sha(self, *, project_id: str, environment_id: str, sha: str) -> Optional[DeployMarkerRow]:
        return self.incidents.get_deploy_marker_by_sha(project_id=project_id, environment_id=environment_id, sha=sha)

    def list_deploy_markers(self, *, project_id: str, environment_id: str, limit: int = 50) -> list[DeployMarkerRow]:
        return self.incidents.list_deploy_markers(project_id=project_id, environment_id=environment_id, limit=limit)

    def nearest_deploy_marker(self, *, project_id: str, environment_id: str, at_ms: int) -> Optional[DeployMarkerRow]:
        return self.incidents.nearest_deploy_marker(project_id=project_id, environment_id=environment_id, at_ms=at_ms)

    def update_failure_deploy(self, *, failure_id: str, deploy_sha: str) -> None:
        return self.incidents.update_failure_deploy(failure_id=failure_id, deploy_sha=deploy_sha)

    def _deploy_marker_from_row(self, row: sqlite3.Row) -> DeployMarkerRow:
        return self.incidents._deploy_marker_from_row(row)

    def upsert_source_map(self, *, project_id: str, environment_id: str, release: str, artifact_url: str, source_map: dict[str, Any], dist: str = "") -> str:
        return self.incidents.upsert_source_map(project_id=project_id, environment_id=environment_id, release=release, artifact_url=artifact_url, source_map=source_map, dist=dist)

    def list_source_maps(self, *, project_id: str, environment_id: str, release: str, dist: Optional[str] = "", limit: int = 100) -> list[SourceMapRow]:
        return self.incidents.list_source_maps(project_id=project_id, environment_id=environment_id, release=release, dist=dist, limit=limit)

    def list_recent_source_maps(self, *, project_id: str, environment_id: str, limit: int = 100) -> list[SourceMapRow]:
        return self.incidents.list_recent_source_maps(project_id=project_id, environment_id=environment_id, limit=limit)

    def _source_map_from_row(self, row: sqlite3.Row) -> SourceMapRow:
        return self.incidents._source_map_from_row(row)

    def _validate_source_map_payload(self, source_map: dict[str, Any]) -> None:
        return self.incidents._validate_source_map_payload(source_map)

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
            status=normalize_failure_status(str(row["status"] or "new")),
            related_deploy_sha=str(row["related_deploy_sha"] or ""),
            related_pr_number=(
                int(row["related_pr_number"])
                if row["related_pr_number"] is not None
                else None
            ),
            linked_tests=_parse_string_list_json(row["linked_tests_json"]),
            linked_repair_task_id=str(row["linked_repair_task_id"] or ""),
            linked_external_thread_id=str(row["linked_external_thread_id"] or ""),
            metadata=dict(_safe_json_obj(row["metadata_json"])),
            created_at=_dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=_dt(row["updated_at"]) or datetime.now(timezone.utc),
        )

    def _evidence_from_row(self, row: sqlite3.Row) -> EvidenceRow:
        return EvidenceRow(
            id=str(row["id"]),
            failure_id=str(row["failure_id"]),
            evidence_type=str(row["evidence_type"]),
            occurred_at_ms=int(row["occurred_at_ms"] or 0),
            source=str(row["source"] or ""),
            redaction_state=str(row["redaction_state"] or "raw"),
            payload=dict(_safe_json_obj(row["payload_json"])),
            artifact_path=str(row["artifact_path"] or ""),
            dedupe_key=str(row["dedupe_key"] or ""),
            created_at=_dt(row["created_at"]) or datetime.now(timezone.utc),
        )























































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
            status=normalize_failure_status(str(row["status"] or "new")),
            related_deploy_sha=str(row["related_deploy_sha"] or ""),
            related_pr_number=(
                int(row["related_pr_number"])
                if row["related_pr_number"] is not None
                else None
            ),
            linked_tests=_parse_string_list_json(row["linked_tests_json"]),
            linked_repair_task_id=str(row["linked_repair_task_id"] or ""),
            linked_external_thread_id=str(row["linked_external_thread_id"] or ""),
            metadata=dict(_safe_json_obj(row["metadata_json"])),
            created_at=_dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=_dt(row["updated_at"]) or datetime.now(timezone.utc),
        )

    def _evidence_from_row(self, row: sqlite3.Row) -> EvidenceRow:
        return EvidenceRow(
            id=str(row["id"]),
            failure_id=str(row["failure_id"]),
            evidence_type=str(row["evidence_type"]),
            occurred_at_ms=int(row["occurred_at_ms"] or 0),
            source=str(row["source"] or ""),
            redaction_state=str(row["redaction_state"] or "raw"),
            payload=dict(_safe_json_obj(row["payload_json"])),
            artifact_path=str(row["artifact_path"] or ""),
            dedupe_key=str(row["dedupe_key"] or ""),
            created_at=_dt(row["created_at"]) or datetime.now(timezone.utc),
        )









    def ensure_workspace(
        self,
        *,
        org_name: str = "Local",
        project_name: str = "Default",
        environment_name: str = "production",
    ) -> WorkspaceIds:
        """Create or return a local cloud-style org/project/environment tuple."""
        org_slug = _slug(org_name)
        project_slug = _slug(project_name)
        env_slug = _slug(environment_name)
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
                project_id = _id("proj")
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
                environment_id = _id("env")
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
        key_id = _id("sdk")
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
        member_id = _id("mem")
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
                        _id("sigdef"),
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
        definition_id = _id("sigdef")
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
            thresholds=_safe_json_obj(row["thresholds_json"]),
            prompt=_safe_json_obj(row["prompt_json"]),
            custom_definition=str(row["custom_definition"] or ""),
            match_count=int(row["match_count"] or 0),
            last_match_at=_dt(row["last_match_at"]),
            created_at=_dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=_dt(row["updated_at"]) or datetime.now(timezone.utc),
        )

    def upsert_failure(self, failure: CanonicalFailure) -> str:
        now = datetime.now(timezone.utc).isoformat()
        failure_id = _id("flr")
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
        linked_tests = _merge_string_lists(
            _parse_string_list_json(existing["linked_tests_json"])
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
        self._replace_failure_trace_map(
            conn,
            failure_id=persisted_failure_id,
            metadata=failure.metadata,
        )
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

    def update_failure_status(
        self,
        *,
        failure_id: str,
        status: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[FailureRow]:
        clean_id = failure_id.strip()
        clean_status = normalize_failure_status(status)
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT metadata_json FROM failures WHERE id = ? OR public_id = ?",
                (clean_id, clean_id),
            ).fetchone()
            if row is None:
                return None
            merged_metadata = dict(_safe_json_obj(row["metadata_json"]))
            if metadata:
                merged_metadata.update(metadata)
            try:
                metadata_json = json.dumps(merged_metadata, sort_keys=True)
            except (TypeError, ValueError) as exc:
                raise ValueError("failure metadata must be JSON-serializable") from exc
            conn.execute(
                """
                UPDATE failures
                SET status = ?,
                    metadata_json = ?,
                    updated_at = ?
                WHERE id = ? OR public_id = ?
                """,
                (clean_status, metadata_json, now, clean_id, clean_id),
            )
            refreshed = conn.execute(
                "SELECT * FROM failures WHERE id = ? OR public_id = ?",
                (clean_id, clean_id),
            ).fetchone()
        return self._failure_from_row(refreshed) if refreshed is not None else None

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
            linked_tests=_parse_string_list_json(row["linked_tests_json"]),
            linked_repair_task_id=str(row["linked_repair_task_id"] or ""),
            linked_external_thread_id=str(row["linked_external_thread_id"] or ""),
            metadata=dict(_safe_json_obj(row["metadata_json"])),
            created_at=_dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=_dt(row["updated_at"]) or datetime.now(timezone.utc),
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
        evidence_id = _id("ev")
        created_at = _now_iso_microseconds()
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












































    def consume_ingest_rate_limit(
        self,
        *,
        project_id: str,
        environment_id: str,
        bucket: str,
        identity: str,
        limit: int,
        window_seconds: int,
        now_ms: Optional[int] = None,
    ) -> RateLimitDecision:
        clean_project = project_id.strip()
        clean_environment = environment_id.strip()
        clean_bucket = bucket.strip().lower()
        clean_identity = identity.strip() or "anonymous"
        clean_limit = max(1, int(limit))
        clean_window_seconds = max(1, int(window_seconds))
        current_ms = (
            int(now_ms)
            if now_ms is not None
            else int(datetime.now(timezone.utc).timestamp() * 1000)
        )
        window_ms = clean_window_seconds * 1000
        window_start_ms = current_ms - (current_ms % window_ms)
        reset_after_seconds = max(
            1, int(((window_start_ms + window_ms) - current_ms + 999) // 1000)
        )
        identity_hash = hashlib.sha256(
            "\n".join(
                [clean_project, clean_environment, clean_bucket, clean_identity]
            ).encode("utf-8")
        ).hexdigest()
        row_id = _id("rlim")
        now = datetime.now(timezone.utc).isoformat()
        cutoff = datetime.fromtimestamp(
            (current_ms / 1000) - INGEST_RATE_LIMIT_RETENTION_SECONDS,
            tz=timezone.utc,
        ).isoformat()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT window_start_ms, count
                FROM ingest_rate_limits
                WHERE project_id = ?
                  AND environment_id = ?
                  AND bucket = ?
                  AND identity_hash = ?
                  AND window_seconds = ?
                """,
                (
                    clean_project,
                    clean_environment,
                    clean_bucket,
                    identity_hash,
                    clean_window_seconds,
                ),
            ).fetchone()
            if row is None or int(row["window_start_ms"] or 0) != window_start_ms:
                count = 1
                conn.execute(
                    """
                    INSERT INTO ingest_rate_limits
                    (id, project_id, environment_id, bucket, identity_hash,
                     window_seconds, window_start_ms, count, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_id, environment_id, bucket, identity_hash, window_seconds)
                    DO UPDATE SET
                        window_start_ms = excluded.window_start_ms,
                        count = excluded.count,
                        updated_at = excluded.updated_at
                    """,
                    (
                        row_id,
                        clean_project,
                        clean_environment,
                        clean_bucket,
                        identity_hash,
                        clean_window_seconds,
                        window_start_ms,
                        count,
                        now,
                    ),
                )
            else:
                previous_count = int(row["count"] or 0)
                count = previous_count + 1
                if count <= clean_limit:
                    cursor = conn.execute(
                        """
                        UPDATE ingest_rate_limits
                        SET count = count + 1, updated_at = ?
                        WHERE project_id = ?
                          AND environment_id = ?
                          AND bucket = ?
                          AND identity_hash = ?
                          AND window_seconds = ?
                          AND window_start_ms = ?
                          AND count < ?
                        """,
                        (
                            now,
                            clean_project,
                            clean_environment,
                            clean_bucket,
                            identity_hash,
                            clean_window_seconds,
                            window_start_ms,
                            clean_limit,
                        ),
                    )
                    if cursor.rowcount <= 0:
                        count = clean_limit + 1
            conn.execute(
                """
                DELETE FROM ingest_rate_limits
                WHERE project_id = ?
                  AND environment_id = ?
                  AND bucket = ?
                  AND updated_at < ?
                """,
                (clean_project, clean_environment, clean_bucket, cutoff),
            )
            conn.execute(
                """
                DELETE FROM ingest_rate_limits
                WHERE id IN (
                    SELECT id
                    FROM ingest_rate_limits
                    WHERE project_id = ?
                      AND environment_id = ?
                      AND bucket = ?
                    ORDER BY updated_at DESC, id DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (
                    clean_project,
                    clean_environment,
                    clean_bucket,
                    INGEST_RATE_LIMIT_MAX_IDENTITIES_PER_BUCKET,
                ),
            )
            allowed = count <= clean_limit
            remaining = max(0, clean_limit - count)
        return RateLimitDecision(
            allowed=allowed,
            limit=clean_limit,
            remaining=remaining,
            reset_after_seconds=reset_after_seconds,
            window_seconds=clean_window_seconds,
        )

    def prune_app_error_retention(
        self,
        *,
        project_id: str,
        environment_id: str,
        failure_retention_days: int = 90,
        evidence_retention_days: int = 90,
        source_map_retention_days: int = 30,
        rate_limit_retention_hours: int = 48,
        dry_run: bool = False,
        now: Optional[datetime] = None,
    ) -> AppErrorRetentionPruneResult:
        clean_project = project_id.strip()
        clean_environment = environment_id.strip()
        clean_failure_days = max(1, int(failure_retention_days))
        clean_evidence_days = max(1, int(evidence_retention_days))
        clean_source_map_days = max(1, int(source_map_retention_days))
        clean_rate_limit_hours = max(1, int(rate_limit_retention_hours))
        current = now or datetime.now(timezone.utc)
        failure_cutoff_ms = int(
            (current.timestamp() - (clean_failure_days * 24 * 60 * 60)) * 1000
        )
        evidence_cutoff = datetime.fromtimestamp(
            current.timestamp() - (clean_evidence_days * 24 * 60 * 60),
            tz=timezone.utc,
        ).isoformat()
        source_map_cutoff = datetime.fromtimestamp(
            current.timestamp() - (clean_source_map_days * 24 * 60 * 60),
            tz=timezone.utc,
        ).isoformat()
        rate_limit_cutoff = datetime.fromtimestamp(
            current.timestamp() - (clean_rate_limit_hours * 60 * 60),
            tz=timezone.utc,
        ).isoformat()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            failure_rows = conn.execute(
                """
                SELECT id
                FROM failures
                WHERE project_id = ?
                  AND environment_id = ?
                  AND source_type = 'monitor_incident'
                  AND status IN ('resolved', 'ignored', 'verified')
                  AND COALESCE(NULLIF(last_seen_ms, 0), first_seen_ms) < ?
                """,
                (clean_project, clean_environment, failure_cutoff_ms),
            ).fetchall()
            failure_ids = [str(row["id"]) for row in failure_rows]
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS tmp_prune_failures (id TEXT PRIMARY KEY)")
            conn.execute("DELETE FROM tmp_prune_failures")
            if failure_ids:
                conn.executemany(
                    "INSERT INTO tmp_prune_failures (id) VALUES (?)",
                    [(failure_id,) for failure_id in failure_ids],
                )
            evidence_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM failure_evidence ev
                    JOIN failures f ON f.id = ev.failure_id
                    JOIN tmp_prune_failures pf ON pf.id = f.id
                    WHERE f.project_id = ?
                      AND f.environment_id = ?
                      AND ev.created_at < ?
                    """,
                    (clean_project, clean_environment, evidence_cutoff),
                ).fetchone()["count"]
            )
            source_map_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM source_maps
                    WHERE project_id = ? AND environment_id = ? AND uploaded_at < ?
                    """,
                    (clean_project, clean_environment, source_map_cutoff),
                ).fetchone()["count"]
            )
            rate_limit_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM ingest_rate_limits
                    WHERE project_id = ? AND environment_id = ? AND updated_at < ?
                    """,
                    (clean_project, clean_environment, rate_limit_cutoff),
                ).fetchone()["count"]
            )
            incident_link_count = 0
            if failure_ids:
                incident_link_count = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) AS count
                        FROM incident_failures
                        WHERE failure_id IN (SELECT id FROM tmp_prune_failures)
                        """,
                    ).fetchone()["count"]
                )
            stale_incidents = conn.execute(
                """
                SELECT i.id
                FROM incidents i
                LEFT JOIN incident_failures inf
                  ON inf.incident_id = i.id
                 AND inf.failure_id NOT IN (SELECT id FROM tmp_prune_failures)
                WHERE i.project_id = ?
                  AND i.environment_id = ?
                  AND inf.incident_id IS NULL
                """,
                (clean_project, clean_environment),
            ).fetchall()
            incident_count = len(stale_incidents)
            if not dry_run:
                conn.execute(
                    """
                    DELETE FROM repair_task_evidence
                    WHERE evidence_id IN (
                        SELECT ev.id
                        FROM failure_evidence ev
                        JOIN failures f ON f.id = ev.failure_id
                        JOIN tmp_prune_failures pf ON pf.id = f.id
                        WHERE f.project_id = ?
                          AND f.environment_id = ?
                          AND ev.created_at < ?
                    )
                    """,
                    (clean_project, clean_environment, evidence_cutoff),
                )
                conn.execute(
                    """
                    DELETE FROM failure_evidence
                    WHERE id IN (
                        SELECT ev.id
                        FROM failure_evidence ev
                        JOIN failures f ON f.id = ev.failure_id
                        JOIN tmp_prune_failures pf ON pf.id = f.id
                        WHERE f.project_id = ?
                          AND f.environment_id = ?
                          AND ev.created_at < ?
                    )
                    """,
                    (clean_project, clean_environment, evidence_cutoff),
                )
                conn.execute(
                    """
                    DELETE FROM source_maps
                    WHERE project_id = ? AND environment_id = ? AND uploaded_at < ?
                    """,
                    (clean_project, clean_environment, source_map_cutoff),
                )
                conn.execute(
                    """
                    DELETE FROM ingest_rate_limits
                    WHERE project_id = ? AND environment_id = ? AND updated_at < ?
                    """,
                    (clean_project, clean_environment, rate_limit_cutoff),
                )
                affected_incident_ids = [
                    str(row["incident_id"])
                    for row in conn.execute(
                        """
                        SELECT DISTINCT incident_id
                        FROM incident_failures
                        WHERE failure_id IN (SELECT id FROM tmp_prune_failures)
                        """
                    ).fetchall()
                ]
                if failure_ids:
                    conn.execute(
                        """
                        DELETE FROM incident_failures
                        WHERE failure_id IN (SELECT id FROM tmp_prune_failures)
                        """
                    )
                    conn.execute(
                        """
                        DELETE FROM failure_trace_map
                        WHERE failure_id IN (SELECT id FROM tmp_prune_failures)
                        """
                    )
                    conn.execute(
                        """
                        DELETE FROM failure_test_links
                        WHERE failure_id IN (SELECT id FROM tmp_prune_failures)
                        """
                    )
                    conn.execute(
                        "DELETE FROM failures WHERE id IN (SELECT id FROM tmp_prune_failures)"
                    )
                stale_incidents = conn.execute(
                    """
                    SELECT i.id
                    FROM incidents i
                    LEFT JOIN incident_failures inf ON inf.incident_id = i.id
                    WHERE i.project_id = ?
                      AND i.environment_id = ?
                      AND inf.incident_id IS NULL
                    """,
                    (clean_project, clean_environment),
                ).fetchall()
                incident_count = len(stale_incidents)
                deleted_incident_ids: set[str] = set()
                if stale_incidents:
                    incident_ids = [str(row["id"]) for row in stale_incidents]
                    deleted_incident_ids = set(incident_ids)
                    conn.execute(
                        "CREATE TEMP TABLE IF NOT EXISTS tmp_prune_incidents (id TEXT PRIMARY KEY)"
                    )
                    conn.execute("DELETE FROM tmp_prune_incidents")
                    conn.executemany(
                        "INSERT INTO tmp_prune_incidents (id) VALUES (?)",
                        [(incident_id,) for incident_id in incident_ids],
                    )
                    conn.execute(
                        "DELETE FROM incidents WHERE id IN (SELECT id FROM tmp_prune_incidents)"
                    )
                for incident_id in affected_incident_ids:
                    if incident_id not in deleted_incident_ids:
                        self._refresh_incident_rollup(conn, incident_id=incident_id)
        return AppErrorRetentionPruneResult(
            dry_run=bool(dry_run),
            failure_retention_days=clean_failure_days,
            evidence_retention_days=clean_evidence_days,
            source_map_retention_days=clean_source_map_days,
            rate_limit_retention_hours=clean_rate_limit_hours,
            failures=len(failure_ids),
            evidence=evidence_count,
            incident_links=incident_link_count,
            incidents=incident_count,
            source_maps=source_map_count,
            rate_limit_rows=rate_limit_count,
        )

    # ---------------------------------------------------------------
    # P2.3 -- global retention helpers.
    #
    # `prune_app_error_retention` above is scoped to a single
    # (project, environment) and the app-error domain. These two
    # helpers prune the high-volume "global" tables that grow
    # regardless of which project they came from. They take the same
    # `dry_run` / `now` parameters so callers can preview the cut.
    # ---------------------------------------------------------------

    def prune_replay_batches(
        self,
        *,
        retention_days: int,
        dry_run: bool = False,
    ) -> int:
        """Delete `replay_batches` rows older than `retention_days`.

        Replay batches store the rrweb payload blobs -- by far the
        largest table by bytes in a healthy install. Pruning by
        `received_at` is safe because nothing else references batch
        rows by FK; orphaned `replay_sessions` rows are tiny and
        retained for the lifecycle-event history.

        The cutoff is computed DB-side via `datetime('now', ?)` so
        it matches the column DEFAULT format under both SQLite and
        Postgres (the P1.5 dialect layer translates the expression).
        """
        interval = _retention_interval(retention_days)
        with self._conn() as conn:
            count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM replay_batches
                    WHERE received_at < datetime('now', ?)
                    """,
                    (interval,),
                ).fetchone()["count"]
            )
            if not dry_run and count:
                conn.execute(
                    "DELETE FROM replay_batches WHERE received_at < datetime('now', ?)",
                    (interval,),
                )
            return count

    def prune_otel_events(
        self,
        *,
        retention_days: int,
        dry_run: bool = False,
    ) -> int:
        """Delete `otel_events` rows older than `retention_days`.

        Pruned by `created_at` rather than `occurred_at_ms` because
        a misconfigured collector that backfills with stale
        timestamps could otherwise wipe fresh ingest. Server clock
        is the source of truth here.
        """
        interval = _retention_interval(retention_days)
        with self._conn() as conn:
            count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM otel_events
                    WHERE created_at < datetime('now', ?)
                    """,
                    (interval,),
                ).fetchone()["count"]
            )
            if not dry_run and count:
                conn.execute(
                    "DELETE FROM otel_events WHERE created_at < datetime('now', ?)",
                    (interval,),
                )
            return count

    def project_environment_pairs(self) -> list[tuple[str, str]]:
        """Return every (project_id, environment_id) pair that the
        retention sweep needs to visit. Unions across `failures`,
        `incidents`, `source_maps`, and `ingest_rate_limits` so a
        scope with source-map uploads or rate-limit rows but no
        failures yet doesn't get skipped."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT project_id, environment_id FROM failures
                UNION
                SELECT project_id, environment_id FROM incidents
                UNION
                SELECT project_id, environment_id FROM source_maps
                UNION
                SELECT project_id, environment_id FROM ingest_rate_limits
                """
            ).fetchall()
        pairs = sorted(
            (str(row["project_id"]), str(row["environment_id"])) for row in rows
        )
        return list(pairs)


    def append_otel_event(
        self,
        *,
        project_id: str,
        environment_id: str,
        signal_type: str,
        trace_id: str = "",
        span_id: str = "",
        name: str = "",
        severity: str = "",
        body: str = "",
        occurred_at_ms: int = 0,
        attributes: Optional[dict[str, Any]] = None,
    ) -> str:
        try:
            attributes_json = json.dumps(attributes or {}, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("otel attributes must be JSON-serializable") from exc
        event_id = _public_id(
            "otel",
            project_id,
            environment_id,
            signal_type,
            trace_id,
            span_id,
            name,
            severity,
            body,
            int(occurred_at_ms),
            attributes_json,
        )
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO otel_events
                (id, project_id, environment_id, signal_type, trace_id, span_id, name,
                 severity, body, occurred_at_ms, attributes_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    project_id,
                    environment_id,
                    signal_type,
                    trace_id,
                    span_id,
                    name,
                    severity,
                    body[:2000],
                    int(occurred_at_ms),
                    attributes_json,
                ),
            )
        return event_id

    def list_failures_by_trace(
        self,
        *,
        project_id: str,
        environment_id: str,
        trace_id: str,
        span_id: str = "",
        limit: int = 1000,
    ) -> list[FailureRow]:
        if not trace_id.strip() and not span_id.strip():
            return []
        params: list[object] = [project_id, environment_id]
        where = "f.project_id = ? AND f.environment_id = ?"
        if trace_id.strip():
            where += " AND ftm.trace_id = ?"
            params.append(trace_id.strip())
        if span_id.strip():
            where += " AND (ftm.span_id = '' OR ftm.span_id = ?)"
            params.append(span_id.strip())
        params.append(max(1, min(int(limit), 5000)))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT f.*
                FROM failure_trace_map ftm
                JOIN failures f ON f.id = ftm.failure_id
                WHERE {where}
                ORDER BY f.updated_at DESC, f.public_id
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._failure_from_row(row) for row in rows]

    def list_otel_events(
        self,
        *,
        project_id: str,
        environment_id: str,
        trace_id: str = "",
        signal_type: str = "",
        limit: int = 100,
    ) -> list[OtelEventRow]:
        where = "project_id = ? AND environment_id = ?"
        params: list[object] = [project_id, environment_id]
        if trace_id:
            where += " AND trace_id = ?"
            params.append(trace_id)
        if signal_type:
            where += " AND signal_type = ?"
            params.append(signal_type)
        params.append(max(1, min(int(limit), 500)))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM otel_events
                WHERE {where}
                ORDER BY occurred_at_ms, created_at, id
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._otel_event_from_row(row) for row in rows]

    def _otel_event_from_row(self, row: sqlite3.Row) -> OtelEventRow:
        return OtelEventRow(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            environment_id=str(row["environment_id"]),
            signal_type=str(row["signal_type"]),
            trace_id=str(row["trace_id"] or ""),
            span_id=str(row["span_id"] or ""),
            name=str(row["name"] or ""),
            severity=str(row["severity"] or ""),
            body=str(row["body"] or ""),
            occurred_at_ms=int(row["occurred_at_ms"] or 0),
            attributes=dict(_safe_json_obj(row["attributes_json"])),
            created_at=_dt(row["created_at"]) or datetime.now(timezone.utc),
        )

    def upsert_failure_with_evidence_and_repair_task(
        self,
        *,
        failure: CanonicalFailure,
        evidence_items: list[EvidenceItem],
        repair_task: dict[str, Any],
    ) -> tuple[str, list[str], str]:
        now = datetime.now(timezone.utc).isoformat()
        failure_id = _id("flr")
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
        task_id = _id("rpr")
        public_id = _public_id("rpr", failure_id)
        normalized_status = normalize_repair_task_status(status)
        clean_likely_files = _merge_string_lists(likely_files or [])
        clean_validation = _merge_string_lists(validation_commands or [])
        clean_evidence_ids = _merge_string_lists(evidence_ids or [])
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
        task_id = task_id or _id("rpr")
        public_id = public_id or _public_id("rpr", failure_id)
        normalized_status = normalize_repair_task_status(status)
        clean_likely_files = _merge_string_lists(likely_files or [])
        clean_validation = _merge_string_lists(validation_commands or [])
        clean_evidence_ids = _merge_string_lists(evidence_ids or [])
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

    def update_repair_task_status(
        self,
        *,
        repair_task_id: str,
        status: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[RepairTaskRow]:
        clean_id = repair_task_id.strip()
        clean_status = normalize_repair_task_status(status)
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT metadata_json
                FROM repair_tasks
                WHERE id = ? OR public_id = ?
                """,
                (clean_id, clean_id),
            ).fetchone()
            if row is None:
                return None
            merged_metadata = dict(_safe_json_obj(row["metadata_json"]))
            if metadata:
                merged_metadata.update(metadata)
            try:
                metadata_json = json.dumps(merged_metadata, sort_keys=True)
            except (TypeError, ValueError) as exc:
                raise ValueError("repair task metadata must be JSON-serializable") from exc
            conn.execute(
                """
                UPDATE repair_tasks
                SET status = ?,
                    metadata_json = ?,
                    updated_at = ?
                WHERE id = ? OR public_id = ?
                """,
                (clean_status, metadata_json, now, clean_id, clean_id),
            )
            refreshed = conn.execute(
                "SELECT * FROM repair_tasks WHERE id = ? OR public_id = ?",
                (clean_id, clean_id),
            ).fetchone()
            evidence_rows = conn.execute(
                """
                SELECT evidence_id
                FROM repair_task_evidence
                WHERE repair_task_id = ?
                ORDER BY created_at, evidence_id
                """,
                (str(refreshed["id"]) if refreshed is not None else clean_id,),
            ).fetchall()
        return (
            self._repair_task_from_row(
                refreshed,
                evidence_ids=[str(item["evidence_id"]) for item in evidence_rows],
            )
            if refreshed is not None
            else None
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

    def create_github_review_run(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        installation_id: str = "",
        sender_login: str = "",
        comment_id: str = "",
        comment_url: str = "",
        status: str = "queued",
        trigger_phrase: str = "@retrace review",
        metadata: Optional[dict[str, Any]] = None,
    ) -> GitHubReviewRunRow:
        repo = repo_full_name.strip()
        if not repo:
            raise ValueError("repo_full_name is required")
        if pr_number <= 0:
            raise ValueError("pr_number must be positive")
        clean_status = _normalize_github_review_run_status(status or "queued")
        run_id = _id("ghrr")
        now = datetime.now(timezone.utc).isoformat()
        try:
            metadata_json = json.dumps(metadata or {}, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("review run metadata must be JSON-serializable") from exc
        clean_comment_id = comment_id.strip()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO github_review_runs
                (id, repo_full_name, pr_number, installation_id, sender_login,
                 comment_id, comment_url, status, trigger_phrase, metadata_json,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    repo,
                    int(pr_number),
                    installation_id.strip(),
                    sender_login.strip(),
                    clean_comment_id,
                    comment_url.strip(),
                    clean_status,
                    trigger_phrase.strip() or "@retrace review",
                    metadata_json,
                    now,
                    now,
                ),
            )
            if clean_comment_id:
                row = conn.execute(
                    """
                    SELECT *
                    FROM github_review_runs
                    WHERE repo_full_name = ? AND pr_number = ? AND comment_id = ?
                    """,
                    (repo, int(pr_number), clean_comment_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM github_review_runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
        assert row is not None
        return self._github_review_run_from_row(row)

    def get_github_review_run(self, run_id: str) -> Optional[GitHubReviewRunRow]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM github_review_runs WHERE id = ?",
                (run_id.strip(),),
            ).fetchone()
        return self._github_review_run_from_row(row) if row is not None else None

    def list_github_review_runs(
        self,
        *,
        repo_full_name: Optional[str] = None,
        pr_number: Optional[int] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[GitHubReviewRunRow]:
        where: list[str] = []
        params: list[object] = []
        if repo_full_name is not None:
            where.append("repo_full_name = ?")
            params.append(repo_full_name.strip())
        if pr_number is not None:
            where.append("pr_number = ?")
            params.append(int(pr_number))
        if status is not None:
            where.append("status = ?")
            params.append(_normalize_github_review_run_status(status))
        clause = " AND ".join(where) if where else "1 = 1"
        params.append(max(1, min(int(limit), 500)))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM github_review_runs
                WHERE {clause}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._github_review_run_from_row(row) for row in rows]

    def update_github_review_run_status(
        self,
        run_id: str,
        *,
        status: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[GitHubReviewRunRow]:
        clean_status = _normalize_github_review_run_status(status)
        now = datetime.now(timezone.utc).isoformat()
        metadata_json = None
        if metadata is not None:
            try:
                metadata_json = json.dumps(metadata, sort_keys=True)
            except (TypeError, ValueError) as exc:
                raise ValueError("review run metadata must be JSON-serializable") from exc
        with self._conn() as conn:
            if metadata_json is None:
                conn.execute(
                    """
                    UPDATE github_review_runs
                    SET status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (clean_status, now, run_id.strip()),
                )
            else:
                conn.execute(
                    """
                    UPDATE github_review_runs
                    SET status = ?, metadata_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (clean_status, metadata_json, now, run_id.strip()),
                )
            row = conn.execute(
                "SELECT * FROM github_review_runs WHERE id = ?",
                (run_id.strip(),),
            ).fetchone()
        return self._github_review_run_from_row(row) if row is not None else None

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
            link_id = _id("ftl")
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

    def list_all_failure_test_links(
        self,
        *,
        failure_id: Optional[str] = None,
        issue_public_id: Optional[str] = None,
        spec_id: Optional[str] = None,
    ) -> list[FailureTestLinkRow]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM failure_test_links
                WHERE (? IS NULL OR failure_id = ?)
                  AND (? IS NULL OR issue_public_id = ?)
                  AND (? IS NULL OR spec_id = ?)
                ORDER BY updated_at DESC, created_at DESC, id
                """,
                (
                    failure_id,
                    failure_id,
                    issue_public_id,
                    issue_public_id,
                    spec_id,
                    spec_id,
                ),
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
                        _id("ftl"),
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
        linked = _merge_string_lists(
            _parse_string_list_json(row["linked_tests_json"]),
            [spec_id],
        )
        if linked != _parse_string_list_json(row["linked_tests_json"]):
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
            latest_run_at=_dt(row["latest_run_at"]),
            created_at=_dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=_dt(row["updated_at"]) or datetime.now(timezone.utc),
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
            likely_files=_parse_string_list_json(row["likely_files_json"]),
            prompt_artifacts=_parse_dict_list_json(row["prompt_artifacts_json"]),
            validation_commands=_parse_string_list_json(
                row["validation_commands_json"]
            ),
            branch=str(row["branch"] or ""),
            pr_url=str(row["pr_url"] or ""),
            risk_notes=str(row["risk_notes"] or ""),
            metadata=dict(_safe_json_obj(row["metadata_json"])),
            evidence_ids=evidence_ids,
            created_at=_dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=_dt(row["updated_at"]) or datetime.now(timezone.utc),
        )

    def _github_review_run_from_row(
        self, row: sqlite3.Row
    ) -> GitHubReviewRunRow:
        return GitHubReviewRunRow(
            id=str(row["id"]),
            repo_full_name=str(row["repo_full_name"]),
            pr_number=int(row["pr_number"]),
            installation_id=str(row["installation_id"] or ""),
            sender_login=str(row["sender_login"] or ""),
            comment_id=str(row["comment_id"] or ""),
            comment_url=str(row["comment_url"] or ""),
            status=str(row["status"] or "queued"),
            trigger_phrase=str(row["trigger_phrase"] or "@retrace review"),
            metadata=dict(_safe_json_obj(row["metadata_json"])),
            created_at=_dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=_dt(row["updated_at"]) or datetime.now(timezone.utc),
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
            payload=dict(_safe_json_obj(row["payload_json"])),
            artifact_path=str(row["artifact_path"] or ""),
            dedupe_key=str(row["dedupe_key"] or ""),
            created_at=_dt(row["created_at"]) or datetime.now(timezone.utc),
        )


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
            revoked_at=_dt(r["revoked_at"]),
            last_used_at=_dt(r["last_used_at"]),
            created_at=_dt(r["created_at"]) or datetime.now(timezone.utc),
        )

    def list_sdk_keys(
        self,
        *,
        project_id: str = "",
        environment_id: str = "",
        include_revoked: bool = False,
        limit: int = 100,
    ) -> list[SDKKeyRow]:
        clauses: list[str] = []
        params: list[object] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if environment_id:
            clauses.append("environment_id = ?")
            params.append(environment_id)
        if not include_revoked:
            clauses.append("revoked_at IS NULL")
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT id, project_id, environment_id, name, prefix, key_hash, last4,
                       revoked_at, last_used_at, created_at
                FROM sdk_keys
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [
            SDKKeyRow(
                id=str(r["id"]),
                project_id=str(r["project_id"]),
                environment_id=str(r["environment_id"]),
                name=str(r["name"]),
                prefix=str(r["prefix"]),
                key_hash=str(r["key_hash"]),
                last4=str(r["last4"]),
                revoked_at=_dt(r["revoked_at"]),
                last_used_at=_dt(r["last_used_at"]),
                created_at=_dt(r["created_at"]) or datetime.now(timezone.utc),
            )
            for r in rows
        ]

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
        token_id = _id("svc")
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
            scopes=_parse_string_list_json(r["scopes_json"]),
            revoked_at=_dt(r["revoked_at"]),
            last_used_at=_dt(r["last_used_at"]),
            created_at=_dt(r["created_at"]) or datetime.now(timezone.utc),
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
        preview = _replay_preview(events)
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
            session_row_id = _id("rs")
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
            existing_metadata = _safe_json_obj(row["metadata_json"])
            merged_metadata = {**existing_metadata, **(metadata or {})}
            existing_preview = _safe_json_obj(row["preview_json"])

            batch_id = _id("rb")
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
                merged_preview = _merge_replay_preview(
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
        job_id = _id("job")
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

    # ---------------------------------------------------------------
    # P3.6 -- server-side replay (scaffold).
    #
    # Storage seam for the eventual Node / Python capture
    # middleware. The middleware itself is intentionally NOT in
    # scope here -- building Node + Python capture without a real
    # SSR-replay user is the speculative-bloat trap. These methods
    # exist so the ingest endpoint has a place to write to and
    # tests can pin the storage round-trip.
    # ---------------------------------------------------------------

    def insert_server_replay_session(
        self,
        *,
        project_id: str,
        environment_id: str,
        session_id: str,
        request_method: str,
        request_path: str,
        request_headers: Optional[dict[str, Any]] = None,
        request_body_text: str = "",
        response_status: int = 0,
        response_headers: Optional[dict[str, Any]] = None,
        rendered_html_snippet: str = "",
        runtime: str = "",
        occurred_at_ms: int = 0,
        error_summary: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        """Persist a single server-replay session record.

        Returns the inserted row id. Sensitive request/response
        headers should be redacted BEFORE this method is called --
        no automatic redaction here, since the caller (capture
        middleware) is the only side that knows what to keep.
        """
        row_id = _id("ssr")
        public_id = _public_id(
            "ssr",
            project_id,
            environment_id,
            session_id,
            request_method,
            request_path,
            int(occurred_at_ms),
        )
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO server_replay_sessions
                    (id, public_id, project_id, environment_id, session_id,
                     request_method, request_path, request_headers_json,
                     request_body_text, response_status, response_headers_json,
                     rendered_html_snippet, runtime, occurred_at_ms,
                     error_summary, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_id,
                    public_id,
                    project_id,
                    environment_id,
                    session_id or "",
                    str(request_method or "").upper(),
                    request_path or "",
                    json.dumps(dict(request_headers or {}), sort_keys=True),
                    request_body_text or "",
                    int(response_status or 0),
                    json.dumps(dict(response_headers or {}), sort_keys=True),
                    rendered_html_snippet or "",
                    runtime or "",
                    int(occurred_at_ms or 0),
                    error_summary or "",
                    json.dumps(dict(metadata or {}), sort_keys=True),
                ),
            )
        return row_id

    def get_server_replay_session(self, row_id: str) -> Optional[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM server_replay_sessions WHERE id = ?",
                (row_id,),
            ).fetchone()

    def list_server_replay_sessions(
        self,
        *,
        project_id: str,
        environment_id: str,
        limit: int = 25,
        path_prefix: str = "",
    ) -> list[sqlite3.Row]:
        clean_limit = max(1, min(500, int(limit)))
        with self._conn() as conn:
            if path_prefix:
                return conn.execute(
                    """
                    SELECT * FROM server_replay_sessions
                    WHERE project_id = ? AND environment_id = ?
                      AND request_path LIKE ?
                    ORDER BY occurred_at_ms DESC, id DESC
                    LIMIT ?
                    """,
                    (project_id, environment_id, f"{path_prefix}%", clean_limit),
                ).fetchall()
            return conn.execute(
                """
                SELECT * FROM server_replay_sessions
                WHERE project_id = ? AND environment_id = ?
                ORDER BY occurred_at_ms DESC, id DESC
                LIMIT ?
                """,
                (project_id, environment_id, clean_limit),
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

        payload = _safe_json_obj(batch["payload_json"])
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
                        _id("sig"),
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
        issue_id = _id("ri")
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
                        WHEN replay_issues.status IN ('resolved', 'verified') THEN 'regressed'
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
                        WHEN replay_issues.status IN ('resolved', 'verified') THEN NULL
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
                failure_id=_id("flr"),
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
                evidence=dict(_safe_json_obj(issue_row["evidence_json"])),
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
                  AND status != 'verified'
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
            "verified",
            "ongoing",
            "regressed",
            "ignored",
        }
        if status not in allowed:
            raise ValueError(f"invalid replay issue status: {status}")
        now = datetime.now(timezone.utc).isoformat()
        resolved_at = now if status in {"resolved", "verified"} else None
        external_state = "created" if status == "ticket_created" else ""
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE replay_issues
                SET status = ?,
                    external_ticket_state = COALESCE(NULLIF(?, ''), external_ticket_state),
                    external_ticket_id = COALESCE(NULLIF(?, ''), external_ticket_id),
                    external_ticket_url = COALESCE(NULLIF(?, ''), external_ticket_url),
                    resolved_at = CASE
                        WHEN ? IN ('resolved', 'verified') THEN ?
                        ELSE NULL
                    END,
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
                error_issue_ids=_parse_string_list_json(r["error_issue_ids_json"]),
                trace_ids=_parse_string_list_json(r["trace_ids_json"]),
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

    # ------------------------------------------------------------------
    # QA Incidents -- the unified shape for replay/UI/API issues.
    #
    # Separate from the master `incidents` table (which tracks monitoring
    # failure groupings + repair tasks). QA incidents own the
    # auto-repro + auto-fix pipeline state.
    # ------------------------------------------------------------------

    def upsert_qa_incident(self, row: dict[str, Any]) -> tuple[str, bool]:
        """Insert or update a QA incident keyed by (project, env, fingerprint).

        Atomic upsert via SQLite's ``ON CONFLICT ... DO UPDATE`` so concurrent
        ingesters can't both miss the existence check and race on the unique
        constraint. Returns ``(incident_id, inserted)``.

        Note: callers that need the **persisted** ``public_id`` should use
        :meth:`upsert_qa_incident_returning` -- the public_id is NOT in the
        DO UPDATE clause, so on conflict the stored value differs from the
        candidate one in ``row``. Returning a bare ``id`` here preserves
        the prior signature for existing callers.
        """
        required = (
            "id", "public_id", "project_id", "environment_id", "fingerprint",
            "title", "summary", "suspected_cause", "severity", "confidence",
            "status", "primary_source_kind", "sources_json", "reproduction_json",
            "expected_outcome", "actual_outcome", "app_url", "evidence_json",
            "affected_count", "affected_users", "first_seen_ms", "last_seen_ms",
            "repro_status", "repro_spec_id", "repro_run_id", "repro_summary",
            "fix_status", "fix_repo", "fix_branch", "fix_pr_url", "fix_prompt_path",
            "created_at", "updated_at",
        )
        missing = [k for k in required if k not in row]
        if missing:
            raise ValueError(f"upsert_qa_incident missing keys: {missing}")

        updatable = (
            "title", "summary", "suspected_cause", "severity", "confidence",
            "status", "primary_source_kind", "sources_json", "reproduction_json",
            "expected_outcome", "actual_outcome", "app_url", "evidence_json",
            "affected_count", "affected_users", "first_seen_ms", "last_seen_ms",
            "repro_status", "repro_spec_id", "repro_run_id", "repro_summary",
            "fix_status", "fix_repo", "fix_branch", "fix_pr_url", "fix_prompt_path",
            "updated_at",
        )

        # These identifiers are derived from a fixed tuple (no untrusted
        # input), so embedding them in the SQL text is safe; values stay
        # parameterised.
        cols = ", ".join(required)
        placeholders = ", ".join(["?"] * len(required))
        do_update = ", ".join(f"{k} = excluded.{k}" for k in updatable)

        sql = (
            f"INSERT INTO qa_incidents ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(project_id, environment_id, fingerprint) DO UPDATE SET {do_update} "
            "RETURNING id, public_id"
        )

        with self._conn() as conn:
            # Pre-check is advisory only -- it informs the return value but the
            # upsert itself remains atomic, so concurrent writers can't lose
            # data. The narrow remaining race (two writers both see "not
            # exists" and both report inserted=True) is benign: the stored
            # row is correct either way.
            existed = conn.execute(
                """
                SELECT 1 FROM qa_incidents
                WHERE project_id = ? AND environment_id = ? AND fingerprint = ?
                """,
                (row["project_id"], row["environment_id"], row["fingerprint"]),
            ).fetchone() is not None

            cur = conn.execute(sql, tuple(row[k] for k in required))
            res = cur.fetchone()
            if res is None:
                # Defensive -- SQLite returns a row from RETURNING for both
                # branches, but if a future driver swap broke that we'd
                # rather raise than lie about persistence.
                raise RuntimeError("upsert_qa_incident: no row returned from upsert")
            return str(res["id"]), not existed

    def upsert_qa_incident_returning(
        self, row: dict[str, Any]
    ) -> tuple[str, str, bool]:
        """Same as :meth:`upsert_qa_incident` but returns the **persisted**
        public_id too.

        Use this from the bridge: on a fingerprint collision the existing
        row keeps its original public_id (we never overwrite it), so the
        candidate value baked into ``row["public_id"]`` is dropped. The
        bridge surfaces these ids straight to the user (`retrace qa show
        INC-...`), so returning the canonical one prevents dead references
        on resync. Returns ``(incident_id, persisted_public_id, inserted)``.
        """
        incident_id, inserted = self.upsert_qa_incident(row)
        with self._conn() as conn:
            stored = conn.execute(
                "SELECT public_id FROM qa_incidents WHERE id = ?",
                (incident_id,),
            ).fetchone()
        if stored is None:  # pragma: no cover - upsert just wrote it
            raise RuntimeError("upsert_qa_incident_returning: row vanished")
        return incident_id, str(stored["public_id"]), inserted

    def update_qa_incident_state(
        self,
        incident_id: str,
        *,
        status: Optional[str] = None,
        repro_status: Optional[str] = None,
        repro_spec_id: Optional[str] = None,
        repro_run_id: Optional[str] = None,
        repro_summary: Optional[str] = None,
        fix_status: Optional[str] = None,
        fix_repo: Optional[str] = None,
        fix_branch: Optional[str] = None,
        fix_pr_url: Optional[str] = None,
        fix_prompt_path: Optional[str] = None,
    ) -> bool:
        """Partial update for the operational state of a QA incident."""
        updates: list[tuple[str, Any]] = []
        for key, val in (
            ("status", status),
            ("repro_status", repro_status),
            ("repro_spec_id", repro_spec_id),
            ("repro_run_id", repro_run_id),
            ("repro_summary", repro_summary),
            ("fix_status", fix_status),
            ("fix_repo", fix_repo),
            ("fix_branch", fix_branch),
            ("fix_pr_url", fix_pr_url),
            ("fix_prompt_path", fix_prompt_path),
        ):
            if val is not None:
                updates.append((key, val))
        if not updates:
            return False
        updates.append(("updated_at", datetime.now(timezone.utc).isoformat()))
        sets = ", ".join(f"{k} = ?" for k, _ in updates)
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE qa_incidents SET {sets} WHERE id = ? OR public_id = ?",
                tuple(v for _, v in updates) + (incident_id, incident_id),
            )
            return int(cur.rowcount) > 0

    def get_qa_incident(self, incident_id_or_public: str) -> Optional[sqlite3.Row]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM qa_incidents WHERE id = ? OR public_id = ?",
                (incident_id_or_public, incident_id_or_public),
            ).fetchone()
        return row

    def list_qa_incidents(
        self,
        *,
        project_id: Optional[str] = None,
        environment_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        where: list[str] = []
        params: list[object] = []
        if project_id:
            where.append("project_id = ?")
            params.append(project_id)
        if environment_id:
            where.append("environment_id = ?")
            params.append(environment_id)
        if status:
            where.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM qa_incidents"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, last_seen_ms DESC LIMIT ? OFFSET ?"
        params.append(max(1, min(int(limit), 500)))
        params.append(max(0, int(offset)))
        with self._conn() as conn:
            return conn.execute(sql, params).fetchall()

    # ---- LLM PR reviews -------------------------------------------------

    def add_llm_pr_review(
        self,
        *,
        repo: str,
        pr_number: int,
        model: str,
        summary: str,
        risk_notes: list[str],
        suggestions: list[dict],
        paths: list[str],
        input_tokens: int = 0,
        output_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
    ) -> int:
        """Persist one LLM review run so future reviews can reference it.

        Returns the row id. The `(repo, pr_number)` pair is NOT unique --
        if you call `retrace review --post-comment` twice on the same
        PR, you get two rows, and `list_llm_pr_reviews_for_paths`
        returns the most recent first.

        P3.5: `input_tokens` / `output_tokens` / `estimated_cost_usd`
        are estimated by `llm_pr_review` from the prompt/response text
        length (chars/4 heuristic) and the static price table in
        `retrace.llm_pricing`. Defaults of 0 keep this argument
        optional for older call sites and historical rows.
        """
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO llm_pr_reviews
                    (repo, pr_number, model, summary, risk_notes_json,
                     suggestions_json, paths_json,
                     input_tokens, output_tokens, estimated_cost_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo or "",
                    int(pr_number or 0),
                    model or "",
                    summary or "",
                    json.dumps(list(risk_notes or [])),
                    json.dumps(list(suggestions or [])),
                    json.dumps(list(paths or [])),
                    max(0, int(input_tokens or 0)),
                    max(0, int(output_tokens or 0)),
                    max(0.0, float(estimated_cost_usd or 0.0)),
                ),
            )
            return int(cur.lastrowid or 0)

    def list_llm_pr_reviews_for_paths(
        self,
        paths: list[str],
        *,
        repo: str = "",
        exclude_pr_number: int = 0,
        limit: int = 5,
    ) -> list[sqlite3.Row]:
        """Find recent LLM reviews touching any path in `paths`.

        SQLite doesn't have a portable JSON-array-contains operator, so
        we scan the most-recent N rows (cheap -- capped by `limit * 6`)
        and filter in Python. Good enough for the OSS scale we target.

        Pass `exclude_pr_number` to filter out the PR we're currently
        reviewing (otherwise the just-persisted review echoes back into
        its own next-run prompt).
        """
        if not paths:
            return []
        wanted = {p for p in paths if p}
        if not wanted:
            return []
        # Pull a window large enough to find `limit` matches in
        # reasonable codebases; cap to keep this O(1).
        scan_window = max(limit * 6, 30)
        sql = "SELECT * FROM llm_pr_reviews"
        clauses: list[str] = []
        params: list[object] = []
        if repo:
            clauses.append("repo = ?")
            params.append(repo)
        if exclude_pr_number:
            clauses.append("pr_number != ?")
            params.append(int(exclude_pr_number))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(scan_window))
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        out: list[sqlite3.Row] = []
        for r in rows:
            try:
                row_paths = set(json.loads(r["paths_json"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if row_paths & wanted:
                out.append(r)
            if len(out) >= limit:
                break
        return out

    # ---------------------------------------------------------------
    # P3.1 -- flake quarantine.
    #
    # Two surfaces:
    #   * `record_tester_run_outcome(spec_id, outcome)` -- call after
    #     every tester run. Appends to the rolling outcome window
    #     and re-evaluates whether the spec should auto-quarantine
    #     or auto-release. Returns the resulting status.
    #   * `is_spec_quarantined(spec_id)` -- fast read used by the
    #     incident-filing gate.
    # ---------------------------------------------------------------

    _RUN_OUTCOME_WINDOW = 20
    _QUARANTINE_FAILURE_WINDOW_HOURS = 24
    _RELEASE_PASS_STREAK = 5

    def record_tester_run_outcome(
        self,
        *,
        spec_id: str,
        run_id: str,
        outcome: str,
    ) -> str:
        """Append a tester-run outcome and re-evaluate quarantine.

        `outcome` is `"pass"` or `"fail"`. Anything else is treated
        as `"fail"` so an unrecognised value never silently drops
        out of the heuristic (e.g. a future `"flaky"` classification
        that should count as failure for quarantine purposes).

        Auto-quarantine: a `pass → fail → pass` pattern within 24h
        on a currently-active spec flips it to `quarantined`.
        Auto-release: 5 consecutive passes on a currently-
        quarantined spec flips it back to `active`.

        Returns the resulting status (`"active"` or `"quarantined"`).
        """
        clean_spec = spec_id.strip()
        if not clean_spec:
            return "active"
        normalized = "pass" if str(outcome or "").strip().lower() == "pass" else "fail"
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO tester_spec_run_outcomes (spec_id, run_id, outcome)
                VALUES (?, ?, ?)
                """,
                (clean_spec, run_id or "", normalized),
            )
            # Prune the rolling window -- keep only the most-recent
            # _RUN_OUTCOME_WINDOW rows per spec so the table doesn't
            # grow without bound on long-lived installs.
            conn.execute(
                """
                DELETE FROM tester_spec_run_outcomes
                WHERE spec_id = ?
                  AND id NOT IN (
                    SELECT id FROM tester_spec_run_outcomes
                    WHERE spec_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                  )
                """,
                (clean_spec, clean_spec, self._RUN_OUTCOME_WINDOW),
            )
            # Snapshot current state + recent outcomes for the
            # heuristic decisions below.
            current = conn.execute(
                "SELECT status FROM tester_spec_quarantine WHERE spec_id = ?",
                (clean_spec,),
            ).fetchone()
            current_status = (
                str(current["status"]) if current is not None else "active"
            )
            recent = conn.execute(
                """
                SELECT outcome, recorded_at FROM tester_spec_run_outcomes
                WHERE spec_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (clean_spec, self._RELEASE_PASS_STREAK),
            ).fetchall()
            new_status = current_status
            reason = ""
            # Auto-release: 5 consecutive passes when quarantined.
            if current_status == "quarantined" and len(recent) >= self._RELEASE_PASS_STREAK:
                if all(str(r["outcome"]) == "pass" for r in recent):
                    new_status = "active"
                    reason = (
                        f"auto-released after {self._RELEASE_PASS_STREAK} "
                        "consecutive passes"
                    )
            # Auto-quarantine: pass → fail → pass within 24h on an
            # active spec. We look at the most-recent 3 outcomes.
            if current_status == "active":
                three = conn.execute(
                    """
                    SELECT outcome, recorded_at FROM tester_spec_run_outcomes
                    WHERE spec_id = ?
                    ORDER BY id DESC
                    LIMIT 3
                    """,
                    (clean_spec,),
                ).fetchall()
                if len(three) == 3:
                    outs = [str(r["outcome"]) for r in three]
                    # most-recent first → [pass, fail, pass] reading right-to-left
                    # is the pass→fail→pass pattern. Equivalently:
                    if outs[0] == "pass" and outs[1] == "fail" and outs[2] == "pass":
                        # Time-bound to a single 24h window so an old
                        # pass / new pass with a months-old fail in
                        # between doesn't trip the heuristic.
                        first = _dt(three[2]["recorded_at"])
                        last = _dt(three[0]["recorded_at"])
                        # `_dt` returns either naive or TZ-aware
                        # depending on whether the stored timestamp
                        # had a `+00:00` suffix. SQLite's
                        # `datetime('now')` default is naive. Coerce
                        # to UTC-aware so the subtraction works
                        # regardless of which storage path wrote the
                        # row.
                        if first and first.tzinfo is None:
                            first = first.replace(tzinfo=timezone.utc)
                        if last and last.tzinfo is None:
                            last = last.replace(tzinfo=timezone.utc)
                        if first and last and (
                            last - first
                        ).total_seconds() <= self._QUARANTINE_FAILURE_WINDOW_HOURS * 3600:
                            new_status = "quarantined"
                            reason = "auto-quarantined: pass→fail→pass within 24h"
            if new_status != current_status:
                self._write_quarantine_state(
                    conn,
                    spec_id=clean_spec,
                    status=new_status,
                    reason=reason,
                )
            elif current is None:
                # First-ever outcome for this spec -- seed an
                # `active` row so `list_quarantined_specs` and
                # `release_spec_quarantine` have a stable target.
                self._write_quarantine_state(
                    conn, spec_id=clean_spec, status="active", reason=""
                )
        return new_status

    def is_spec_quarantined(self, spec_id: str) -> bool:
        clean = spec_id.strip()
        if not clean:
            return False
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status FROM tester_spec_quarantine WHERE spec_id = ?",
                (clean,),
            ).fetchone()
        return bool(row is not None and str(row["status"]) == "quarantined")

    def list_quarantined_specs(self) -> list[dict[str, Any]]:
        """Currently-quarantined specs with their reason + timestamp."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT spec_id, status, quarantine_reason, quarantined_at,
                       released_at, updated_at
                FROM tester_spec_quarantine
                WHERE status = 'quarantined'
                ORDER BY quarantined_at DESC
                """
            ).fetchall()
        return [
            {
                "spec_id": str(row["spec_id"]),
                "status": str(row["status"]),
                "quarantine_reason": str(row["quarantine_reason"] or ""),
                "quarantined_at": str(row["quarantined_at"] or ""),
                "released_at": str(row["released_at"] or ""),
                "updated_at": str(row["updated_at"] or ""),
            }
            for row in rows
        ]

    def list_llm_pr_review_costs(
        self,
        *,
        since_days: int = 7,
        group_by: str = "model",
    ) -> list[dict[str, Any]]:
        """Aggregate LLM-PR-review costs for the cost-summary CLI.

        `group_by` is `"model"`, `"repo"`, or `"pr"`. The total /
        per-group counts come from the `input_tokens`,
        `output_tokens`, `estimated_cost_usd` columns added in P3.5.

        Returns a list of dicts shaped:
          `{"group": <name>, "reviews": N, "input_tokens": ...,
            "output_tokens": ..., "estimated_cost_usd": ...}`
        sorted by `estimated_cost_usd` descending.
        """
        since = max(1, int(since_days))
        if group_by == "model":
            group_col = "model"
            group_expr = "model"
        elif group_by == "repo":
            group_col = "repo"
            group_expr = "repo"
        elif group_by == "pr":
            group_col = "pr"
            group_expr = "repo || '#' || CAST(pr_number AS TEXT)"
        else:
            raise ValueError(f"unknown group_by: {group_by!r}")
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    {group_expr} AS group_key,
                    COUNT(*) AS reviews,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(estimated_cost_usd), 0.0) AS estimated_cost_usd
                FROM llm_pr_reviews
                WHERE created_at >= datetime('now', ?)
                GROUP BY {group_expr}
                ORDER BY estimated_cost_usd DESC, group_key ASC
                """,
                (f"-{since} days",),
            ).fetchall()
        return [
            {
                group_col: str(row["group_key"] or ""),
                "reviews": int(row["reviews"] or 0),
                "input_tokens": int(row["input_tokens"] or 0),
                "output_tokens": int(row["output_tokens"] or 0),
                "estimated_cost_usd": float(row["estimated_cost_usd"] or 0.0),
            }
            for row in rows
        ]

    def force_quarantine_spec(self, *, spec_id: str, reason: str = "") -> None:
        """Manual override -- operator decided a spec is flaky and
        wants it off the qa_incident escalation path even though the
        auto-quarantine heuristic hasn't tripped yet."""
        clean = spec_id.strip()
        if not clean:
            raise ValueError("spec_id is required")
        with self._conn() as conn:
            self._write_quarantine_state(
                conn,
                spec_id=clean,
                status="quarantined",
                reason=(reason.strip() or "manual"),
            )

    def release_spec_quarantine(self, *, spec_id: str, reason: str = "") -> None:
        clean = spec_id.strip()
        if not clean:
            raise ValueError("spec_id is required")
        with self._conn() as conn:
            self._write_quarantine_state(
                conn,
                spec_id=clean,
                status="active",
                reason=(reason.strip() or "manual"),
            )

    def quarantine_status(self, spec_id: str) -> dict[str, Any]:
        """Full status row for the `quarantine show` CLI."""
        clean = spec_id.strip()
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT spec_id, status, quarantine_reason, quarantined_at,
                       released_at, created_at, updated_at
                FROM tester_spec_quarantine
                WHERE spec_id = ?
                """,
                (clean,),
            ).fetchone()
            outcomes = conn.execute(
                """
                SELECT outcome, run_id, recorded_at FROM tester_spec_run_outcomes
                WHERE spec_id = ?
                ORDER BY id DESC
                LIMIT 10
                """,
                (clean,),
            ).fetchall()
        if row is None:
            return {
                "spec_id": clean,
                "status": "active",
                "quarantine_reason": "",
                "quarantined_at": "",
                "released_at": "",
                "recent_outcomes": [],
            }
        return {
            "spec_id": str(row["spec_id"]),
            "status": str(row["status"]),
            "quarantine_reason": str(row["quarantine_reason"] or ""),
            "quarantined_at": str(row["quarantined_at"] or ""),
            "released_at": str(row["released_at"] or ""),
            "recent_outcomes": [
                {
                    "outcome": str(o["outcome"]),
                    "run_id": str(o["run_id"] or ""),
                    "recorded_at": str(o["recorded_at"] or ""),
                }
                for o in outcomes
            ],
        }

    def _write_quarantine_state(
        self,
        conn: Any,
        *,
        spec_id: str,
        status: str,
        reason: str,
    ) -> None:
        """Upsert helper for the quarantine state row.

        `quarantined_at` is set when transitioning to `quarantined`;
        `released_at` is set when transitioning to `active`. We
        preserve the original quarantined_at across releases so the
        history of recent quarantines is recoverable.
        """
        existing = conn.execute(
            "SELECT status, quarantined_at FROM tester_spec_quarantine WHERE spec_id = ?",
            (spec_id,),
        ).fetchone()
        now_iso = datetime.now(timezone.utc).isoformat()
        if existing is None:
            quarantined_at = now_iso if status == "quarantined" else ""
            released_at = now_iso if status == "active" else ""
            conn.execute(
                """
                INSERT INTO tester_spec_quarantine
                    (spec_id, status, quarantine_reason,
                     quarantined_at, released_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (spec_id, status, reason, quarantined_at, released_at, now_iso),
            )
            return
        # Existing row -- only stamp transitions, preserve the
        # pre-existing quarantined_at when toggling back to active so
        # an audit trail survives.
        new_quarantined_at = (
            now_iso
            if status == "quarantined" and existing["status"] != "quarantined"
            else str(existing["quarantined_at"] or "")
        )
        new_released_at = (
            now_iso
            if status == "active" and existing["status"] != "active"
            else ""
        )
        conn.execute(
            """
            UPDATE tester_spec_quarantine
            SET status = ?,
                quarantine_reason = ?,
                quarantined_at = ?,
                released_at = ?,
                updated_at = ?
            WHERE spec_id = ?
            """,
            (status, reason, new_quarantined_at, new_released_at, now_iso, spec_id),
        )

    def next_open_qa_incident(
        self,
        *,
        project_id: Optional[str] = None,
        environment_id: Optional[str] = None,
    ) -> Optional[sqlite3.Row]:
        """The single highest-priority QA incident worth working on now.

        Priority order: open > reproduced > fix_proposed; severity critical
        > high > medium > low; then most recent.
        """
        where: list[str] = ["status IN ('open', 'reproduced', 'fix_proposed')"]
        params: list[object] = []
        if project_id:
            where.append("project_id = ?")
            params.append(project_id)
        if environment_id:
            where.append("environment_id = ?")
            params.append(environment_id)
        sql = f"""
            SELECT *,
                CASE severity
                    WHEN 'critical' THEN 0
                    WHEN 'high'     THEN 1
                    WHEN 'medium'   THEN 2
                    WHEN 'low'      THEN 3
                    ELSE 4
                END AS sev_rank,
                CASE status
                    WHEN 'open'         THEN 0
                    WHEN 'reproduced'   THEN 1
                    WHEN 'fix_proposed' THEN 2
                    ELSE 3
                END AS status_rank
            FROM qa_incidents
            WHERE {" AND ".join(where)}
            ORDER BY status_rank ASC, sev_rank ASC, updated_at DESC
            LIMIT 1
        """
        with self._conn() as conn:
            return conn.execute(sql, params).fetchone()
