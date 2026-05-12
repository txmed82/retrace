"""P2.3 — data retention.

`retrace data retention apply` walks the live install and purges
rows + files older than the configured TTLs. The contract is:

  - DB-side: prune the high-volume tables (`failures` + evidence
    cluster, `replay_batches`, `otel_events`, `source_maps`,
    `ingest_rate_limits`). Reference / configuration tables
    (`projects`, `environments`, `sdk_keys`, `signal_definitions`,
    `alert_routes`, baselines on disk, ...) are NEVER touched.
  - FS-side: sweep `data_dir/ui-tests/runs/` and
    `data_dir/api-tests/runs/`. Each subdirectory is a single test
    run; if its mtime is older than `run_artifact_days`, it gets
    removed wholesale. Spec / queue / baseline directories are NOT
    swept.

The design keeps retention OUT of the ingest hot path. It runs
periodically (cron / one-shot) via the CLI; the engine is pure
business logic that takes a `RetentionPolicy` and reports a
`RetentionResult` dataclass.

Per-project retention thresholds are NOT modeled yet — the roadmap
flags this as aspirational, and adding `project_retention_policies`
table is a bigger schema change than fits in this slice. Today the
policy is install-global; per-project overrides land as a follow-up
once a real user surfaces the need.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from retrace.api_testing import api_runs_dir_for_data_dir
from retrace.storage import AppErrorRetentionPruneResult, Storage
from retrace.tester import runs_dir_for_data_dir


@dataclass(frozen=True)
class RetentionPolicy:
    """TTLs for each retention domain. Days unless noted.

    Defaults are conservative — long enough that an unattended
    install doesn't lose useful debug data, short enough that disk
    growth doesn't sneak up on a self-host operator.
    """

    failures_days: int = 90
    evidence_days: int = 90
    source_maps_days: int = 30
    rate_limit_hours: int = 48
    replay_batches_days: int = 30
    otel_events_days: int = 30
    run_artifact_days: int = 30


@dataclass
class RetentionResult:
    dry_run: bool
    policy: RetentionPolicy
    # DB-side aggregates (sums across project/env pairs for the
    # app-error domain; single numbers for the global tables).
    failures: int = 0
    evidence: int = 0
    incident_links: int = 0
    incidents: int = 0
    source_maps: int = 0
    rate_limit_rows: int = 0
    replay_batches: int = 0
    otel_events: int = 0
    # FS-side: count + total byte size of removed run directories.
    run_artifact_dirs: int = 0
    run_artifact_bytes: int = 0
    # Per-pair detail kept for the CLI's JSON output; useful when
    # debugging unexpected purge counts on a multi-project install.
    app_error_pairs: list[dict] = field(default_factory=list)


def apply_retention(
    *,
    store: Storage,
    data_dir: Path,
    policy: RetentionPolicy,
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> RetentionResult:
    """Apply `policy` against `store` + `data_dir`. Returns the result.

    Pure orchestration — DB pruning lives on `Storage`, filesystem
    sweeping lives in `_sweep_run_artifacts` below. The caller
    decides what to do with the result (CLI prints it as JSON; a
    future cron wrapper might surface it via metrics).
    """
    current = now or datetime.now(timezone.utc)
    result = RetentionResult(dry_run=bool(dry_run), policy=policy)

    # App-error domain — `prune_app_error_retention` requires a
    # (project_id, environment_id) scope, so iterate every pair we
    # have failure data for. New installs with no failures yet get a
    # no-op here, which is correct.
    for project_id, environment_id in store.project_environment_pairs():
        pair_result: AppErrorRetentionPruneResult = store.prune_app_error_retention(
            project_id=project_id,
            environment_id=environment_id,
            failure_retention_days=policy.failures_days,
            evidence_retention_days=policy.evidence_days,
            source_map_retention_days=policy.source_maps_days,
            rate_limit_retention_hours=policy.rate_limit_hours,
            dry_run=dry_run,
            now=current,
        )
        result.failures += pair_result.failures
        result.evidence += pair_result.evidence
        result.incident_links += pair_result.incident_links
        result.incidents += pair_result.incidents
        result.source_maps += pair_result.source_maps
        result.rate_limit_rows += pair_result.rate_limit_rows
        result.app_error_pairs.append(
            {
                "project_id": project_id,
                "environment_id": environment_id,
                "failures": pair_result.failures,
                "evidence": pair_result.evidence,
                "incidents": pair_result.incidents,
                "source_maps": pair_result.source_maps,
                "rate_limit_rows": pair_result.rate_limit_rows,
            }
        )

    # Global high-volume tables — single cutoff, no per-project
    # filtering. Pruning these per-project would be slower for the
    # same net effect; the cutoff is identical regardless of which
    # project owns a row.
    result.replay_batches = store.prune_replay_batches(
        retention_days=policy.replay_batches_days,
        dry_run=dry_run,
        now=current,
    )
    result.otel_events = store.prune_otel_events(
        retention_days=policy.otel_events_days,
        dry_run=dry_run,
        now=current,
    )

    # Filesystem sweep — only the per-run directories grow over time.
    # Specs / baselines / queues are kept (they're reference state).
    dirs, total_bytes = _sweep_run_artifacts(
        data_dir=data_dir,
        retention_days=policy.run_artifact_days,
        dry_run=dry_run,
        now=current,
    )
    result.run_artifact_dirs = dirs
    result.run_artifact_bytes = total_bytes

    return result


def _sweep_run_artifacts(
    *,
    data_dir: Path,
    retention_days: int,
    dry_run: bool,
    now: datetime,
) -> tuple[int, int]:
    """Remove run-artifact subdirectories older than `retention_days`.

    Walks `ui-tests/runs/` and `api-tests/runs/`. Returns
    `(directories_removed, total_bytes_removed)`. Counts are
    accurate in both dry-run and real-run mode — the bytes value
    is the on-disk size *before* removal, computed once.

    Each immediate child of those `runs/` parents is treated as a
    single run unit. We don't descend further; partial removal
    inside a run dir would leave junk behind. mtime is read off
    the directory itself (set when the run finishes writing its
    artifacts), which is the simplest correct signal — `created_at`
    isn't available at fs level.
    """
    days = max(1, int(retention_days))
    cutoff_ts = now.timestamp() - (days * 24 * 60 * 60)
    targets = [
        runs_dir_for_data_dir(data_dir),
        api_runs_dir_for_data_dir(data_dir),
    ]
    removed_count = 0
    removed_bytes = 0
    for parent in targets:
        if not parent.exists() or not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if not child.is_dir():
                continue
            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff_ts:
                continue
            try:
                size = _dir_size_bytes(child)
            except OSError:
                size = 0
            removed_count += 1
            removed_bytes += size
            if not dry_run:
                try:
                    shutil.rmtree(child)
                except OSError:
                    # The user is responsible for fs-level perms;
                    # surface the count we attempted, swallow the
                    # individual failure rather than abort the sweep
                    # halfway through.
                    pass
    return removed_count, removed_bytes


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def result_to_dict(result: RetentionResult) -> dict:
    """Stable dict shape for JSON emission. Keys match the CLI's
    documented output so downstream tooling can parse it without
    introspecting the dataclass."""
    return {
        "dry_run": result.dry_run,
        "policy": {
            "failures_days": result.policy.failures_days,
            "evidence_days": result.policy.evidence_days,
            "source_maps_days": result.policy.source_maps_days,
            "rate_limit_hours": result.policy.rate_limit_hours,
            "replay_batches_days": result.policy.replay_batches_days,
            "otel_events_days": result.policy.otel_events_days,
            "run_artifact_days": result.policy.run_artifact_days,
        },
        "pruned": {
            "failures": result.failures,
            "evidence": result.evidence,
            "incident_links": result.incident_links,
            "incidents": result.incidents,
            "source_maps": result.source_maps,
            "rate_limit_rows": result.rate_limit_rows,
            "replay_batches": result.replay_batches,
            "otel_events": result.otel_events,
            "run_artifact_dirs": result.run_artifact_dirs,
            "run_artifact_bytes": result.run_artifact_bytes,
        },
        "app_error_pairs": result.app_error_pairs,
        "applied_at": datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat(),
    }


__all__ = [
    "RetentionPolicy",
    "RetentionResult",
    "apply_retention",
    "result_to_dict",
]
