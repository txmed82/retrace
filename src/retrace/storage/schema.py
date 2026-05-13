"""SQL schema for Retrace storage. Extracted from monolithic storage.py."""

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
);

CREATE INDEX IF NOT EXISTS idx_incident_lifecycle_events_incident_time
ON incident_lifecycle_events(incident_id, created_at);

CREATE INDEX IF NOT EXISTS idx_incident_lifecycle_events_scope_time
ON incident_lifecycle_events(project_id, environment_id, created_at);

CREATE TABLE IF NOT EXISTS app_error_alert_rules (
    id TEXT PRIMARY KEY,
    public_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    precedence INTEGER NOT NULL DEFAULT 0,
    action TEXT NOT NULL DEFAULT 'alert',
    min_severity TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    title_contains TEXT NOT NULL DEFAULT '',
    fingerprint_contains TEXT NOT NULL DEFAULT '',
    route_contains TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, environment_id, name)
);

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

CREATE TABLE IF NOT EXISTS source_maps (
    id TEXT PRIMARY KEY,
    public_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    release TEXT NOT NULL,
    dist TEXT NOT NULL DEFAULT '',
    artifact_url TEXT NOT NULL,
    source_map_json TEXT NOT NULL DEFAULT '{}',
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, environment_id, release, dist, artifact_url)
);

CREATE INDEX IF NOT EXISTS idx_source_maps_scope_release
ON source_maps(project_id, environment_id, release, dist, uploaded_at DESC);

CREATE TABLE IF NOT EXISTS ingest_rate_limits (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    bucket TEXT NOT NULL,
    identity_hash TEXT NOT NULL,
    window_seconds INTEGER NOT NULL,
    window_start_ms INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, environment_id, bucket, identity_hash, window_seconds)
);

CREATE INDEX IF NOT EXISTS idx_ingest_rate_limits_scope
ON ingest_rate_limits(project_id, environment_id, bucket, updated_at DESC);

CREATE TABLE IF NOT EXISTS otel_events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    trace_id TEXT NOT NULL DEFAULT '',
    span_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    occurred_at_ms INTEGER NOT NULL DEFAULT 0,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_otel_events_trace
ON otel_events(project_id, environment_id, trace_id, occurred_at_ms);

CREATE INDEX IF NOT EXISTS idx_otel_events_signal
ON otel_events(project_id, environment_id, signal_type, occurred_at_ms);

CREATE TABLE IF NOT EXISTS failure_trace_map (
    failure_id TEXT NOT NULL,
    trace_id TEXT NOT NULL DEFAULT '',
    span_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(failure_id, trace_id, span_id)
);

CREATE INDEX IF NOT EXISTS idx_failure_trace_map_trace
ON failure_trace_map(trace_id, span_id, failure_id);

CREATE INDEX IF NOT EXISTS idx_failure_trace_map_span
ON failure_trace_map(span_id, failure_id);

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

