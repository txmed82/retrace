"""Shared SDK test fixtures.

`make_client` returns a `Client` wired to an in-memory `FakeTransport`
that records every envelope it would have sent. Tests assert against
that record instead of standing up an HTTP server.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from retrace_sdk import set_client
from retrace_sdk.client import Client
from retrace_sdk.envelope import build_envelope_bytes  # noqa: F401  (re-export hint)
from retrace_sdk.scope import Scope
from retrace_sdk.transport import Transport


class FakeTransport(Transport):
    """A `Transport` that records its outgoing envelopes instead of
    POSTing them. Inherits real queue/worker semantics so tests catch
    threading bugs (drops, races, atexit flush)."""

    def __init__(self, **kwargs: Any) -> None:
        self.records: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        # Force an extremely-short HTTP timeout so any accidental real
        # network call would surface fast rather than hanging the test.
        kwargs.setdefault("http_timeout", 0.05)
        kwargs.setdefault("queue_size", 50)

        def _capture(url: str, headers: dict[str, str], body: bytes) -> None:
            with self._lock:
                self.records.append({"url": url, "headers": dict(headers), "body": bytes(body)})

        kwargs["sender"] = _capture
        super().__init__(url="http://recorded/", public_key="rtpk_fake", **kwargs)

    @property
    def sent(self) -> list[dict[str, Any]]:
        return list(self.records)


@pytest.fixture
def fake_transport() -> FakeTransport:
    transport = FakeTransport()
    yield transport
    transport.shutdown(timeout=1.0)


@pytest.fixture
def client_factory(fake_transport: FakeTransport):
    """Build a `Client` with the fake transport pre-wired.

    Use this from tests rather than `retrace_sdk.init()` so the test
    keeps total control over its lifecycle and we don't leak globals."""
    created: list[Client] = []

    def _make(**overrides: Any) -> Client:
        defaults: dict[str, Any] = {
            "dsn": "http://rtpk_fake@127.0.0.1:8788/proj_test",
            "release": "test-release",
            "environment": "test",
            "transport": fake_transport,
        }
        defaults.update(overrides)
        client = Client(**defaults)
        created.append(client)
        return client

    yield _make
    for c in created:
        try:
            c.close(timeout=1.0)
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _reset_global_state():
    """Clear scope + active client between tests so cross-test leakage
    can't mask real bugs."""
    Scope.replace_current(Scope())
    set_client(None)
    yield
    Scope.replace_current(Scope())
    set_client(None)
