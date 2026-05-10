from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from threading import Thread

import pytest

import retrace.commands.api as api_module
from retrace.commands.api import _handler
from retrace.notification_sinks import NotificationPayload
from retrace.monitoring_ingest import ingest_monitoring_webhook
from retrace.sdk_keys import create_sdk_key, create_service_token
from retrace.sentry_compat import parse_sentry_envelope
from retrace.source_maps import upload_source_map
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


class _CaptureSink:
    name = "capture"

    def __init__(self) -> None:
        self.payloads: list[NotificationPayload] = []

    def send(self, payload: NotificationPayload) -> object:
        self.payloads.append(payload)
        return type(
            "Result",
            (),
            {"ok": True, "sink": self.name, "target": "", "status_code": 200},
        )()


def _vlq(values: list[int]) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    encoded = ""
    for value in values:
        sign_bit = 1 if value < 0 else 0
        raw = (abs(value) << 1) | sign_bit
        while True:
            digit = raw & 31
            raw >>= 5
            if raw:
                digit |= 32
            encoded += alphabet[digit]
            if not raw:
                break
    return encoded


def _source_map() -> dict[str, object]:
    return {
        "version": 3,
        "file": "app.min.js",
        "sources": ["src/checkout.ts"],
        "names": ["submit"],
        "mappings": _vlq([143, 0, 41, 12, 0]),
    }


@contextmanager
def _server(store: Storage, **handler_kwargs: object):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(store, **handler_kwargs))
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


