from __future__ import annotations

from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread

from retrace.commands.api import _handler
from retrace.deploys import correlate_failure_to_deploy, record_deploy
from retrace.failures import canonical_failure_from_monitor_incident
from retrace.incidents import ensure_incident_repair_task, group_failure_into_incident
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


def _failure(store: Storage, workspace: WorkspaceIds, *, occurred_at_ms: int) -> str:
    return store.upsert_failure(
        canonical_failure_from_monitor_incident(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            provider="sentry",
            external_id="evt-1",
            title="TypeError in checkout",
            summary="Cannot read cart total.",
            severity="high",
            fingerprint="checkout-type-error",
            occurred_at_ms=occurred_at_ms,
            metadata={"top_stack_frame": "src/checkout.ts:submit:42"},
        )
    )


def test_deploy_marker_can_be_recorded(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)

    deploy = record_deploy(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        sha="abc123",
        branch="main",
        author="dev@example.com",
        deployed_at_ms=1_000,
        changed_files=["src/checkout.ts", "src/cart.ts"],
    )

    stored = store.get_deploy_marker(deploy.id)
    assert stored is not None
    assert stored.sha == "abc123"
    assert stored.changed_files == ["src/checkout.ts", "src/cart.ts"]


def test_failure_after_deploy_links_to_nearest_deploy(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    record_deploy(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        sha="old",
        deployed_at_ms=1_000,
        changed_files=["src/old.ts"],
    )
    record_deploy(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        sha="new",
        deployed_at_ms=2_000,
        changed_files=["src/checkout.ts"],
    )
    failure_id = _failure(store, workspace, occurred_at_ms=2_500)

    result = correlate_failure_to_deploy(store=store, failure_id=failure_id)
    failure = store.get_failure_by_id(failure_id)

    assert result is not None
    assert result.deploy_sha == "new"
    assert result.changed_files == ["src/checkout.ts"]
    assert failure is not None
    assert failure.related_deploy_sha == "new"


def test_incident_repair_task_includes_deploy_changed_files(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    record_deploy(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        sha="abc123",
        deployed_at_ms=1_000,
        changed_files=["src/checkout.ts", "src/cart.ts"],
    )
    failure_id = _failure(store, workspace, occurred_at_ms=2_000)
    correlate_failure_to_deploy(store=store, failure_id=failure_id)
    incident = group_failure_into_incident(store=store, failure_id=failure_id)

    repair_task_id = ensure_incident_repair_task(
        store=store,
        incident_id=incident.incident_id,
    )
    repair_task = store.get_repair_task(repair_task_id)

    assert repair_task is not None
    assert repair_task.likely_files == ["src/checkout.ts", "src/cart.ts"]
    assert repair_task.metadata["deploy_changed_files"] == [
        "src/checkout.ts",
        "src/cart.ts",
    ]


def test_deploy_endpoint_records_marker_and_correlates_failures(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    failure_id = _failure(store, workspace, occurred_at_ms=2_000)
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="Deploy",
        scopes=["deploy:write"],
    )
    body = json.dumps(
        {
            "sha": "abc123",
            "branch": "main",
            "deployed_at_ms": 1_000,
            "changed_files": ["src/checkout.ts"],
        }
    ).encode("utf-8")

    with _server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            f"/api/deploys?environment_id={workspace.environment_id}",
            body=body,
            headers={
                "Authorization": f"Bearer {service.token}",
                "Content-Type": "application/json",
            },
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    failure = store.get_failure_by_id(failure_id)
    assert response.status == 202
    assert payload["deploy"]["sha"] == "abc123"
    assert payload["correlated_failures"][0]["failure_id"] == failure_id
    assert failure is not None
    assert failure.related_deploy_sha == "abc123"
