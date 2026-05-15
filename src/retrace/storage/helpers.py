"""Constants and helper functions for Retrace storage."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"

def _public_id(prefix: str, *parts: object) -> str:
    raw = "\x1f".join(str(p) for p in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"

def _dt(value: object) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

def _now_iso_microseconds() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")

def _safe_json_obj(raw: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}

def _parse_string_list_json(raw: object) -> list[str]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]

def _parse_dict_list_json(raw: object) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]

def _merge_string_lists(*values: list[str]) -> list[str]:
    merged = []
    seen = set()
    for value_list in values:
        for value in value_list:
            item = str(value or "").strip()
            if item and item not in seen:
                seen.add(item)
                merged.append(item)
    return merged

def _replay_preview(events: list[dict[str, object]]) -> dict[str, object]:
    preview = {"event_count": len(events)}
    timestamps = [
        int(event["timestamp"])
        for event in events
        if isinstance(event.get("timestamp"), int)
    ]
    if timestamps:
        preview["first_timestamp_ms"] = min(timestamps)
        preview["last_timestamp_ms"] = max(timestamps)
    for event in events:
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        href = data.get("href")
        if event.get("type") == 4 and isinstance(href, str) and href:
            preview["url"] = href
            break
    return preview

def _merge_replay_preview(
    existing: dict[str, Any],
    incoming: dict[str, object],
    *,
    event_count: int,
) -> dict[str, object]:
    merged = {**existing, "event_count": int(event_count)}
    for key, reducer in (
        ("first_timestamp_ms", min),
        ("last_timestamp_ms", max),
    ):
        old = existing.get(key)
        new = incoming.get(key)
        if isinstance(old, int) and isinstance(new, int):
            merged[key] = reducer(old, new)
        elif isinstance(new, int):
            merged[key] = new
        elif isinstance(old, int):
            merged[key] = old
    if not merged.get("url") and incoming.get("url"):
        merged["url"] = str(incoming["url"])
    return merged

def _slug(value: str) -> str:
    out = "".join(
        c.lower() if c.isalnum() else "-"
        for c in str(value or "").strip()
    ).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "default"

def _string_values(raw: object) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(v) for v in raw if v]
    if isinstance(raw, str):
        return [v.strip() for v in raw.split(",") if v.strip()]
    return [str(raw)]

def _normalize_github_review_run_status(value: object) -> str:
    GITHUB_REVIEW_RUN_STATUSES = ("queued", "running", "succeeded", "failed", "canceled")
    s = str(value or "").strip().lower()
    if s in GITHUB_REVIEW_RUN_STATUSES:
        return s
    return "queued"

def _normalize_app_error_incident_status(value: object) -> str:
    APP_ERROR_INCIDENT_STATUSES = ("open", "triaged", "investigating", "resolved", "ignored")
    s = str(value or "").strip().lower()
    if s in APP_ERROR_INCIDENT_STATUSES:
        return s
    if s == "new":
        return "open"
    raise ValueError(f"invalid app-error incident status: {value!r}")



FAILURE_TEST_COVERAGE_STATES = (
    "not_covered",
    "covered_unverified",
    "covered_passing",
    "covered_failing",
    "covered_flaky",
)

GITHUB_REVIEW_RUN_STATUSES = ("queued", "running", "succeeded", "failed", "canceled")

_SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}
INGEST_RATE_LIMIT_RETENTION_SECONDS = 48 * 60 * 60
INGEST_RATE_LIMIT_MAX_IDENTITIES_PER_BUCKET = 10000
APP_ERROR_INCIDENT_STATUSES = ("open", "triaged", "investigating", "resolved", "ignored")
APP_ERROR_FAILURE_STATUS_BY_INCIDENT_STATUS = {
    "open": "new",
    "triaged": "triaged",
    "investigating": "triaged",
    "resolved": "resolved",
    "ignored": "ignored",
}



def _rollup_severity(values: list[str]) -> str:
    highest = "medium"
    highest_score = 0
    for value in values:
        severity = str(value or "medium").strip().lower()
        score = _SEVERITY_ORDER.get(severity, 2)
        if score > highest_score:
            highest = severity if severity in _SEVERITY_ORDER else "medium"
            highest_score = score
    return highest


def _string_values(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _normalize_github_review_run_status(value: str) -> str:
    status = value.strip().lower()
    if status not in GITHUB_REVIEW_RUN_STATUSES:
        allowed = ", ".join(GITHUB_REVIEW_RUN_STATUSES)
        raise ValueError(f"invalid github review run status: {value!r}; allowed: {allowed}")
    return status


def _normalize_app_error_incident_status(value: str) -> str:
    status = value.strip().lower()
    if status == "reopened":
        status = "open"
    if status not in APP_ERROR_INCIDENT_STATUSES:
        allowed = ", ".join(APP_ERROR_INCIDENT_STATUSES)
        raise ValueError(f"invalid app-error incident status: {value!r}; allowed: {allowed}")
    return status



def _retention_interval(days: int) -> str:
    """Format the `datetime('now', ?)` modifier for a retention sweep.

    Using SQLite's `datetime('now', '-N days')` (translated by the
    P1.5 dialect layer to `now() - interval` on Postgres) means the
    cutoff is computed by the DB engine in the SAME shape as the
    column DEFAULT was stored — sidesteps the
    Python-isoformat-vs-SQLite-stored-format mismatch (`T` 0x54 vs
    ` ` 0x20) that would otherwise over-prune any row whose
    time-of-day was later than the cutoff's.
    """
    return f"-{max(1, int(days))} days"

