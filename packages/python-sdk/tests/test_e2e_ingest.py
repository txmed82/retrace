"""End-to-end: SDK envelope → Retrace's Sentry-compat ingest → DB row.

This skips the network entirely by calling `ingest_sentry_compat_request`
directly with the envelope bytes the transport would have POSTed. That
gives us a real, deterministic check that the SDK's wire format is
acceptable to the server side — without an HTTP server in the test loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# These imports cross into the main `retrace` package, which is only
# available when this test runs from the monorepo root. The package
# also has a separate stand-alone test invocation that doesn't include
# this file (selected by the `e2e` mark).
pytest.importorskip("retrace")


def _make_workspace(store) -> tuple[str, str, str]:
    """Create a workspace + SDK key; return (project_id, environment_id, public_key)."""
    from retrace.sdk_keys import create_sdk_key

    ws = store.ensure_workspace(project_name="SDK e2e")
    created = create_sdk_key(
        store,
        project_id=ws.project_id,
        environment_id=ws.environment_id,
        name="sdk-e2e",
    )
    return ws.project_id, ws.environment_id, created.key


@pytest.mark.e2e
def test_sdk_envelope_round_trips_to_qa_incident(tmp_path: Path):
    """The exact bytes our transport would POST get accepted by the
    ingest function and produce a stored app-error incident."""
    from retrace.sentry_compat import ingest_sentry_compat_request
    from retrace.storage import Storage

    from retrace_sdk.client import Client
    from retrace_sdk.transport import Transport

    db_path = tmp_path / "retrace.db"
    store = Storage(db_path)
    store.init_schema()
    project_id, _env_id, public_key = _make_workspace(store)

    # Capture envelope bytes with a sender that records but doesn't send.
    captured: list[bytes] = []

    def _record(url: str, headers: dict[str, str], body: bytes) -> None:
        captured.append(body)

    transport = Transport(
        url=f"http://127.0.0.1:0/api/sentry/{project_id}/envelope/",
        public_key=public_key,
        sender=_record,
    )
    try:
        client = Client(
            dsn=f"http://{public_key}@127.0.0.1:0/{project_id}",
            release="e2e-rel",
            environment="e2e",
            transport=transport,
        )
        try:
            raise ValueError("e2e-boom")
        except ValueError as exc:
            client.capture_exception(exc)
        client.flush(timeout=2.0)
    finally:
        transport.shutdown(timeout=2.0)

    assert len(captured) == 1
    body = captured[0]

    # Hand the same bytes to the server-side ingest. If wire format
    # ever drifts on either side, this is the canary.
    resp = ingest_sentry_compat_request(
        store=store,
        project_id=project_id,
        endpoint="envelope",
        headers={
            "Content-Type": "application/x-sentry-envelope",
            "X-Sentry-Auth": f"Sentry sentry_key={public_key}",
        },
        body=body,
    )
    assert resp.accepted
    assert resp.event_count == 1
