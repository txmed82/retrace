"""P2.3 — tests for `retrace.retention` + the storage helpers it
builds on. Tests advance the clock via the `now` parameter rather
than backdating data — simpler and tests the same semantics.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from retrace.retention import (
    RetentionPolicy,
    apply_retention,
    result_to_dict,
)
from retrace.storage import Storage


def _store(tmp_path: Path) -> Storage:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    return store


def _insert_replay_batch(store: Storage) -> None:
    """Insert one replay batch + its session at the current time."""
    store.insert_replay_batch(
        project_id="proj_1",
        environment_id="env_1",
        session_id="sess_a",
        sequence=1,
        events=[{"type": 0, "timestamp": 1}],
        flush_type="normal",
    )


def _insert_otel_event(store: Storage) -> None:
    """Insert one OTel event at the current time."""
    store.append_otel_event(
        project_id="proj_1",
        environment_id="env_1",
        signal_type="log",
        trace_id="tr_1",
        span_id="sp_1",
        name="test.event",
        severity="INFO",
        body="hello",
        occurred_at_ms=1,
        attributes={},
    )


# ---------------------------------------------------------------------------
# Storage helpers — the building blocks
# ---------------------------------------------------------------------------


def test_prune_replay_batches_dry_run_counts_without_deleting(tmp_path):
    store = _store(tmp_path)
    _insert_replay_batch(store)
    future = datetime.now(timezone.utc) + timedelta(days=60)
    count = store.prune_replay_batches(
        retention_days=30, dry_run=True, now=future
    )
    assert count == 1
    # Row still there.
    assert store.prune_replay_batches(
        retention_days=30, dry_run=True, now=future
    ) == 1


def test_prune_replay_batches_deletes_when_not_dry_run(tmp_path):
    store = _store(tmp_path)
    _insert_replay_batch(store)
    future = datetime.now(timezone.utc) + timedelta(days=60)
    count = store.prune_replay_batches(
        retention_days=30, dry_run=False, now=future
    )
    assert count == 1
    # Second call: nothing left to prune.
    assert store.prune_replay_batches(
        retention_days=30, dry_run=False, now=future
    ) == 0


def test_prune_replay_batches_respects_cutoff(tmp_path):
    """A row inserted now must NOT be pruned with the current clock,
    even at a tight retention horizon."""
    store = _store(tmp_path)
    _insert_replay_batch(store)
    # Now-ish; batch is at most milliseconds old.
    assert store.prune_replay_batches(retention_days=1, dry_run=False) == 0


def test_prune_otel_events(tmp_path):
    store = _store(tmp_path)
    _insert_otel_event(store)
    future = datetime.now(timezone.utc) + timedelta(days=60)
    assert (
        store.prune_otel_events(retention_days=30, dry_run=False, now=future) == 1
    )
    assert (
        store.prune_otel_events(retention_days=30, dry_run=False, now=future) == 0
    )


def test_project_environment_pairs_empty_on_fresh_install(tmp_path):
    store = _store(tmp_path)
    assert store.project_environment_pairs() == []


# ---------------------------------------------------------------------------
# apply_retention orchestration
# ---------------------------------------------------------------------------


def test_apply_retention_dry_run_on_empty_install(tmp_path):
    """Fresh install → zero everything, no errors. The CLI is safe
    to run before any data has been recorded."""
    store = _store(tmp_path)
    result = apply_retention(
        store=store,
        data_dir=tmp_path,
        policy=RetentionPolicy(),
        dry_run=True,
    )
    assert result.dry_run is True
    assert result.failures == 0
    assert result.replay_batches == 0
    assert result.otel_events == 0
    assert result.run_artifact_dirs == 0


def test_apply_retention_prunes_replay_batches_and_otel(tmp_path):
    store = _store(tmp_path)
    _insert_replay_batch(store)
    _insert_otel_event(store)

    future = datetime.now(timezone.utc) + timedelta(days=60)
    result = apply_retention(
        store=store,
        data_dir=tmp_path,
        policy=RetentionPolicy(replay_batches_days=30, otel_events_days=30),
        dry_run=False,
        now=future,
    )
    assert result.replay_batches == 1
    assert result.otel_events == 1


def test_apply_retention_dry_run_does_not_delete(tmp_path):
    store = _store(tmp_path)
    _insert_replay_batch(store)
    _insert_otel_event(store)

    future = datetime.now(timezone.utc) + timedelta(days=60)
    apply_retention(
        store=store,
        data_dir=tmp_path,
        policy=RetentionPolicy(replay_batches_days=30, otel_events_days=30),
        dry_run=True,
        now=future,
    )
    # Row still queryable via a second pass.
    second = apply_retention(
        store=store,
        data_dir=tmp_path,
        policy=RetentionPolicy(replay_batches_days=30, otel_events_days=30),
        dry_run=True,
        now=future,
    )
    assert second.replay_batches == 1
    assert second.otel_events == 1


def test_apply_retention_sweeps_run_artifact_directories(tmp_path):
    """Old run directories under `ui-tests/runs/` and
    `api-tests/runs/` are removed; specs and baselines are kept."""
    store = _store(tmp_path)
    ui_runs = tmp_path / "ui-tests" / "runs"
    api_runs = tmp_path / "api-tests" / "runs"
    baselines = tmp_path / "ui-tests" / "baselines"
    specs = tmp_path / "ui-tests" / "specs"
    for d in (ui_runs, api_runs, baselines, specs):
        d.mkdir(parents=True)
    # Two old runs + one fresh run + reference data that must NOT
    # be touched.
    old_ui = ui_runs / "run_old_ui"
    old_api = api_runs / "run_old_api"
    fresh_ui = ui_runs / "run_fresh_ui"
    for d in (old_ui, old_api, fresh_ui):
        d.mkdir()
        (d / "screenshot.png").write_bytes(b"x" * 1000)
    (baselines / "spec_1").mkdir()
    (baselines / "spec_1" / "baseline.png").write_bytes(b"baseline")
    (specs / "spec_1.json").write_text("{}")

    # Backdate the two "old" directories.
    old_ts = (datetime.now(timezone.utc) - timedelta(days=120)).timestamp()
    import os
    os.utime(old_ui, (old_ts, old_ts))
    os.utime(old_api, (old_ts, old_ts))

    result = apply_retention(
        store=store,
        data_dir=tmp_path,
        policy=RetentionPolicy(run_artifact_days=30),
        dry_run=False,
    )
    assert result.run_artifact_dirs == 2
    assert result.run_artifact_bytes >= 2000

    # Old run dirs gone.
    assert not old_ui.exists()
    assert not old_api.exists()
    # Fresh run kept.
    assert fresh_ui.exists()
    # Reference state untouched.
    assert (baselines / "spec_1" / "baseline.png").exists()
    assert (specs / "spec_1.json").exists()


def test_apply_retention_dry_run_does_not_remove_dirs(tmp_path):
    store = _store(tmp_path)
    ui_runs = tmp_path / "ui-tests" / "runs"
    ui_runs.mkdir(parents=True)
    old = ui_runs / "run_old"
    old.mkdir()
    (old / "f.txt").write_bytes(b"hello")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=120)).timestamp()
    import os
    os.utime(old, (old_ts, old_ts))

    result = apply_retention(
        store=store,
        data_dir=tmp_path,
        policy=RetentionPolicy(run_artifact_days=30),
        dry_run=True,
    )
    assert result.run_artifact_dirs == 1
    # But the directory is still there.
    assert old.exists()
    assert (old / "f.txt").exists()


def test_apply_retention_missing_data_dir_is_noop(tmp_path):
    """The retention sweep tolerates a `data_dir` that doesn't have
    `ui-tests/runs/` or `api-tests/runs/` yet — fresh installs.
    """
    store = _store(tmp_path)
    nowhere = tmp_path / "does-not-exist"
    result = apply_retention(
        store=store,
        data_dir=nowhere,
        policy=RetentionPolicy(),
        dry_run=False,
    )
    assert result.run_artifact_dirs == 0
    assert result.run_artifact_bytes == 0


def test_result_to_dict_shape_is_stable(tmp_path):
    """The CLI emits this JSON; downstream tools rely on the keys."""
    store = _store(tmp_path)
    result = apply_retention(
        store=store, data_dir=tmp_path, policy=RetentionPolicy(), dry_run=True
    )
    out = result_to_dict(result)
    assert set(out["pruned"].keys()) == {
        "failures",
        "evidence",
        "incident_links",
        "incidents",
        "source_maps",
        "rate_limit_rows",
        "replay_batches",
        "otel_events",
        "run_artifact_dirs",
        "run_artifact_bytes",
    }
    assert set(out["policy"].keys()) == {
        "failures_days",
        "evidence_days",
        "source_maps_days",
        "rate_limit_hours",
        "replay_batches_days",
        "otel_events_days",
        "run_artifact_days",
    }
    # Round-trips through json.
    assert json.loads(json.dumps(out)) == out
