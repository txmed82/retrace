import json
from pathlib import Path

import pytest

from retrace.failures import (
    canonical_failure_from_api_run,
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


def test_failure_test_links_track_latest_run_state(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    created = store.upsert_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        fingerprint="checkout-button-dead",
        session_ids=["sess_1"],
        signal_summary={"dead_click": 1},
        first_seen_ms=100,
        last_seen_ms=100,
    )
    issue = store.get_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        issue_id=created.public_id,
    )
    assert issue is not None
    failure_id = str(issue["canonical_failure_id"])

    assert store.coverage_state_for_failure(failure_id) == "not_covered"
    link_id = store.upsert_failure_test_link(
        failure_id=failure_id,
        issue_id=str(issue["id"]),
        issue_public_id=str(issue["public_id"]),
        spec_id="checkout-regression",
        spec_name="Checkout regression",
        source="replay_issue",
    )
    failure = store.get_failure(
        project_id="proj_1",
        environment_id="env_1",
        failure_id=failure_id,
    )
    assert failure is not None
    assert failure.linked_tests == ["checkout-regression"]
    assert store.coverage_state_for_failure(failure_id) == "covered_unverified"

    failing = RunResult(
        run_id="run_1",
        spec_id="checkout-regression",
        ok=False,
        exit_code=1,
        run_dir="",
        harness_log_path="",
        app_log_path="",
        command="",
        final_prompt="",
        attempts=1,
        flaky=False,
        flake_reason="",
        status="failed",
        failure_classification="app_bug",
        error="button still dead",
    )
    links = store.update_failure_test_link_run(
        spec_id="checkout-regression",
        link_id=link_id,
        run_result=failing,
    )
    assert links[0].coverage_state == "covered_failing"
    assert links[0].latest_run_id == "run_1"
    assert links[0].latest_run_classification == "app_bug"
    assert links[0].latest_run_ok is False
    assert store.coverage_state_for_failure(failure_id) == "covered_failing"

    passing = RunResult(
        run_id="run_2",
        spec_id="checkout-regression",
        ok=True,
        exit_code=0,
        run_dir="",
        harness_log_path="",
        app_log_path="",
        command="",
        final_prompt="",
        attempts=1,
        flaky=False,
        flake_reason="",
        status="passed",
        failure_classification="unknown",
        error="",
    )
    links = store.update_failure_test_link_run(
        spec_id="checkout-regression",
        link_id=link_id,
        run_result=passing,
    )
    assert links[0].coverage_state == "covered_passing"
    assert links[0].latest_run_id == "run_2"
    assert links[0].latest_run_classification == "unknown"
    assert links[0].latest_run_ok is True
    assert store.coverage_state_for_failure(failure_id) == "covered_passing"

    store.upsert_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        fingerprint="checkout-button-dead",
        session_ids=["sess_1", "sess_2"],
        signal_summary={"dead_click": 2},
        first_seen_ms=100,
        last_seen_ms=200,
    )
    refreshed_failure = store.get_failure(
        project_id="proj_1",
        environment_id="env_1",
        failure_id=failure_id,
    )
    assert refreshed_failure is not None
    assert refreshed_failure.linked_tests == ["checkout-regression"]


def test_failure_test_run_update_can_target_exact_link(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    first = store.upsert_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        fingerprint="checkout-button-dead",
        session_ids=["sess_1"],
        signal_summary={"dead_click": 1},
        first_seen_ms=100,
        last_seen_ms=100,
    )
    second = store.upsert_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        fingerprint="cart-api-500",
        session_ids=["sess_2"],
        signal_summary={"network_5xx": 1},
        first_seen_ms=200,
        last_seen_ms=200,
    )
    first_issue = store.get_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        issue_id=first.public_id,
    )
    second_issue = store.get_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        issue_id=second.public_id,
    )
    assert first_issue is not None
    assert second_issue is not None
    first_link_id = store.upsert_failure_test_link(
        failure_id=str(first_issue["canonical_failure_id"]),
        issue_id=str(first_issue["id"]),
        issue_public_id=str(first_issue["public_id"]),
        spec_id="shared-regression",
    )
    second_link_id = store.upsert_failure_test_link(
        failure_id=str(second_issue["canonical_failure_id"]),
        issue_id=str(second_issue["id"]),
        issue_public_id=str(second_issue["public_id"]),
        spec_id="shared-regression",
    )
    result = RunResult(
        run_id="run_exact",
        spec_id="shared-regression",
        ok=False,
        exit_code=1,
        run_dir="",
        harness_log_path="",
        app_log_path="",
        command="",
        final_prompt="",
        attempts=1,
        flaky=False,
        flake_reason="",
        status="failed",
        error="still broken",
    )

    links = store.update_failure_test_link_run(
        spec_id="shared-regression",
        link_id=first_link_id,
        run_result=result,
    )

    assert [link.id for link in links] == [first_link_id]
    first_link = store.list_failure_test_links(
        failure_id=str(first_issue["canonical_failure_id"])
    )[0]
    second_link = store.list_failure_test_links(
        failure_id=str(second_issue["canonical_failure_id"])
    )[0]
    assert first_link.coverage_state == "covered_failing"
    assert first_link.latest_run_id == "run_exact"
    assert second_link.id == second_link_id
    assert second_link.coverage_state == "covered_unverified"

    with pytest.raises(ValueError, match="link_id is required"):
        store.update_failure_test_link_run(
            spec_id="shared-regression",
            run_result=result,
        )
    with pytest.raises(ValueError, match="unknown failure_test_link"):
        store.update_failure_test_link_run(
            spec_id="different-regression",
            link_id=first_link_id,
            run_result=result,
        )


