from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest

from retrace.storage import Storage, SessionMeta


def test_storage_round_trips_session_meta(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()

    session = SessionMeta(
        id="sess-abc",
        project_id="42",
        started_at=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
        duration_ms=60_000,
        distinct_id="user-1",
        event_count=123,
    )
    store.upsert_session(session)

    out = store.get_session("sess-abc")
    assert out == session


def test_storage_last_cursor_defaults_to_none_then_persists(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()

    assert store.get_last_run_cursor() is None

    ts = datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc)
    store.set_last_run_cursor(ts)
    assert store.get_last_run_cursor() == ts


def test_storage_start_and_finish_run(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()

    run_id = store.start_run()
    assert isinstance(run_id, int) and run_id > 0

    store.finish_run(run_id, sessions_scanned=5, findings_count=2, status="ok")
    row = store.get_run(run_id)
    assert row.sessions_scanned == 5
    assert row.findings_count == 2
    assert row.status == "ok"
    assert row.finished_at is not None


def test_finish_processing_job_only_updates_running_jobs(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    job_id = store.enqueue_processing_job(
        project_id="proj_1",
        environment_id="env_1",
        kind="replay.finalize",
        subject_id="rs_1",
        payload={"session_id": "sess_1"},
    )

    not_running = store.finish_processing_job(job_id=job_id, status="succeeded")
    assert not_running.updated is False
    assert store.claim_processing_job(job_id) is True

    succeeded = store.finish_processing_job(job_id=job_id, status="succeeded")
    assert succeeded.updated is True

    stale = store.finish_processing_job(job_id=job_id, status="failed")
    assert stale.updated is False
    jobs = store.list_processing_jobs(kind="replay.finalize", status="succeeded")
    assert len(jobs) == 1


def test_init_schema_migrates_replay_session_public_ids(tmp_path: Path):
    db = tmp_path / "retrace.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE replay_sessions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            environment_id TEXT NOT NULL,
            stable_id TEXT NOT NULL,
            distinct_id TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            event_count INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(project_id, environment_id, stable_id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO replay_sessions
        (id, project_id, environment_id, stable_id, started_at, last_seen_at)
        VALUES ('rs_1', 'proj_1', 'env_1', 'sess_1', '2026-04-26T00:00:00+00:00',
                '2026-04-26T00:00:00+00:00')
        """
    )
    conn.commit()
    conn.close()

    store = Storage(db)
    store.init_schema()

    session = store.get_replay_session(
        project_id="proj_1",
        environment_id="env_1",
        session_id="sess_1",
    )
    assert session is not None
    assert session["public_id"] == Storage.make_replay_public_id(
        "proj_1", "env_1", "sess_1"
    )


def test_init_schema_backfills_empty_replay_public_ids(tmp_path: Path):
    db = tmp_path / "retrace.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE replay_sessions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            environment_id TEXT NOT NULL,
            stable_id TEXT NOT NULL,
            public_id TEXT NOT NULL DEFAULT '',
            distinct_id TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            event_count INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(project_id, environment_id, stable_id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO replay_sessions
        (id, project_id, environment_id, stable_id, public_id, started_at, last_seen_at)
        VALUES ('rs_2', 'proj_1', 'env_1', 'sess_2', '',
                '2026-04-26T00:00:00+00:00', '2026-04-26T00:00:00+00:00')
        """
    )
    conn.commit()
    conn.close()

    store = Storage(db)
    store.init_schema()

    session = store.get_replay_session(
        project_id="proj_1",
        environment_id="env_1",
        session_id="sess_2",
    )
    assert session is not None
    assert session["public_id"] == Storage.make_replay_public_id(
        "proj_1", "env_1", "sess_2"
    )


def test_get_run_returns_none_for_unknown_id(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    assert store.get_run(9999) is None


def test_upsert_session_rejects_naive_datetime(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    s = SessionMeta(
        id="s",
        project_id="p",
        started_at=datetime(2026, 4, 19, 12, 0),  # no tzinfo
        duration_ms=0,
        distinct_id=None,
        event_count=0,
    )
    with pytest.raises(ValueError):
        store.upsert_session(s)


def test_storage_github_repo_crud(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()

    rid = store.upsert_github_repo(
        repo_full_name="acme/widgets",
        default_branch="main",
        remote_url="https://github.com/acme/widgets.git",
    )
    assert rid > 0

    got = store.get_github_repo("acme/widgets")
    assert got is not None
    assert got.default_branch == "main"
    assert got.local_path == ""

    rows = store.list_github_repos()
    assert len(rows) == 1
    assert rows[0].repo_full_name == "acme/widgets"

    removed = store.delete_github_repo("acme/widgets")
    assert removed == 1
    assert store.get_github_repo("acme/widgets") is None


def test_storage_report_findings_upsert_and_list(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()

    fid = store.upsert_report_finding(
        report_path="reports/2026-04-22.md",
        finding_hash="abc123",
        title="Dead clicks on checkout",
        severity="high",
        category="functional_error",
        session_url="https://us.i.posthog.com/project/1/replay/s1",
        evidence_text="Evidence A",
        distinct_id="user-123",
        error_issue_ids=["ERR-42"],
        trace_ids=["trace-1"],
        top_stack_frame="TypeError: Cannot read properties of undefined",
        error_tracking_url="https://us.i.posthog.com/project/1/error_tracking?session_id=s1",
        logs_url="https://us.i.posthog.com/project/1/logs?session_id=s1",
        first_error_ts_ms=100,
        last_error_ts_ms=200,
    )
    assert fid > 0

    # upsert path
    fid2 = store.upsert_report_finding(
        report_path="reports/2026-04-22.md",
        finding_hash="abc123",
        title="Dead clicks on checkout (updated)",
        severity="high",
        category="functional_error",
        session_url="https://us.i.posthog.com/project/1/replay/s1",
        evidence_text="Evidence B",
        distinct_id="user-123",
        error_issue_ids=["ERR-42"],
        trace_ids=["trace-1"],
        top_stack_frame="TypeError: Cannot read properties of undefined",
        error_tracking_url="https://us.i.posthog.com/project/1/error_tracking?session_id=s1",
        logs_url="https://us.i.posthog.com/project/1/logs?session_id=s1",
        first_error_ts_ms=100,
        last_error_ts_ms=200,
    )
    assert fid2 == fid

    rows = store.list_report_findings("reports/2026-04-22.md")
    assert len(rows) == 1
    assert rows[0].title == "Dead clicks on checkout (updated)"
    assert rows[0].evidence_text == "Evidence B"
    assert rows[0].distinct_id == "user-123"
    assert rows[0].error_issue_ids == ["ERR-42"]
    assert rows[0].trace_ids == ["trace-1"]
    assert rows[0].top_stack_frame.startswith("TypeError")
    assert rows[0].error_tracking_url.endswith("/error_tracking?session_id=s1")
    assert rows[0].logs_url.endswith("/logs?session_id=s1")
    assert rows[0].first_error_ts_ms == 100
    assert rows[0].last_error_ts_ms == 200
    assert rows[0].regression_state == "new"
    assert rows[0].regression_occurrence_count == 1


def test_reconcile_regression_states_tracks_new_ongoing_resolved_regressed(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()

    # First report: A and B are new
    for h in ["A", "B"]:
        store.upsert_report_finding(
            report_path="reports/r1.md",
            finding_hash=h,
            title=f"Finding {h}",
            severity="medium",
            category="functional_error",
            session_url=f"https://x/replay/{h}",
        )
    s1 = store.reconcile_regression_states(
        report_path="reports/r1.md", finding_hashes=["A", "B"]
    )
    assert s1["A"] == ("new", 1)
    assert s1["B"] == ("new", 1)

    # Second report: A remains, B resolved
    store.upsert_report_finding(
        report_path="reports/r2.md",
        finding_hash="A",
        title="Finding A",
        severity="medium",
        category="functional_error",
        session_url="https://x/replay/A",
    )
    s2 = store.reconcile_regression_states(
        report_path="reports/r2.md", finding_hashes=["A"]
    )
    assert s2["A"] == ("ongoing", 2)

    # Third report: B appears again -> regressed
    store.upsert_report_finding(
        report_path="reports/r3.md",
        finding_hash="B",
        title="Finding B",
        severity="medium",
        category="functional_error",
        session_url="https://x/replay/B",
    )
    s3 = store.reconcile_regression_states(
        report_path="reports/r3.md", finding_hashes=["B"]
    )
    assert s3["B"] == ("regressed", 2)

    rows = store.list_report_findings("reports/r3.md")
    assert rows[0].finding_hash == "B"
    assert rows[0].regression_state == "regressed"
    assert rows[0].regression_occurrence_count == 2
