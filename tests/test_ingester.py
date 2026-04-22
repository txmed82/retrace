import json
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


def test_fetch_since_follows_pagination_next_link(
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
                    "id": "sess-a",
                    "start_time": "2026-04-19T11:00:00+00:00",
                    "recording_duration": 10,
                    "distinct_id": "u1",
                    "click_count": 1,
                    "event_count": 5,
                }
            ],
            "next": (
                "https://us.i.posthog.com/api/projects/42/session_recordings"
                "?date_from=2026-04-19T10%3A00%3A00%2B00%3A00&limit=50&cursor=abc"
            ),
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=(
            "https://us.i.posthog.com/api/projects/42/session_recordings"
            "?date_from=2026-04-19T10%3A00%3A00%2B00%3A00&limit=50&cursor=abc"
        ),
        json={
            "results": [
                {
                    "id": "sess-b",
                    "start_time": "2026-04-19T11:30:00+00:00",
                    "recording_duration": 20,
                    "distinct_id": "u2",
                    "click_count": 0,
                    "event_count": 3,
                }
            ],
            "next": None,
        },
    )
    for sid in ("sess-a", "sess-b"):
        httpx_mock.add_response(
            method="GET",
            url=f"https://us.i.posthog.com/api/projects/42/session_recordings/{sid}/snapshots",
            json={"snapshots": []},
        )

    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    ingester = PostHogIngester(cfg, store, data_dir=tmp_path / "data")

    ids = ingester.fetch_since(since, max_sessions=50)

    assert ids == ["sess-a", "sess-b"]


def test_fetch_since_stops_at_max_sessions_mid_page(
    httpx_mock: HTTPXMock, tmp_path: Path, cfg: PostHogConfig
):
    since = datetime(2026, 4, 19, 10, 0, tzinfo=timezone.utc)
    httpx_mock.add_response(
        method="GET",
        url=(
            "https://us.i.posthog.com/api/projects/42/session_recordings"
            "?date_from=2026-04-19T10%3A00%3A00%2B00%3A00&limit=2"
        ),
        json={
            "results": [
                {"id": "sess-1", "start_time": "2026-04-19T11:00:00+00:00", "recording_duration": 1, "distinct_id": None, "click_count": 0, "event_count": 0},
                {"id": "sess-2", "start_time": "2026-04-19T11:01:00+00:00", "recording_duration": 1, "distinct_id": None, "click_count": 0, "event_count": 0},
                {"id": "sess-3", "start_time": "2026-04-19T11:02:00+00:00", "recording_duration": 1, "distinct_id": None, "click_count": 0, "event_count": 0},
            ],
            "next": "https://us.i.posthog.com/...cursor=xyz",
        },
    )
    for sid in ("sess-1", "sess-2"):
        httpx_mock.add_response(
            method="GET",
            url=f"https://us.i.posthog.com/api/projects/42/session_recordings/{sid}/snapshots",
            json={"snapshots": []},
        )

    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    ingester = PostHogIngester(cfg, store, data_dir=tmp_path / "data")

    ids = ingester.fetch_since(since, max_sessions=2)

    assert ids == ["sess-1", "sess-2"]


def test_fetch_sessions_supports_posthog_blob_v2_sources(
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
                    "id": "sess-blob",
                    "start_time": "2026-04-19T11:00:00+00:00",
                    "recording_duration": 7,
                    "distinct_id": "u-blob",
                    "click_count": 1,
                }
            ],
            "next": None,
        },
    )
    httpx_mock.add_response(
        method="GET",
        url="https://us.i.posthog.com/api/projects/42/session_recordings/sess-blob/snapshots",
        json={"sources": [{"source": "blob_v2", "blob_key": "0"}]},
    )
    httpx_mock.add_response(
        method="GET",
        url=(
            "https://us.i.posthog.com/api/projects/42/session_recordings/sess-blob/snapshots"
            "?source=blob_v2&start_blob_key=0&end_blob_key=0"
        ),
        text=(
            '["sess-blob",{"timestamp":1,"type":4,"data":{"href":"https://cerebrallabs.io"}}]\n'
            '["sess-blob",{"timestamp":2,"type":6,"data":{"source":"console","level":"error","payload":"boom"}}]\n'
        ),
        headers={"content-type": "application/jsonl"},
    )

    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    ingester = PostHogIngester(cfg, store, data_dir=tmp_path / "data")

    ids = ingester.fetch_since(since, max_sessions=50)

    assert ids == ["sess-blob"]
    events_path = tmp_path / "data" / "sessions" / "sess-blob.json"
    events = json.loads(events_path.read_text())
    assert len(events) == 2
    assert events[0]["type"] == 4
    assert events[1]["type"] == 6
