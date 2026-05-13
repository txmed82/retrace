from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import click

from retrace.config import load_config
from retrace.deploys import correlate_recent_failures_to_deploys, record_deploy
from retrace.ingester import PostHogIngester
from retrace.issue_sink_clients import GitHubClient, IssueSinkError, LinearClient
from retrace.issue_sinks import compact_issue_card, promote_replay_issue
from retrace.notification_sinks import (
    NotificationEvent,
    NotificationPayload,
    build_sinks_from_config,
    close_sinks,
    dispatch_notification,
)
from retrace.replay_core import process_queued_replay_jobs
from retrace.sdk_keys import create_sdk_key, create_service_token
from retrace.sentry_compat import build_sentry_dsn
from retrace.source_maps import upload_source_map
from retrace.storage import Storage


# Re-exported from api_handler.py for backward compatibility
from retrace.commands.api_handler import (  # noqa: F401
    INGEST_RATE_LIMITS,
    HOSTED_ONBOARDING_SCOPES,
    CorrelationEnricher,
    _alert_rule_api_dict,
    _app_error_notification_payload,
    _bearer_token,
    _build_enricher,
    _consume_rate_limit,
    _cors_headers,
    _dispatch_app_error_notifications,
    _dt_api,
    _evidence_api_dict,
    _extract_replay_sdk_key,
    _failure_api_dict,
    _handler,
    _header_value,
    _hosted_onboarding_manifest,
    _incident_api_dict,
    _incident_lifecycle_event_api_dict,
    _issue_cards_for_items,
    _json_response,
    _latest_failure,
    _maybe_llm_client,
    _optional_bool,
    _optional_int,
    _query_dict,
    _rate_limit_headers,
    _rate_limited_response,
    _repair_task_api_dict,
    _require_service_token,
    _retention_result_api_dict,
    _row_dict,
    _sentry_ingest_path_parts,
)

logger = logging.getLogger(__name__)

@click.group("api")
def api_group() -> None:
    """Run first-party Retrace APIs."""


@api_group.command("create-sdk-key")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--org", default="Local", show_default=True)
@click.option("--project", "project_name", default="Default", show_default=True)
@click.option("--environment", default="production", show_default=True)
@click.option("--name", default="Browser SDK", show_default=True)
@click.option("--api-base-url", default="http://127.0.0.1:8788", show_default=True)
def api_create_sdk_key(
    config_path: Path,
    org: str,
    project_name: str,
    environment: str,
    name: str,
    api_base_url: str,
) -> None:
    """Create a browser-safe write-only SDK key."""
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(
        org_name=org,
        project_name=project_name,
        environment_name=environment,
    )
    created = create_sdk_key(
        store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        name=name,
    )
    click.echo(
        json.dumps(
            {
                "id": created.id,
                "project_id": workspace.project_id,
                "environment_id": workspace.environment_id,
                "key": created.key,
                "sentry_dsn": build_sentry_dsn(
                    public_key=created.key,
                    base_url=api_base_url,
                    project_id=workspace.project_id,
                ),
            },
            indent=2,
        )
    )


@api_group.command("create-service-token")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--org", default="Local", show_default=True)
@click.option("--project", "project_name", default="Default", show_default=True)
@click.option("--environment", default="production", show_default=True)
@click.option("--name", default="Service token", show_default=True)
@click.option("--scope", "scopes", multiple=True, default=("mcp:read",))
def api_create_service_token(
    config_path: Path,
    org: str,
    project_name: str,
    environment: str,
    name: str,
    scopes: tuple[str, ...],
) -> None:
    """Create a secret service token for read/MCP/admin APIs."""
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(
        org_name=org,
        project_name=project_name,
        environment_name=environment,
    )
    created = create_service_token(
        store,
        project_id=workspace.project_id,
        name=name,
        scopes=list(scopes),
    )
    click.echo(
        json.dumps(
            {
                "id": created.id,
                "project_id": workspace.project_id,
                "token": created.token,
                "scopes": created.scopes,
            },
            indent=2,
        )
    )


