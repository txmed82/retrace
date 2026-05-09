from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from retrace.evidence import EvidenceItem, evidence_dedupe_key
from retrace.failures import canonical_failure_from_monitor_incident
from retrace.storage import Storage


@dataclass(frozen=True)
class MonitoringAlert:
    provider: str
    external_id: str
    title: str
    summary: str
    severity: str
    fingerprint: str
    occurred_at_ms: int
    metadata: dict[str, Any]
    evidence: dict[str, Any]


@dataclass(frozen=True)
class MonitoringIngestResult:
    provider: str
    external_id: str
    failure_id: str
    failure_public_id: str
    created: bool
    evidence_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "external_id": self.external_id,
            "failure_id": self.failure_id,
            "failure_public_id": self.failure_public_id,
            "created": self.created,
            "evidence_id": self.evidence_id,
        }


def ingest_monitoring_webhook(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    provider: str,
    payload: dict[str, Any],
) -> MonitoringIngestResult:
    alert = normalize_monitoring_alert(provider=provider, payload=payload)
    source_external_id = f"{alert.provider}:{alert.external_id}"
    existing = store.find_failure_by_source(
        project_id=project_id,
        environment_id=environment_id,
        source_type="monitor_incident",
        source_external_id=source_external_id,
    )
    failure = canonical_failure_from_monitor_incident(
        project_id=project_id,
        environment_id=environment_id,
        provider=alert.provider,
        external_id=alert.external_id,
        title=alert.title,
        summary=alert.summary,
        severity=alert.severity,
        fingerprint=alert.fingerprint,
        metadata=alert.metadata,
    )
    failure_id = store.upsert_failure(failure)
    persisted = store.get_failure(
        project_id=project_id,
        environment_id=environment_id,
        failure_id=failure_id,
    )
    evidence = EvidenceItem(
        failure_id=failure_id,
        evidence_type="monitoring_alert",
        occurred_at_ms=alert.occurred_at_ms,
        source=source_external_id,
        redaction_state="sensitive",
        payload=alert.evidence,
        dedupe_key=evidence_dedupe_key(
            failure_id=failure_id,
            evidence_type="monitoring_alert",
            source=source_external_id,
            occurred_at_ms=alert.occurred_at_ms,
            payload=alert.evidence,
        ),
    )
    evidence_id = store.append_failure_evidence(evidence)
    return MonitoringIngestResult(
        provider=alert.provider,
        external_id=alert.external_id,
        failure_id=failure_id,
        failure_public_id=str(getattr(persisted, "public_id", "") or failure.public_id),
        created=existing is None,
        evidence_id=evidence_id,
    )


def normalize_monitoring_alert(*, provider: str, payload: dict[str, Any]) -> MonitoringAlert:
    clean_provider = str(provider or payload.get("provider") or "generic").strip().lower()
    if clean_provider == "sentry":
        return _sentry_alert(payload)
    if clean_provider == "posthog":
        return _posthog_alert(payload)
    return _generic_alert(clean_provider, payload)


def _sentry_alert(payload: dict[str, Any]) -> MonitoringAlert:
    data = _dict(payload.get("data"))
    event = _dict(data.get("event")) or _dict(payload.get("event"))
    issue = _dict(data.get("issue")) or _dict(payload.get("issue"))
    external_id = _first_str(
        event,
        "event_id",
        "id",
        default=_first_str(issue, "id", "short_id", default=""),
    )
    title = _first_str(
        event,
        "title",
        "message",
        default=_first_str(issue, "title", "short_id", default="Sentry alert"),
    )
    exception = _first_exception(event)
    exception_type = _first_str(exception, "type", default="")
    exception_value = _first_str(exception, "value", default="")
    summary_parts = [
        part
        for part in (
            exception_type,
            exception_value,
            _first_str(event, "culprit", "transaction", default=""),
        )
        if part
    ]
    level = _first_str(event, "level", default=_first_str(issue, "level", default="error"))
    fingerprint = _fingerprint(
        "sentry",
        external_id,
        event.get("fingerprint") or issue.get("fingerprint") or title,
    )
    metadata = {
        "external_id": external_id,
        "issue_id": _first_str(issue, "id", default=""),
        "issue_url": _first_str(issue, "url", "web_url", "permalink", default=""),
        "event_url": _first_str(event, "url", "web_url", default=""),
        "level": level,
        "trace_ids": _trace_ids_from_sentry(event),
        "top_stack_frame": _top_stack_frame(event),
    }
    return MonitoringAlert(
        provider="sentry",
        external_id=external_id or _payload_hash(payload),
        title=title,
        summary=". ".join(summary_parts) or title,
        severity=_severity(level),
        fingerprint=fingerprint,
        occurred_at_ms=_timestamp_ms(event) or _timestamp_ms(payload),
        metadata=_without_empty(metadata),
        evidence=_without_empty(
            {
                "provider": "sentry",
                "external_id": external_id,
                "title": title,
                "summary": ". ".join(summary_parts) or title,
                "level": level,
                "issue": _trim_dict(issue, {"id", "short_id", "title", "url", "web_url"}),
                "trace_ids": _trace_ids_from_sentry(event),
                "top_stack_frame": _top_stack_frame(event),
            }
        ),
    )


