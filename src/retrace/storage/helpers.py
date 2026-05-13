"""Constants and helper functions for Retrace storage."""

from __future__ import annotations

from typing import Any

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

