"""Generalized job worker substrate (RET-33).

Extracts the claim → execute → finish plumbing from
`replay_core.process_queued_replay_jobs` into a small, reusable runner.
New job types (e.g. `repair.run`, `digest.daily`) register a handler and
ride on the same plumbing without re-implementing locking, retry, or error
capture.

Handlers receive the raw job row plus a parsed JSON payload and return
either a dict of metadata (succeeded) or raise (failed).  The worker logs,
finishes the job appropriately, and aggregates per-kind counters into a
`WorkerRunSummary` for callers (CLI, cron, the long-running worker).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from retrace.storage import Storage

log = logging.getLogger(__name__)


HandlerResult = Mapping[str, Any]
JobHandler = Callable[[Any, Mapping[str, Any]], HandlerResult]


@dataclass
class JobOutcome:
    job_id: str
    kind: str
    status: str
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


@dataclass
class WorkerRunSummary:
    seen: int = 0
    processed: int = 0
    failed: int = 0
    skipped: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    outcomes: list[JobOutcome] = field(default_factory=list)

    def record(self, outcome: JobOutcome) -> None:
        self.seen += 1
        self.outcomes.append(outcome)
        self.by_kind[outcome.kind] = self.by_kind.get(outcome.kind, 0) + 1
        if outcome.status == "succeeded":
            self.processed += 1
        elif outcome.status == "skipped":
            self.skipped += 1
        else:
            self.failed += 1


class JobWorker:
    """Single-process worker that drains queued jobs through registered handlers."""

    def __init__(self, store: Storage) -> None:
        self.store = store
        self._handlers: dict[str, JobHandler] = {}

    def register(self, kind: str, handler: JobHandler) -> None:
        if not kind.strip():
            raise ValueError("kind is required")
        if kind in self._handlers:
            raise ValueError(f"handler for kind {kind!r} already registered")
        self._handlers[kind] = handler

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    def run_once(
        self,
        *,
        kinds: Iterable[str] | None = None,
        limit: int = 25,
        project_id: str | None = None,
    ) -> WorkerRunSummary:
        """Drain up to `limit` queued jobs of each kind.

        If `kinds` is omitted, all registered kinds are drained.  Other kinds
        present in the queue are left untouched so multiple worker
        deployments can split responsibility cleanly.
        """
        target_kinds = tuple(kinds) if kinds else self.kinds
        summary = WorkerRunSummary()
        for kind in target_kinds:
            handler = self._handlers.get(kind)
            if handler is None:
                log.warning("worker has no handler for kind %r — skipping", kind)
                continue
            jobs = self.store.list_processing_jobs(
                kind=kind,
                status="queued",
                project_id=project_id,
                limit=limit,
            )
            for job in jobs:
                outcome = self._execute(handler=handler, job=job)
                summary.record(outcome)
        return summary

    def _execute(self, *, handler: JobHandler, job: Any) -> JobOutcome:
        job_id = str(job["id"])
        kind = str(job["kind"])
        if not self.store.claim_processing_job(job_id):
            # Already taken by a parallel worker.
            return JobOutcome(
                job_id=job_id, kind=kind, status="skipped", error="not_claimed"
            )
        metadata: HandlerResult = {}
        handler_succeeded = False
        try:
            payload = json.loads(str(job["payload_json"] or "{}"))
            if not isinstance(payload, dict):
                raise ValueError(
                    f"job payload must be an object, got {type(payload).__name__}"
                )
            metadata = handler(job, payload)
            handler_succeeded = True
        except Exception as exc:
            log.exception("job %s (kind=%s) failed", job_id, kind)
            try:
                self.store.finish_processing_job(
                    job_id=job_id, status="failed", error=str(exc)
                )
            except Exception:
                # If we can't even mark failure, log loudly; the job stays
                # 'running' and will need manual recovery (or a stuck-job
                # sweeper later).  We still return a failed outcome so the
                # summary reflects reality.
                log.exception(
                    "job %s (kind=%s) failed AND finish_processing_job raised",
                    job_id,
                    kind,
                )
            return JobOutcome(
                job_id=job_id, kind=kind, status="failed", error=str(exc)
            )

        # Handler succeeded; record success.  If finish_processing_job raises
        # the row stays in 'running' state, but the in-memory outcome reflects
        # success so the caller can decide whether to act on `metadata`.
        try:
            self.store.finish_processing_job(job_id=job_id, status="succeeded")
        except Exception as finish_exc:
            log.exception(
                "job %s (kind=%s) handler succeeded but finish_processing_job failed",
                job_id,
                kind,
            )
            return JobOutcome(
                job_id=job_id,
                kind=kind,
                status="failed",
                error=f"handler succeeded but finish failed: {finish_exc}",
                metadata=dict(metadata or {}),
            )
        # Defensive: only reachable when handler_succeeded; explicit branch
        # documents the contract for readers.
        assert handler_succeeded
        return JobOutcome(
            job_id=job_id,
            kind=kind,
            status="succeeded",
            metadata=dict(metadata or {}),
        )