@api_group.command("onboard-hosted")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--org", default="Local", show_default=True)
@click.option("--project", "project_name", default="Default", show_default=True)
@click.option("--environment", default="production", show_default=True)
@click.option("--api-base-url", default="http://127.0.0.1:8788", show_default=True)
@click.option("--sdk-key-name", default="Hosted browser SDK", show_default=True)
@click.option("--service-token-name", default="Hosted onboarding", show_default=True)
@click.option("--service-token-scope", "scopes", multiple=True)
@click.option("--release", default="$GITHUB_SHA", show_default=True)
@click.option(
    "--artifact-url",
    default="https://cdn.example.com/assets/app.min.js",
    show_default=True,
)
def api_onboard_hosted(
    config_path: Path,
    org: str,
    project_name: str,
    environment: str,
    api_base_url: str,
    sdk_key_name: str,
    service_token_name: str,
    scopes: tuple[str, ...],
    release: str,
    artifact_url: str,
) -> None:
    """Create hosted/self-host onboarding credentials and integration snippets."""
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(
        org_name=org,
        project_name=project_name,
        environment_name=environment,
    )
    sdk = create_sdk_key(
        store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        name=sdk_key_name,
    )
    service_scopes = list(scopes) if scopes else list(HOSTED_ONBOARDING_SCOPES)
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name=service_token_name,
        scopes=service_scopes,
    )
    click.echo(
        json.dumps(
            {
                "onboarding": _hosted_onboarding_manifest(
                    base_url=api_base_url,
                    project_id=workspace.project_id,
                    environment_id=workspace.environment_id,
                    sdk_key=sdk.key,
                    service_token=service.token,
                    service_token_scopes=service.scopes,
                    release=release,
                    artifact_url=artifact_url,
                )
            },
            indent=2,
        )
    )


@api_group.command("serve")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8788, show_default=True, type=int)
def api_serve(config_path: Path, host: str, port: int) -> None:
    """Serve the first-party replay ingest API."""
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    notification_sinks = (
        build_sinks_from_config(cfg.notifications) if cfg.notifications.enabled else []
    )
    httpd = ThreadingHTTPServer(
        (host, port),
        _handler(
            store,
            enricher=_build_enricher(cfg, store),
            github_webhook_secret=cfg.github_app.webhook_secret,
            notification_sinks=notification_sinks,
        ),
    )
    click.echo(f"Retrace API running at http://{host}:{port}")
    click.echo("Replay ingest endpoint: POST /api/sdk/replay")
    click.echo("GitHub App webhook endpoint: POST /api/github/webhook")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        click.echo("Stopping Retrace API")
    finally:
        close_sinks(notification_sinks)
        httpd.server_close()


@api_group.command("process-replays")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--limit", default=25, show_default=True, type=int)
@click.option(
    "--ai/--no-ai",
    default=False,
    show_default=True,
    help="Run configured LLM analysis for detector-backed replay issues.",
)
def api_process_replays(config_path: Path, limit: int, ai: bool) -> None:
    """Process queued final replay batches into signals and issues."""
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    llm_client = _maybe_llm_client(cfg, enabled=ai)
    try:
        result = process_queued_replay_jobs(
            store=store,
            limit=limit,
            enricher=_build_enricher(cfg, store),
            llm_client=llm_client,
        )
    finally:
        if llm_client is not None:
            llm_client.close()
    if (
        result.issues_inserted or result.issues_regressed
    ) and cfg.notifications.enabled:
        sinks = build_sinks_from_config(cfg.notifications)
        inserted_cards = _issue_cards_for_items(store, list(result.inserted_details))
        regressed_cards = _issue_cards_for_items(store, list(result.regressed_details))
        try:
            if result.issues_inserted:
                dispatch_notification(
                    sinks,
                    NotificationPayload(
                        event=NotificationEvent.ISSUE_CREATED.value,
                        title=f"{result.issues_inserted} new replay issue(s)",
                        summary=(
                            f"New: {', '.join(result.inserted_public_ids[:5]) or '—'}"
                        ),
                        extra={
                            "public_ids": list(result.inserted_public_ids),
                            "issue_cards": inserted_cards,
                        },
                    ),
                )
            if result.issues_regressed:
                dispatch_notification(
                    sinks,
                    NotificationPayload(
                        event=NotificationEvent.ISSUE_REGRESSED.value,
                        title=f"{result.issues_regressed} previously-resolved issue(s) regressed",
                        summary=(
                            f"Regressed: {', '.join(result.regressed_public_ids[:5]) or '—'}"
                        ),
                        extra={
                            "public_ids": list(result.regressed_public_ids),
                            "regressions": list(result.regressed_details),
                            "issue_cards": regressed_cards,
                        },
                    ),
                )
        finally:
            close_sinks(sinks)
    click.echo(
        json.dumps(
            {
                "jobs_seen": result.jobs_seen,
                "jobs_processed": result.jobs_processed,
                "jobs_failed": result.jobs_failed,
                "sessions_processed": result.sessions_processed,
                "issues_created_or_updated": result.issues_created_or_updated,
                "ai_analysis": bool(ai),
            },
            indent=2,
        )
    )


