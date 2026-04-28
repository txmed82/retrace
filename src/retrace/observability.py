from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any

from retrace.storage import Storage


@dataclass(frozen=True)
class LocalObservabilitySnapshot:
    generated_at: str
    api: dict[str, Any]
    replay_processing: dict[str, Any]
    ai_analysis: dict[str, Any]
    storage: dict[str, Any]
    test_runs: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _count_by_status(rows: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row["status"] or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def collect_local_observability(store: Storage) -> LocalObservabilitySnapshot:
    """Return provider-neutral operational counters for local/self-host installs."""
    with store._conn() as conn:
        replay_sessions = conn.execute(
            "SELECT status, event_count FROM replay_sessions"
        ).fetchall()
        replay_batches = conn.execute(
            "SELECT event_count, received_at FROM replay_batches"
        ).fetchall()
        replay_issues = conn.execute(
            """
            SELECT status, severity, analysis_status
            FROM replay_issues
            """
        ).fetchall()
        jobs = conn.execute(
            """
            SELECT kind, status, attempts, last_error
            FROM processing_jobs
            """
        ).fetchall()
        signals = conn.execute(
            "SELECT detector FROM replay_signals"
        ).fetchall()
        tester_runs = conn.execute(
            "SELECT status, findings_count FROM runs"
        ).fetchall()

    job_counts: dict[str, dict[str, int]] = {}
    failed_jobs = 0
    retrying_jobs = 0
    for job in jobs:
        kind = str(job["kind"] or "unknown")
        status = str(job["status"] or "unknown")
        job_counts.setdefault(kind, {})
        job_counts[kind][status] = job_counts[kind].get(status, 0) + 1
        failed_jobs += 1 if status == "failed" else 0
        retrying_jobs += 1 if int(job["attempts"] or 0) > 1 else 0

    detector_counts: dict[str, int] = {}
    for signal in signals:
        detector = str(signal["detector"] or "unknown")
        detector_counts[detector] = detector_counts.get(detector, 0) + 1

    issue_severity: dict[str, int] = {}
    analysis_status: dict[str, int] = {}
    for issue in replay_issues:
        severity = str(issue["severity"] or "unknown")
        issue_severity[severity] = issue_severity.get(severity, 0) + 1
        status = str(issue["analysis_status"] or "unknown")
        analysis_status[status] = analysis_status.get(status, 0) + 1

    return LocalObservabilitySnapshot(
        generated_at=datetime.now(timezone.utc).isoformat(),
        api={
            "replay_sessions": len(replay_sessions),
            "replay_batches": len(replay_batches),
            "replay_events": sum(int(row["event_count"] or 0) for row in replay_batches),
        },
        replay_processing={
            "jobs": job_counts,
            "failed_jobs": failed_jobs,
            "retrying_jobs": retrying_jobs,
            "signals_by_detector": detector_counts,
        },
        ai_analysis={
            "issues": len(replay_issues),
            "issues_by_status": _count_by_status(replay_issues),
            "issues_by_severity": issue_severity,
            "analysis_by_status": analysis_status,
        },
        storage={
            "database_path": str(store.path),
            "replay_blob_backend": (
                store.replay_blob_store.backend if store.replay_blob_store else "sqlite"
            ),
        },
        test_runs={
            "runs": len(tester_runs),
            "runs_by_status": _count_by_status(tester_runs),
        },
    )
