"""P3.6 (scaffold) — server-side replay storage + ingest tests.

Pins the storage seam and the HTTP ingest contract so the eventual
capture middleware has a stable target. Does NOT test capture-side
logic (Node/Python middleware) — that's the deferred follow-up.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from retrace.commands.api import INGEST_RATE_LIMITS, _handler
from retrace.sdk_keys import create_sdk_key
from retrace.storage import Storage, WorkspaceIds


def _store(tmp_path: Path) -> tuple[Storage, WorkspaceIds]:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(
        org_name="Acme",
        project_name="Web",
        environment_name="production",
    )
    return store, workspace


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


def _payload(**overrides) -> dict:
    base = {
        "session_id": "sess-1",
        "request": {
            "method": "GET",
            "path": "/api/checkout",
            "headers": {"X-Trace": "abc"},
            "body": "",
        },
        "response": {"status": 500, "headers": {"Content-Type": "text/html"}},
        "rendered_html": "<html><body>boom</body></html>",
        "runtime": "node-20",
        "occurred_at_ms": 1000,
        "error_summary": "checkout failed",
        "metadata": {"region": "us-east-1"},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Storage round-trip
# ---------------------------------------------------------------------------


def test_insert_and_fetch(tmp_path):
    store, workspace = _store(tmp_path)
    row_id = store.insert_server_replay_session(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-1",
        request_method="POST",
        request_path="/api/login",
        request_headers={"Content-Type": "application/json"},
        request_body_text='{"email":"x@y"}',
        response_status=500,
        response_headers={"X-Trace": "tr1"},
        rendered_html_snippet="<html>err</html>",
        runtime="python-3.13",
        occurred_at_ms=12345,
        error_summary="db timeout",
        metadata={"req_id": "r1"},
    )
    row = store.get_server_replay_session(row_id)
    assert row is not None
    assert row["session_id"] == "sess-1"
    assert row["request_method"] == "POST"
    assert row["request_path"] == "/api/login"
    assert row["response_status"] == 500
    assert row["runtime"] == "python-3.13"
    assert row["error_summary"] == "db timeout"
    headers = json.loads(row["request_headers_json"])
    assert headers["Content-Type"] == "application/json"
    metadata = json.loads(row["metadata_json"])
    assert metadata["req_id"] == "r1"


def test_list_orders_by_recency(tmp_path):
    store, workspace = _store(tmp_path)
    ids = []
    for i, ts in enumerate([100, 300, 200]):
        ids.append(
            store.insert_server_replay_session(
                project_id=workspace.project_id,
                environment_id=workspace.environment_id,
                session_id=f"sess-{i}",
                request_method="GET",
                request_path=f"/p/{i}",
                occurred_at_ms=ts,
            )
        )
    rows = store.list_server_replay_sessions(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )
    # Newest first by occurred_at_ms.
    assert [int(r["occurred_at_ms"]) for r in rows] == [300, 200, 100]


def test_list_path_prefix_filter(tmp_path):
    store, workspace = _store(tmp_path)
    for path in ("/api/auth/login", "/api/auth/logout", "/api/billing/checkout"):
        store.insert_server_replay_session(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            session_id="s",
            request_method="GET",
            request_path=path,
            occurred_at_ms=1,
        )
    rows = store.list_server_replay_sessions(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        path_prefix="/api/auth/",
    )
    paths = sorted(str(r["request_path"]) for r in rows)
    assert paths == ["/api/auth/login", "/api/auth/logout"]


# ---------------------------------------------------------------------------
# HTTP ingest
# ---------------------------------------------------------------------------


def _post(server, *, body: bytes, headers: dict) -> tuple[int, dict]:
    host, port = server.server_address
    conn = HTTPConnection(host, port, timeout=5)
    conn.request("POST", "/api/sdk/server-replay", body=body, headers=headers)
    resp = conn.getresponse()
    text = resp.read().decode("utf-8")
    conn.close()
    body_out = json.loads(text) if text else {}
    return resp.status, body_out


def test_ingest_happy_path(tmp_path):
    store, workspace = _store(tmp_path)
    key = create_sdk_key(
        store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        name="ssr",
    )
    with _server(store) as server:
        status, body = _post(
            server,
            body=json.dumps(_payload()).encode("utf-8"),
            headers={
                "X-Retrace-Key": key.key,
                "Content-Type": "application/json",
            },
        )
    assert status == 202, body
    assert body["accepted"] == 1
    assert body["id"]
    # Storage round-trip via the row id.
    row = store.get_server_replay_session(body["id"])
    assert row is not None
    assert row["request_path"] == "/api/checkout"
    assert row["error_summary"] == "checkout failed"


def test_ingest_unauthorized_without_key(tmp_path):
    store, _workspace = _store(tmp_path)
    with _server(store) as server:
        status, body = _post(
            server,
            body=json.dumps(_payload()).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
    assert status == 401
    assert body["error"] == "unauthorized"


def test_ingest_rejects_invalid_json(tmp_path):
    store, workspace = _store(tmp_path)
    key = create_sdk_key(
        store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        name="ssr",
    )
    with _server(store) as server:
        status, body = _post(
            server,
            body=b"{not valid json",
            headers={
                "X-Retrace-Key": key.key,
                "Content-Type": "application/json",
            },
        )
    assert status == 400
    assert body["error"] == "invalid_json"


def test_ingest_rejects_non_object_request_block(tmp_path):
    store, workspace = _store(tmp_path)
    key = create_sdk_key(
        store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        name="ssr",
    )
    bad_payload = _payload(request="not-an-object")
    with _server(store) as server:
        status, body = _post(
            server,
            body=json.dumps(bad_payload).encode("utf-8"),
            headers={
                "X-Retrace-Key": key.key,
                "Content-Type": "application/json",
            },
        )
    assert status == 400
    assert "must be objects" in body["message"]


def test_ingest_rate_limit_returns_429_with_retry_after(tmp_path, monkeypatch):
    monkeypatch.setitem(INGEST_RATE_LIMITS, "server_replay", (1, 60))
    store, workspace = _store(tmp_path)
    key = create_sdk_key(
        store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        name="ssr",
    )
    headers = {
        "X-Retrace-Key": key.key,
        "Content-Type": "application/json",
    }
    body_bytes = json.dumps(_payload()).encode("utf-8")
    with _server(store) as srv:
        # First request — allowed.
        s1, _ = _post(srv, body=body_bytes, headers=headers)
        assert s1 == 202
        # Second request — rate-limited.
        host, port = srv.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST", "/api/sdk/server-replay", body=body_bytes, headers=headers
        )
        resp = conn.getresponse()
        all_headers = {k.lower(): v for k, v in resp.getheaders()}
        resp.read()
        status = resp.status
        conn.close()
    assert status == 429
    assert "retry-after" in all_headers
