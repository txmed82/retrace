from pathlib import Path

from retrace.failures import (
    canonical_failure_from_monitor_incident,
    canonical_failure_from_replay_issue,
    canonical_failure_from_test_run,
)
from retrace.storage import Storage
from retrace.tester import TesterRunResult as RunResult


def test_replay_issue_maps_to_canonical_failure(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    result = store.upsert_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        fingerprint="network-500-/api/checkout",
        session_ids=["sess_1", "sess_2"],
        signal_summary={"network_5xx": 2},
        first_seen_ms=100,
        last_seen_ms=250,
        title="Checkout API returned 500",
        summary="Two sessions saw checkout fail.",
        severity="high",
        confidence="high",
        evidence={"signals": [{"detector": "network_5xx"}]},
        trace_ids=["trace-1"],
    )

    issue = store.get_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        issue_id=result.public_id,
    )
    assert issue is not None
    assert issue["canonical_failure_id"]

    failure = store.get_failure(
        project_id="proj_1",
        environment_id="env_1",
        failure_id=str(issue["canonical_failure_id"]),
    )
    assert failure is not None
    assert failure.source_type == "replay_issue"
    assert failure.source_external_id == result.public_id
    assert failure.fingerprint == "network-500-/api/checkout"
    assert failure.title == "Checkout API returned 500"
    assert failure.status == "new"
    assert failure.severity == "high"
    assert failure.confidence == "high"
    assert failure.affected_sessions == 2
    assert failure.metadata["trace_ids"] == ["trace-1"]


def test_replay_issue_status_is_normalized_for_canonical_failure(
    tmp_path: Path,
) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    created = store.upsert_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        fingerprint="blank-render-home",
        session_ids=["sess_1"],
        signal_summary={"blank_render": 1},
        first_seen_ms=100,
        last_seen_ms=100,
    )
    assert store.resolve_replay_issue(created.issue_id) is True
    regressed = store.upsert_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        fingerprint="blank-render-home",
        session_ids=["sess_2"],
        signal_summary={"blank_render": 1},
        first_seen_ms=200,
        last_seen_ms=200,
    )
    assert regressed.current_status == "regressed"

    issue = store.get_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        issue_id=created.public_id,
    )
    assert issue is not None
    failure = store.get_failure(
        project_id="proj_1",
        environment_id="env_1",
        failure_id=str(issue["canonical_failure_id"]),
    )
    assert failure is not None
    assert failure.status == "regressed"
    assert failure.affected_sessions == 2


def test_test_run_failure_can_be_represented_as_canonical_failure() -> None:
    result = RunResult(
        run_id="run_1",
        spec_id="checkout-smoke",
        ok=False,
        exit_code=1,
        run_dir="/tmp/run",
        harness_log_path="/tmp/run/harness.log",
        app_log_path="/tmp/run/app.log",
        command="browser-harness run ...",
        final_prompt="Verify checkout",
        attempts=1,
        flaky=False,
        flake_reason="",
        status="failed",
        error="Missing checkout confirmation",
        execution_engine="harness",
        artifacts=[{"artifact_id": "log_1"}],
        assertion_results=[{"assertion_id": "a1", "ok": False}],
    )

    failure = canonical_failure_from_test_run(
        project_id="proj_1",
        environment_id="env_1",
        run_result=result,
        spec_name="Checkout smoke",
    )

    assert failure.source_type == "test_run"
    assert failure.source_external_id == "run_1"
    assert failure.status == "new"
    assert failure.linked_tests == ["checkout-smoke"]
    assert failure.metadata["assertion_results"] == [
        {"assertion_id": "a1", "ok": False}
    ]


def test_future_monitor_incident_needs_no_schema_specific_fields() -> None:
    failure = canonical_failure_from_monitor_incident(
        project_id="proj_1",
        environment_id="prod",
        provider="sentry",
        external_id="ERR-42",
        title="TypeError in checkout",
        summary="Unhandled exception in payment submission.",
        severity="high",
        metadata={"service": "web"},
    )

    assert failure.source_type == "monitor_incident"
    assert failure.source_external_id == "sentry:ERR-42"
    assert failure.severity == "high"
    assert failure.metadata == {"provider": "sentry", "service": "web"}


def test_replay_issue_row_builder_preserves_public_id() -> None:
    failure = canonical_failure_from_replay_issue(
        {
            "id": "ri_1",
            "project_id": "proj_1",
            "environment_id": "env_1",
            "public_id": "bug_123",
            "fingerprint": "fp",
            "title": "Dead click",
            "summary": "Button did not respond",
            "status": "ticket_created",
            "affected_count": 1,
        }
    )

    assert failure.source_external_id == "bug_123"
    assert failure.status == "in_progress"
    assert failure.metadata["replay_issue_public_id"] == "bug_123"