@api_group.command("import-posthog-replays")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--since-hours", default=24, show_default=True, type=int)
@click.option("--max-sessions", default=50, show_default=True, type=int)
@click.option("--project-name", default="Default", show_default=True)
@click.option("--environment-name", default="production", show_default=True)
@click.option(
    "--process/--no-process",
    "process_now",
    default=True,
    show_default=True,
    help="Process imported replay finalize jobs immediately.",
)
@click.option(
    "--ai/--no-ai",
    default=False,
    show_default=True,
    help="Use configured LLM analysis while processing imported replays.",
)
def api_import_posthog_replays(
    config_path: Path,
    since_hours: int,
    max_sessions: int,
    project_name: str,
    environment_name: str,
    process_now: bool,
    ai: bool,
) -> None:
    """Import PostHog session recordings into replay issues."""
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(
        project_name=project_name,
        environment_name=environment_name,
    )
    ingester = PostHogIngester(cfg.posthog, store, data_dir=cfg.run.data_dir)
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, since_hours))
    imported = ingester.import_since_as_replays(
        since,
        max(1, max_sessions),
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )
    processed_payload: dict[str, Any] = {}
    if process_now:
        llm_client = _maybe_llm_client(cfg, enabled=ai)
        try:
            processed = process_queued_replay_jobs(
                store=store,
                limit=max(1, max_sessions),
                project_id=workspace.project_id,
                enricher=_build_enricher(cfg, store),
                llm_client=llm_client,
            )
        finally:
            if llm_client is not None:
                llm_client.close()
        processed_payload = {
            "jobs_seen": processed.jobs_seen,
            "jobs_processed": processed.jobs_processed,
            "jobs_failed": processed.jobs_failed,
            "sessions_processed": processed.sessions_processed,
            "issues_created_or_updated": processed.issues_created_or_updated,
            "issues_inserted": processed.issues_inserted,
            "issues_regressed": processed.issues_regressed,
        }
    click.echo(
        json.dumps(
            {
                "imported_sessions": imported.session_ids,
                "skipped_sessions": imported.skipped_session_ids,
                "processing_job_ids": imported.processing_job_ids,
                "project_id": workspace.project_id,
                "environment_id": workspace.environment_id,
                "processed": processed_payload,
                "ai_analysis": bool(ai and process_now),
            },
            indent=2,
        )
    )


