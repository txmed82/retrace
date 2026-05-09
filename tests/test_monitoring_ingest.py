from __future__ import annotations

from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread

from retrace.commands.api import _handler
from retrace.monitoring_ingest import ingest_monitoring_webhook
from retrace.sdk_keys import create_sdk_key, create_service_token
from retrace.sentry_compat import parse_sentry_envelope
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


def test_sentry_webhook_creates_and_dedupes_failure(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    payload = {
        "data": {
            "event": {
                "event_id": "evt-1",
                "title": "TypeError: failed checkout",
                "level": "error",
                "timestamp": "2026-05-09T05:00:00Z",
                "culprit": "checkout.submit",
                "fingerprint": ["checkout", "type-error"],
                "contexts": {"trace": {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"}},
                "exception": {
                    "values": [
                        {
                            "type": "TypeError",
                            "value": "Cannot read properties of undefined",
                            "stacktrace": {
                                "frames": [
                                    {
                                        "filename": "src/checkout.ts",
                                        "function": "submit",
                                        "lineno": 42,
                                    }
                                ]
                            },
                        }
                    ]
                },
            },
            "issue": {"id": "ISSUE-1", "title": "Checkout failure"},
        }
    }

    first = ingest_monitoring_webhook(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        provider="sentry",
        payload=payload,
    )
    second = ingest_monitoring_webhook(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        provider="sentry",
        payload=payload,
    )

    failures = store.list_failures(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        source_type="monitor_incident",
    )
    assert first.created is True
    assert second.created is False
    assert second.failure_id == first.failure_id
    assert len(failures) == 1
    assert failures[0].source_external_id == "sentry:evt-1"
    assert failures[0].severity == "high"
    assert failures[0].metadata["trace_ids"] == ["4bf92f3577b34da6a3ce929d0e0e4736"]
    evidence = store.list_failure_evidence(failure_id=first.failure_id)
    assert len(evidence) == 1
    assert evidence[0].redaction_state == "sensitive"
    assert evidence[0].occurred_at_ms == 1_778_302_800_000
    assert evidence[0].payload["top_stack_frame"] == "src/checkout.ts:submit:42"


def test_raw_sentry_sdk_events_group_into_one_incident(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    base_event = {
        "title": "TypeError: checkout failed",
        "level": "error",
        "transaction": "/checkout",
        "exception": {
            "values": [
                {
                    "type": "TypeError",
                    "value": "Cannot read properties of undefined",
                    "stacktrace": {
                        "frames": [
                            {
                                "filename": "src/checkout.ts",
                                "function": "submit",
                                "lineno": 42,
                            }
                        ]
                    },
                }
            ]
        },
    }

    first = ingest_monitoring_webhook(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        provider="sentry",
        payload={"event": {"event_id": "evt-sdk-1", **base_event}},
    )
    second = ingest_monitoring_webhook(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        provider="sentry",
        payload={"event": {"event_id": "evt-sdk-2", **base_event}},
    )

    failures = store.list_failures(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        source_type="monitor_incident",
    )
    assert len(failures) == 2
    assert first.failure_id != second.failure_id
    assert first.incident_id == second.incident_id
    assert failures[0].metadata["grouping_fingerprint"]
    assert failures[0].metadata["top_stack_frame"] == "src/checkout.ts:submit:42"


def test_sentry_envelope_parser_accepts_length_delimited_event() -> None:
    event = {"event_id": "evt-envelope-1", "level": "error", "message": "boom"}
    event_bytes = json.dumps(event, separators=(",", ":")).encode("utf-8")
    body = b"\n".join(
        [
            b'{"dsn":"https://rtpk_example@retrace.local/123"}',
            json.dumps({"type": "event", "length": len(event_bytes)}).encode("utf-8"),
            event_bytes,
        ]
    )

    assert parse_sentry_envelope(body) == [event]


def test_posthog_exception_webhook_creates_and_dedupes_failure(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    payload = {
        "event": "$exception",
        "uuid": "event-1",
        "properties": {
            "$exception_fingerprint": "posthog-fp-1",
            "$exception_type": "ReferenceError",
            "$exception_message": "cartTotal is not defined",
            "$current_url": "https://example.com/cart",
            "$trace_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        },
    }

    first = ingest_monitoring_webhook(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        provider="posthog",
        payload=payload,
    )
    second = ingest_monitoring_webhook(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        provider="posthog",
        payload=payload,
    )

    failures = store.list_failures(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        source_type="monitor_incident",
    )
    assert first.created is True
    assert second.created is False
    assert second.failure_id == first.failure_id
    assert len(failures) == 1
    assert failures[0].source_external_id == "posthog:posthog-fp-1"
    assert failures[0].title == "ReferenceError: cartTotal is not defined"
    assert failures[0].metadata["trace_ids"] == ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]


def test_monitoring_webhook_endpoint_ingests_sentry_payload(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="Ingest",
        scopes=["monitoring:write"],
    )
    body = json.dumps(
        {
            "event": {
                "event_id": "evt-api-1",
                "title": "RangeError in signup",
                "level": "fatal",
            }
        }
    ).encode("utf-8")

    with _server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            f"/api/monitoring/webhook/sentry?environment_id={workspace.environment_id}",
            body=body,
            headers={
                "Authorization": f"Bearer {service.token}",
                "Content-Type": "application/json",
            },
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 202
    assert payload["provider"] == "sentry"
    assert payload["external_id"] == "evt-api-1"
    failure = store.find_failure_by_source(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        source_type="monitor_incident",
        source_external_id="sentry:evt-api-1",
    )
    assert failure is not None
    assert failure.severity == "critical"


def test_sentry_store_endpoint_ingests_sdk_event_with_query_key(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    sdk = create_sdk_key(
        store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        name="Browser",
    )
    body = json.dumps(
        {
            "event_id": "evt-store-1",
            "title": "ReferenceError in settings",
            "level": "error",
            "timestamp": "2026-05-09T06:00:00Z",
            "contexts": {"trace": {"trace_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}},
        }
    ).encode("utf-8")

    with _server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            f"/api/sentry/{workspace.project_id}/store/?sentry_key={sdk.key}",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 202
    assert payload["accepted"] is True
    assert payload["event_count"] == 1
    assert payload["results"][0]["external_id"] == "evt-store-1"
    failure = store.find_failure_by_source(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        source_type="monitor_incident",
        source_external_id="sentry:evt-store-1",
    )
    assert failure is not None
    assert failure.metadata["trace_ids"] == ["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"]


def test_sentry_envelope_endpoint_accepts_x_sentry_auth(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    sdk = create_sdk_key(
        store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        name="Browser",
    )
    event = {
        "event_id": "evt-envelope-api-1",
        "title": "TypeError in profile",
        "level": "fatal",
    }
    body = (
        b'{"sent_at":"2026-05-09T06:00:00Z"}\n'
        b'{"type":"event"}\n'
        + json.dumps(event).encode("utf-8")
        + b"\n"
    )

    with _server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            f"/api/sentry/{workspace.project_id}/envelope/",
            body=body,
            headers={
                "Content-Type": "application/x-sentry-envelope",
                "X-Sentry-Auth": f"Sentry sentry_version=7,sentry_key={sdk.key}",
            },
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 202
    assert payload["event_count"] == 1
    assert payload["results"][0]["external_id"] == "evt-envelope-api-1"


def test_sentry_envelope_endpoint_accepts_dsn_key_fallback(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    sdk = create_sdk_key(
        store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        name="Browser",
    )
    event = {
        "event_id": "evt-envelope-dsn-1",
        "title": "TypeError in billing",
        "level": "error",
    }
    body = (
        json.dumps({"dsn": f"https://{sdk.key}@retrace.local/{workspace.project_id}"}).encode(
            "utf-8"
        )
        + b"\n"
        + b'{"type":"event"}\n'
        + json.dumps(event).encode("utf-8")
        + b"\n"
    )

    with _server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            f"/api/sentry/{workspace.project_id}/envelope/",
            body=body,
            headers={"Content-Type": "application/x-sentry-envelope"},
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 202
    assert payload["event_count"] == 1
    assert payload["results"][0]["external_id"] == "evt-envelope-dsn-1"


def test_sentry_endpoint_rejects_project_mismatch(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    sdk = create_sdk_key(
        store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        name="Browser",
    )

    with _server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            f"/api/sentry/not-{workspace.project_id}/store/?sentry_key={sdk.key}",
            body=json.dumps({"event_id": "evt-nope"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 403
    assert payload["error"] == "forbidden"


def test_sentry_endpoint_rejects_extra_path_segments(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    sdk = create_sdk_key(
        store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        name="Browser",
    )

    with _server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            f"/api/sentry/{workspace.project_id}/store/extra?sentry_key={sdk.key}",
            body=json.dumps({"event_id": "evt-extra-path"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 404
    assert payload["error"] == "not_found"


def test_monitoring_webhook_endpoint_rejects_empty_payload(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="Ingest",
        scopes=["monitoring:write"],
    )

    with _server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            f"/api/monitoring/webhook/sentry?environment_id={workspace.environment_id}",
            body=json.dumps({}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {service.token}",
                "Content-Type": "application/json",
            },
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 400
    assert payload["error"] == "invalid_payload"
