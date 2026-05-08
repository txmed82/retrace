from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal


EvidenceRedactionState = Literal["raw", "redacted", "sensitive"]
PROMPT_SAFE_REDACTION_STATES = ("raw", "redacted")


@dataclass(frozen=True)
class EvidenceItem:
    failure_id: str
    evidence_type: str
    occurred_at_ms: int
    source: str
    redaction_state: EvidenceRedactionState
    payload: dict[str, Any] = field(default_factory=dict)
    artifact_path: str = ""
    dedupe_key: str = ""

    @property
    def safe_for_prompts(self) -> bool:
        return self.redaction_state in PROMPT_SAFE_REDACTION_STATES


def validate_evidence_payload(payload: dict[str, Any]) -> None:
    try:
        json.dumps(payload, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("evidence payload must be JSON-serializable") from exc


def evidence_dedupe_key(
    *,
    failure_id: str,
    evidence_type: str,
    source: str,
    occurred_at_ms: int,
    payload: dict[str, Any],
) -> str:
    validate_evidence_payload(payload)
    raw = json.dumps(
        {
            "failure_id": failure_id,
            "evidence_type": evidence_type,
            "source": source,
            "occurred_at_ms": int(occurred_at_ms),
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def evidence_items_from_replay_issue(
    *,
    failure_id: str,
    issue_public_id: str,
    evidence: dict[str, Any],
) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    source = f"replay_issue:{issue_public_id}"
    for signal in _dict_items(evidence.get("signals")):
        occurred_at_ms = _safe_int(signal.get("timestamp_ms"))
        payload = {**_without_issue_public_id(signal), "issue_public_id": issue_public_id}
        items.append(
            EvidenceItem(
                failure_id=failure_id,
                evidence_type=_signal_evidence_type(signal),
                occurred_at_ms=occurred_at_ms,
                source=source,
                redaction_state="redacted",
                payload=payload,
                dedupe_key=evidence_dedupe_key(
                    failure_id=failure_id,
                    evidence_type=_signal_evidence_type(signal),
                    source=source,
                    occurred_at_ms=occurred_at_ms,
                    payload=payload,
                ),
            )
        )
    for event in _dict_items(evidence.get("events")):
        occurred_at_ms = _safe_int(event.get("timestamp_ms"))
        payload = {**_without_issue_public_id(event), "issue_public_id": issue_public_id}
        items.append(
            EvidenceItem(
                failure_id=failure_id,
                evidence_type="replay_event",
                occurred_at_ms=occurred_at_ms,
                source=source,
                redaction_state="redacted",
                payload=payload,
                dedupe_key=evidence_dedupe_key(
                    failure_id=failure_id,
                    evidence_type="replay_event",
                    source=source,
                    occurred_at_ms=occurred_at_ms,
                    payload=payload,
                ),
            )
        )
    if not items and evidence:
        payload = {"issue_public_id": issue_public_id, "evidence": evidence}
        items.append(
            EvidenceItem(
                failure_id=failure_id,
                evidence_type="replay_evidence_bundle",
                occurred_at_ms=0,
                source=source,
                redaction_state="redacted",
                payload=payload,
                dedupe_key=evidence_dedupe_key(
                    failure_id=failure_id,
                    evidence_type="replay_evidence_bundle",
                    source=source,
                    occurred_at_ms=0,
                    payload=payload,
                ),
            )
        )
    return sorted(items, key=lambda item: (item.occurred_at_ms, item.evidence_type))


def build_evidence_timeline(rows: list[Any]) -> list[dict[str, Any]]:
    timeline = [_timeline_event_from_row(row) for row in rows]
    return sorted(
        timeline,
        key=lambda item: (
            _safe_int(item.get("occurred_at_ms")),
            str(item.get("type") or ""),
            str(item.get("id") or ""),
        ),
    )


def _timeline_event_from_row(row: Any) -> dict[str, Any]:
    evidence_type = str(getattr(row, "evidence_type", "") or "")
    payload = dict(getattr(row, "payload", {}) or {})
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    detail_map = {**payload, **details}
    title, summary, kind = _timeline_copy(evidence_type, detail_map)
    detector = str(payload.get("detector") or detail_map.get("detector") or "")
    confidence = _first_str(detail_map, "confidence", default="")
    reason_codes = _string_list(
        payload.get("reason_codes") or detail_map.get("reason_codes")
    )
    return {
        "id": str(getattr(row, "id", "") or ""),
        "type": evidence_type,
        "kind": kind,
        "occurred_at_ms": _safe_int(getattr(row, "occurred_at_ms", 0)),
        "source": str(getattr(row, "source", "") or ""),
        "title": title,
        "summary": summary,
        "detector": detector,
        "detector_hit": bool(detector) or evidence_type != "replay_event",
        "confidence": confidence,
        "reason_codes": reason_codes,
        "artifact_path": str(getattr(row, "artifact_path", "") or ""),
        "payload": payload,
    }


def _timeline_copy(
    evidence_type: str,
    values: dict[str, Any],
) -> tuple[str, str, str]:
    if evidence_type == "network_request":
        method = _first_str(values, "method", "request_method", default="REQUEST")
        url = _first_str(values, "request_url", "url", "href", default="unknown URL")
        status = _first_str(values, "status", "status_code", default="unknown")
        timing = _first_str(
            values,
            "duration_ms",
            "elapsed_ms",
            "timing_ms",
            default="",
        )
        suffix = f" in {timing}ms" if timing else ""
        return (
            f"Network {status}",
            f"{method.upper()} {url} returned {status}{suffix}",
            "network",
        )
    if evidence_type == "console_log":
        level = _first_str(values, "level", default="error")
        message = _console_message(values.get("message", values.get("payload", "")))
        return (f"Console {level}", message or "Console event captured", "console")
    if evidence_type == "replay_event":
        return _replay_event_copy(values)
    if evidence_type == "frontend_exception":
        message = _first_str(values, "message", "error", default="Exception captured")
        return ("Frontend exception", message, "exception")
    if evidence_type == "dom_snapshot":
        return ("DOM signal", "Blank or unexpected render state detected", "dom")
    label = evidence_type.replace("_", " ").strip().title() or "Evidence"
    detector = _first_str(values, "detector", default="")
    return (label, detector.replace("_", " ") if detector else label, "detector")


def _replay_event_copy(values: dict[str, Any]) -> tuple[str, str, str]:
    event_type = _safe_int(values.get("type"))
    source = _safe_int(values.get("source"))
    data_type = _safe_int(values.get("data_type"))
    target = _first_str(values, "id", default="unknown")
    if event_type == 4:
        return (
            "Navigation",
            f"Opened {_first_str(values, 'href', default='unknown URL')}",
            "replay",
        )
    if event_type == 3 and source == 2 and data_type == 2:
        return ("Click", f"Clicked element id {target}", "replay")
    if event_type == 3 and source == 5:
        return ("Input", f"Entered text into element id {target}", "replay")
    plugin = _first_str(values, "plugin", default="")
    if plugin:
        return ("Replay plugin event", plugin, "replay")
    return ("Replay event", f"rrweb event type {event_type}", "replay")


def _first_str(source: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        candidate = source.get(key)
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return default


def _console_message(value: object) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value if str(item).strip())
    return str(value or "").strip()


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def _signal_evidence_type(signal: dict[str, Any]) -> str:
    detector = str(signal.get("detector") or "").strip()
    if detector == "network_5xx" or detector == "network_4xx":
        return "network_request"
    if detector == "console_error":
        return "console_log"
    if detector == "blank_render":
        return "dom_snapshot"
    if detector == "error_toast":
        return "frontend_exception"
    return "detector_signal"


def _dict_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _without_issue_public_id(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "issue_public_id"}


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
