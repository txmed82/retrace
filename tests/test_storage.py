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