@api_group.command("promote-issue")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option(
    "--provider",
    type=click.Choice(["linear", "github"], case_sensitive=False),
    required=True,
)
@click.option("--project-id", default="", help="Project ID override.")
@click.option("--environment-id", default="", help="Environment ID override.")
@click.option("--base-url", default="", help="Base URL used for replay deep links.")
@click.option("--external-id", default="", help="Existing external issue ID.")
@click.option("--external-url", default="", help="Existing external issue URL.")
@click.option("--repo", default="", help="GitHub owner/name override (github only).")
@click.option(
    "--team-id", default="", help="Linear team UUID override (linear only)."
)
@click.option(
    "--team-key",
    default="",
    help="Linear team key (e.g. 'ENG') resolved to a team id at promote time.",
)
@click.option("--label", "labels", multiple=True, help="Label to apply (repeatable).")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Skip remote API calls; emit stub external IDs/URLs instead.",
)
@click.argument("issue_id")
def api_promote_issue(
    config_path: Path,
    provider: str,
    project_id: str,
    environment_id: str,
    base_url: str,
    external_id: str,
    external_url: str,
    repo: str,
    team_id: str,
    team_key: str,
    labels: tuple[str, ...],
    dry_run: bool,
    issue_id: str,
) -> None:
    """Promote a replay-backed issue into Linear or GitHub."""
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")

    provider_lc = provider.strip().lower()
    linear_client: LinearClient | None = None
    linear_team_id = ""
    linear_team_key_to_resolve = ""
    github_client: GitHubClient | None = None
    github_repo = ""
    effective_labels: list[str] = list(labels) if labels else []

    if not dry_run and provider_lc == "linear" and cfg.linear.enabled:
        linear_client = LinearClient(
            api_key=cfg.linear.api_key,
            endpoint=cfg.linear.endpoint,
        )
        linear_team_id = team_id.strip() or cfg.linear.team_id.strip()
        if not linear_team_id:
            linear_team_key_to_resolve = (
                team_key.strip() or cfg.linear.team_key.strip()
            )
        if not effective_labels:
            effective_labels = list(cfg.linear.labels)

    if not dry_run and provider_lc == "github" and cfg.github_sink.enabled:
        github_client = GitHubClient(
            api_key=cfg.github_sink.api_key,
            base_url=cfg.github_sink.base_url,
        )
        github_repo = repo.strip() or cfg.github_sink.repo.strip()
        if not effective_labels:
            effective_labels = list(cfg.github_sink.labels)

    try:
        if (
            linear_client is not None
            and not linear_team_id
            and linear_team_key_to_resolve
        ):
            linear_team_id = linear_client.resolve_team_id(linear_team_key_to_resolve)
        result = promote_replay_issue(
            store=store,
            project_id=project_id.strip() or workspace.project_id,
            environment_id=environment_id.strip() or workspace.environment_id,
            issue_id=issue_id,
            provider=provider_lc,
            base_url=base_url,
            external_id=external_id,
            external_url=external_url,
            linear_client=linear_client,
            linear_team_id=linear_team_id,
            github_client=github_client,
            github_repo=github_repo,
            labels=effective_labels,
        )
    except IssueSinkError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        if linear_client is not None:
            linear_client.close()
        if github_client is not None:
            github_client.close()
    if result.created and cfg.notifications.enabled:
        notify_sinks = build_sinks_from_config(cfg.notifications)
        try:
            dispatch_notification(
                notify_sinks,
                NotificationPayload(
                    event=NotificationEvent.TICKET_CREATED.value,
                    title=str(result.payload.get("title") or "Replay issue ticket created"),
                    summary=str(result.payload.get("summary") or ""),
                    severity=str(result.payload.get("severity") or ""),
                    public_id=result.issue_public_id,
                    url=result.external_url,
                    extra={
                        "provider": result.provider,
                        "issue_card": compact_issue_card(result.payload),
                    },
                ),
            )
        finally:
            close_sinks(notify_sinks)
    click.echo(
        json.dumps(
            {
                "issue_id": result.issue_id,
                "issue_public_id": result.issue_public_id,
                "provider": result.provider,
                "external_id": result.external_id,
                "external_url": result.external_url,
                "created": result.created,
                "payload": result.payload,
            },
            indent=2,
        )
    )


