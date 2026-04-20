import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from retrace.config import (
    DetectorsConfig,
    LLMConfig,
    PostHogConfig,
    RetraceConfig,
    RunConfig,
)
from retrace.pipeline import run_pipeline
from retrace.storage import SessionMeta, Storage

# Trigger detector self-registration on import.
import retrace.detectors.console_error  # noqa: F401
import retrace.detectors.network_5xx  # noqa: F401
import retrace.detectors.rage_click  # noqa: F401


def _make_cfg(tmp_path: Path, max_sessions: int = 10) -> RetraceConfig:
    return RetraceConfig(
        posthog=PostHogConfig(host="https://ph", project_id="42", api_key="phx"),
        llm=LLMConfig(base_url="http://llm/v1", model="m", api_key=None),
        run=RunConfig(
            lookback_hours=6,
            max_sessions_per_run=max_sessions,
            output_dir=tmp_path / "reports",
            data_dir=tmp_path / "data",
        ),
        detectors=DetectorsConfig(console_error=True, network_5xx=True, rage_click=True),
    )


def _error_session_events() -> list[dict]:
    return [
        {"type": 4, "timestamp": 0, "data": {"href": "https://x/checkout"}},
        {
            "type": 6,
            "timestamp": 500,
            "data": {
                "plugin": "rrweb/console@1",
                "payload": {"level": "error", "payload": ["TypeError boom"]},
            },
        },
    ]


def _llm_happy_path_response() -> dict:
    return {
        "title": "Checkout crashes",
        "severity": "critical",
        "category": "functional_error",
        "what_happened": "TypeError shown after open.",
        "likely_cause": "Null ref.",
        "reproduction_steps": ["open /checkout"],
        "confidence": "high",
    }


def test_run_pipeline_end_to_end_with_fake_llm_and_ingester(tmp_path: Path):
    cfg = _make_cfg(tmp_path)
    store = Storage(tmp_path / "data" / "retrace.db")
    store.init_schema()

    ingester = MagicMock()
    ingester.fetch_since.return_value = ["sess-1"]
    ingester.load_events.return_value = _error_session_events()

    store.upsert_session(
        SessionMeta(
            id="sess-1",
            project_id="42",
            started_at=datetime(2026, 4, 19, 13, 0, tzinfo=timezone.utc),
            duration_ms=1000,
            distinct_id="u1",
            event_count=2,
        )
    )

    llm_client = MagicMock()
    llm_client.chat_json.return_value = _llm_happy_path_response()

    summary = run_pipeline(
        cfg=cfg,
        store=store,
        ingester=ingester,
        llm_client=llm_client,
        now=datetime(2026, 4, 19, 14, 0, tzinfo=timezone.utc),
    )

    assert summary.sessions_scanned == 1
    assert summary.sessions_flagged == 1
    assert summary.sessions_errored == 0
    assert summary.cap_hit is False

    reports = list((tmp_path / "reports").glob("*.md"))
    assert len(reports) == 1
    text = reports[0].read_text()
    assert "Checkout crashes" in text
    assert "Critical" in text


def test_run_pipeline_skips_sessions_with_no_signals(tmp_path: Path):
    cfg = _make_cfg(tmp_path)
    store = Storage(tmp_path / "data" / "retrace.db")
    store.init_schema()

    ingester = MagicMock()
    ingester.fetch_since.return_value = ["sess-clean"]
    ingester.load_events.return_value = [
        {"type": 4, "timestamp": 0, "data": {"href": "https://x/home"}}
    ]

    llm_client = MagicMock()

    summary = run_pipeline(
        cfg=cfg,
        store=store,
        ingester=ingester,
        llm_client=llm_client,
        now=datetime(2026, 4, 19, 14, 0, tzinfo=timezone.utc),
    )

    assert summary.sessions_scanned == 1
    assert summary.sessions_flagged == 0
    llm_client.chat_json.assert_not_called()