def _posthog_alert(payload: dict[str, Any]) -> MonitoringAlert:
    event = _dict(payload.get("event"))
    properties = _dict(payload.get("properties")) or _dict(event.get("properties"))
    exception_list = properties.get("$exception_list")
    exception = _dict(exception_list[0]) if isinstance(exception_list, list) and exception_list else {}
    message = _first_str(
        properties,
        "$exception_message",
        "$exception_value",
        default=_first_str(exception, "value", "message", default="PostHog exception"),
    )
    exception_type = _first_str(
        properties,
        "$exception_type",
        default=_first_str(exception, "type", default=""),
    )
    title = f"{exception_type}: {message}" if exception_type and message else message
    external_id = _first_str(
        properties,
        "$exception_fingerprint",
        "$exception_id",
        default=_first_str(payload, "uuid", "event_uuid", "id", default=""),
    )
    level = _first_str(properties, "level", "$level", "severity", default="error")
    trace_ids = _string_list(
        properties.get("$trace_id")
        or properties.get("trace_id")
        or properties.get("traceId")
    )
    fingerprint = _fingerprint(
        "posthog",
        external_id,
        properties.get("$exception_fingerprint") or title,
    )
    metadata = {
        "external_id": external_id,
        "event": _first_str(payload, "event", default=_first_str(event, "event", default="")),
        "distinct_id": _first_str(properties, "distinct_id", default=""),
        "current_url": _first_str(properties, "$current_url", "current_url", default=""),
        "trace_ids": trace_ids,
        "exception_type": exception_type,
    }
    return MonitoringAlert(
        provider="posthog",
        external_id=external_id or _payload_hash(payload),
        title=title,
        summary=message,
        severity=_severity(level),
        fingerprint=fingerprint,
        occurred_at_ms=_timestamp_ms(payload) or _timestamp_ms(event),
        metadata=_without_empty(metadata),
        evidence=_without_empty(
            {
                "provider": "posthog",
                "external_id": external_id,
                "title": title,
                "message": message,
                "exception_type": exception_type,
                "current_url": metadata["current_url"],
                "trace_ids": trace_ids,
            }
        ),
    )


def _generic_alert(provider: str, payload: dict[str, Any]) -> MonitoringAlert:
    external_id = _first_str(payload, "id", "event_id", "uuid", "fingerprint", default="")
    title = _first_str(payload, "title", "message", "name", default="External monitoring alert")
    severity = _severity(_first_str(payload, "level", "severity", "status", default="error"))
    return MonitoringAlert(
        provider=provider or "generic",
        external_id=external_id or _payload_hash(payload),
        title=title,
        summary=_first_str(payload, "summary", "description", "message", default=title),
        severity=severity,
        fingerprint=_fingerprint(provider or "generic", external_id, title),
        occurred_at_ms=_timestamp_ms(payload),
        metadata=_without_empty({"external_id": external_id}),
        evidence=_without_empty({"provider": provider or "generic", "external_id": external_id, "title": title}),
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_str(mapping: dict[str, Any], *keys: str, default: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _severity(level: str) -> str:
    raw = str(level or "").strip().lower()
    if raw in {"fatal", "critical", "panic"}:
        return "critical"
    if raw in {"error", "exception"}:
        return "high"
    if raw in {"warning", "warn"}:
        return "medium"
    if raw in {"info", "debug", "notice"}:
        return "low"
    return "medium"


def _fingerprint(provider: str, external_id: str, seed: Any) -> str:
    if isinstance(seed, list):
        seed = "|".join(str(item) for item in seed)
    raw = json.dumps(
        {"provider": provider, "external_id": external_id, "seed": str(seed or "")},
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]


def _timestamp_ms(payload: dict[str, Any]) -> int:
    for key in ("timestamp_ms", "timestamp", "datetime", "dateCreated"):
        value = payload.get(key)
        if value is None:
            continue
        parsed = _parse_timestamp_ms(value)
        if parsed:
            return parsed
    return 0


def _parse_timestamp_ms(value: Any) -> int:
    if isinstance(value, int | float):
        return int(value if value > 10_000_000_000 else value * 1000)
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        numeric = float(text)
    except ValueError:
        numeric = 0
    if numeric:
        return int(numeric if numeric > 10_000_000_000 else numeric * 1000)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _first_exception(event: dict[str, Any]) -> dict[str, Any]:
    values = _dict(event.get("exception")).get("values")
    if isinstance(values, list) and values:
        return _dict(values[0])
    return {}


def _top_stack_frame(event: dict[str, Any]) -> str:
    exception = _first_exception(event)
    frames = _dict(exception.get("stacktrace")).get("frames")
    if not isinstance(frames, list) or not frames:
        return ""
    frame = _dict(frames[-1])
    filename = _first_str(frame, "filename", "abs_path", "module", default="")
    function = _first_str(frame, "function", default="")
    line = _first_str(frame, "lineno", default="")
    parts = [part for part in (filename, function, line) if part]
    return ":".join(parts)


def _trace_ids_from_sentry(event: dict[str, Any]) -> list[str]:
    contexts = _dict(event.get("contexts"))
    trace = _dict(contexts.get("trace"))
    tags = event.get("tags")
    values = [
        trace.get("trace_id"),
        trace.get("traceId"),
    ]
    if isinstance(tags, dict):
        values.extend([tags.get("trace_id"), tags.get("traceId")])
    if isinstance(tags, list):
        for item in tags:
            if not isinstance(item, list | tuple) or len(item) < 2:
                continue
            if str(item[0]) in {"trace_id", "traceId"}:
                values.append(item[1])
    return _string_list([item for item in values if item])


def _trim_dict(payload: dict[str, Any], keys: set[str]) -> dict[str, Any]:
    return {key: payload[key] for key in keys if key in payload and payload[key]}


def _without_empty(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if value not in ("", None, [], {})
    }
