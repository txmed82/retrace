"""P3.2 — parallel runner for the tester spec suite.

The existing `run_spec` already isolates each test in its own
browser-harness subprocess and its own `runs/<run_id>/` artifact
directory — no cross-spec shared state to coordinate. The
parallelism we want is "kick off N of them at once, collect the
results, surface a summary."

Concurrency model: **ThreadPoolExecutor**, not multiprocessing.
The per-spec work is I/O-bound (subprocess wait + browser
automation + DB writes); the GIL doesn't bottleneck it. Threads
also avoid the multiprocessing pickling tax and the gotchas
around sharing the Storage handle across processes.

Each spec's subprocess gives us true process isolation between
specs anyway (the browser harness is its own process). That's
what the roadmap calls "share nothing" — and we already have it.

Deterministic per-spec ordering: results are returned in the same
order the input list was given. Worker assignment is NOT pinned
to a specific thread — the runtime cost of pinning (a
single-consumer queue per worker) doesn't pay off when every
spec's bottleneck is its own subprocess.
"""

from __future__ import annotations

import fnmatch
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from retrace.tester import (
    TesterRunResult,
    TesterSpec,
    list_specs,
    load_spec,
    run_spec,
    runs_dir_for_data_dir,
    specs_dir_for_data_dir,
)


@dataclass(frozen=True)
class PoolRunResult:
    """Aggregate over a parallel run of N specs."""

    total: int
    ok_count: int
    fail_count: int
    skipped_count: int
    duration_seconds: float
    per_spec: list[TesterRunResult]


def _default_runner(
    spec: TesterSpec,
    *,
    runs_dir: Path,
    cwd: Path,
    max_retries: int,
) -> TesterRunResult:
    """Default per-spec runner — separate function so tests can
    swap in a fake."""
    return run_spec(
        spec=spec,
        runs_dir=runs_dir,
        max_retries=max_retries,
        cwd=cwd,
    )


def select_specs(
    *,
    data_dir: Path,
    match_pattern: str = "",
    spec_ids: Optional[list[str]] = None,
) -> list[TesterSpec]:
    """Pick the specs to run.

    `spec_ids`, if given, wins — only those specs are loaded.
    Otherwise `match_pattern` filters all specs in the data_dir's
    specs directory by `fnmatch` against the spec name AND id.
    Empty pattern + no ids = every spec in the directory.
    """
    specs_dir = specs_dir_for_data_dir(data_dir)
    if spec_ids:
        out: list[TesterSpec] = []
        for sid in spec_ids:
            if sid.strip():
                out.append(load_spec(specs_dir, sid.strip()))
        return out
    all_specs = list_specs(specs_dir)
    if not match_pattern.strip():
        return all_specs
    pattern = match_pattern.strip()
    return [
        s
        for s in all_specs
        if fnmatch.fnmatchcase(s.spec_id, pattern)
        or fnmatch.fnmatchcase(s.name, pattern)
    ]


def run_specs_parallel(
    specs: list[TesterSpec],
    *,
    data_dir: Path,
    cwd: Path,
    workers: int = 4,
    max_retries: int = 1,
    runner: Optional[Callable[..., TesterRunResult]] = None,
) -> PoolRunResult:
    """Run `specs` concurrently using `workers` threads.

    Returns a `PoolRunResult` with the per-spec `TesterRunResult`s
    in the **input order** (not completion order). A spec whose
    runner raises is captured as a skipped result rather than
    aborting the whole pool — one bad spec shouldn't drop the
    summary for the others.
    """
    if not specs:
        return PoolRunResult(
            total=0,
            ok_count=0,
            fail_count=0,
            skipped_count=0,
            duration_seconds=0.0,
            per_spec=[],
        )
    use_runner = runner or _default_runner
    runs_dir = runs_dir_for_data_dir(data_dir)
    safe_workers = max(1, min(int(workers), len(specs)))

    import time

    started = time.perf_counter()
    results: dict[int, Any] = {}
    skipped: dict[int, str] = {}

    with ThreadPoolExecutor(max_workers=safe_workers) as pool:
        future_to_idx = {
            pool.submit(
                use_runner,
                spec,
                runs_dir=runs_dir,
                cwd=cwd,
                max_retries=max_retries,
            ): i
            for i, spec in enumerate(specs)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                # Capture rather than abort the pool — one bad spec
                # shouldn't take the summary down for the others.
                skipped[idx] = f"{type(exc).__name__}: {exc}"

    duration = time.perf_counter() - started

    per_spec_ordered: list[TesterRunResult] = []
    ok_count = 0
    fail_count = 0
    for i, spec in enumerate(specs):
        if i in results:
            res = results[i]
            per_spec_ordered.append(res)
            if getattr(res, "ok", False):
                ok_count += 1
            else:
                fail_count += 1
        # Skipped: don't synthesize a fake TesterRunResult — surfaces
        # via the `skipped_count` and the caller's JSON output.

    return PoolRunResult(
        total=len(specs),
        ok_count=ok_count,
        fail_count=fail_count,
        skipped_count=len(skipped),
        duration_seconds=round(duration, 3),
        per_spec=per_spec_ordered,
    )


__all__ = [
    "PoolRunResult",
    "run_specs_parallel",
    "select_specs",
]
