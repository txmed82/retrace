from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from retrace.alert_rules import evaluate_app_error_alert_rules
from retrace.deploys import correlate_failure_to_deploy
from retrace.evidence import EvidenceItem, evidence_dedupe_key
from retrace.failures import canonical_failure_from_monitor_incident
from retrace.incidents import group_failure_into_incident
from retrace.source_maps import diagnose_stack_frame_mapping, map_stack_frame
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
    incident_id: str = ""
    incident_public_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "external_id": self.external_id,
            "failure_id": self.failure_id,
            "failure_public_id": self.failure_public_id,
            "created": self.created,
            "evidence_id": self.evidence_id,
            "incident_id": self.incident_id,
            "incident_public_id": self.incident_public_id,
        }


def ingest_monitoring_webhook(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    provider: str,
    payload: dict[str, Any],
) -> MonitoringIngestResult:
    alert = normalize_monitoring_alert(
        provider=provider,
        payload=payload,
        store=store,
        project_id=project_id,
        environment_id=environment_id,
    )
    rule_decision = evaluate_app_error_alert_rules(
        store=store,
        project_id=project_id,
        environment_id=environment_id,
        alert=alert,
    )
    alert = MonitoringAlert(
        provider=alert.provider,
        external_id=alert.external_id,
        title=alert.title,
        summary=alert.summary,
        severity=alert.severity,
        fingerprint=alert.fingerprint,
        occurred_at_ms=alert.occurred_at_ms,
        metadata={**alert.metadata, **rule_decision.metadata()},
        evidence={
            **alert.evidence,
            "alert_state": rule_decision.state,
            "alert_rule_name": rule_decision.rule_name,
        },
    )
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
        occurred_at_ms=alert.occurred_at_ms,
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
    correlate_failure_to_deploy(store=store, failure_id=failure_id)
    incident = group_failure_into_incident(store=store, failure_id=failure_id)
    return MonitoringIngestResult(
        provider=alert.provider,
        external_id=alert.external_id,
        failure_id=failure_id,
        failure_public_id=str(getattr(persisted, "public_id", "") or failure.public_id),
        created=existing is None,
        evidence_id=evidence_id,
        incident_id=incident.incident_id,
        incident_public_id=incident.incident_public_id,
    )


def normalize_monitoring_alert(
    *,
    provider: str,
    payload: dict[str, Any],
    store: Storage | None = None,
    project_id: str = "",
    environment_id: str = "",
) -> MonitoringAlert:
    clean_provider = str(provider or payload.get("provider") or "generic").strip().lower()
    if clean_provider == "sentry":
        return _sentry_alert(
            payload,
            store=store,
            project_id=project_id,
            environment_id=environment_id,
        )
    if clean_provider == "posthog":
        return _posthog_alert(payload)
    return _generic_alert(clean_provider, payload)


