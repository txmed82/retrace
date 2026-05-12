"""P2.3 — tests for `retrace.retention` + the storage helpers it
builds on. Tests advance the clock via the `now` parameter rather
than backdating data — simpler and tests the same semantics.
"""

from __future__ import annotations

import json
import sqlite3
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


def _insert_replay_batch(store: Storage, *, backdated_days: int = 0) -> None:
    """Insert one replay batch. If `backdated_days > 0`, rewrite
    `received_at` after insert so the row appears that many days old.

    Backdating in the DB rather than fake-clocking the prune helpers
    is more honest: the DB engine computes the cutoff via
    `datetime('now', ?)`, which we cannot override from Python.
    """
    store.insert_replay_batch(
        project_id="proj_1",
        environment_id="env_1",
        session_id="sess_a",
        sequence=1,
        events=[{"type": 0, "timestamp": 1}],
        flush_type="normal",
    )
    if backdated_days > 0:
        backdated = (
            datetime.now(timezone.utc) - timedelta(days=backdated_days)
        ).strftime("%Y-%m-%d %H:%M:%S")
        _raw(store.path).execute(
            "UPDATE replay_batches SET received_at = ?", (backdated,)
        ).connection.commit()


def _insert_otel_event(store: Storage, *, backdated_days: int = 0) -> None:
    """Insert one OTel event. Backdates `created_at` like the replay
    helper above for testability."""
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
    if backdated_days > 0:
        backdated = (
            datetime.now(timezone.utc) - timedelta(days=backdated_days)
        ).strftime("%Y-%m-%d %H:%M:%S")
        _raw(store.path).execute(
            "UPDATE otel_events SET created_at = ?", (backdated,)
        ).connection.commit()


def _raw(path: Path) -> sqlite3.Cursor:
    """Direct sqlite cursor for test-time backdating only. Bypasses
    the Storage abstraction so we can write to columns that aren't
    exposed through the public API."""
    conn = sqlite3.connect(str(path))
    return conn.cursor()


# ---------------------------------------------------------------------------
# Storage helpers — the building blocks
# ---------------------------------------------------------------------------


def test_prune_replay_batches_dry_run_counts_without_deleting(tmp_path):
    store = _store(tmp_path)
    _insert_replay_batch(store, backdated_days=60)
    count = store.prune_replay_batches(retention_days=30, dry_run=True)
    assert count == 1
    # Row still there.
    assert store.prune_replay_batches(retention_days=30, dry_run=True) == 1


def test_prune_replay_batches_deletes_when_not_dry_run(tmp_path):
    store = _store(tmp_path)
    _insert_replay_batch(store, backdated_days=60)
    count = store.prune_replay_batches(retention_days=30, dry_run=False)
    assert count == 1
    # Second call: nothing left to prune.
    assert store.prune_replay_batches(retention_days=30, dry_run=False) == 0


def test_prune_replay_batches_respects_cutoff(tmp_path):
    """A row inserted now must NOT be pruned with the current clock,
    even at a tight retention horizon."""
    store = _store(tmp_path)
    _insert_replay_batch(store)
    # Now-ish; batch is at most milliseconds old.
    assert store.prune_replay_batches(retention_days=1, dry_run=False) == 0


def test_prune_replay_batches_handles_evening_time_of_day(tmp_path):
    """Regression for the cutoff-format bug CodeRabbit caught on
    PR #138: a row stored at e.g. `2026-04-12 23:00:00` (SQLite
    space format) would over-prune under any cutoff later in the
    same day if the cutoff was an isoformat string (`T` separator
    sorts after space). Using `datetime('now', ?)` for the cutoff
    avoids the format mismatch by computing the cutoff DB-side."""
    store = _store(tmp_path)
    # Insert a row whose `received_at` is in the recent past, then
    # force a time-of-day in the second half of the day.
    store.insert_replay_batch(
        project_id="proj_1",
        environment_id="env_1",
        session_id="sess_a",
        sequence=1,
        events=[{"type": 0}],
        flush_type="normal",
    )
    today_evening = datetime.now(timezone.utc).replace(
        hour=23, minute=0, second=0, microsecond=0
    ).strftime("%Y-%m-%d %H:%M:%S")
    _raw(store.path).execute(
        "UPDATE replay_batches SET received_at = ?", (today_evening,)
    ).connection.commit()
    # 1 day retention — a row from this evening must NOT be pruned.
    assert store.prune_replay_batches(retention_days=1, dry_run=False) == 0


def test_prune_otel_events(tmp_path):
    store = _store(tmp_path)
    _insert_otel_event(store, backdated_days=60)
    assert store.prune_otel_events(retention_days=30, dry_run=False) == 1
    assert store.prune_otel_events(retention_days=30, dry_run=False) == 0


def test_project_environment_pairs_empty_on_fresh_install(tmp_path):
    store = _store(tmp_path)
    assert store.project_environment_pairs() == []


def test_project_environment_pairs_unions_source_maps_without_failures(tmp_path):
    """Regression for CodeRabbit's PR #138 finding: a project with
    source-map uploads but no failures yet would otherwise never
    have its source_maps swept, because the old query only looked
    at the `failures` table."""
    store = _store(tmp_path)
    # Insert a source_maps row directly — there's no Storage method
    # that lets us bypass project provisioning for this test.
    conn = sqlite3.connect(str(store.path))
    try:
        conn.execute(
            """
            INSERT INTO source_maps
              (id, public_id, project_id, environment_id,
               release, artifact_url, uploaded_at)
            VALUES (?, ?, ?, ?, 'v1', 'https://x/app.js', datetime('now'))
            """,
            ("sm_1", "sm_pub_1", "proj_x", "env_x"),
        )
        conn.commit()
    finally:
        conn.close()
    pairs = store.project_environment_pairs()
    assert ("proj_x", "env_x") in pairs


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
    _insert_replay_batch(store, backdated_days=60)
    _insert_otel_event(store, backdated_days=60)

    result = apply_retention(
        store=store,
        data_dir=tmp_path,
        policy=RetentionPolicy(replay_batches_days=30, otel_events_days=30),
        dry_run=False,
    )
    assert result.replay_batches == 1
    assert result.otel_events == 1


def test_apply_retention_dry_run_does_not_delete(tmp_path):
    store = _store(tmp_path)
    _insert_replay_batch(store, backdated_days=60)
    _insert_otel_event(store, backdated_days=60)

    apply_retention(
        store=store,
        data_dir=tmp_path,
        policy=RetentionPolicy(replay_batches_days=30, otel_events_days=30),
        dry_run=True,
    )
    # Row still queryable via a second pass.
    second = apply_retention(
        store=store,
        data_dir=tmp_path,
        policy=RetentionPolicy(replay_batches_days=30, otel_events_days=30),
        dry_run=True,
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
