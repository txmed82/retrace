from __future__ import annotations

from pathlib import Path

from retrace.evidence import EvidenceItem, evidence_dedupe_key
from retrace.failures import canonical_failure_from_monitor_incident
from retrace.incidents import (
    ensure_incident_repair_task,
    get_incident_detail,
    group_failure_into_incident,
)
from retrace.monitoring_ingest import ingest_monitoring_webhook
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


def _monitor_failure(
    store: Storage,
    workspace: WorkspaceIds,
    *,
    external_id: str,
    severity: str,
) -> str:
    failure = canonical_failure_from_monitor_incident(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        provider="sentry",
        external_id=external_id,
        title="TypeError in checkout",
        summary="Cannot read cart total.",
        severity=severity,
        fingerprint="checkout-type-error",
        metadata={
            "provider": "sentry",
            "service": "web",
            "route": "/checkout",
            "top_stack_frame": "src/checkout.ts:submit:42",
            "trace_ids": ["trace-1"],
        },
    )
    failure_id = store.upsert_failure(failure)
    payload = {"external_id": external_id, "top_stack_frame": "src/checkout.ts:submit:42"}
    store.append_failure_evidence(
        EvidenceItem(
            failure_id=failure_id,
            evidence_type="monitoring_alert",
            occurred_at_ms=1000,
            source=f"sentry:{external_id}",
            redaction_state="sensitive",
            payload=payload,
            dedupe_key=evidence_dedupe_key(
                failure_id=failure_id,
                evidence_type="monitoring_alert",
                source=f"sentry:{external_id}",
                occurred_at_ms=1000,
                payload=payload,
            ),
        )
    )
    return failure_id


def test_equivalent_monitor_failures_group_into_one_incident(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    first_failure_id = _monitor_failure(
        store,
        workspace,
        external_id="evt-1",
        severity="medium",
    )
    second_failure_id = _monitor_failure(
        store,
        workspace,
        external_id="evt-2",
        severity="critical",
    )

    first = group_failure_into_incident(store=store, failure_id=first_failure_id)
    second = group_failure_into_incident(store=store, failure_id=second_failure_id)
    detail = get_incident_detail(store=store, incident_id=first.incident_id)

    assert first.incident_id == second.incident_id
    assert second.created is False
    assert detail.incident.failure_count == 2
    assert detail.incident.evidence_count == 2
    assert detail.incident.severity == "critical"
    assert {failure.id for failure in detail.failures} == {
        first_failure_id,
        second_failure_id,
    }
    assert {item.source for item in detail.evidence} == {"sentry:evt-1", "sentry:evt-2"}


def test_incident_can_generate_one_repair_task(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    first_failure_id = _monitor_failure(
        store,
        workspace,
        external_id="evt-1",
        severity="high",
    )
    second_failure_id = _monitor_failure(
        store,
        workspace,
        external_id="evt-2",
        severity="high",
    )
    incident = group_failure_into_incident(store=store, failure_id=first_failure_id)
    group_failure_into_incident(store=store, failure_id=second_failure_id)

    first_task = ensure_incident_repair_task(store=store, incident_id=incident.incident_id)
    second_task = ensure_incident_repair_task(store=store, incident_id=incident.incident_id)
    detail = get_incident_detail(store=store, incident_id=incident.incident_id)
    task = store.get_repair_task(first_task)

    assert first_task == second_task
    assert detail.incident.repair_task_id == first_task
    assert task is not None
    assert task.source_type == "incident"
    assert task.source_external_id == detail.incident.public_id
    assert set(task.metadata["failure_ids"]) == {first_failure_id, second_failure_id}


def test_monitoring_webhook_ingest_links_failure_to_incident(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)

    result = ingest_monitoring_webhook(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        provider="posthog",
        payload={
            "event": "$exception",
            "properties": {
                "$exception_fingerprint": "checkout-type-error",
                "$exception_type": "TypeError",
                "$exception_message": "Cannot read cart total.",
                "$current_url": "https://example.com/checkout",
            },
        },
    )

    detail = get_incident_detail(store=store, incident_id=result.incident_id)
    assert result.incident_public_id.startswith("inc_")
    assert detail.incident.failure_count == 1
    assert detail.failures[0].id == result.failure_id
