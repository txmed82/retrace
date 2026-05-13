"""P3.1 — flake-quarantine heuristic tests.

Covers the storage-side state machine; the tester-CLI integration
test is the smallest possible smoke check that the gate fires.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from retrace.storage import Storage


def _store(tmp_path: Path) -> Storage:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    return store


def _record(store: Storage, *, spec_id: str, outcome: str, run_id: str = "") -> str:
    return store.record_tester_run_outcome(
        spec_id=spec_id,
        run_id=run_id or f"run-{int(time.time() * 1000)}",
        outcome=outcome,
    )


# ---------------------------------------------------------------------------
# Outcome recording
# ---------------------------------------------------------------------------


def test_first_pass_keeps_active(tmp_path):
    store = _store(tmp_path)
    status = _record(store, spec_id="spec_a", outcome="pass")
    assert status == "active"
    assert store.is_spec_quarantined("spec_a") is False


def test_first_fail_keeps_active(tmp_path):
    store = _store(tmp_path)
    status = _record(store, spec_id="spec_a", outcome="fail")
    assert status == "active"


def test_unknown_outcome_is_treated_as_fail(tmp_path):
    """The auto-quarantine heuristic must not silently exclude
    unexpected outcome labels — they count as failures."""
    store = _store(tmp_path)
    _record(store, spec_id="spec_a", outcome="pass")
    _record(store, spec_id="spec_a", outcome="weird-label")
    _record(store, spec_id="spec_a", outcome="pass")
    assert store.is_spec_quarantined("spec_a") is True


# ---------------------------------------------------------------------------
# Auto-quarantine: pass → fail → pass within 24h
# ---------------------------------------------------------------------------


def test_pass_fail_pass_within_24h_quarantines(tmp_path):
    store = _store(tmp_path)
    _record(store, spec_id="spec_a", outcome="pass")
    _record(store, spec_id="spec_a", outcome="fail")
    status = _record(store, spec_id="spec_a", outcome="pass")
    assert status == "quarantined"
    assert store.is_spec_quarantined("spec_a") is True
    s = store.quarantine_status("spec_a")
    assert s["status"] == "quarantined"
    assert "pass→fail→pass" in s["quarantine_reason"]


def test_old_fail_then_recent_pass_pass_does_not_quarantine(tmp_path):
    """If the pass → fail happened more than 24h ago, the heuristic
    shouldn't fire — that's just a recovered spec, not a flake."""
    store = _store(tmp_path)
    _record(store, spec_id="spec_a", outcome="pass")
    _record(store, spec_id="spec_a", outcome="fail")
    # Backdate the first two outcomes to ~3 days ago.
    backdated = (datetime.now(timezone.utc) - timedelta(days=3)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )
    conn = sqlite3.connect(str(store.path))
    try:
        conn.execute(
            "UPDATE tester_spec_run_outcomes SET recorded_at = ? WHERE spec_id = ?",
            (backdated, "spec_a"),
        )
        conn.commit()
    finally:
        conn.close()
    status = _record(store, spec_id="spec_a", outcome="pass")
    assert status == "active"


def test_two_fails_in_a_row_does_not_quarantine(tmp_path):
    """Two failures in a row is a real regression, not a flake.
    Quarantine is for intermittent failures only."""
    store = _store(tmp_path)
    _record(store, spec_id="spec_a", outcome="pass")
    _record(store, spec_id="spec_a", outcome="fail")
    status = _record(store, spec_id="spec_a", outcome="fail")
    assert status == "active"


def test_pass_pass_pass_does_not_quarantine(tmp_path):
    store = _store(tmp_path)
    for _ in range(3):
        _record(store, spec_id="spec_a", outcome="pass")
    assert store.is_spec_quarantined("spec_a") is False


# ---------------------------------------------------------------------------
# Auto-release: 5 consecutive passes
# ---------------------------------------------------------------------------


def test_five_consecutive_passes_release_a_quarantined_spec(tmp_path):
    store = _store(tmp_path)
    _record(store, spec_id="spec_a", outcome="pass")
    _record(store, spec_id="spec_a", outcome="fail")
    _record(store, spec_id="spec_a", outcome="pass")
    assert store.is_spec_quarantined("spec_a") is True
    # 5 passes in a row → released.
    for _ in range(5):
        _record(store, spec_id="spec_a", outcome="pass")
    assert store.is_spec_quarantined("spec_a") is False
    s = store.quarantine_status("spec_a")
    assert s["status"] == "active"


def test_intermittent_fail_during_release_streak_keeps_quarantined(tmp_path):
    """A fresh failure inside what would have been the release
    streak resets the count — the spec stays quarantined."""
    store = _store(tmp_path)
    _record(store, spec_id="spec_a", outcome="pass")
    _record(store, spec_id="spec_a", outcome="fail")
    _record(store, spec_id="spec_a", outcome="pass")  # quarantines
    assert store.is_spec_quarantined("spec_a") is True
    # Three passes (not yet 5 most-recent-passes counting the
    # quarantining pass), then a fail — keeps the spec quarantined.
    _record(store, spec_id="spec_a", outcome="pass")
    _record(store, spec_id="spec_a", outcome="pass")
    _record(store, spec_id="spec_a", outcome="pass")
    _record(store, spec_id="spec_a", outcome="fail")
    assert store.is_spec_quarantined("spec_a") is True


# ---------------------------------------------------------------------------
# Manual overrides
# ---------------------------------------------------------------------------


def test_force_quarantine(tmp_path):
    store = _store(tmp_path)
    store.force_quarantine_spec(spec_id="spec_a", reason="known flaky upstream")
    assert store.is_spec_quarantined("spec_a") is True
    s = store.quarantine_status("spec_a")
    assert s["quarantine_reason"] == "known flaky upstream"


def test_release_after_force_quarantine(tmp_path):
    store = _store(tmp_path)
    store.force_quarantine_spec(spec_id="spec_a", reason="manual")
    store.release_spec_quarantine(spec_id="spec_a", reason="fixed upstream")
    assert store.is_spec_quarantined("spec_a") is False


def test_force_quarantine_requires_spec_id(tmp_path):
    import pytest

    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.force_quarantine_spec(spec_id="   ", reason="")


# ---------------------------------------------------------------------------
# Listing / inspection
# ---------------------------------------------------------------------------


def test_list_quarantined_specs(tmp_path):
    store = _store(tmp_path)
    store.force_quarantine_spec(spec_id="spec_a", reason="manual a")
    store.force_quarantine_spec(spec_id="spec_b", reason="manual b")
    _record(store, spec_id="spec_c", outcome="pass")
    rows = store.list_quarantined_specs()
    ids = {r["spec_id"] for r in rows}
    assert ids == {"spec_a", "spec_b"}
    assert all(r["status"] == "quarantined" for r in rows)


def test_quarantine_status_returns_active_for_unknown_spec(tmp_path):
    store = _store(tmp_path)
    s = store.quarantine_status("never-seen")
    assert s["status"] == "active"
    assert s["recent_outcomes"] == []


def test_quarantine_status_includes_recent_outcomes(tmp_path):
    store = _store(tmp_path)
    _record(store, spec_id="spec_a", outcome="pass")
    _record(store, spec_id="spec_a", outcome="fail")
    _record(store, spec_id="spec_a", outcome="pass")
    s = store.quarantine_status("spec_a")
    outs = [o["outcome"] for o in s["recent_outcomes"]]
    # Most-recent first.
    assert outs == ["pass", "fail", "pass"]


def test_outcome_window_is_pruned(tmp_path):
    """The rolling outcome window caps at 20 rows per spec."""
    store = _store(tmp_path)
    for i in range(30):
        _record(store, spec_id="spec_a", outcome="pass")
    conn = sqlite3.connect(str(store.path))
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM tester_spec_run_outcomes WHERE spec_id = ?",
            ("spec_a",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert cnt == 20
