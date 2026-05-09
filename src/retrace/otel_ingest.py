from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from retrace.evidence import EvidenceItem, evidence_dedupe_key
from retrace.storage import Storage


@dataclass(frozen=True)
class OtelIngestResult:
    accepted: int = 0
    stored_event_ids: list[str] = field(default_factory=list)
    linked_evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "stored_event_ids": self.stored_event_ids,
            "linked_evidence_ids": self.linked_evidence_ids,
        }


def ingest_otel_logs(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    payload: dict[str, Any],
) -> OtelIngestResult:
    return _ingest_items(
        store=store,
        project_id=project_id,
        environment_id=environment_id,
        signal_type="log",
        items=_log_records(payload),
    )


def ingest_otel_traces(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    payload: dict[str, Any],
) -> OtelIngestResult:
    return _ingest_items(
        store=store,
        project_id=project_id,
        environment_id=environment_id,
        signal_type="span",
        items=_spans(payload),
    )


def _ingest_items(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    signal_type: str,
    items: list[dict[str, Any]],
) -> OtelIngestResult:
    stored_event_ids: list[str] = []
    linked_evidence_ids: list[str] = []
    for item in items:
        event_id = store.append_otel_event(
            project_id=project_id,
            environment_id=environment_id,
            signal_type=signal_type,
            trace_id=str(item.get("trace_id") or ""),
            span_id=str(item.get("span_id") or ""),
            name=str(item.get("name") or ""),
            severity=str(item.get("severity") or ""),
            body=str(item.get("body") or ""),
            occurred_at_ms=int(item.get("occurred_at_ms") or 0),
            attributes=dict(item.get("attributes") or {}),
        )
        stored_event_ids.append(event_id)
        linked_evidence_ids.extend(
            _link_evidence_to_trace_failures(
                store=store,
                project_id=project_id,
                environment_id=environment_id,
                signal_type=signal_type,
                item={**item, "otel_event_id": event_id},
            )
        )
    return OtelIngestResult(
        accepted=len(items),
        stored_event_ids=stored_event_ids,
        linked_evidence_ids=linked_evidence_ids,
    )


def _link_evidence_to_trace_failures(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    signal_type: str,
    item: dict[str, Any],
) -> list[str]:
    trace_id = str(item.get("trace_id") or "").strip()
    span_id = str(item.get("span_id") or "").strip()
    if not trace_id and not span_id:
        return []
    failures = store.list_failures_by_trace(
        project_id=project_id,
        environment_id=environment_id,
        trace_id=trace_id,
        span_id=span_id,
    )
    out: list[str] = []
    for failure in failures:
        payload = _compact_payload(signal_type=signal_type, item=item)
        stable_payload = {k: v for k, v in payload.items() if k != "otel_event_id"}
        evidence = EvidenceItem(
            failure_id=failure.id,
            evidence_type=f"otel_{signal_type}",
            occurred_at_ms=int(item.get("occurred_at_ms") or 0),
            source=f"otel:{trace_id or span_id}",
            redaction_state="sensitive",
            payload=payload,
            dedupe_key=evidence_dedupe_key(
                failure_id=failure.id,
                evidence_type=f"otel_{signal_type}",
                source=f"otel:{trace_id or span_id}",
                occurred_at_ms=int(item.get("occurred_at_ms") or 0),
                payload=stable_payload,
            ),
        )
        out.append(store.append_failure_evidence(evidence))
    return out


def _log_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload.get("logs"), list):
        return [_normalize_log(item) for item in payload["logs"] if isinstance(item, dict)]
    out: list[dict[str, Any]] = []
    for resource in _dicts(payload.get("resourceLogs")):
        for scope in _dicts(resource.get("scopeLogs")):
            for record in _dicts(scope.get("logRecords")):
                out.append(_normalize_log(record))
    return out


def _spans(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload.get("spans"), list):
        return [_normalize_span(item) for item in payload["spans"] if isinstance(item, dict)]
    out: list[dict[str, Any]] = []
    for resource in _dicts(payload.get("resourceSpans")):
        for scope in _dicts(resource.get("scopeSpans")):
            for span in _dicts(scope.get("spans")):
                out.append(_normalize_span(span))
    return out


def _normalize_log(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "trace_id": _first(record, "traceId", "trace_id"),
        "span_id": _first(record, "spanId", "span_id"),
        "name": "",
        "severity": _first(record, "severityText", "severity", "severityNumber"),
        "body": _value(record.get("body")) or _first(record, "message", "body"),
        "occurred_at_ms": (
            _time_unix_nano_ms(record.get("timeUnixNano"))
            or _time_ms(record.get("timestamp_ms"))
        ),
        "attributes": _attributes(record.get("attributes")),
    }


def _normalize_span(span: dict[str, Any]) -> dict[str, Any]:
    return {
        "trace_id": _first(span, "traceId", "trace_id"),
        "span_id": _first(span, "spanId", "span_id"),
        "name": _first(span, "name"),
        "severity": _first(span, "status"),
        "body": _first(span, "name"),
        "occurred_at_ms": (
            _time_unix_nano_ms(span.get("startTimeUnixNano"))
            or _time_ms(span.get("timestamp_ms"))
        ),
        "attributes": _attributes(span.get("attributes")),
    }


def _compact_payload(*, signal_type: str, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "signal_type": signal_type,
        "otel_event_id": str(item.get("otel_event_id") or ""),
        "trace_id": str(item.get("trace_id") or ""),
        "span_id": str(item.get("span_id") or ""),
        "name": str(item.get("name") or ""),
        "severity": str(item.get("severity") or ""),
        "body": str(item.get("body") or "")[:500],
        "attributes": dict(item.get("attributes") or {}),
    }


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value or [] if isinstance(item, dict)]


def _attributes(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(k): _value(v) for k, v in value.items()}
    if isinstance(value, list):
        out: dict[str, Any] = {}
        for item in value:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if key:
                out[key] = _value(item.get("value"))
        return out
    return {}


def _value(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if key in value:
                return value[key]
    return value


def _first(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        if isinstance(value, dict):
            value = _value(value)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _time_ms(value: Any) -> int:
    try:
        raw = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if raw > 10_000_000_000_000:
        return raw // 1_000_000
    if raw > 10_000_000_000:
        return raw
    return raw * 1000 if raw else 0


def _time_unix_nano_ms(value: Any) -> int:
    try:
        raw = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return raw // 1_000_000 if raw else 0
