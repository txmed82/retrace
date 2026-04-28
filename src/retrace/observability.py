from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import threading
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
    runtime: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _count_rows(rows: list[Any], key: str = "name") -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        name = str(row[key] or "unknown")
        counts[name] = int(row["count"] or 0)
    return counts


_runtime_lock = threading.Lock()
_api_requests: list[dict[str, Any]] = []
_MAX_RUNTIME_EVENTS = 1000


def record_api_request(
    *,
    method: str,
    path: str,
    status: int,
    latency_ms: float,
    trace_id: str,
) -> None:
    with _runtime_lock:
        _api_requests.append(
            {
                "method": method,
                "path": path,
                "status": int(status),
                "latency_ms": float(latency_ms),
                "trace_id": trace_id,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if len(_api_requests) > _MAX_RUNTIME_EVENTS:
            del _api_requests[: len(_api_requests) - _MAX_RUNTIME_EVENTS]


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return round(float(ordered[idx]), 3)


def _runtime_snapshot() -> dict[str, Any]:
    with _runtime_lock:
        requests = list(_api_requests)
    latencies = [float(item["latency_ms"]) for item in requests]
    failures = [item for item in requests if int(item["status"]) >= 500]
    by_path: dict[str, int] = {}
    for item in requests:
        key = f"{item['method']} {item['path']}"
        by_path[key] = by_path.get(key, 0) + 1
    return {
        "api_requests": len(requests),
        "api_failures": len(failures),
        "api_failure_rate": round(len(failures) / len(requests), 4) if requests else 0.0,
        "api_latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 3) if latencies else 0.0,
        },
        "api_requests_by_route": by_path,
        "recent_trace_ids": [str(item["trace_id"]) for item in requests[-10:]],
    }


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
        queued_jobs = int(
            conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM processing_jobs
                WHERE status = 'queued'
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
        run_duration_rows = conn.execute(
            """
            SELECT started_at, finished_at
            FROM runs
            WHERE finished_at IS NOT NULL
            ORDER BY finished_at DESC
            LIMIT 100
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
            "queue_depth": queued_jobs,
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
            "run_duration_seconds": _duration_summary(run_duration_rows),
        },
        runtime=_runtime_snapshot(),
    )


def _duration_summary(rows: list[Any]) -> dict[str, float]:
    durations: list[float] = []
    for row in rows:
        try:
            started = datetime.fromisoformat(str(row["started_at"]))
            finished = datetime.fromisoformat(str(row["finished_at"]))
        except Exception:
            continue
        durations.append(max(0.0, (finished - started).total_seconds()))
    return {
        "count": len(durations),
        "p50": _percentile(durations, 0.50),
        "p95": _percentile(durations, 0.95),
        "max": round(max(durations), 3) if durations else 0.0,
    }
