from __future__ import annotations

from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread

from retrace.commands.api import _handler
from retrace.failures import canonical_failure_from_monitor_incident
from retrace.otel_ingest import ingest_otel_logs, ingest_otel_traces
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


def _failure_with_trace(store: Storage, workspace: WorkspaceIds) -> str:
    return store.upsert_failure(
        canonical_failure_from_monitor_incident(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            provider="sentry",
            external_id="evt-1",
            title="Checkout exception",
            severity="high",
            metadata={
                "trace_ids": ["trace-1"],
                "span_ids": ["span-1"],
            },
        )
    )


def test_otlp_like_log_payload_is_stored_and_linked_to_failure(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    failure_id = _failure_with_trace(store, workspace)

    result = ingest_otel_logs(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        payload={
            "resourceLogs": [
                {
                    "scopeLogs": [
                        {
                            "logRecords": [
                                {
                                    "traceId": "trace-1",
                                    "spanId": "span-1",
                                    "timeUnixNano": "2000000000",
                                    "severityText": "ERROR",
                                    "body": {"stringValue": "database failed"},
                                    "attributes": [
                                        {
                                            "key": "service.name",
                                            "value": {"stringValue": "api"},
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                }
            ]
        },
    )

    events = store.list_otel_events(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        trace_id="trace-1",
    )
    evidence = store.list_failure_evidence(failure_id=failure_id)
    assert result.accepted == 1
    assert len(events) == 1
    assert events[0].body == "database failed"
    assert events[0].attributes["service.name"] == "api"
    assert len(evidence) == 1
    assert evidence[0].evidence_type == "otel_log"
    assert evidence[0].payload["trace_id"] == "trace-1"


def test_otlp_like_trace_payload_is_stored_and_linked_to_failure(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    failure_id = _failure_with_trace(store, workspace)

    result = ingest_otel_traces(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        payload={
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "trace-1",
                                    "spanId": "span-1",
                                    "name": "POST /checkout",
                                    "startTimeUnixNano": "3000000000",
                                    "attributes": {
                                        "http.route": "/checkout",
                                    },
                                }
                            ]
                        }
                    ]
                }
            ]
        },
    )

    events = store.list_otel_events(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        signal_type="span",
    )
    evidence = store.list_failure_evidence(failure_id=failure_id)
    assert result.accepted == 1
    assert events[0].span_id == "span-1"
    assert events[0].name == "POST /checkout"
    assert evidence[0].evidence_type == "otel_span"
    assert evidence[0].payload["span_id"] == "span-1"


def test_otel_log_api_endpoint_ingests_payload(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="OTel",
        scopes=["otel:write"],
    )
    body = json.dumps(
        {
            "logs": [
                {
                    "trace_id": "trace-api",
                    "span_id": "span-api",
                    "timestamp_ms": 1000,
                    "severity": "INFO",
                    "message": "worker started",
                }
            ]
        }
    ).encode("utf-8")

    with _server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            f"/api/otel/v1/logs?environment_id={workspace.environment_id}",
            body=body,
            headers={
                "Authorization": f"Bearer {service.token}",
                "Content-Type": "application/json",
            },
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    events = store.list_otel_events(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        trace_id="trace-api",
    )
    assert response.status == 202
    assert payload["accepted"] == 1
    assert events[0].body == "worker started"