CREATE TABLE IF NOT EXISTS github_review_runs (
    id TEXT PRIMARY KEY,
    repo_full_name TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    installation_id TEXT NOT NULL DEFAULT '',
    sender_login TEXT NOT NULL DEFAULT '',
    comment_id TEXT NOT NULL DEFAULT '',
    comment_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    trigger_phrase TEXT NOT NULL DEFAULT '@retrace review',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_github_review_runs_repo_pr
ON github_review_runs(repo_full_name, pr_number, updated_at);

CREATE INDEX IF NOT EXISTS idx_github_review_runs_status
ON github_review_runs(status, updated_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_github_review_runs_comment
ON github_review_runs(repo_full_name, pr_number, comment_id)
WHERE comment_id != '';

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

CREATE TABLE IF NOT EXISTS qa_incidents (
    id TEXT PRIMARY KEY,
    public_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    suspected_cause TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'medium',
    confidence TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'open',
    primary_source_kind TEXT NOT NULL DEFAULT 'replay',
    sources_json TEXT NOT NULL DEFAULT '[]',
    reproduction_json TEXT NOT NULL DEFAULT '[]',
    expected_outcome TEXT NOT NULL DEFAULT '',
    actual_outcome TEXT NOT NULL DEFAULT '',
    app_url TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    affected_count INTEGER NOT NULL DEFAULT 1,
    affected_users INTEGER NOT NULL DEFAULT 1,
    first_seen_ms INTEGER NOT NULL DEFAULT 0,
    last_seen_ms INTEGER NOT NULL DEFAULT 0,
    repro_status TEXT NOT NULL DEFAULT 'not_attempted',
    repro_spec_id TEXT NOT NULL DEFAULT '',
    repro_run_id TEXT NOT NULL DEFAULT '',
    repro_summary TEXT NOT NULL DEFAULT '',
    fix_status TEXT NOT NULL DEFAULT 'not_started',
    fix_repo TEXT NOT NULL DEFAULT '',
    fix_branch TEXT NOT NULL DEFAULT '',
    fix_pr_url TEXT NOT NULL DEFAULT '',
    fix_prompt_path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, environment_id, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_qa_incidents_status
ON qa_incidents(project_id, environment_id, status, updated_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_qa_incidents_public
ON qa_incidents(public_id);

-- Alert routes (P1.1) — fan-out destinations for alert-rule trips.
-- A rule firing an `action=alert` decision triggers a lookup in this
-- table; every matching enabled route posts to its target. Routes can
-- be scoped to a specific named `rule_name`, or left empty to match
-- every alert at-or-above `min_severity`.
CREATE TABLE IF NOT EXISTS alert_routes (
    id TEXT PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    rule_name TEXT NOT NULL DEFAULT '',
    target_kind TEXT NOT NULL,
    target_url TEXT NOT NULL,
    target_secret TEXT NOT NULL DEFAULT '',
    min_severity TEXT NOT NULL DEFAULT '',
    dedup_window_seconds INTEGER NOT NULL DEFAULT 300,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, environment_id, name)
);

CREATE INDEX IF NOT EXISTS idx_alert_routes_lookup
ON alert_routes(project_id, environment_id, enabled);

-- Alert dispatches (P1.1) — per-fired-alert delivery log. Doubles as
-- the dedup table: we look up `(route_id, fingerprint)` rows newer
-- than `dedup_window_seconds` to suppress fast-repeat sends.
CREATE TABLE IF NOT EXISTS alert_dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    target_kind TEXT NOT NULL,
    target_url TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_alert_dispatches_dedup
ON alert_dispatches(route_id, fingerprint, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_alert_dispatches_recent
ON alert_dispatches(project_id, environment_id, created_at DESC);

-- LLM-driven PR reviews (`llm_pr_review.py` output, persisted per PR
-- so the *next* review on overlapping files can fold prior risk notes
-- into the prompt instead of re-flagging the same issue every time.
CREATE TABLE IF NOT EXISTS llm_pr_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL DEFAULT '',
    pr_number INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    risk_notes_json TEXT NOT NULL DEFAULT '[]',
    suggestions_json TEXT NOT NULL DEFAULT '[]',
    paths_json TEXT NOT NULL DEFAULT '[]',
    -- P3.5 cost-visibility columns. Estimated server-side from
    -- input/output text length (chars/4 heuristic) rather than
    -- real provider `usage` blocks today — directionally correct,
    -- and avoids invasive plumbing into the LLM client surface
    -- that other call sites don't need.
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_llm_pr_reviews_recent
ON llm_pr_reviews(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_llm_pr_reviews_pr
ON llm_pr_reviews(repo, pr_number);

-- P3.6 (scaffold): server-side replay.
--
-- rrweb captures only what runs in the browser. SSR exceptions
-- (the page that 500ed before hydration), backend-only services,
-- and request/response cycles outside the browser have no replay
-- story today. This table is the **storage seam** for the
-- eventual Node/Python capture middleware to write into; the
-- middleware itself is deferred until a real SSR-replay user
-- surfaces demand.
--
-- A "session" here is a single captured request/response with an
-- optional rendered-HTML snippet at the failure moment. Not a
-- continuous DOM stream like browser replay — that primitive is
-- too heavy server-side.
CREATE TABLE IF NOT EXISTS server_replay_sessions (
    id TEXT PRIMARY KEY,
    public_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    request_method TEXT NOT NULL DEFAULT '',
    request_path TEXT NOT NULL DEFAULT '',
    request_headers_json TEXT NOT NULL DEFAULT '{}',
    request_body_text TEXT NOT NULL DEFAULT '',
    response_status INTEGER NOT NULL DEFAULT 0,
    response_headers_json TEXT NOT NULL DEFAULT '{}',
    rendered_html_snippet TEXT NOT NULL DEFAULT '',
    runtime TEXT NOT NULL DEFAULT '',
    occurred_at_ms INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_server_replay_sessions_scope_time
ON server_replay_sessions(project_id, environment_id, occurred_at_ms DESC);

CREATE INDEX IF NOT EXISTS idx_server_replay_sessions_path
ON server_replay_sessions(project_id, environment_id, request_path, occurred_at_ms DESC);

-- P3.1: flake quarantine.
--
-- `tester_spec_run_outcomes` is a rolling window of the most-recent
-- run outcomes per spec. We prune to ~20 rows per spec because the
-- auto-quarantine and auto-release heuristics only need to look at
-- recent history; the long-term run record lives in `runs/` on disk.
--
-- `tester_spec_quarantine` is the per-spec current state — a row
-- exists for any spec that's been observed at least once. `status`
-- is `active` (default) or `quarantined`. Once quarantined, the
-- `_persist_harness_failure` path in `commands/tester.py` skips
-- the `qa_incident` filing so an intermittent spec doesn't keep
-- re-escalating the same flake.

CREATE TABLE IF NOT EXISTS tester_spec_run_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spec_id TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tester_spec_run_outcomes_spec_recent
ON tester_spec_run_outcomes(spec_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS tester_spec_quarantine (
    spec_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active',
    quarantine_reason TEXT NOT NULL DEFAULT '',
    quarantined_at TEXT NOT NULL DEFAULT '',
    released_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tester_spec_quarantine_status
ON tester_spec_quarantine(status);
"""