@api_group.command("sync-tickets")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--project-id", default="", help="Project ID override.")
@click.option("--environment-id", default="", help="Environment ID override.")
@click.option(
    "--limit",
    default=50,
    show_default=True,
    type=int,
    help="Max ticketed issues to inspect in one run.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show plan; do not transition state or fire notifications.",
)
def api_sync_tickets(
    config_path: Path,
    project_id: str,
    environment_id: str,
    limit: int,
    dry_run: bool,
) -> None:
    """Poll Linear/GitHub for the state of tickets Retrace has filed.

    For each replay issue with an external_ticket_id, query the upstream
    provider.  If the ticket is closed/done, transition the Retrace issue to
    `resolved` and fire issue.resolved.  Skips issues whose ticket id doesn't
    match any configured provider so multi-provider workspaces stay safe.
    """
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")
    pid = project_id.strip() or workspace.project_id
    eid = environment_id.strip() or workspace.environment_id

    rows = store.list_ticketed_replay_issues(
        project_id=pid, environment_id=eid, limit=limit
    )

    linear_client: LinearClient | None = None
    github_client: GitHubClient | None = None
    if cfg.linear.enabled:
        linear_client = LinearClient(
            api_key=cfg.linear.api_key, endpoint=cfg.linear.endpoint
        )
    if cfg.github_sink.enabled:
        github_client = GitHubClient(
            api_key=cfg.github_sink.api_key, base_url=cfg.github_sink.base_url
        )
    notify_sinks = (
        build_sinks_from_config(cfg.notifications)
        if cfg.notifications.enabled and not dry_run
        else []
    )

    plan: list[dict[str, Any]] = []
    resolved: list[str] = []
    skipped: list[dict[str, Any]] = []

    try:
        for row in rows:
            issue_id = str(row["id"])
            public_id = str(row["public_id"])
            ticket_id = str(row["external_ticket_id"] or "").strip()
            current_status = str(row["status"] or "")
            entry: dict[str, Any] = {
                "public_id": public_id,
                "issue_id": issue_id,
                "ticket_id": ticket_id,
                "current_status": current_status,
            }
            if not ticket_id:
                skipped.append({**entry, "reason": "no_ticket_id"})
                continue
            if current_status == "resolved":
                skipped.append({**entry, "reason": "already_resolved"})
                continue

            provider = _classify_ticket_id(ticket_id)
            entry["provider"] = provider

            try:
                if provider == "github":
                    if github_client is None:
                        skipped.append({**entry, "reason": "github_not_configured"})
                        continue
                    repo, number = _parse_github_ticket_id(ticket_id)
                    state = github_client.get_issue_state(repo=repo, number=number)
                    upstream_closed = state.get("state") == "closed"
                elif provider == "linear":
                    if linear_client is None:
                        skipped.append({**entry, "reason": "linear_not_configured"})
                        continue
                    state = linear_client.get_issue_state(ticket_id)
                    upstream_closed = state.get("type") in {"completed", "canceled"}
                else:
                    skipped.append({**entry, "reason": f"unknown_provider:{provider}"})
                    continue
            except IssueSinkError as exc:
                skipped.append({**entry, "reason": f"lookup_failed: {exc}"})
                continue

            entry["upstream_state"] = state
            plan.append(entry)
            if not upstream_closed:
                continue
            if dry_run:
                continue
            store.transition_replay_issue(issue_id, status="resolved")
            resolved.append(public_id)
            if notify_sinks:
                dispatch_notification(
                    notify_sinks,
                    NotificationPayload(
                        event=NotificationEvent.ISSUE_RESOLVED.value,
                        title=str(row["title"] or "Replay issue"),
                        summary="Closed upstream; Retrace marked resolved.",
                        severity=str(row["severity"] or ""),
                        public_id=public_id,
                        url=str(row["external_ticket_url"] or ""),
                        extra={
                            "ticket_id": ticket_id,
                            "provider": provider,
                            "upstream_state": state,
                        },
                    ),
                )
    finally:
        if linear_client is not None:
            linear_client.close()
        if github_client is not None:
            github_client.close()
        if notify_sinks:
            close_sinks(notify_sinks)

    click.echo(
        json.dumps(
            {
                "plan": plan,
                "resolved": resolved,
                "skipped": skipped,
                "dry_run": dry_run,
            },
            indent=2,
        )
    )


def _classify_ticket_id(ticket_id: str) -> str:
    """Decide whether a stored external_ticket_id points at GitHub or Linear.

    GitHub format we emit: `owner/name#123`.  Linear format: `ENG-42` or a
    UUID.  Anything else returns 'unknown'.
    """
    if "#" in ticket_id and "/" in ticket_id.split("#", 1)[0]:
        return "github"
    if "-" in ticket_id:
        return "linear"
    return "unknown"


