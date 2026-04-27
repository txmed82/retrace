from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import click

from retrace.config import load_config
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
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
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
    for key in ("metadata_json", "signal_summary_json", "reproduction_steps_json"):
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

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path == "/healthz":
                _json_response(self, 200, {"ok": True})
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
            self.send_response(204)
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
