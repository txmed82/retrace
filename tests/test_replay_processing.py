from __future__ import annotations

import json
from pathlib import Path

from retrace.replay_processing import (
    ReplaySignalConfig,
    process_replay_session,
)
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


def _console_event(message: str, ts: int = 1000) -> dict[str, object]:
    return {
        "type": 6,
        "timestamp": ts,
        "data": {
            "plugin": "retrace/console@1",
            "payload": {
                "level": "error",
                "payload": [message],
            },
        },
    }


def test_replay_playback_orders_batches_and_has_stable_public_id(tmp_path: Path) -> None:
    store, workspace = _workspace(tmp_path)

    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-playback",
        sequence=1,
        events=[_console_event("second", ts=2000)],
        flush_type="normal",
    )
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-playback",
        sequence=0,
        events=[{"type": 4, "timestamp": 0, "data": {"href": "https://x/start"}}],
        flush_type="normal",
    )

    playback = store.get_replay_playback(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-playback",
    )
    assert playback is not None
    assert playback.session["public_id"].startswith("rpl_")
    assert [batch["sequence"] for batch in playback.batches] == [0, 1]
    assert playback.events[0]["type"] == 4
    assert playback.events[1]["timestamp"] == 2000

    by_public_id = store.get_replay_playback(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        replay_id=playback.session["public_id"],
    )
    assert by_public_id is not None
    assert by_public_id.session["stable_id"] == "sess-playback"


def test_replay_processing_detects_configured_signals_and_creates_issue(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-console",
        sequence=0,
        events=[
            {"type": 4, "timestamp": 0, "data": {"href": "https://x/checkout"}},
            _console_event("TypeError: total is undefined"),
        ],
        flush_type="final",
        distinct_id="user-1",
    )

    results = process_replay_session(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-console",
        config=ReplaySignalConfig.from_names(["console_error"]),
    )

    assert len(results) == 1
    assert results[0].public_id.startswith("bug_")
    signals = store.list_replay_signals(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-console",
    )
    assert len(signals) == 1
    assert signals[0]["detector"] == "console_error"
    issues = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )
    assert len(issues) == 1
    assert issues[0]["public_id"] == results[0].public_id
    assert issues[0]["status"] == "open"
    assert issues[0]["affected_count"] == 1
    assert "TypeError" in issues[0]["title"]
    assert json.loads(issues[0]["reproduction_steps_json"])[0].startswith("Open ")


def test_replay_processing_respects_disabled_detectors(tmp_path: Path) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-disabled",
        sequence=0,
        events=[_console_event("boom")],
        flush_type="normal",
    )

    results = process_replay_session(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-disabled",
        config=ReplaySignalConfig.from_names(["rage_click"]),
    )

    assert results == []
    assert (
        store.list_replay_signals(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            session_id="sess-disabled",
        )
        == []
    )


def test_replay_issue_lifecycle_regresses_resolved_issue(tmp_path: Path) -> None:
    store, workspace = _workspace(tmp_path)
    kwargs = {
        "project_id": workspace.project_id,
        "environment_id": workspace.environment_id,
        "fingerprint": "console_error|https://x/checkout|boom",
        "session_ids": ["s1"],
        "signal_summary": {"console_error": 1},
        "first_seen_ms": 100,
        "last_seen_ms": 100,
        "title": "boom",
        "summary": "first summary",
    }
    created = store.upsert_replay_issue(**kwargs)
    assert created.inserted is True
    assert store.resolve_replay_issue(created.issue_id) is True

    updated = store.upsert_replay_issue(
        **{**kwargs, "session_ids": ["s1", "s2"], "last_seen_ms": 200}
    )
    assert updated.inserted is False
    assert updated.issue_id == created.issue_id
    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]
    assert issue["status"] == "regressed"
    assert issue["affected_count"] == 2
    assert issue["public_id"] == created.public_id


def test_replay_issue_summary_uses_replay_context(tmp_path: Path) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-summary",
        sequence=0,
        events=[
            {"type": 4, "timestamp": 0, "data": {"href": "https://x/cart"}},
            _console_event("ReferenceError: cart is not defined", ts=500),
        ],
        flush_type="normal",
    )
    results = process_replay_session(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-summary",
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]

    assert results[0].issue_id == issue["id"]
    assert "ReferenceError" in issue["title"]
    assert "console_error" in issue["summary"]