def test_run_pipeline_isolates_failing_session_and_continues(tmp_path: Path):
    cfg = _make_cfg(tmp_path)
    store = Storage(tmp_path / "data" / "retrace.db")
    store.init_schema()

    for sid, ts in [
        ("sess-fail", datetime(2026, 4, 19, 13, 0, tzinfo=timezone.utc)),
        ("sess-ok", datetime(2026, 4, 19, 13, 30, tzinfo=timezone.utc)),
    ]:
        store.upsert_session(
            SessionMeta(
                id=sid,
                project_id="42",
                started_at=ts,
                duration_ms=1000,
                distinct_id=None,
                event_count=0,
            )
        )

    ingester = MagicMock()
    ingester.fetch_since.return_value = ["sess-fail", "sess-ok"]

    def _load(sid: str):
        if sid == "sess-fail":
            raise RuntimeError("corrupt snapshot")
        return _error_session_events()

    ingester.load_events.side_effect = _load

    llm_client = MagicMock()
    llm_client.chat_json.return_value = _llm_happy_path_response()

    summary = run_pipeline(
        cfg=cfg,
        store=store,
        ingester=ingester,
        llm_client=llm_client,
        now=datetime(2026, 4, 19, 14, 0, tzinfo=timezone.utc),
    )

    assert summary.sessions_scanned == 2
    assert summary.sessions_flagged == 1
    assert summary.sessions_errored == 1


def test_run_pipeline_cap_hit_rewinds_cursor_to_oldest_processed(tmp_path: Path):
    cfg = _make_cfg(tmp_path, max_sessions=2)
    store = Storage(tmp_path / "data" / "retrace.db")
    store.init_schema()

    oldest = datetime(2026, 4, 19, 11, 0, tzinfo=timezone.utc)
    newest = datetime(2026, 4, 19, 13, 0, tzinfo=timezone.utc)
    for sid, ts in [("sess-old", oldest), ("sess-new", newest)]:
        store.upsert_session(
            SessionMeta(
                id=sid,
                project_id="42",
                started_at=ts,
                duration_ms=0,
                distinct_id=None,
                event_count=0,
            )
        )

    ingester = MagicMock()
    ingester.fetch_since.return_value = ["sess-new", "sess-old"]
    ingester.load_events.return_value = [
        {"type": 4, "timestamp": 0, "data": {"href": "https://x/"}}
    ]

    llm_client = MagicMock()

    run_pipeline(
        cfg=cfg,
        store=store,
        ingester=ingester,
        llm_client=llm_client,
        now=datetime(2026, 4, 19, 14, 0, tzinfo=timezone.utc),
    )

    assert store.get_last_run_cursor() == oldest


def test_run_pipeline_cap_not_hit_advances_cursor_to_now(tmp_path: Path):
    cfg = _make_cfg(tmp_path, max_sessions=10)
    store = Storage(tmp_path / "data" / "retrace.db")
    store.init_schema()

    store.upsert_session(
        SessionMeta(
            id="sess-1",
            project_id="42",
            started_at=datetime(2026, 4, 19, 13, 0, tzinfo=timezone.utc),
            duration_ms=0,
            distinct_id=None,
            event_count=0,
        )
    )

    ingester = MagicMock()
    ingester.fetch_since.return_value = ["sess-1"]
    ingester.load_events.return_value = [
        {"type": 4, "timestamp": 0, "data": {"href": "https://x/"}}
    ]

    llm_client = MagicMock()

    now = datetime(2026, 4, 19, 14, 0, tzinfo=timezone.utc)
    run_pipeline(cfg=cfg, store=store, ingester=ingester, llm_client=llm_client, now=now)

    assert store.get_last_run_cursor() == now


def test_run_pipeline_finishes_run_with_error_status_on_ingester_failure(tmp_path: Path):
    cfg = _make_cfg(tmp_path)
    store = Storage(tmp_path / "data" / "retrace.db")
    store.init_schema()

    ingester = MagicMock()
    ingester.fetch_since.side_effect = RuntimeError("posthog unreachable")
    llm_client = MagicMock()

    run_pipeline(
        cfg=cfg,
        store=store,
        ingester=ingester,
        llm_client=llm_client,
        now=datetime(2026, 4, 19, 14, 0, tzinfo=timezone.utc),
    )

    # The last run row should reflect error status, not a dangling "running".
    with store._conn() as conn:
        row = conn.execute(
            "SELECT status, error FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["status"] == "error"
    assert "posthog unreachable" in (row["error"] or "")
