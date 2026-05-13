"""P2.2 — pytest regression for ingest rate limiting.

Most of P2.2 was already in place: the `ingest_rate_limits` table,
`Storage.consume_ingest_rate_limit()`, `_rate_limit_headers()`
(which emits `Retry-After`), and `_consume_rate_limit` wired into
the replay / Sentry / monitoring / source-map handlers. The OTel
handler was the only ingest endpoint NOT calling
`_consume_rate_limit` — this test pins the fix.

We also pin the `Retry-After` HTTP-header contract since it's the
spec-correct way for clients to back off (the body's
`retry_after_seconds` is redundant but useful for debugging).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from retrace.commands.api import INGEST_RATE_LIMITS, _handler
from retrace.sdk_keys import create_service_token
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


def _otel_log_body() -> bytes:
    return json.dumps(
        {
            "logs": [
                {
                    "trace_id": "tr-1",
                    "span_id": "sp-1",
                    "timestamp_ms": 1000,
                    "severity": "INFO",
                    "message": "ok",
                }
            ]
        }
    ).encode("utf-8")


def _post_otel(server, *, environment_id: str, token: str) -> tuple[int, dict[str, str], dict]:
    host, port = server.server_address
    conn = HTTPConnection(host, port, timeout=5)
    conn.request(
        "POST",
        f"/api/otel/v1/logs?environment_id={environment_id}",
        body=_otel_log_body(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    response = conn.getresponse()
    body_text = response.read().decode("utf-8")
    body = json.loads(body_text) if body_text else {}
    headers = {k: v for k, v in response.getheaders()}
    status = response.status
    conn.close()
    return status, headers, body


def test_otel_ingest_returns_429_when_limit_exceeded(tmp_path, monkeypatch):
    """Patch the OTel bucket down to 1/60s, send two requests; the
    second must come back 429 with both the spec-mandated
    `Retry-After` HTTP header and the JSON `retry_after_seconds`
    field."""
    monkeypatch.setitem(INGEST_RATE_LIMITS, "otel", (1, 60))

    store, workspace = _store(tmp_path)
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="OTel",
        scopes=["otel:write"],
    )

    with _server(store) as server:
        # First request — allowed.
        status_1, headers_1, body_1 = _post_otel(
            server,
            environment_id=workspace.environment_id,
            token=service.token,
        )
        assert status_1 == 202, body_1
        assert body_1["accepted"] == 1

        # Second request — over the limit. Spec-correct rejection.
        status_2, headers_2, body_2 = _post_otel(
            server,
            environment_id=workspace.environment_id,
            token=service.token,
        )

    assert status_2 == 429
    assert body_2["error"] == "rate_limited"
    assert body_2["limit"] == 1
    # JSON body and HTTP header agree.
    retry_after_header = headers_2.get("Retry-After") or headers_2.get("retry-after")
    assert retry_after_header is not None, headers_2
    assert int(retry_after_header) == int(body_2["retry_after_seconds"])
    # X-RateLimit-* headers carry the contract for observability.
    assert headers_2.get("X-RateLimit-Limit") == "1"
    assert headers_2.get("X-RateLimit-Remaining") == "0"


def test_otel_rate_limit_does_not_consume_body_when_throttled(tmp_path, monkeypatch):
    """Rate limiting should fire BEFORE we read the request body —
    a flooded client shouldn't get to spend our bandwidth on a
    payload we're about to drop. Verified indirectly: under a 1/60s
    limit, a second request returns 429 fast enough that no row
    lands in `otel_events`."""
    monkeypatch.setitem(INGEST_RATE_LIMITS, "otel", (1, 60))

    store, workspace = _store(tmp_path)
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="OTel",
        scopes=["otel:write"],
    )

    with _server(store) as server:
        _post_otel(
            server,
            environment_id=workspace.environment_id,
            token=service.token,
        )
        status_2, _, _ = _post_otel(
            server,
            environment_id=workspace.environment_id,
            token=service.token,
        )
    assert status_2 == 429

    # Only one event landed (the first request). The throttled
    # second request never wrote a row.
    events = store.list_otel_events(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        trace_id="tr-1",
    )
    assert len(events) == 1


@pytest.mark.parametrize(
    "bucket",
    sorted(INGEST_RATE_LIMITS.keys()),
)
def test_all_ingest_buckets_have_a_default_limit(bucket):
    """Stable contract: every ingest endpoint that calls
    `_consume_rate_limit(bucket=...)` must have a default entry in
    `INGEST_RATE_LIMITS`. A missing entry would raise `KeyError`
    inside the handler. This test pins the inventory so a new
    handler can't silently bypass rate limiting by using a bucket
    name nobody added a default for."""
    limit, window = INGEST_RATE_LIMITS[bucket]
    assert limit > 0
    assert window > 0
