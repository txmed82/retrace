"""Row/entity models for Retrace storage."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Protocol

from retrace.evidence import PROMPT_SAFE_REDACTION_STATES

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
class IncidentLifecycleEventRow:
    id: str
    incident_id: str
    project_id: str
    environment_id: str
    from_status: str
    to_status: str
    actor_type: str
    actor_id: str
    reason: str
    metadata: dict[str, Any]
    created_at: datetime


@dataclass
class AppErrorAlertRuleRow:
    id: str
    public_id: str
    project_id: str
    environment_id: str
    name: str
    enabled: bool
    precedence: int
    action: str
    min_severity: str
    provider: str
    title_contains: str
    fingerprint_contains: str
    route_contains: str
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass
class AlertRouteRow:
    id: str
    public_id: str
    project_id: str
    environment_id: str
    name: str
    enabled: bool
    rule_name: str
    target_kind: str
    target_url: str
    target_secret: str
    min_severity: str
    dedup_window_seconds: int
    created_at: datetime
    updated_at: datetime


@dataclass
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_after_seconds: int
    window_seconds: int


@dataclass
class AppErrorRetentionPruneResult:
    dry_run: bool
    failure_retention_days: int
    evidence_retention_days: int
    source_map_retention_days: int
    rate_limit_retention_hours: int
    failures: int
    evidence: int
    incident_links: int
    incidents: int
    source_maps: int
    rate_limit_rows: int


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
class SourceMapRow:
    id: str
    public_id: str
    project_id: str
    environment_id: str
    release: str
    dist: str
    artifact_url: str
    source_map: dict[str, Any]
    uploaded_at: datetime


@dataclass
class OtelEventRow:
    id: str
    project_id: str
    environment_id: str
    signal_type: str
    trace_id: str
    span_id: str
    name: str
    severity: str
    body: str
    occurred_at_ms: int
    attributes: dict[str, Any]
    created_at: datetime


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
class GitHubReviewRunRow:
    id: str
    repo_full_name: str
    pr_number: int
    installation_id: str
    sender_login: str
    comment_id: str
    comment_url: str
    status: str
    trigger_phrase: str
    metadata: dict[str, Any]
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
            self.previous_status in {"resolved", "verified"}
            and self.current_status == "regressed"
        )


@dataclass
class ProcessingJobUpdateResult:
    job_id: str
    updated: bool