def test_failure_test_links_reject_unknown_failure(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()

    with pytest.raises(ValueError, match="unknown failure_id"):
        store.upsert_failure_test_link(
            failure_id="flr_missing",
            spec_id="missing-failure-regression",
        )


def test_failure_coverage_state_aggregates_all_links(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    created = store.upsert_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        fingerprint="large-coverage-set",
        session_ids=["sess_1"],
        signal_summary={"dead_click": 1},
        first_seen_ms=100,
        last_seen_ms=100,
    )
    issue = store.get_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        issue_id=created.public_id,
    )
    assert issue is not None
    failure_id = str(issue["canonical_failure_id"])
    with store._conn() as conn:
        for index in range(501):
            state = "covered_failing" if index == 0 else "covered_unverified"
            conn.execute(
                """
                INSERT INTO failure_test_links
                (id, failure_id, spec_id, coverage_state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    f"ftl_{index}",
                    failure_id,
                    f"spec_{index}",
                    state,
                    f"2026-05-08T00:{index // 60:02d}:{index % 60:02d}+00:00",
                    f"2026-05-08T00:{index // 60:02d}:{index % 60:02d}+00:00",
                ),
            )

    assert store.coverage_state_for_failure(failure_id) == "covered_failing"


def test_failure_test_links_backfill_legacy_linked_tests(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    created = store.upsert_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        fingerprint="legacy-coverage",
        session_ids=["sess_1"],
        signal_summary={"dead_click": 1},
        first_seen_ms=100,
        last_seen_ms=100,
    )
    issue = store.get_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        issue_id=created.public_id,
    )
    assert issue is not None
    failure_id = str(issue["canonical_failure_id"])
    with store._conn() as conn:
        conn.execute("DELETE FROM meta WHERE key = ?", ("failure_test_links_backfill_v1",))
        conn.execute("DELETE FROM failure_test_links WHERE failure_id = ?", (failure_id,))
        conn.execute(
            """
            UPDATE failures
            SET linked_tests_json = ?
            WHERE id = ?
            """,
            (
                '[{"spec_id":"legacy-checkout","spec_name":"Legacy checkout",'
                '"spec_path":"specs/legacy.json","coverage_state":"covered_passing",'
                '"latest_run_id":"run_legacy","latest_run_status":"passed",'
                '"latest_run_ok":true,"latest_run_at":"2026-05-08T00:00:00Z"}]',
                failure_id,
            ),
        )

    store.init_schema()

    links = store.list_failure_test_links(failure_id=failure_id)
    assert len(links) == 1
    assert links[0].spec_id == "legacy-checkout"
    assert links[0].spec_name == "Legacy checkout"
    assert links[0].spec_path == "specs/legacy.json"
    assert links[0].source == "legacy"
    assert links[0].coverage_state == "covered_passing"
    assert links[0].latest_run_id == "run_legacy"
    assert links[0].latest_run_status == "passed"
    assert links[0].latest_run_ok is True
    assert store.coverage_state_for_failure(failure_id) == "covered_passing"


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
        failure_classification="app_bug",
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
    assert failure.title == "Tester run failed (app_bug): Checkout smoke"
    assert failure.metadata["failure_classification"] == "app_bug"
    assert failure.metadata["assertion_results"] == [
        {"assertion_id": "a1", "ok": False}
    ]


def test_test_run_failure_extracts_backend_trace_ids_from_network_artifacts(
    tmp_path: Path,
) -> None:
    network_path = tmp_path / "network.json"
    network_path.write_text(
        json.dumps(
            [
                {
                    "url": "https://app.example/api/checkout",
                    "status": 500,
                    "headers": {
                        "Traceparent": (
                            "00-4bf92f3577b34da6a3ce929d0e0e4736-"
                            "00f067aa0ba902b7-01"
                        )
                    },
                },
                {
                    "url": "https://app.example/api/cart",
                    "Trace_ID": "trace-short-1",
                },
            ]
        )
    )
    result = RunResult(
        run_id="run_1",
        spec_id="checkout-smoke",
        ok=False,
        exit_code=1,
        run_dir=str(tmp_path),
        harness_log_path=str(tmp_path / "harness.log"),
        app_log_path=str(tmp_path / "app.log"),
        command="browser-harness run ...",
        final_prompt="Verify checkout",
        attempts=1,
        flaky=False,
        flake_reason="",
        status="failed",
        failure_classification="app_bug",
        error="Checkout failed",
        execution_engine="harness",
        artifacts=[
            {
                "artifact_id": "browser-harness-network",
                "artifact_type": "network_output",
                "path": str(network_path),
            }
        ],
        assertion_results=[],
    )

    failure = canonical_failure_from_test_run(
        project_id="proj_1",
        environment_id="env_1",
        run_result=result,
        spec_name="Checkout smoke",
    )

    assert failure.metadata["trace_ids"] == [
        "4bf92f3577b34da6a3ce929d0e0e4736",
        "trace-short-1",
    ]


def test_api_run_failure_summary_uses_assertion_failure_when_status_matches() -> None:
    class Spec:
        spec_id = "api_health"
        name = "Health API"
        method = "GET"
        url = "http://example.test/api/health"
        query = {}
        expected_status = 200

    class Result:
        run_id = "run_api_1"
        spec_id = "api_health"
        ok = False
        status_code = 200
        status = "failed"
        error = ""
        artifacts = []
        assertion_results = [
            {
                "assertion_id": "json-0",
                "ok": False,
                "message": "Expected $.ok to equal true.",
            }
        ]

    failure = canonical_failure_from_api_run(
        project_id="proj_1",
        environment_id="env_1",
        spec=Spec(),
        run_result=Result(),
    )

    assert "expected status 200, got 200" not in failure.summary
    assert "assertion failed" in failure.summary
    assert "Expected $.ok to equal true." in failure.summary


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