def test_sentry_ingest_applies_uploaded_source_map(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    upload_source_map(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        release="abc123",
        artifact_url="https://cdn.example.com/assets/app.min.js",
        source_map=_source_map(),
    )

    result = ingest_monitoring_webhook(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        provider="sentry",
        payload={
            "event": {
                "event_id": "evt-sourcemap-1",
                "title": "TypeError: failed checkout",
                "release": "abc123",
                "exception": {
                    "values": [
                        {
                            "type": "TypeError",
                            "value": "Cannot read cart total",
                            "stacktrace": {
                                "frames": [
                                    {
                                        "filename": "https://cdn.example.com/assets/app.min.js",
                                        "function": "n",
                                        "lineno": 1,
                                        "colno": 143,
                                    }
                                ]
                            },
                        }
                    ]
                },
            }
        },
    )

    failure = store.get_failure_by_id(result.failure_id)
    evidence = store.list_failure_evidence(failure_id=result.failure_id)
    assert failure is not None
    assert failure.metadata["top_stack_frame"] == "src/checkout.ts:submit:42"
    assert failure.metadata["stack_frames"][0]["source_mapped"] is True
    assert failure.metadata["stack_frames"][0]["generated_filename"].endswith(
        "/assets/app.min.js"
    )
    assert evidence[0].payload["top_stack_frame"] == "src/checkout.ts:submit:42"


def test_sentry_source_map_lookup_does_not_cross_dist(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    upload_source_map(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        release="abc123",
        dist="beta",
        artifact_url="https://cdn.example.com/assets/app.min.js",
        source_map=_source_map(),
    )

    result = ingest_monitoring_webhook(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        provider="sentry",
        payload={
            "event": {
                "event_id": "evt-sourcemap-dist-1",
                "title": "TypeError: failed checkout",
                "release": "abc123",
                "exception": {
                    "values": [
                        {
                            "type": "TypeError",
                            "value": "Cannot read cart total",
                            "stacktrace": {
                                "frames": [
                                    {
                                        "filename": "https://cdn.example.com/assets/app.min.js",
                                        "function": "n",
                                        "lineno": 1,
                                        "colno": 143,
                                    }
                                ]
                            },
                        }
                    ]
                },
            }
        },
    )

    failure = store.get_failure_by_id(result.failure_id)
    assert failure is not None
    assert failure.metadata["top_stack_frame"].endswith("/assets/app.min.js:n:1")
    assert "source_mapped" not in failure.metadata["stack_frames"][0]
    frame = failure.metadata["stack_frames"][0]
    assert frame["source_map_status"] == "unmapped"
    assert frame["source_map_reason"] == "no_source_maps_for_release_dist"
    assert frame["source_map_diagnostic"]["release"] == "abc123"


def test_sentry_source_map_miss_records_artifact_diagnostics(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    upload_source_map(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        release="abc123",
        artifact_url="https://cdn.example.com/assets/other.min.js",
        source_map={**_source_map(), "file": "other.min.js"},
    )

    result = ingest_monitoring_webhook(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        provider="sentry",
        payload={
            "event": {
                "event_id": "evt-sourcemap-miss-1",
                "title": "TypeError: failed checkout",
                "release": "abc123",
                "exception": {
                    "values": [
                        {
                            "type": "TypeError",
                            "value": "Cannot read cart total",
                            "stacktrace": {
                                "frames": [
                                    {
                                        "filename": "https://cdn.example.com/assets/app.min.js",
                                        "function": "n",
                                        "lineno": 1,
                                        "colno": 143,
                                    }
                                ]
                            },
                        }
                    ]
                },
            }
        },
    )

    failure = store.get_failure_by_id(result.failure_id)
    evidence = store.list_failure_evidence(failure_id=result.failure_id)
    assert failure is not None
    frame = failure.metadata["stack_frames"][0]
    assert frame["source_map_status"] == "unmapped"
    assert frame["source_map_reason"] == "no_matching_artifact"
    assert frame["source_map_diagnostic"]["candidate_artifacts"] == [
        "https://cdn.example.com/assets/other.min.js"
    ]
    assert evidence[0].payload["stack_frames"][0]["source_map_reason"] == (
        "no_matching_artifact"
    )


def test_source_map_api_endpoint_accepts_upload(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="Source maps",
        scopes=["source_maps:write"],
    )

    body = json.dumps(
        {
            "release": "abc123",
            "artifact_url": "https://cdn.example.com/assets/app.min.js",
            "source_map": _source_map(),
        }
    ).encode("utf-8")

    with _server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            f"/api/source-maps?environment_id={workspace.environment_id}",
            body=body,
            headers={
                "Authorization": f"Bearer {service.token}",
                "Content-Type": "application/json",
            },
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    rows = store.list_source_maps(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        release="abc123",
    )
    assert response.status == 202
    assert payload["source_map"]["release"] == "abc123"
    assert rows[0].artifact_url == "https://cdn.example.com/assets/app.min.js"


def test_source_map_api_endpoint_rate_limits_uploads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(api_module.INGEST_RATE_LIMITS, "source_maps", (1, 60))
    store, workspace = _store(tmp_path)
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="Source maps",
        scopes=["source_maps:write"],
    )

    with _server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        for artifact in ("app-a.min.js", "app-b.min.js"):
            conn.request(
                "POST",
                f"/api/source-maps?environment_id={workspace.environment_id}",
                body=json.dumps(
                    {
                        "release": "abc123",
                        "artifact_url": f"https://cdn.example.com/assets/{artifact}",
                        "source_map": _source_map(),
                    }
                ).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {service.token}",
                    "Content-Type": "application/json",
                },
            )
            response = conn.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 429
    assert payload["error"] == "rate_limited"
    assert payload["limit"] == 1
    assert response.getheader("Retry-After")


def test_source_map_api_endpoint_rejects_unsupported_map(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="Source maps",
        scopes=["source_maps:write"],
    )

    body = json.dumps(
        {
            "release": "abc123",
            "artifact_url": "https://cdn.example.com/assets/app.min.js",
            "source_map": {"version": 3},
        }
    ).encode("utf-8")

    with _server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            f"/api/source-maps?environment_id={workspace.environment_id}",
            body=body,
            headers={
                "Authorization": f"Bearer {service.token}",
                "Content-Type": "application/json",
            },
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 400
    assert payload["error"] == "invalid_source_map"
    assert "mappings" in payload["message"]


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


def test_app_error_alert_rule_suppresses_matching_error(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    store.upsert_app_error_alert_rule(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        name="Ignore noisy checkout beta",
        action="suppress",
        provider="sentry",
        title_contains="checkout failed",
        min_severity="medium",
    )

    result = ingest_monitoring_webhook(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        provider="sentry",
        payload={
            "event": {
                "event_id": "evt-alert-rule-1",
                "title": "TypeError: checkout failed",
                "level": "error",
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
        },
    )

    failure = store.get_failure_by_id(result.failure_id)
    incident = store.get_incident(result.incident_id)
    evidence = store.list_failure_evidence(failure_id=result.failure_id)
    assert failure is not None
    assert incident is not None
    assert failure.metadata["alert_state"] == "suppressed"
    assert failure.metadata["alert_rule_name"] == "Ignore noisy checkout beta"
    assert incident.metadata["alert_state"] == "suppressed"
    assert evidence[0].payload["alert_state"] == "suppressed"


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


def test_monitoring_webhook_endpoint_rate_limits_by_token_environment_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(api_module.INGEST_RATE_LIMITS, "monitoring", (1, 60))
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
        for event_id in ("evt-rate-1", "evt-rate-2"):
            conn.request(
                "POST",
                f"/api/monitoring/webhook/sentry?environment_id={workspace.environment_id}",
                body=json.dumps({"event": {"event_id": event_id, "title": "Boom"}}).encode(
                    "utf-8"
                ),
                headers={
                    "Authorization": f"Bearer {service.token}",
                    "Content-Type": "application/json",
                },
            )
            response = conn.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 429
    assert payload["error"] == "rate_limited"
    assert payload["limit"] == 1
    assert response.getheader("Retry-After")
    assert response.getheader("X-RateLimit-Remaining") == "0"


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


def test_standard_sentry_store_endpoint_ingests_sdk_event(tmp_path: Path) -> None:
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
            f"/api/{workspace.project_id}/store/?sentry_key={sdk.key}",
            body=json.dumps(
                {
                    "event_id": "evt-standard-store-1",
                    "title": "TypeError in search",
                    "level": "error",
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 202
    assert payload["accepted"] is True
    assert payload["event_count"] == 1
    assert payload["results"][0]["external_id"] == "evt-standard-store-1"
    failure = store.find_failure_by_source(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        source_type="monitor_incident",
        source_external_id="sentry:evt-standard-store-1",
    )
    assert failure is not None


def test_sentry_store_endpoint_dispatches_app_error_notification(
    tmp_path: Path,
) -> None:
    store, workspace = _store(tmp_path)
    sdk = create_sdk_key(
        store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        name="Browser",
    )
    sink = _CaptureSink()
    body = json.dumps(
        {
            "event_id": "evt-notify-1",
            "title": "TypeError in billing",
            "level": "fatal",
            "contexts": {"trace": {"trace_id": "dddddddddddddddddddddddddddddddd"}},
        }
    ).encode("utf-8")

    with _server(store, notification_sinks=[sink]) as server:
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

        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            f"/api/sentry/{workspace.project_id}/store/?sentry_key={sdk.key}",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        duplicate_response = conn.getresponse()
        duplicate_payload = json.loads(duplicate_response.read().decode("utf-8"))
        conn.close()

    assert response.status == 202
    assert payload["results"][0]["created"] is True
    assert duplicate_response.status == 202
    assert duplicate_payload["results"][0]["created"] is False
    assert len(sink.payloads) == 1
    notification = sink.payloads[0]
    assert notification.event == "app_error.created"
    assert notification.severity == "critical"
    assert notification.public_id == payload["results"][0]["incident_public_id"]
    assert notification.extra["failure_public_id"] == payload["results"][0]["failure_public_id"]
    assert notification.extra["trace_ids"] == ["dddddddddddddddddddddddddddddddd"]


def test_monitoring_webhook_dispatches_app_error_notification(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="Ingest",
        scopes=["monitoring:write"],
    )
    sink = _CaptureSink()
    body = json.dumps(
        {
            "event": {
                "event_id": "evt-monitoring-notify-1",
                "title": "TypeError in checkout",
                "level": "fatal",
                "contexts": {"trace": {"trace_id": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"}},
            }
        }
    ).encode("utf-8")

    with _server(store, notification_sinks=[sink]) as server:
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
        duplicate_response = conn.getresponse()
        duplicate_payload = json.loads(duplicate_response.read().decode("utf-8"))
        conn.close()

    assert response.status == 202
    assert payload["created"] is True
    assert duplicate_response.status == 202
    assert duplicate_payload["created"] is False
    assert len(sink.payloads) == 1
    notification = sink.payloads[0]
    assert notification.event == "app_error.created"
    assert notification.severity == "critical"
    assert notification.public_id == payload["incident_public_id"]
    assert notification.extra["failure_public_id"] == payload["failure_public_id"]
    assert notification.extra["trace_ids"] == ["eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"]


def test_app_error_notification_failure_does_not_fail_ingest(tmp_path: Path) -> None:
    class BoomSink:
        name = "boom"

        def send(self, payload: NotificationPayload) -> object:
            raise RuntimeError("notification target down")

    store, workspace = _store(tmp_path)
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="Ingest",
        scopes=["monitoring:write"],
    )

    with _server(store, notification_sinks=[BoomSink()]) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            f"/api/monitoring/webhook/sentry?environment_id={workspace.environment_id}",
            body=json.dumps(
                {
                    "event": {
                        "event_id": "evt-notify-boom-1",
                        "title": "TypeError in checkout",
                        "level": "error",
                    }
                }
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {service.token}",
                "Content-Type": "application/json",
            },
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 202
    assert payload["created"] is True
    assert payload["external_id"] == "evt-notify-boom-1"
    failure = store.find_failure_by_source(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        source_type="monitor_incident",
        source_external_id="sentry:evt-notify-boom-1",
    )
    assert failure is not None


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


def test_sentry_envelope_endpoint_rate_limits_dsn_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(api_module.INGEST_RATE_LIMITS, "sentry", (1, 60))
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
        for event_id in ("evt-envelope-rate-1", "evt-envelope-rate-2"):
            body = (
                json.dumps(
                    {"dsn": f"https://{sdk.key}@retrace.local/{workspace.project_id}"}
                ).encode("utf-8")
                + b"\n"
                + b'{"type":"event"}\n'
                + json.dumps({"event_id": event_id, "title": "TypeError"}).encode(
                    "utf-8"
                )
                + b"\n"
            )
            conn.request(
                "POST",
                f"/api/sentry/{workspace.project_id}/envelope/",
                body=body,
                headers={"Content-Type": "application/x-sentry-envelope"},
            )
            response = conn.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 429
    assert payload["error"] == "rate_limited"
    assert payload["limit"] == 1
    assert response.getheader("Retry-After")


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


def test_app_error_incident_api_lists_monitoring_incidents(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="Reader",
        scopes=["issues:read"],
    )
    event = {
        "event_id": "evt-list-1",
        "title": "TypeError in checkout",
        "level": "error",
        "transaction": "/checkout",
        "release": "abc123",
        "contexts": {"trace": {"trace_id": "cccccccccccccccccccccccccccccccc"}},
        "exception": {
            "values": [
                {
                    "type": "TypeError",
                    "value": "Cannot read cart total",
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
    result = ingest_monitoring_webhook(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        provider="sentry",
        payload={"event": event},
    )
    later_event = {**event, "event_id": "evt-list-2", "timestamp": "2026-05-09T07:00:00Z"}
    ingest_monitoring_webhook(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        provider="sentry",
        payload={"event": later_event},
    )

    with _server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "GET",
            f"/api/app-errors?environment_id={workspace.environment_id}",
            headers={"Authorization": f"Bearer {service.token}"},
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 200
    assert payload["project_id"] == workspace.project_id
    assert len(payload["incidents"]) == 1
    incident = payload["incidents"][0]
    assert incident["public_id"] == result.incident_public_id
    assert incident["failure_count"] == 2
    assert incident["evidence_count"] == 2
    assert incident["trace_ids"] == ["cccccccccccccccccccccccccccccccc"]
    assert incident["top_stack_frame"] == "src/checkout.ts:submit:42"
    assert incident["transaction"] == "/checkout"
    assert incident["release"] == "abc123"
    assert incident["latest_failure"]["source_external_id"] == "sentry:evt-list-2"


def test_app_error_alert_rule_api_creates_and_lists_rules(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="App error writer",
        scopes=["app_errors:write", "app_errors:read"],
    )
    body = json.dumps(
        {
            "name": "Critical checkout only",
            "action": "alert",
            "precedence": 10,
            "min_severity": "critical",
            "provider": "sentry",
            "route_contains": "/checkout",
        }
    ).encode("utf-8")

    with _server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            f"/api/app-error-alert-rules?environment_id={workspace.environment_id}",
            body=body,
            headers={
                "Authorization": f"Bearer {service.token}",
                "Content-Type": "application/json",
            },
        )
        response = conn.getresponse()
        created = json.loads(response.read().decode("utf-8"))
        conn.request(
            "GET",
            f"/api/app-error-alert-rules?environment_id={workspace.environment_id}",
            headers={"Authorization": f"Bearer {service.token}"},
        )
        list_response = conn.getresponse()
        listed = json.loads(list_response.read().decode("utf-8"))
        conn.close()

    assert response.status == 202
    assert created["rule"]["name"] == "Critical checkout only"
    assert created["rule"]["precedence"] == 10
    assert created["rule"]["min_severity"] == "critical"
    assert list_response.status == 200
    assert listed["rules"][0]["route_contains"] == "/checkout"


def test_app_error_alert_rule_rejects_non_object_metadata(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)

    with pytest.raises(ValueError, match="metadata must be a JSON object"):
        store.upsert_app_error_alert_rule(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            name="Bad metadata",
            metadata=["not", "an", "object"],  # type: ignore[arg-type]
        )


def test_app_error_alert_rule_api_requires_write_scope(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="Read only",
        scopes=["app_errors:read"],
    )

    with _server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            f"/api/app-error-alert-rules?environment_id={workspace.environment_id}",
            body=json.dumps({"name": "No write", "action": "suppress"}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {service.token}",
                "Content-Type": "application/json",
            },
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 403
    assert payload["error"] == "forbidden"


def test_app_error_alert_rule_api_requires_app_error_read_scope(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    store.upsert_app_error_alert_rule(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        name="Rule",
    )
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="Issue reader",
        scopes=["issues:read"],
    )

    with _server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "GET",
            f"/api/app-error-alert-rules?environment_id={workspace.environment_id}",
            headers={"Authorization": f"Bearer {service.token}"},
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 403
    assert payload["error"] == "forbidden"


def test_app_error_incident_api_detail_controls_sensitive_evidence(
    tmp_path: Path,
) -> None:
    store, workspace = _store(tmp_path)
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="Reader",
        scopes=["app_errors:read"],
    )
    weak_service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="Weak reader",
        scopes=["issues:read"],
    )
    result = ingest_monitoring_webhook(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        provider="sentry",
        payload={
            "event": {
                "event_id": "evt-detail-1",
                "title": "ReferenceError in settings",
                "level": "error",
            }
        },
    )

    with _server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "GET",
            f"/api/app-errors/{result.incident_public_id}?environment_id={workspace.environment_id}",
            headers={"Authorization": f"Bearer {service.token}"},
        )
        safe_response = conn.getresponse()
        safe_payload = json.loads(safe_response.read().decode("utf-8"))
        conn.close()

        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "GET",
            (
                f"/api/app-errors/{result.incident_public_id}"
                f"?environment_id={workspace.environment_id}&include_sensitive=true"
            ),
            headers={"Authorization": f"Bearer {service.token}"},
        )
        sensitive_response = conn.getresponse()
        sensitive_payload = json.loads(sensitive_response.read().decode("utf-8"))
        conn.close()

        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "GET",
            (
                f"/api/app-errors/{result.incident_public_id}"
                f"?environment_id={workspace.environment_id}&include_sensitive=true"
            ),
            headers={"Authorization": f"Bearer {weak_service.token}"},
        )
        weak_response = conn.getresponse()
        weak_payload = json.loads(weak_response.read().decode("utf-8"))
        conn.close()

    assert safe_response.status == 200
    assert safe_payload["incident"]["public_id"] == result.incident_public_id
    assert len(safe_payload["failures"]) == 1
    assert safe_payload["evidence"] == []
    assert sensitive_response.status == 200
    assert len(sensitive_payload["evidence"]) == 1
    assert sensitive_payload["evidence"][0]["redaction_state"] == "sensitive"
    assert sensitive_payload["evidence"][0]["payload"]["external_id"] == "evt-detail-1"
    assert weak_response.status == 403
    assert weak_payload["error"] == "forbidden"


def test_app_error_incident_lifecycle_resolves_and_reopens_on_regression(
    tmp_path: Path,
) -> None:
    store, workspace = _store(tmp_path)
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="Incident manager",
        scopes=["app_errors:read", "app_errors:write"],
    )
    event = {
        "event_id": "evt-lifecycle-1",
        "title": "Checkout submit failed",
        "level": "error",
        "transaction": "/checkout",
        "exception": {
            "values": [
                {
                    "type": "TypeError",
                    "value": "Cannot read cart total",
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
    result = ingest_monitoring_webhook(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        provider="sentry",
        payload={"event": event},
    )

    with _server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            (
                f"/api/app-errors/{result.incident_public_id}/lifecycle"
                f"?environment_id={workspace.environment_id}"
            ),
            body=json.dumps(
                {
                    "action": "resolve",
                    "reason": "fixed in deploy 2026.05.10",
                    "metadata": {"deploy_sha": "abc123"},
                }
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {service.token}",
                "Content-Type": "application/json",
            },
        )
        resolve_response = conn.getresponse()
        resolved_payload = json.loads(resolve_response.read().decode("utf-8"))
        conn.request(
            "GET",
            f"/api/app-errors?environment_id={workspace.environment_id}&status=resolved",
            headers={"Authorization": f"Bearer {service.token}"},
        )
        list_response = conn.getresponse()
        listed_payload = json.loads(list_response.read().decode("utf-8"))
        conn.close()

    assert resolve_response.status == 202
    assert resolved_payload["incident"]["status"] == "resolved"
    assert resolved_payload["failures"][0]["status"] == "resolved"
    assert resolved_payload["lifecycle_events"][0]["from_status"] == "open"
    assert resolved_payload["lifecycle_events"][0]["to_status"] == "resolved"
    assert resolved_payload["lifecycle_events"][0]["reason"] == "fixed in deploy 2026.05.10"
    assert listed_payload["incidents"][0]["public_id"] == result.incident_public_id

    regressed = {**event, "event_id": "evt-lifecycle-2"}
    ingest_monitoring_webhook(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        provider="sentry",
        payload={"event": regressed},
    )

    with _server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "GET",
            f"/api/app-errors/{result.incident_public_id}?environment_id={workspace.environment_id}",
            headers={"Authorization": f"Bearer {service.token}"},
        )
        detail_response = conn.getresponse()
        detail_payload = json.loads(detail_response.read().decode("utf-8"))
        conn.close()

    assert list_response.status == 200
    assert detail_response.status == 200
    assert detail_payload["incident"]["status"] == "open"
    assert detail_payload["incident"]["failure_count"] == 2
    assert detail_payload["lifecycle_events"][0]["from_status"] == "resolved"
    assert detail_payload["lifecycle_events"][0]["to_status"] == "open"
    assert detail_payload["lifecycle_events"][0]["metadata"]["trigger"] == "ingest_regression"


def test_app_error_prune_endpoint_deletes_old_resolved_monitoring_data(
    tmp_path: Path,
) -> None:
    store, workspace = _store(tmp_path)
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="App error writer",
        scopes=["app_errors:write"],
    )
    result = ingest_monitoring_webhook(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        provider="sentry",
        payload={
            "event": {
                "event_id": "evt-retention-1",
                "title": "Old resolved error",
                "level": "error",
                "timestamp": "2026-01-01T00:00:00Z",
            }
        },
    )
    source_map_row = upload_source_map(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        release="old-release",
        artifact_url="https://cdn.example.com/old.js",
        source_map=_source_map(),
    )
    store.consume_ingest_rate_limit(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        bucket="sentry",
        identity="old-sdk",
        limit=10,
        window_seconds=60,
    )
    old_created_at = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    with sqlite3.connect(store.path) as conn:
        rate_limit_row_id = conn.execute(
            """
            SELECT id
            FROM ingest_rate_limits
            WHERE project_id = ? AND environment_id = ? AND bucket = ?
            """,
            (workspace.project_id, workspace.environment_id, "sentry"),
        ).fetchone()[0]
        conn.execute("UPDATE failures SET status = 'resolved' WHERE id = ?", (result.failure_id,))
        conn.execute(
            "UPDATE failure_evidence SET created_at = ? WHERE failure_id = ?",
            (old_created_at, result.failure_id),
        )
        conn.execute(
            "UPDATE source_maps SET uploaded_at = ? WHERE id = ?",
            (old_created_at, source_map_row.id),
        )
        conn.execute(
            "UPDATE ingest_rate_limits SET updated_at = ? WHERE id = ?",
            (old_created_at, rate_limit_row_id),
        )

    with _server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            f"/api/app-errors/prune?environment_id={workspace.environment_id}",
            body=json.dumps(
                {
                    "failure_retention_days": 1,
                    "evidence_retention_days": 1,
                    "source_map_retention_days": 1,
                    "rate_limit_retention_hours": 1,
                }
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {service.token}",
                "Content-Type": "application/json",
            },
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 202
    assert payload["retention"]["deleted"]["failures"] == 1
    assert payload["retention"]["deleted"]["evidence"] == 1
    assert payload["retention"]["deleted"]["source_maps"] == 1
    assert payload["retention"]["deleted"]["rate_limit_rows"] == 1
    assert store.get_failure_by_id(result.failure_id) is None
    assert (
        store.list_source_maps(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            release="old-release",
        )
        == []
    )


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
