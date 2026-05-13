"""P3.3 — shared e2e fixtures.

The `live_api` fixture spins up `retrace api serve`'s actual
`_handler` against a fresh sqlite store on an ephemeral port,
yields the connection details, and tears down cleanly.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from retrace.commands.api import _handler
from retrace.sdk_keys import create_sdk_key, create_service_token
from retrace.storage import Storage


@dataclass
class LiveAPI:
    """Connection details for the in-process API server."""

    base_url: str
    sdk_key: str
    service_token: str
    project_id: str
    environment_id: str
    store: Storage


@contextmanager
def _server(store: Storage):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(store))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def live_api(tmp_path: Path):
    """Start `retrace api serve` against a fresh sqlite store.

    Yields a `LiveAPI` with everything an e2e scenario needs:
    base URL, a usable SDK key, a service token with broad
    scopes, and the workspace ids the keys are scoped to.
    """
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(
        org_name="E2E",
        project_name="Web",
        environment_name="production",
    )
    sdk = create_sdk_key(
        store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        name="e2e",
    )
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="e2e-service",
        scopes=["ingest", "otel:write", "replay:read", "admin"],
    )
    with _server(store) as server:
        host, port = server.server_address
        yield LiveAPI(
            base_url=f"http://{host}:{port}",
            sdk_key=sdk.key,
            service_token=service.token,
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            store=store,
        )
