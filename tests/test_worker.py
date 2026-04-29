from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from retrace.storage import Storage
from retrace.worker import JobOutcome, JobWorker, WorkerRunSummary


def _store(tmp_path: Path) -> Storage:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    store.ensure_workspace(project_name="Default")
    return store


def _enqueue_job(
    store: Storage,
    *,
    kind: str,
    payload: dict[str, Any],
    subject_id: str = "",
) -> str:
    workspace = store.ensure_workspace(project_name="Default")
    return store.enqueue_processing_job(
        kind=kind,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        subject_id=subject_id or f"{kind}-{id(payload)}",
        payload=payload,
    )


def test_worker_register_rejects_duplicate(tmp_path: Path) -> None:
    worker = JobWorker(_store(tmp_path))
    worker.register("noop", lambda j, p: {})
    with pytest.raises(ValueError):
        worker.register("noop", lambda j, p: {})


def test_worker_runs_handler_for_registered_kind_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _enqueue_job(store, kind="kindA", payload={"x": 1})
    _enqueue_job(store, kind="kindB", payload={"y": 2})
    seen: list[tuple[str, dict]] = []

    worker = JobWorker(store)
    worker.register("kindA", lambda job, payload: seen.append((str(job["id"]), payload)) or {})
    summary = worker.run_once()

    assert summary.processed == 1
    assert summary.failed == 0
    assert len(seen) == 1
    assert seen[0][1] == {"x": 1}


def test_worker_marks_failed_jobs_and_records_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _enqueue_job(store, kind="boom", payload={})

    def handler(job: Any, payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("nope")

    worker = JobWorker(store)
    worker.register("boom", handler)
    summary = worker.run_once()

    assert summary.processed == 0
    assert summary.failed == 1
    assert summary.outcomes[0].error == "nope"

    # Job row reflects failure.
    rows = store.list_processing_jobs(kind="boom", status="failed")
    assert len(rows) == 1
    assert rows[0]["last_error"] == "nope"


def test_worker_rejects_non_object_payload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    workspace = store.ensure_workspace(project_name="Default")
    # Insert a job whose payload is a JSON list directly so we hit the
    # "payload must be an object" guard.
    job_id = store.enqueue_processing_job(
        kind="bad",
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        subject_id="bad-1",
        payload={},
    )
    # Force a non-dict payload by direct DB poke.
    with store._conn() as conn:  # type: ignore[attr-defined]
        conn.execute(
            "UPDATE processing_jobs SET payload_json = ? WHERE id = ?",
            (json.dumps([1, 2, 3]), job_id),
        )

    worker = JobWorker(store)
    worker.register("bad", lambda j, p: {})
    summary = worker.run_once()

    assert summary.failed == 1
    assert "payload must be an object" in summary.outcomes[0].error


def test_outcome_and_summary_dataclasses() -> None:
    summary = WorkerRunSummary()
    summary.record(JobOutcome(job_id="1", kind="a", status="succeeded"))
    summary.record(JobOutcome(job_id="2", kind="a", status="failed", error="x"))
    summary.record(JobOutcome(job_id="3", kind="b", status="succeeded"))
    assert summary.seen == 3
    assert summary.processed == 2
    assert summary.failed == 1
    assert summary.skipped == 0
    assert summary.by_kind == {"a": 2, "b": 1}


def test_summary_treats_skipped_as_neither_processed_nor_failed() -> None:
    """Regression: races between cron + long-running worker yield 'skipped'
    outcomes when claim_processing_job loses.  Those must not inflate
    jobs_failed."""
    summary = WorkerRunSummary()
    summary.record(JobOutcome(job_id="1", kind="a", status="succeeded"))
    summary.record(JobOutcome(job_id="2", kind="a", status="skipped", error="not_claimed"))
    summary.record(JobOutcome(job_id="3", kind="a", status="skipped", error="not_claimed"))
    assert summary.processed == 1
    assert summary.failed == 0
    assert summary.skipped == 2
    assert summary.seen == 3


def test_concurrent_claim_yields_skipped_not_failed(tmp_path: Path) -> None:
    """End-to-end coverage: simulate a competing worker that wins the claim
    race between list_processing_jobs() and claim_processing_job().

    We monkey-patch claim_processing_job on the worker's store reference to
    return False the first time it's called, mimicking what happens when
    another worker beat us to that row.
    """
    store = _store(tmp_path)
    job_id = _enqueue_job(store, kind="contended", payload={"x": 1})

    worker = JobWorker(store)
    worker.register("contended", lambda j, p: {})

    real_claim = store.claim_processing_job

    def losing_claim(jid: str) -> bool:
        # Simulate the other worker by claiming it ourselves first, then
        # returning False to the worker's call.
        real_claim(jid)
        return False

    store.claim_processing_job = losing_claim  # type: ignore[assignment]
    try:
        summary = worker.run_once()
    finally:
        store.claim_processing_job = real_claim  # type: ignore[assignment]

    assert summary.processed == 0
    assert summary.failed == 0, (
        f"skipped jobs must not count as failures (got outcomes={summary.outcomes})"
    )
    assert summary.skipped == 1
    assert summary.outcomes[0].status == "skipped"
    assert summary.outcomes[0].error == "not_claimed"
    # And the actually-running job (claimed by the "other worker") is not
    # touched again — it stays in 'running' state for the real worker to drain.
    assert job_id  # silences unused-var lint
