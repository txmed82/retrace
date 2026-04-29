from __future__ import annotations

import json
import logging
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import click

from retrace.config import load_config
from retrace.issue_sink_clients import GitHubClient, IssueSinkError, LinearClient
from retrace.issue_sinks import promote_replay_issue
from retrace.notification_sinks import (
    NotificationEvent,
    NotificationPayload,
    build_sinks_from_config,
    close_sinks,
    dispatch_notification,
)
from retrace.observability import collect_local_observability, record_api_request
from retrace.replay_api import (
    MAX_REPLAY_BODY_BYTES,
    ReplayIngestError,
    ingest_replay_request,
)
from retrace.replay_core import process_queued_replay_jobs
from retrace.sdk_keys import (
    authenticate_service_token,
    create_sdk_key,
    create_service_token,
)
from retrace.storage import Storage


logger = logging.getLogger(__name__)


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    setattr(handler, "_retrace_response_status", status)
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    trace_id = str(getattr(handler, "_retrace_trace_id", "") or "")
    if trace_id:
        handler.send_header("X-Retrace-Trace-Id", trace_id)
    _cors_headers(handler)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _cors_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header(
        "Access-Control-Allow-Headers",
        "authorization, content-encoding, content-type, x-retrace-key",
    )
    handler.send_header("Access-Control-Max-Age", "86400")


def _query_dict(query: str) -> dict[str, str]:
    return {k: v[-1] for k, v in parse_qs(query, keep_blank_values=True).items()}