def _parse_github_ticket_id(ticket_id: str) -> tuple[str, int]:
    if "#" not in ticket_id:
        raise ValueError(f"GitHub ticket id must be 'owner/name#N': {ticket_id!r}")
    repo, _, number_str = ticket_id.partition("#")
    if "/" not in repo:
        raise ValueError(f"GitHub ticket id repo must be 'owner/name': {ticket_id!r}")
    if not number_str.isdigit():
        raise ValueError(
            f"GitHub ticket id issue number must be numeric: {ticket_id!r}"
        )
    return repo, int(number_str)


@api_group.command("verify-resolved")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--project-id", default="", help="Project ID override.")
@click.option("--environment-id", default="", help="Environment ID override.")
@click.option(
    "--limit",
    default=10,
    show_default=True,
    type=int,
    help="Max resolved issues to verify in one run.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Identify candidates and print plan; do not run specs or transition state.",
)
def api_verify_resolved(
    config_path: Path,
    project_id: str,
    environment_id: str,
    limit: int,
    dry_run: bool,
) -> None:
    """Re-run replay-derived specs against issues marked resolved.

    Pairs each resolved issue with the most recent replay-derived spec linked
    to it via fixtures.issue_public_id. If the spec fails, the issue is
    transitioned back to 'regressed' and an issue.regressed notification is
    fired so the team knows the fix didn't stick.
    """
    from retrace.tester import (
        list_specs,
        run_spec,
        runs_dir_for_data_dir,
        specs_dir_for_data_dir,
    )

    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")
    pid = project_id.strip() or workspace.project_id
    eid = environment_id.strip() or workspace.environment_id

    specs_dir = specs_dir_for_data_dir(cfg.run.data_dir)
    runs_dir = runs_dir_for_data_dir(cfg.run.data_dir)

    specs_by_id: dict[str, Any] = {}
    specs_by_issue: dict[str, Any] = {}
    for spec in list_specs(specs_dir):
        specs_by_id[spec.spec_id] = spec
        public_id = str(spec.fixtures.get("issue_public_id") or "").strip()
        if not public_id:
            continue
        existing = specs_by_issue.get(public_id)
        if existing is None or spec.updated_at > existing.updated_at:
            specs_by_issue[public_id] = spec

    resolved = store.list_replay_issues(
        project_id=pid, environment_id=eid, status="resolved"
    )
    plan: list[dict[str, Any]] = []
    for row in resolved[: max(1, int(limit))]:
        public_id = str(row["public_id"])
        spec = None
        link_id = ""
        failure_id = str(row["canonical_failure_id"] or "")
        if failure_id:
            for link in store.list_failure_test_links(failure_id=failure_id):
                linked_spec = specs_by_id.get(link.spec_id)
                if linked_spec is not None:
                    spec = linked_spec
                    link_id = link.id
                    break
        if spec is None:
            spec = specs_by_issue.get(public_id)
        plan.append(
            {
                "public_id": public_id,
                "issue_id": str(row["id"]),
                "failure_id": failure_id,
                "coverage_link_id": link_id,
                "spec_id": spec.spec_id if spec else "",
                "has_spec": spec is not None,
            }
        )

    if dry_run:
        click.echo(json.dumps({"plan": plan, "verified": [], "regressed": []}, indent=2))
        return

    verified: list[str] = []
    regressed: list[dict[str, str]] = []
    notify_sinks = (
        build_sinks_from_config(cfg.notifications)
        if cfg.notifications.enabled
        else []
    )
    try:
        for entry in plan:
            spec_id = entry["spec_id"]
            if not spec_id:
                continue
            spec = specs_by_id.get(spec_id) or specs_by_issue[entry["public_id"]]
            try:
                result = run_spec(
                    spec=spec,
                    runs_dir=runs_dir,
                    cwd=config_path.parent,
                )
            except Exception as exc:
                regressed.append(
                    {
                        "public_id": entry["public_id"],
                        "issue_id": entry["issue_id"],
                        "error": f"run_spec raised: {exc}",
                    }
                )
                continue
            coverage_link_id = str(entry.get("coverage_link_id") or "")
            if coverage_link_id:
                try:
                    store.update_failure_test_link_run(
                        spec_id=result.spec_id,
                        run_result=result,
                        link_id=coverage_link_id,
                    )
                except Exception:
                    logger.warning(
                        "failed to persist failure_test_link run metadata",
                        extra={"spec_id": result.spec_id, "run_id": result.run_id},
                        exc_info=True,
                    )
            if result.ok:
                verified.append(entry["public_id"])
                continue
            store.transition_replay_issue(entry["issue_id"], status="regressed")
            regressed.append(
                {
                    "public_id": entry["public_id"],
                    "issue_id": entry["issue_id"],
                    "run_id": result.run_id,
                    "exit_code": str(result.exit_code),
                    "error": result.error,
                }
            )
            if notify_sinks:
                dispatch_notification(
                    notify_sinks,
                    NotificationPayload(
                        event=NotificationEvent.ISSUE_REGRESSED.value,
                        title=(
                            f"Resolved issue {entry['public_id']} regressed under verification"
                        ),
                        summary=result.error
                        or f"spec {spec.spec_id} exited {result.exit_code}",
                        public_id=entry["public_id"],
                        extra={"run_id": result.run_id, "spec_id": spec.spec_id},
                    ),
                )
    finally:
        if notify_sinks:
            close_sinks(notify_sinks)

    click.echo(
        json.dumps(
            {
                "plan": plan,
                "verified": verified,
                "regressed": regressed,
            },
            indent=2,
        )
    )


