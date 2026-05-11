from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from retrace.storage import EvidenceRow, FailureRow, IncidentRow, RepairTaskRow, Storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IncidentGroupingResult:
    incident_id: str
    incident_public_id: str
    group_key: str
    created: bool
    failure_ids: list[str]


@dataclass(frozen=True)
class IncidentDetail:
    incident: IncidentRow
    failures: list[FailureRow]
    evidence: list[EvidenceRow]
    repair_task: RepairTaskRow | None = None


def group_failure_into_incident(
    *,
    store: Storage,
    failure_id: str,
) -> IncidentGroupingResult:
    failure = store.get_failure_by_id(failure_id)
    if failure is None:
        raise ValueError(f"unknown failure_id: {failure_id}")
    group = incident_group_key(failure)
    existing = store.find_incident_by_group(
        project_id=failure.project_id,
        environment_id=failure.environment_id,
        group_key=group,
    )
    incident_id = store.upsert_incident(
        project_id=failure.project_id,
        environment_id=failure.environment_id,
        group_key=group,
        title=incident_title(failure),
        summary=incident_summary(failure),
        severity=failure.severity,
        metadata=incident_metadata(failure),
        reopen_resolved=True,
    )
    store.move_failure_to_incident(incident_id=incident_id, failure_id=failure.id)
    incident = store.get_incident(incident_id)
    failures = store.list_incident_failures(incident_id=incident_id)

    # Mirror into the QA incident pipeline so `retrace qa list` / `qa auto`
    # see this signal alongside replay-derived and UI-test-derived
    # incidents. Import is local to avoid a circular dependency with the
    # bridge (which imports from `qa_incidents`/`storage`).
    try:
        from retrace.qa_incident_bridge import sync_qa_incidents_from_failures

        sync_qa_incidents_from_failures(
            store=store,
            failure_ids=[row.id for row in failures],
        )
    except Exception as exc:  # pragma: no cover - bridge errors must not fail ingest
        logger.warning("qa_incident bridge sync failed for failure %s: %s", failure.id, exc)

    return IncidentGroupingResult(
        incident_id=incident_id,
        incident_public_id=str(getattr(incident, "public_id", "") or ""),
        group_key=group,
        created=existing is None,
        failure_ids=[row.id for row in failures],
    )


def get_incident_detail(
    *,
    store: Storage,
    incident_id: str,
    include_sensitive_evidence: bool = True,
) -> IncidentDetail:
    incident = store.get_incident(incident_id)
    if incident is None:
        raise ValueError(f"unknown incident_id: {incident_id}")
    repair_task = (
        store.get_repair_task(incident.repair_task_id)
        if incident.repair_task_id
        else None
    )
    return IncidentDetail(
        incident=incident,
        failures=store.list_incident_failures(incident_id=incident.id),
        evidence=store.list_incident_evidence(
            incident_id=incident.id,
            include_sensitive=include_sensitive_evidence,
        ),
        repair_task=repair_task,
    )


def ensure_incident_repair_task(*, store: Storage, incident_id: str) -> str:
    detail = get_incident_detail(store=store, incident_id=incident_id)
    if not detail.failures:
        raise ValueError(f"incident has no linked failures: {incident_id}")
    existing_task = (
        store.get_repair_task(detail.incident.repair_task_id)
        if detail.incident.repair_task_id
        else None
    )
    representative = next(
        (
            failure
            for failure in detail.failures
            if existing_task is not None and failure.id == existing_task.failure_id
        ),
        detail.failures[0],
    )
    evidence_ids = [
        item.id for item in detail.evidence if item.failure_id == representative.id
    ]
    changed_files = _unique_strings(
        file
        for failure in detail.failures
        for file in _safe_changed_files_for_failure(store=store, failure=failure)
    )
    repair_task_id = store.upsert_repair_task(
        failure_id=representative.id,
        title=f"Repair incident: {detail.incident.title}",
        source_type="incident",
        source_external_id=detail.incident.public_id,
        status="open",
        likely_files=changed_files,
        risk_notes=(
            "Review all linked incident failures and monitoring evidence before "
            "applying a fix."
        ),
        metadata={
            "incident_id": detail.incident.id,
            "incident_public_id": detail.incident.public_id,
            "group_key": detail.incident.group_key,
            "failure_ids": [failure.id for failure in detail.failures],
            "evidence_ids": [item.id for item in detail.evidence],
            "deploy_changed_files": changed_files,
        },
        evidence_ids=evidence_ids,
    )
    if not detail.incident.repair_task_id:
        store.set_incident_repair_task(
            incident_id=detail.incident.id,
            repair_task_id=repair_task_id,
        )
    return detail.incident.repair_task_id or repair_task_id


def incident_group_key(failure: FailureRow) -> str:
    metadata = dict(failure.metadata or {})
    dimensions = {
        "stack": _first_string(
            metadata,
            "top_stack_frame",
            "stack_frame",
            "in_app_frame",
            "exception_type",
        ),
        "route": _first_string(
            metadata,
            "route",
            "url",
            "current_url",
            "transaction",
        ),
        "service": _first_string(metadata, "service", "service_name", "project"),
        "trace": _first_list_item(metadata.get("trace_ids")),
        "deploy": failure.related_deploy_sha
        or _first_string(metadata, "deploy_sha", "release", "commit_sha"),
        "fingerprint": str(metadata.get("grouping_fingerprint") or failure.fingerprint),
    }
    payload = {key: value for key, value in dimensions.items() if value}
    if not payload:
        payload = {
            "source_type": failure.source_type,
            "source_external_id": failure.source_external_id,
        }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def incident_title(failure: FailureRow) -> str:
    metadata = dict(failure.metadata or {})
    service = _first_string(metadata, "service", "service_name", "project")
    frame = _first_string(metadata, "top_stack_frame", "stack_frame")
    if service and frame:
        return f"{service}: {frame}"
    if frame:
        return frame
    return failure.title or "Monitoring incident"


def incident_summary(failure: FailureRow) -> str:
    metadata = dict(failure.metadata or {})
    parts = [
        failure.summary,
        _first_string(metadata, "current_url", "route", "transaction", "url"),
        _first_list_item(metadata.get("trace_ids")),
    ]
    return " | ".join(part for part in parts if part)


def incident_metadata(failure: FailureRow) -> dict[str, Any]:
    metadata = dict(failure.metadata or {})
    keys = {
        "provider",
        "service",
        "service_name",
        "route",
        "current_url",
        "transaction",
        "top_stack_frame",
        "trace_ids",
        "release",
        "deploy_sha",
        "alert_state",
        "alert_action",
        "alert_rule_id",
        "alert_rule_public_id",
        "alert_rule_name",
    }
    return {
        "source_failure_id": failure.id,
        **{key: metadata[key] for key in keys if key in metadata and metadata[key]},
    }


def _first_string(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _first_list_item(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            text = str(item or "").strip()
            if text:
                return text
    text = str(value or "").strip()
    return text


def _unique_strings(values: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _safe_changed_files_for_failure(*, store: Storage, failure: FailureRow) -> list[str]:
    from retrace.deploys import changed_files_for_failure

    try:
        return changed_files_for_failure(store=store, failure=failure)
    except Exception:
        logger.warning(
            "failed to load deploy changed files for incident repair context",
            extra={"failure_id": failure.id, "deploy_sha": failure.related_deploy_sha},
            exc_info=True,
        )
        return []
