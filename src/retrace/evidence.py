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
        return self.redaction_state != "sensitive"


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
