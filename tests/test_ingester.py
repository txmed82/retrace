from datetime import datetime, timezone
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from retrace.config import PostHogConfig
from retrace.ingester import PostHogIngester
from retrace.storage import Storage


@pytest.fixture
def cfg() -> PostHogConfig:
    return PostHogConfig(host="https://us.i.posthog.com", project_id="42", api_key="phx_test")


def test_fetch_sessions_since_stores_metadata_and_snapshots(
    httpx_mock: HTTPXMock, tmp_path: Path, cfg: PostHogConfig
):
    since = datetime(2026, 4, 19, 10, 0, tzinfo=timezone.utc)

    httpx_mock.add_response(
        method="GET",
        url=(
            "https://us.i.posthog.com/api/projects/42/session_recordings"
            "?date_from=2026-04-19T10%3A00%3A00%2B00%3A00&limit=50"
        ),
        json={
            "results": [
                {
                    "id": "sess-1",
                    "start_time": "2026-04-19T11:00:00+00:00",
                    "recording_duration": 42,
                    "distinct_id": "user-1",
                    "click_count": 2,
                }
            ],
            "next": None,
        },
    )
    httpx_mock.add_response(
        method="GET",
        url="https://us.i.posthog.com/api/projects/42/session_recordings/sess-1/snapshots",
        json={"snapshots": [{"type": 4, "timestamp": 0, "data": {"href": "https://x.com/"}}]},
    )

    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    ingester = PostHogIngester(cfg, store, data_dir=tmp_path / "data")

    ids = ingester.fetch_since(since, max_sessions=50)

    assert ids == ["sess-1"]
    assert store.get_session("sess-1") is not None
    events_path = tmp_path / "data" / "sessions" / "sess-1.json"
    assert events_path.exists()


def test_fetch_since_handles_null_recording_duration(
    httpx_mock: HTTPXMock, tmp_path: Path, cfg: PostHogConfig
):
    since = datetime(2026, 4, 19, 10, 0, tzinfo=timezone.utc)

    httpx_mock.add_response(
        method="GET",
        url=(
            "https://us.i.posthog.com/api/projects/42/session_recordings"
            "?date_from=2026-04-19T10%3A00%3A00%2B00%3A00&limit=50"
        ),
        json={
            "results": [
                {
                    "id": "sess-null",
                    "start_time": "2026-04-19T11:00:00+00:00",
                    "recording_duration": None,
                    "distinct_id": None,
                    "click_count": None,
                }
            ],
            "next": None,
        },
    )
    httpx_mock.add_response(
        method="GET",
        url="https://us.i.posthog.com/api/projects/42/session_recordings/sess-null/snapshots",
        json={"snapshots": []},
    )

    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    ingester = PostHogIngester(cfg, store, data_dir=tmp_path / "data")

    ids = ingester.fetch_since(since, max_sessions=50)

    assert ids == ["sess-null"]
    s = store.get_session("sess-null")
    assert s is not None
    assert s.duration_ms == 0
    assert s.event_count == 0


def test_fetch_since_skips_failing_session_and_continues(
    httpx_mock: HTTPXMock, tmp_path: Path, cfg: PostHogConfig
):
    since = datetime(2026, 4, 19, 10, 0, tzinfo=timezone.utc)

    httpx_mock.add_response(
        method="GET",
        url=(
            "https://us.i.posthog.com/api/projects/42/session_recordings"
            "?date_from=2026-04-19T10%3A00%3A00%2B00%3A00&limit=50"
        ),
        json={
            "results": [
                {
                    "id": "sess-fail",
                    "start_time": "2026-04-19T11:00:00+00:00",
                    "recording_duration": 10,
                    "distinct_id": "u1",
                    "click_count": 1,
                },
                {
                    "id": "sess-ok",
                    "start_time": "2026-04-19T11:05:00+00:00",
                    "recording_duration": 5,
                    "distinct_id": "u2",
                    "click_count": 1,
                },
            ],
            "next": None,
        },
    )
    httpx_mock.add_response(
        method="GET",
        url="https://us.i.posthog.com/api/projects/42/session_recordings/sess-fail/snapshots",
        status_code=500,
        json={"error": "boom"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://us.i.posthog.com/api/projects/42/session_recordings/sess-ok/snapshots",
        json={"snapshots": []},
    )

    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    ingester = PostHogIngester(cfg, store, data_dir=tmp_path / "data")

    ids = ingester.fetch_since(since, max_sessions=50)

    assert ids == ["sess-ok"]
    assert store.get_session("sess-fail") is None
    assert store.get_session("sess-ok") is not None
