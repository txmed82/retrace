from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from retrace.detectors.base import Signal
from retrace.replay_core import (
    ReplaySignalConfig,
    process_queued_replay_jobs,
    process_replay_sessions,
    summarize_replay_issue,
)
from retrace.sinks.base import Cluster
from retrace.storage import Storage


def _workspace(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(
        org_name="Acme",
        project_name="Web",
        environment_name="production",
    )
    return store, workspace


def _navigation(url: str, ts: int = 0) -> dict[str, object]:
    return {"type": 4, "timestamp": ts, "data": {"href": url}}


def _click(node_id: int, ts: int = 100) -> dict[str, object]:
    return {
        "type": 3,
        "timestamp": ts,
        "data": {"source": 2, "type": 2, "id": node_id},
    }


def _console_error(message: str, ts: int = 1000) -> dict[str, object]:
    return {
        "type": 6,
        "timestamp": ts,
        "data": {
            "plugin": "retrace/console@1",
            "payload": {"level": "error", "payload": [message]},
        },
    }


class FailingLLM:
    def chat_json(self, *, system: str, user: str) -> dict[str, Any]:
        raise RuntimeError("offline")


def test_replay_core_aggregates_playback_batches_before_detection(tmp_path: Path) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-batches",
        sequence=1,
        events=[_click(42), _console_error("TypeError: total is undefined")],
        flush_type="normal",
    )
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-batches",
        sequence=0,
        events=[_navigation("https://app.example/checkout")],
        flush_type="normal",
    )

    result = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-batches"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )

    assert result.sessions_scanned == 1
    assert result.sessions_with_signals == 1
    assert result.signals_detected == 1
    signals = store.list_replay_signals(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-batches",
    )
    assert signals[0]["url"] == "https://app.example/checkout"
    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]
    assert json.loads(issue["reproduction_steps_json"])[:2] == [
        "Open https://app.example/checkout",
        "Click element id 42",
    ]


def test_replay_core_persists_only_configured_signal_matches(tmp_path: Path) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-config",
        sequence=0,
        events=[_navigation("https://app.example/cart"), _console_error("boom")],
        flush_type="normal",
    )

    result = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-config"],
        config=ReplaySignalConfig.from_names(["rage_click"]),
    )

    assert result.signals_detected == 0
    assert result.signals_inserted == 0
    assert result.issues == []
    assert store.list_replay_signals(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-config",
    ) == []


def test_replay_core_clusters_sessions_and_regresses_resolved_issue(tmp_path: Path) -> None:
    store, workspace = _workspace(tmp_path)
    for session_id in ["sess-a", "sess-b"]:
        store.insert_replay_batch(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            session_id=session_id,
            sequence=0,
            events=[
                _navigation("https://app.example/checkout"),
                _console_error("TypeError: total is undefined"),
            ],
            flush_type="final",
        )

    first = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-a"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    assert len(first.issues) == 1
    assert first.issues[0].inserted is True
    assert store.resolve_replay_issue(first.issues[0].issue_id) is True

    second = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-a", "sess-b"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )

    assert len(second.issues) == 1
    assert second.issues[0].inserted is False
    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]
    assert issue["id"] == first.issues[0].issue_id
    assert issue["status"] == "regressed"
    assert issue["affected_count"] == 2
    assert json.loads(issue["signal_summary_json"]) == {"console_error": 2}


def test_replay_core_uses_deterministic_fallback_when_llm_fails(tmp_path: Path) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-fallback",
        sequence=0,
        events=[
            _navigation("https://app.example/cart"),
            _click(7),
            _console_error("ReferenceError: cart is not defined", ts=500),
        ],
        flush_type="final",
    )

    result = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-fallback"],
        config=ReplaySignalConfig.from_names(["console_error"]),
        llm_client=FailingLLM(),  # type: ignore[arg-type]
    )

    assert len(result.issues) == 1
    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]
    assert issue["title"] == "ReferenceError: cart is not defined on replay"
    assert "console_error across 1 replay session(s)" in issue["summary"]
    assert issue["likely_cause"].startswith("Generated from replay signals")
    assert json.loads(issue["reproduction_steps_json"]) == [
        "Open https://app.example/cart",
        "Click element id 7",
    ]


def test_replay_summary_severity_uses_all_cluster_signals() -> None:
    cluster = Cluster(
        fingerprint="mixed",
        session_ids=["low", "high"],
        signal_summary={"dead_click": 1, "network_5xx": 1},
        primary_url="https://example.com",
        first_seen_ms=10,
        last_seen_ms=20,
    )
    finding = summarize_replay_issue(
        cluster=cluster,
        events_by_session={
            "low": [_navigation("https://example.com")],
            "high": [_navigation("https://example.com/api")],
        },
        signals_by_session={
            "low": [
                Signal(
                    session_id="low",
                    detector="dead_click",
                    timestamp_ms=10,
                    url="https://example.com",
                )
            ],
            "high": [
                Signal(
                    session_id="high",
                    detector="network_5xx",
                    timestamp_ms=20,
                    url="https://example.com/api",
                    details={"request_url": "/api/save", "status": 500},
                )
            ],
        },
    )

    assert finding.severity == "high"


def test_replay_core_processes_queued_finalize_jobs(tmp_path: Path) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-job",
        sequence=0,
        events=[
            _navigation("https://app.example/settings"),
            _console_error("Error: settings failed"),
        ],
        flush_type="final",
    )

    result = process_queued_replay_jobs(
        store=store,
        project_id=workspace.project_id,
        config=ReplaySignalConfig.from_names(["console_error"]),
    )

    assert result.jobs_seen == 1
    assert result.jobs_processed == 1
    assert result.jobs_failed == 0
    assert result.sessions_processed == 1
    assert result.issues_created_or_updated == 1
    jobs = store.list_processing_jobs(kind="replay.finalize", status="succeeded")
    assert len(jobs) == 1
    assert jobs[0]["last_error"] == ""
    issues = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )
    assert issues[0]["title"] == "Error: settings failed on replay"


def test_replay_job_project_filter_is_applied_before_limit(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    first = store.ensure_workspace(
        org_name="First",
        project_name="Web",
        environment_name="production",
    )
    second = store.ensure_workspace(
        org_name="Second",
        project_name="Web",
        environment_name="production",
    )
    store.insert_replay_batch(
        project_id=first.project_id,
        environment_id=first.environment_id,
        session_id="sess-first",
        sequence=0,
        events=[_navigation("https://first.example"), _console_error("Error: first")],
        flush_type="final",
    )
    store.insert_replay_batch(
        project_id=second.project_id,
        environment_id=second.environment_id,
        session_id="sess-second",
        sequence=0,
        events=[_navigation("https://second.example"), _console_error("Error: second")],
        flush_type="final",
    )

    result = process_queued_replay_jobs(
        store=store,
        project_id=second.project_id,
        limit=1,
        config=ReplaySignalConfig.from_names(["console_error"]),
    )

    assert result.jobs_seen == 1
    assert result.jobs_processed == 1
    first_jobs = store.list_processing_jobs(
        kind="replay.finalize",
        status="queued",
        project_id=first.project_id,
    )
    second_jobs = store.list_processing_jobs(
        kind="replay.finalize",
        status="succeeded",
        project_id=second.project_id,
    )
    assert len(first_jobs) == 1
    assert len(second_jobs) == 1
    second_issues = store.list_replay_issues(
        project_id=second.project_id,
        environment_id=second.environment_id,
    )
    assert second_issues[0]["title"] == "Error: second on replay"
