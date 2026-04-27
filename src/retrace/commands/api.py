from __future__ import annotations

import json
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
from retrace.sdk_keys import create_sdk_key, create_service_token
from retrace.storage import Storage


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


def _handler(store: Storage) -> type[BaseHTTPRequestHandler]:
    class RetraceAPIHandler(BaseHTTPRequestHandler):
        server_version = "retrace-api/0.1"

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                _json_response(self, 200, {"ok": True})
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
            if parsed.path != "/api/sdk/replay":
                _json_response(self, 404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
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
                    query={
                        k: v[-1]
                        for k, v in parse_qs(parsed.query, keep_blank_values=True).items()
                    },
                )
                _json_response(self, 202, result)
            except ReplayIngestError as exc:
                _json_response(
                    self,
                    exc.status,
                    {"error": exc.code, "message": exc.message},
                )
            except Exception as exc:
                _json_response(
                    self,
                    500,
                    {"error": "internal_error", "message": str(exc)},
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
