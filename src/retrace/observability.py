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


def _count_rows(rows: list[Any], key: str = "name") -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        name = str(row[key] or "unknown")
        counts[name] = int(row["count"] or 0)
    return counts


def collect_local_observability(store: Storage) -> LocalObservabilitySnapshot:
    """Return provider-neutral operational counters for local/self-host installs."""
    with store._conn() as conn:
        replay_session_count = int(
            conn.execute("SELECT COUNT(*) AS count FROM replay_sessions").fetchone()[
                "count"
            ]
            or 0
        )
        replay_batch_summary = conn.execute(
            """
            SELECT COUNT(*) AS batch_count,
                   COALESCE(SUM(event_count), 0) AS event_count
            FROM replay_batches
            """
        ).fetchone()
        issue_count = int(
            conn.execute("SELECT COUNT(*) AS count FROM replay_issues").fetchone()[
                "count"
            ]
            or 0
        )
        issue_status_rows = conn.execute(
            """
            SELECT COALESCE(NULLIF(status, ''), 'unknown') AS name,
                   COUNT(*) AS count
            FROM replay_issues
            GROUP BY COALESCE(NULLIF(status, ''), 'unknown')
            """
        ).fetchall()
        issue_severity_rows = conn.execute(
            """
            SELECT COALESCE(NULLIF(severity, ''), 'unknown') AS name,
                   COUNT(*) AS count
            FROM replay_issues
            GROUP BY COALESCE(NULLIF(severity, ''), 'unknown')
            """
        ).fetchall()
        analysis_status_rows = conn.execute(
            """
            SELECT COALESCE(NULLIF(analysis_status, ''), 'unknown') AS name,
                   COUNT(*) AS count
            FROM replay_issues
            GROUP BY COALESCE(NULLIF(analysis_status, ''), 'unknown')
            """
        ).fetchall()
        job_rows = conn.execute(
            """
            SELECT COALESCE(NULLIF(kind, ''), 'unknown') AS kind,
                   COALESCE(NULLIF(status, ''), 'unknown') AS status,
                   COUNT(*) AS count
            FROM processing_jobs
            GROUP BY COALESCE(NULLIF(kind, ''), 'unknown'),
                     COALESCE(NULLIF(status, ''), 'unknown')
            """
        ).fetchall()
        failed_jobs = int(
            conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM processing_jobs
                WHERE status = 'failed'
                """
            ).fetchone()["count"]
            or 0
        )
        retrying_jobs = int(
            conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM processing_jobs
                WHERE attempts > 1
                """
            ).fetchone()["count"]
            or 0
        )
        detector_rows = conn.execute(
            """
            SELECT COALESCE(NULLIF(detector, ''), 'unknown') AS name,
                   COUNT(*) AS count
            FROM replay_signals
            GROUP BY COALESCE(NULLIF(detector, ''), 'unknown')
            """
        ).fetchall()
        tester_run_count = int(
            conn.execute("SELECT COUNT(*) AS count FROM runs").fetchone()["count"] or 0
        )
        tester_run_status_rows = conn.execute(
            """
            SELECT COALESCE(NULLIF(status, ''), 'unknown') AS name,
                   COUNT(*) AS count
            FROM runs
            GROUP BY COALESCE(NULLIF(status, ''), 'unknown')
            """
        ).fetchall()

    job_counts: dict[str, dict[str, int]] = {}
    for row in job_rows:
        kind = str(row["kind"] or "unknown")
        status = str(row["status"] or "unknown")
        job_counts.setdefault(kind, {})
        job_counts[kind][status] = int(row["count"] or 0)

    return LocalObservabilitySnapshot(
        generated_at=datetime.now(timezone.utc).isoformat(),
        api={
            "replay_sessions": replay_session_count,
            "replay_batches": int(replay_batch_summary["batch_count"] or 0),
            "replay_events": int(replay_batch_summary["event_count"] or 0),
        },
        replay_processing={
            "jobs": job_counts,
            "failed_jobs": failed_jobs,
            "retrying_jobs": retrying_jobs,
            "signals_by_detector": _count_rows(detector_rows),
        },
        ai_analysis={
            "issues": issue_count,
            "issues_by_status": _count_rows(issue_status_rows),
            "issues_by_severity": _count_rows(issue_severity_rows),
            "analysis_by_status": _count_rows(analysis_status_rows),
        },
        storage={
            "database_path": str(store.path),
            "replay_blob_backend": (
                store.replay_blob_store.backend if store.replay_blob_store else "sqlite"
            ),
        },
        test_runs={
            "runs": tester_run_count,
            "runs_by_status": _count_rows(tester_run_status_rows),
        },
    )
