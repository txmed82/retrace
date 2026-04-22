from datetime import datetime, timezone
from pathlib import Path

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


import pytest


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
    )
    assert fid2 == fid

    rows = store.list_report_findings("reports/2026-04-22.md")
    assert len(rows) == 1
    assert rows[0].title == "Dead clicks on checkout (updated)"
    assert rows[0].evidence_text == "Evidence B"