def _sentry_alert(
    payload: dict[str, Any],
    *,
    store: Storage | None = None,
    project_id: str = "",
    environment_id: str = "",
) -> MonitoringAlert:
    data = _dict(payload.get("data"))
    event = _dict(data.get("event")) or _dict(payload.get("event")) or payload
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
    release = _first_str(event, "release", default="")
    dist = _first_str(event, "dist", default=_first_str(event, "distribution", default=""))
    stack_frames = _stack_frames(event)
    stack_frames = _apply_source_maps(
        stack_frames,
        store=store,
        project_id=project_id,
        environment_id=environment_id,
        release=release,
        dist=dist,
    )
    top_stack_frame = _top_stack_frame_from_frames(stack_frames)
    transaction = _first_str(event, "transaction", default="")
    grouping_fingerprint = _fingerprint(
        "sentry-group",
        "",
        "|".join(
            part
            for part in (
                exception_type,
                exception_value,
                top_stack_frame,
                transaction,
            )
            if part
        )
        or title,
    )
    fingerprint = _fingerprint(
        "sentry",
        external_id,
        event.get("fingerprint") or issue.get("fingerprint") or title,
    )
    breadcrumb_trail = _breadcrumbs_from_sentry(event)
    console_excerpts = _console_excerpts_from_breadcrumbs(breadcrumb_trail)
    network_failures = _network_failures_from_breadcrumbs(breadcrumb_trail)
    metadata = {
        "external_id": external_id,
        "issue_id": _first_str(issue, "id", default=""),
        "issue_url": _first_str(issue, "url", "web_url", "permalink", default=""),
        "event_url": _first_str(event, "url", "web_url", default=""),
        "level": level,
        "trace_ids": _trace_ids_from_sentry(event),
        "top_stack_frame": top_stack_frame,
        "stack_frames": stack_frames,
        "transaction": transaction,
        "release": release,
        "dist": dist,
        "environment": _first_str(event, "environment", default=""),
        "grouping_fingerprint": grouping_fingerprint,
        # Breadcrumbs flow from the browser SDK or any Sentry SDK with
        # an `event.breadcrumbs.values` array. We promote the obvious
        # signal classes (console / HTTP) into the `IncidentEvidence`
        # fields the bridge already reads, and keep the raw trail in
        # metadata so future consumers (e.g. the repair prompt) can
        # see the full sequence.
        "breadcrumbs": breadcrumb_trail,
        "console_excerpts": console_excerpts,
        "network_failures": network_failures,
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
                "top_stack_frame": top_stack_frame,
                "stack_frames": stack_frames,
                "transaction": transaction,
                "release": release,
                "dist": dist,
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
    return _top_stack_frame_from_frames(_stack_frames(event))


def _stack_frames(event: dict[str, Any]) -> list[dict[str, Any]]:
    exception = _first_exception(event)
    frames = _dict(exception.get("stacktrace")).get("frames")
    if not isinstance(frames, list) or not frames:
        return []
    out: list[dict[str, Any]] = []
    for raw in frames:
        frame = _dict(raw)
        filename = _first_str(frame, "filename", "abs_path", "module", default="")
        function = _first_str(frame, "function", default="")
        lineno = _safe_int(frame.get("lineno"))
        colno = _safe_int(frame.get("colno") or frame.get("column"))
        if filename or function or lineno:
            out.append(
                _without_empty(
                    {
                        "filename": filename,
                        "function": function,
                        "lineno": lineno,
                        "colno": colno,
                    }
                )
            )
    return out


def _apply_source_maps(
    frames: list[dict[str, Any]],
    *,
    store: Storage | None,
    project_id: str,
    environment_id: str,
    release: str,
    dist: str,
) -> list[dict[str, Any]]:
    if store is None or not release.strip():
        return frames
    mapped_frames: list[dict[str, Any]] = []
    for frame in frames:
        filename = str(frame.get("filename") or "").strip()
        lineno = _safe_int(frame.get("lineno"))
        colno = _safe_int(frame.get("colno"))
        match = map_stack_frame(
            store=store,
            project_id=project_id,
            environment_id=environment_id,
            release=release,
            dist=dist,
            generated_file=filename,
            line=lineno,
            column=colno,
        )
        if match is None:
            diagnostic = diagnose_stack_frame_mapping(
                store=store,
                project_id=project_id,
                environment_id=environment_id,
                release=release,
                dist=dist,
                generated_file=filename,
                line=lineno,
                column=colno,
            )
            mapped_frames.append(
                {
                    **frame,
                    "source_map_status": diagnostic.status,
                    "source_map_reason": diagnostic.reason,
                    "source_map_diagnostic": diagnostic.to_dict(),
                }
            )
            continue
        mapped_frames.append(
            {
                **frame,
                "generated_filename": filename,
                "generated_lineno": lineno,
                "generated_colno": colno,
                "filename": match.source,
                "function": match.name or str(frame.get("function") or ""),
                "lineno": match.line,
                "colno": match.column,
                "source_mapped": True,
                "source_map_artifact": match.artifact_url,
            }
        )
    return mapped_frames


def _top_stack_frame_from_frames(frames: list[dict[str, Any]]) -> str:
    if not frames:
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


def _breadcrumbs_from_sentry(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize Sentry-style breadcrumbs to a clean list of dicts.

    Sentry's wire shape is `event.breadcrumbs.values = [...]`, but
    older SDKs and synthetic payloads sometimes send `breadcrumbs`
    directly as a list. We handle both; entries that aren't dicts are
    dropped.
    """
    raw = event.get("breadcrumbs")
    if isinstance(raw, dict):
        items = raw.get("values")
    else:
        items = raw
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        normalized: dict[str, Any] = {
            "timestamp": entry.get("timestamp"),
            "category": str(entry.get("category") or "default").strip(),
            "message": str(entry.get("message") or "").strip(),
            "level": str(entry.get("level") or "info").strip().lower(),
        }
        data = entry.get("data")
        if isinstance(data, dict):
            normalized["data"] = data
        out.append(normalized)
    return out


def _console_excerpts_from_breadcrumbs(crumbs: list[dict[str, Any]]) -> list[str]:
    """Pick console / log breadcrumbs out of the trail.

    Mirrors the rule used in `auto_fix.py`: a short, ordered list of
    strings that the repair prompt can quote verbatim.
    """
    out: list[str] = []
    for c in crumbs:
        category = str(c.get("category") or "").lower()
        if category not in {"console", "log"}:
            continue
        message = str(c.get("message") or "").strip()
        if not message:
            continue
        out.append(message[:500])
        if len(out) >= 20:
            break
    return out


def _network_failures_from_breadcrumbs(crumbs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pick failed HTTP-category breadcrumbs (status >= 400 or explicit
    error) out of the trail.

    Shape matches `IncidentEvidence.network_failures` in
    `qa_incidents.py` so the bridge can ingest verbatim.
    """
    out: list[dict[str, Any]] = []
    for c in crumbs:
        category = str(c.get("category") or "").lower()
        if category not in {"http", "fetch", "xhr"}:
            continue
        data = c.get("data") if isinstance(c.get("data"), dict) else {}
        status = data.get("status_code") or data.get("status")
        try:
            status_int = int(status) if status is not None else 0
        except (TypeError, ValueError):
            status_int = 0
        err = str(data.get("error") or "").strip()
        if status_int < 400 and not err:
            continue
        entry: dict[str, Any] = {
            "method": str(data.get("method") or "").upper() or "GET",
            "url": str(data.get("url") or ""),
        }
        if status_int:
            entry["status_code"] = status_int
        if err:
            entry["error"] = err
        if data.get("duration_ms") is not None:
            try:
                entry["duration_ms"] = int(data["duration_ms"])
            except (TypeError, ValueError):
                pass
        out.append(entry)
        if len(out) >= 20:
            break
    return out


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _trim_dict(payload: dict[str, Any], keys: set[str]) -> dict[str, Any]:
    return {key: payload[key] for key in keys if key in payload and payload[key]}


def _without_empty(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if value not in ("", None, [], {})
    }