@api_group.command("record-deploy")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--project-id", default="", help="Project ID override.")
@click.option("--environment-id", default="", help="Environment ID override.")
@click.option("--sha", required=True, help="Deploy commit SHA.")
@click.option("--branch", default="", help="Deploy branch.")
@click.option("--author", default="", help="Deploy author.")
@click.option("--deployed-at-ms", default=0, type=int, help="Deploy timestamp in ms.")
@click.option("--changed-file", "changed_files", multiple=True, help="Changed file path.")
@click.option(
    "--source-map-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help=(
        "Optional directory of source maps to upload as part of this deploy. "
        "Pairs are inferred from filenames: foo.js.map -> foo.js. "
        "Use --source-map-artifact-prefix to set the URL prefix."
    ),
)
@click.option(
    "--source-map-artifact-prefix",
    default="",
    help="URL prefix prepended to each source-map's artifact_url (e.g. https://cdn.example.com/static/).",
)
def api_record_deploy(
    config_path: Path,
    project_id: str,
    environment_id: str,
    sha: str,
    branch: str,
    author: str,
    deployed_at_ms: int,
    changed_files: tuple[str, ...],
    source_map_dir: Path | None,
    source_map_artifact_prefix: str,
) -> None:
    """Record a deploy marker and (optionally) auto-upload source maps."""
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")
    pid = project_id.strip() or workspace.project_id
    eid = environment_id.strip() or workspace.environment_id
    deploy = record_deploy(
        store=store,
        project_id=pid,
        environment_id=eid,
        sha=sha,
        branch=branch,
        author=author,
        deployed_at_ms=deployed_at_ms,
        changed_files=list(changed_files),
        metadata={"source": "cli"},
    )
    correlations = correlate_recent_failures_to_deploys(
        store=store,
        project_id=pid,
        environment_id=eid,
    )

    uploaded_maps: list[dict] = []
    skipped_maps: list[dict] = []
    if source_map_dir is not None:
        from retrace.source_maps import upload_source_map

        prefix = (source_map_artifact_prefix or "").rstrip("/")
        for path in sorted(source_map_dir.rglob("*.map")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                skipped_maps.append({"path": str(path), "reason": f"unreadable: {exc}"})
                continue
            if not isinstance(payload, dict):
                skipped_maps.append({"path": str(path), "reason": "not a JSON object"})
                continue
            # foo.js.map -> foo.js for artifact_url
            relative = path.relative_to(source_map_dir).as_posix()
            generated = relative[:-4] if relative.endswith(".map") else relative
            artifact_url = (
                f"{prefix}/{generated}" if prefix else generated
            )
            try:
                row = upload_source_map(
                    store=store,
                    project_id=pid,
                    environment_id=eid,
                    release=sha,
                    dist="",
                    artifact_url=artifact_url,
                    source_map=payload,
                )
            except ValueError as exc:
                skipped_maps.append({"path": str(path), "reason": str(exc)})
                continue
            uploaded_maps.append(
                {
                    "path": str(path),
                    "artifact_url": row.artifact_url,
                    "public_id": row.public_id,
                }
            )

    click.echo(
        json.dumps(
            {
                "deploy": {
                    "id": deploy.id,
                    "public_id": deploy.public_id,
                    "sha": deploy.sha,
                    "changed_files": deploy.changed_files,
                },
                "correlated_failures": [
                    {"failure_id": item.failure_id, "deploy_sha": item.deploy_sha}
                    for item in correlations
                ],
                "uploaded_source_maps": uploaded_maps,
                "skipped_source_maps": skipped_maps,
            },
            indent=2,
        )
    )


@api_group.command("upload-source-map")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--project-id", default="", help="Project ID override.")
@click.option("--environment-id", default="", help="Environment ID override.")
@click.option("--release", required=True, help="Release or commit SHA.")
@click.option("--dist", default="", help="Optional release distribution.")
@click.option("--artifact-url", required=True, help="Generated JS URL or path.")
@click.argument("source_map_path", type=click.Path(path_type=Path, exists=True))
def api_upload_source_map(
    config_path: Path,
    project_id: str,
    environment_id: str,
    release: str,
    dist: str,
    artifact_url: str,
    source_map_path: Path,
) -> None:
    """Upload a Source Map v3 file for app-error stack mapping."""
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")
    pid = project_id.strip() or workspace.project_id
    eid = environment_id.strip() or workspace.environment_id
    try:
        source_map = json.loads(source_map_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise click.ClickException(f"failed to read source map: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"invalid source map JSON: {exc}") from exc
    if not isinstance(source_map, dict):
        raise click.ClickException("source map must be a JSON object")
    try:
        row = upload_source_map(
            store=store,
            project_id=pid,
            environment_id=eid,
            release=release,
            dist=dist,
            artifact_url=artifact_url,
            source_map=source_map,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        json.dumps(
            {
                "source_map": {
                    "id": row.id,
                    "public_id": row.public_id,
                    "release": row.release,
                    "dist": row.dist,
                    "artifact_url": row.artifact_url,
                }
            },
            indent=2,
        )
    )


@api_group.command("resolve-issue")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--project-id", default="", help="Project ID override.")
@click.option("--environment-id", default="", help="Environment ID override.")
@click.argument("issue_id")
def api_resolve_issue(
    config_path: Path,
    project_id: str,
    environment_id: str,
    issue_id: str,
) -> None:
    """Mark a replay-backed issue resolved and fire issue.resolved notifications."""
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")
    issue = store.get_replay_issue(
        project_id=project_id.strip() or workspace.project_id,
        environment_id=environment_id.strip() or workspace.environment_id,
        issue_id=issue_id,
    )
    if issue is None:
        raise click.ClickException(f"Replay issue not found: {issue_id}")
    success = store.transition_replay_issue(str(issue["id"]), status="resolved")
    public_id = str(issue["public_id"])
    if success and cfg.notifications.enabled:
        sinks = build_sinks_from_config(cfg.notifications)
        try:
            dispatch_notification(
                sinks,
                NotificationPayload(
                    event=NotificationEvent.ISSUE_RESOLVED.value,
                    title=str(issue["title"] or "Replay issue"),
                    summary=str(issue["summary"] or ""),
                    severity=str(issue["severity"] or ""),
                    public_id=public_id,
                ),
            )
        finally:
            close_sinks(sinks)
    click.echo(
        json.dumps(
            {
                "issue_id": str(issue["id"]),
                "public_id": public_id,
                "resolved": success,
            },
            indent=2,
        )
    )
