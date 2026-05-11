"""FastAPI integration smoke."""

from __future__ import annotations

import json
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")

# Framework imports MUST come after `importorskip` so collection works
# on systems without fastapi installed — hence E402 here is intentional.
from retrace_sdk import set_client  # noqa: E402
from retrace_sdk.integrations.fastapi import FastAPIIntegration  # noqa: E402


def _decode(body: bytes) -> dict[str, Any]:
    _, _, item_body = body.splitlines()
    return json.loads(item_body)


def test_fastapi_captures_unhandled_exception(client_factory, fake_transport):
    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    client = client_factory()
    set_client(client)

    app = FastAPI()

    @app.get("/boom")
    def _boom():
        raise RuntimeError("kaboom")

    FastAPIIntegration.attach(app)

    test_client = TestClient(app, raise_server_exceptions=False)
    # The middleware re-raises after capture; TestClient with
    # raise_server_exceptions=False returns a 500 instead of bubbling.
    resp = test_client.get("/boom")
    assert resp.status_code == 500

    client.flush(timeout=1.0)
    assert len(fake_transport.sent) == 1
    event = _decode(fake_transport.sent[0]["body"])
    assert event["exception"]["values"][0]["type"] == "RuntimeError"
    assert event["exception"]["values"][0]["value"] == "kaboom"
    assert event["transaction"] == "GET /boom"
    # Request breadcrumb attached.
    crumbs = event["breadcrumbs"]["values"]
    assert any(c.get("category") == "http" for c in crumbs)


def test_fastapi_attach_is_idempotent(client_factory):
    from fastapi import FastAPI

    client = client_factory()
    set_client(client)
    app = FastAPI()
    FastAPIIntegration.attach(app)
    FastAPIIntegration.attach(app)  # must not raise / duplicate
    # exactly one Retrace middleware mounted
    from retrace_sdk.integrations.fastapi import _RetraceASGIMiddleware

    retrace_mws = [m for m in app.user_middleware if m.cls is _RetraceASGIMiddleware]
    assert len(retrace_mws) == 1


def test_fastapi_request_without_active_client_passes_through(client_factory, fake_transport):
    """When the SDK is disabled, the middleware must be transparent."""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    set_client(None)
    app = FastAPI()
    FastAPIIntegration.attach(app)

    @app.get("/ok")
    def _ok():
        return {"ok": True}

    test_client = TestClient(app)
    assert test_client.get("/ok").json() == {"ok": True}
    assert fake_transport.sent == []