def _bearer_token(headers: Any) -> str:
    auth = str(headers.get("Authorization") or headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _require_service_token(
    handler: BaseHTTPRequestHandler,
    store: Storage,
    *,
    scopes: set[str],
):
    token = authenticate_service_token(store, _bearer_token(handler.headers))
    if token is None:
        _json_response(
            handler,
            401,
            {"error": "unauthorized", "message": "Missing or invalid service token."},
        )
        return None
    if scopes and not scopes.intersection(set(token.scopes)):
        _json_response(
            handler,
            403,
            {"error": "forbidden", "message": "Service token lacks the required scope."},
        )
        return None
    return token


def _row_dict(row: Any, *, include_payload: bool = False) -> dict[str, Any]:
    out = {k: row[k] for k in row.keys()}
    for key in (
        "metadata_json",
        "preview_json",
        "signal_summary_json",
        "reproduction_steps_json",
    ):
        if key in out:
            try:
                out[key.removesuffix("_json")] = json.loads(out[key] or "{}")
            except json.JSONDecodeError:
                out[key.removesuffix("_json")] = {} if key != "reproduction_steps_json" else []
            del out[key]
    if not include_payload and "payload_json" in out:
        del out["payload_json"]
    return out


def _handler(store: Storage) -> type[BaseHTTPRequestHandler]:
    class RetraceAPIHandler(BaseHTTPRequestHandler):
        server_version = "retrace-api/0.1"

        def handle_one_request(self) -> None:
            self._retrace_trace_id = uuid.uuid4().hex
            self._retrace_response_status = 500
            started = time.perf_counter()
            try:
                super().handle_one_request()
            finally:
                latency_ms = (time.perf_counter() - started) * 1000
                method = str(getattr(self, "command", "") or "")
                path = urlsplit(str(getattr(self, "path", "") or "")).path
                status = int(getattr(self, "_retrace_response_status", 500))
                if method and path:
                    record_api_request(
                        method=method,
                        path=path,
                        status=status,
                        latency_ms=latency_ms,
                        trace_id=self._retrace_trace_id,
                    )
                    logger.info(
                        json.dumps(
                            {
                                "event": "api_request",
                                "trace_id": self._retrace_trace_id,
                                "method": method,
                                "path": path,
                                "status": status,
                                "latency_ms": round(latency_ms, 3),
                            },
                            separators=(",", ":"),
                        )
                    )

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path == "/healthz":
                _json_response(self, 200, {"ok": True})
                return
            if parsed.path == "/api/metrics":
                self._handle_metrics()
                return
            if parsed.path == "/api/replays":
                self._handle_list_replays(parsed.query)
                return
            if parsed.path.startswith("/api/replays/"):
                replay_id = parsed.path.removeprefix("/api/replays/").strip("/")
                self._handle_get_replay(replay_id, parsed.query)
                return
            if parsed.path == "/api/issues":
                self._handle_list_issues(parsed.query)
                return
            _json_response(self, 404, {"error": "not_found"})

        def do_OPTIONS(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path != "/api/sdk/replay":
                _json_response(self, 404, {"error": "not_found"})
                return
            self._retrace_response_status = 204
            self.send_response(204)
            trace_id = str(getattr(self, "_retrace_trace_id", "") or "")
            if trace_id:
                self.send_header("X-Retrace-Trace-Id", trace_id)
            _cors_headers(self)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path == "/api/replays/process":
                self._handle_process_replays()
                return
            if parsed.path != "/api/sdk/replay":
                _json_response(self, 404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            if length < 0:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            if length > MAX_REPLAY_BODY_BYTES:
                _json_response(
                    self,
                    413,
                    {
                        "error": "body_too_large",
                        "message": "Replay batch is too large.",
                    },
                )
                return
            try:
                body = self.rfile.read(length)
                result = ingest_replay_request(
                    store=store,
                    headers={k: v for k, v in self.headers.items()},
                    body=body,
                    query=_query_dict(parsed.query),
                )
                _json_response(self, 202, result)
            except ReplayIngestError as exc:
                _json_response(
                    self,
                    exc.status,
                    {"error": exc.code, "message": exc.message},
                )
            except Exception:
                logger.exception("Unhandled replay ingest error")
                _json_response(
                    self,
                    500,
                    {
                        "error": "internal_error",
                        "message": "An internal server error occurred.",
                    },
                )

        def _handle_list_replays(self, query: str) -> None:
            token = _require_service_token(
                self, store, scopes={"replay:read", "mcp:read", "admin"}
            )
            if token is None:
                return
            params = _query_dict(query)
            environment_id = str(params.get("environment_id") or "").strip()
            if not environment_id:
                _json_response(
                    self,
                    400,
                    {
                        "error": "missing_environment_id",
                        "message": "environment_id is required.",
                    },
                )
                return
            status = str(params.get("status") or "").strip() or None
            try:
                limit = int(params.get("limit") or "100")
            except ValueError:
                _json_response(self, 400, {"error": "invalid_limit"})
                return
            rows = store.list_replay_sessions(
                project_id=token.project_id,
                environment_id=environment_id,
                status=status,
                limit=limit,
            )
            _json_response(
                self,
                200,
                {
                    "project_id": token.project_id,
                    "environment_id": environment_id,
                    "sessions": [_row_dict(r) for r in rows],
                },
            )

        def _handle_metrics(self) -> None:
            token = _require_service_token(
                self, store, scopes={"admin", "mcp:read"}
            )
            if token is None:
                return
            _json_response(self, 200, collect_local_observability(store).to_dict())

        def _handle_process_replays(self) -> None:
            token = _require_service_token(
                self, store, scopes={"replay:write", "admin"}
            )
            if token is None:
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            body = self.rfile.read(max(0, length)) if length else b"{}"
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "invalid_json"})
                return
            try:
                limit = int(payload.get("limit") or 25)
            except (TypeError, ValueError):
                _json_response(self, 400, {"error": "invalid_limit"})
                return
            result = process_queued_replay_jobs(
                store=store,
                limit=limit,
                project_id=token.project_id,
            )
            _json_response(
                self,
                200,
                {
                    "jobs_seen": result.jobs_seen,
                    "jobs_processed": result.jobs_processed,
                    "jobs_failed": result.jobs_failed,
                    "sessions_processed": result.sessions_processed,
                    "issues_created_or_updated": result.issues_created_or_updated,
                    "project_id": token.project_id,
                },
            )

        def _handle_get_replay(self, replay_id: str, query: str) -> None:
            token = _require_service_token(
                self, store, scopes={"replay:read", "mcp:read", "admin"}
            )
            if token is None:
                return
            params = _query_dict(query)
            environment_id = str(params.get("environment_id") or "").strip()
            if not environment_id:
                _json_response(self, 400, {"error": "missing_environment_id"})
                return
            replay_id = replay_id.strip()
            if not replay_id:
                _json_response(self, 404, {"error": "not_found"})
                return
            playback = store.get_replay_playback(
                project_id=token.project_id,
                environment_id=environment_id,
                replay_id=replay_id,
            )
            if playback is None:
                _json_response(self, 404, {"error": "not_found"})
                return
            _json_response(
                self,
                200,
                {
                    "session": _row_dict(playback.session),
                    "batches": [_row_dict(b) for b in playback.batches],
                    "events": playback.events,
                },
            )

        def _handle_list_issues(self, query: str) -> None:
            token = _require_service_token(
                self, store, scopes={"issues:read", "mcp:read", "admin"}
            )
            if token is None:
                return
            params = _query_dict(query)
            environment_id = str(params.get("environment_id") or "").strip()
            if not environment_id:
                _json_response(self, 400, {"error": "missing_environment_id"})
                return
            status = str(params.get("status") or "").strip() or None
            rows = store.list_replay_issues(
                project_id=token.project_id,
                environment_id=environment_id,
                status=status,
            )
            _json_response(
                self,
                200,
                {
                    "project_id": token.project_id,
                    "environment_id": environment_id,
                    "issues": [_row_dict(r) for r in rows],
                },
            )

        def log_message(self, format: str, *args: object) -> None:
            click.echo(f"{self.address_string()} - {format % args}", err=True)

    return RetraceAPIHandler


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
def api_create_sdk_key(
    config_path: Path,
    org: str,
    project_name: str,
    environment: str,
    name: str,
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
    httpd = ThreadingHTTPServer((host, port), _handler(store))
    click.echo(f"Retrace API running at http://{host}:{port}")
    click.echo("Replay ingest endpoint: POST /api/sdk/replay")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        click.echo("Stopping Retrace API")
    finally:
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
def api_process_replays(config_path: Path, limit: int) -> None:
    """Process queued final replay batches into signals and issues."""
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    result = process_queued_replay_jobs(store=store, limit=limit)
    if (
        result.issues_inserted or result.issues_regressed
    ) and cfg.notifications.enabled:
        sinks = build_sinks_from_config(cfg.notifications)
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
                        extra={"public_ids": list(result.inserted_public_ids)},
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
                        extra={"public_ids": list(result.regressed_public_ids)},
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
                    extra={"provider": result.provider},
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

    rows = _list_ticketed_issues(store, project_id=pid, environment_id=eid, limit=limit)

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


def _list_ticketed_issues(
    store: Storage, *, project_id: str, environment_id: str, limit: int
) -> list[Any]:
    with store._conn() as conn:  # type: ignore[attr-defined]
        return conn.execute(
            """
            SELECT * FROM replay_issues
            WHERE project_id = ? AND environment_id = ?
              AND external_ticket_id IS NOT NULL AND external_ticket_id != ''
              AND status != 'resolved'
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (project_id, environment_id, max(1, int(limit))),
        ).fetchall()


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
    repo, _, number_str = ticket_id.partition("#")
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

    specs_by_issue: dict[str, Any] = {}
    for spec in list_specs(specs_dir):
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
        spec = specs_by_issue.get(public_id)
        plan.append(
            {
                "public_id": public_id,
                "issue_id": str(row["id"]),
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
            spec = specs_by_issue[entry["public_id"]]
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
