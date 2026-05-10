from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from retrace.commands.ui import (
    _api_specs_payload,
    _api_suites_payload,
    _create_sdk_key_payload,
    _edit_ui_draft_payload,
    _generate_replay_issue_api_spec_payload,
    _generate_replay_issue_fix_prompts_payload,
    _generate_replay_issue_specs_payload,
    _INDEX_HTML,
    _issue_workflow_payload,
    _replay_api_calls,
    _replay_api_check,
    _generate_replay_issue_spec_payload,
    _run_replay_issue_api_spec_payload,
    _run_api_spec_payload,
    _run_api_suite_payload,
    _to_replay_dashboard_payload,
    _transition_replay_issue_payload,
    _verify_resolved_issues_payload,
)
from retrace.api_suites import create_api_suite
from retrace.api_testing import APITestRunResult, api_specs_dir_for_data_dir, create_api_spec
from retrace.replay_core import ReplaySignalConfig, process_replay_sessions
from retrace.sdk_keys import authenticate_sdk_key
from retrace.storage import Storage
from retrace.tester import (
    DEFAULT_HARNESS_COMMAND,
    TesterRunResult as RetraceTesterRunResult,
    create_spec,
    specs_dir_for_data_dir,
)


def _workspace(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(
        org_name="Acme",
        project_name="Web",
        environment_name="production",
    )
    return store, workspace


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return


class _CheckoutHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/checkout":
            _ = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
            body = b'{"ok":true,"order_id":"ord_123"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return


def test_replay_api_check_reports_reachable_server() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        payload = _replay_api_check(f"http://{host}:{port}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert payload["reachable"] is True
    assert payload["detail"] == "OK (200)"
    assert payload["commands"]["serve"] == "retrace api serve"


def test_replay_api_check_reports_unreachable_server() -> None:
    payload = _replay_api_check("http://127.0.0.1:9")

    assert payload["reachable"] is False
    assert payload["url"] == "http://127.0.0.1:9"
    assert payload["commands"]["serve"] == "retrace api serve"
    assert payload["detail"]


def test_index_html_escape_helper_escapes_single_quotes() -> None:
    assert "[&<>\"']" in _INDEX_HTML
    assert '"\'":\'&#39;\'' in _INDEX_HTML
    assert "onclick=" not in _INDEX_HTML
    assert "sendSdkSmokeReplay" in _INDEX_HTML
    assert "X-Retrace-Key" in _INDEX_HTML
    assert "copyEvidenceBundle" in _INDEX_HTML
    assert "timelineTypeFilter" in _INDEX_HTML
    assert "renderIssueWorkflow" in _INDEX_HTML
    assert "renderIssueReadiness" in _INDEX_HTML
    assert "renderRepairTask" in _INDEX_HTML
    assert "data-workflow-action" in _INDEX_HTML
    assert "QA Loop Status" in _INDEX_HTML
    assert "generate_api_regression" in _INDEX_HTML
    assert "/api/api-suites" in _INDEX_HTML
    assert "API Suites" in _INDEX_HTML
    assert "/api/api-specs" in _INDEX_HTML
    assert "/api/api-spec/run" in _INDEX_HTML
    assert "UI Spec Inventory" in _INDEX_HTML
    assert "API Spec Inventory" in _INDEX_HTML
    assert "runManagedApiSpec" in _INDEX_HTML
    assert "/api/api-suite/run" in _INDEX_HTML
    assert "runManagedApiSuite" in _INDEX_HTML
    assert "apiSuiteRunMatrix" in _INDEX_HTML
    assert "Generated Draft Review" in _INDEX_HTML
    assert "/api/tester/draft" in _INDEX_HTML
    assert "saveDraftSpec" in _INDEX_HTML
    assert "runAcceptedDraftSpec" in _INDEX_HTML
    assert _INDEX_HTML.index("await refreshTesterAndReplay(issue.public_id);") < (
        _INDEX_HTML.index("renderReplayFixSuggestions(data);")
    )
    assert "Confidence:" in _INDEX_HTML
    assert "Reasons:" in _INDEX_HTML
    assert 'data-view="issues"' in _INDEX_HTML
    assert 'data-view="findings"' in _INDEX_HTML
    assert "linkedFailureTests" in _INDEX_HTML
    assert "generateReplayIssueApiSpec" in _INDEX_HTML
    assert "runReplayIssueApiSpec" in _INDEX_HTML
    assert "generateGroupedReplayIssueSpecs" in _INDEX_HTML
    assert "/api/replay-issues/specs" in _INDEX_HTML
    assert "data-issue-select" in _INDEX_HTML
    assert "limit: Math.min(issueIds.length || 25, 100)" in _INDEX_HTML
    assert "failed ${(data.failed || []).length}" in _INDEX_HTML
    assert 'role="button" tabindex="0"' in _INDEX_HTML
    assert 'rel="noopener noreferrer"' in _INDEX_HTML
    assert "hashchange" in _INDEX_HTML
    assert "safeExternalUrl" in _INDEX_HTML
    assert "safeHashUrl(issue.share_url, '#issue=')" in _INDEX_HTML
    assert "#issue=${encodeURIComponent(issue.public_id)}" in _INDEX_HTML
    assert "#replay=${encodeURIComponent(replayId)}" in _INDEX_HTML


def test_replay_api_calls_redacts_sensitive_query_values() -> None:
    calls = _replay_api_calls(
        {
            "signals": [
                {
                    "detector": "network_5xx",
                    "timestamp_ms": 120,
                    "details": {
                        "method": "GET",
                        "request_url": "https://app.example/api/me?token=secret&tab=profile",
                        "status": 500,
                    },
                }
            ]
        }
    )

    assert calls[0]["url"] == (
        "https://app.example/api/me?token=%5Bredacted-api-input%5D&tab=profile"
    )
    assert "secret" not in calls[0]["url"]


def test_issue_workflow_treats_covered_passing_as_terminal_without_repair() -> None:
    workflow = _issue_workflow_payload(
        {
            "status": "unresolved",
            "timeline": [{"type": "replay_signal"}],
            "reproduction_steps": ["Open checkout"],
            "sessions": [{"session_id": "sess-1"}],
            "api_calls": [],
            "test_links": [{"coverage_state": "covered_passing"}],
            "repair_task": None,
        }
    )

    assert workflow["coverage_state"] == "covered_passing"
    assert workflow["primary_action"] == "none"
    assert workflow["primary_label"] == "Covered by passing test"
    assert workflow["stage_states"]["verification"] == "complete"
    assert workflow["readiness"] == "verified"
    assert workflow["blockers"] == []


def test_issue_workflow_recommends_ui_and_api_regressions() -> None:
    workflow = _issue_workflow_payload(
        {
            "status": "unresolved",
            "timeline": [{"type": "replay_signal"}],
            "reproduction_steps": ["Open checkout"],
            "sessions": [{"session_id": "sess-1"}],
            "api_calls": [{"method": "POST", "url": "/api/checkout", "status": 500}],
            "test_links": [],
            "repair_task": None,
        }
    )

    assert workflow["readiness"] == "needs_test"
    assert workflow["counts"]["api_tests"] == 0
    assert "No regression test covers this issue yet." in workflow["blockers"]
    assert [item["action"] for item in workflow["recommended_actions"]] == [
        "generate_replay_spec",
        "generate_api_regression",
    ]


def test_api_suites_payload_summarizes_import_quality(tmp_path: Path) -> None:
    create_api_suite(
        suites_dir=tmp_path / "api-tests" / "suites",
        name="OpenAPI Demo",
        source="openapi_import",
        spec_ids=["api_get_user"],
        auth_profile="local-jwt",
        env_profile="staging",
        import_summary={
            "coverage_percent": 100.0,
            "quality_warnings": {
                "missing_operation_ids": ["api_get_user"],
                "missing_response_schemas": [],
            },
        },
        operations=[
            {
                "spec_id": "api_get_user",
                "method": "GET",
                "path": "/v1/users/{id}",
                "operation_id": "",
            }
        ],
    )

    payload = _api_suites_payload(tmp_path)

    assert payload["suites"][0]["name"] == "OpenAPI Demo"
    assert payload["suites"][0]["spec_count"] == 1
    assert payload["suites"][0]["operation_count"] == 1
    assert payload["suites"][0]["quality_warning_count"] == 1
    assert payload["suites"][0]["auth_profile"] == "local-jwt"


def test_api_specs_payload_summarizes_management_fields(tmp_path: Path) -> None:
    spec = create_api_spec(
        specs_dir=api_specs_dir_for_data_dir(tmp_path),
        name="Checkout API",
        method="POST",
        url="https://app.example/api/checkout",
        expected_status=201,
        json_assertions=[{"path": "$.ok", "equals": True}],
        auth_profile="local-jwt",
        env_profile="staging",
        fixtures={
            "source": "openapi_import",
            "issue_public_id": "iss_checkout",
            "operation_id": "createCheckout",
            "openapi_path": "/api/checkout",
        },
    )

    payload = _api_specs_payload(tmp_path)

    assert payload["specs"][0]["spec_id"] == spec.spec_id
    assert payload["specs"][0]["method"] == "POST"
    assert payload["specs"][0]["expected_status"] == 201
    assert payload["specs"][0]["json_assertion_count"] == 1
    assert payload["specs"][0]["source"] == "openapi_import"
    assert payload["specs"][0]["issue_public_id"] == "iss_checkout"
    assert payload["specs"][0]["operation_id"] == "createCheckout"


def test_run_api_spec_payload_runs_saved_api_spec(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        spec = create_api_spec(
            specs_dir=api_specs_dir_for_data_dir(tmp_path),
            name="Health API",
            method="GET",
            url=f"http://{host}:{port}/healthz",
            expected_status=200,
        )
        payload, status = _run_api_spec_payload(data_dir=tmp_path, spec_id=spec.spec_id)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 200
    assert payload["ok"] is True
    assert payload["result"]["spec_id"] == spec.spec_id
    assert payload["result"]["status"] == "passed"


def test_run_api_suite_payload_returns_pass_fail_matrix(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        passing = create_api_spec(
            specs_dir=api_specs_dir_for_data_dir(tmp_path),
            name="Health API",
            method="GET",
            url=f"http://{host}:{port}/healthz",
            expected_status=200,
        )
        failing = create_api_spec(
            specs_dir=api_specs_dir_for_data_dir(tmp_path),
            name="Missing API",
            method="GET",
            url=f"http://{host}:{port}/missing",
            expected_status=200,
        )
        suite = create_api_suite(
            suites_dir=tmp_path / "api-tests" / "suites",
            name="Smoke Suite",
            source="manual",
            spec_ids=[passing.spec_id, failing.spec_id],
        )
        payload, status = _run_api_suite_payload(
            data_dir=tmp_path,
            suite_id=suite.suite_id,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 400
    assert payload["ok"] is False
    assert payload["total"] == 2
    assert payload["passed"] == 1
    assert payload["failed"] == 1
    assert [item["spec_id"] for item in payload["results"]] == [
        passing.spec_id,
        failing.spec_id,
    ]
    assert payload["results"][0]["ok"] is True
    assert payload["results"][1]["ok"] is False


def test_edit_ui_draft_payload_persists_reviewed_steps_and_accepts(
    tmp_path: Path,
) -> None:
    spec = create_spec(
        specs_dir=specs_dir_for_data_dir(tmp_path),
        name="Generated checkout draft",
        prompt="Explore checkout",
        app_url="https://app.example",
        start_command="",
        harness_command=DEFAULT_HARNESS_COMMAND,
        execution_engine="native",
        exact_steps=[{"id": "old", "action": "navigate", "url": "https://app.example"}],
        assertions=[{"id": "old-status", "type": "status_code", "expected": 200}],
        fixtures={
            "draft_status": "draft",
            "generation": {"review": {"summary": "Needs stable checkout path"}},
        },
    )

    payload, status = _edit_ui_draft_payload(
        data_dir=tmp_path,
        spec_id=spec.spec_id,
        name="Accepted checkout draft",
        prompt="Run checkout smoke",
        steps=[
            {
                "id": "checkout",
                "action": "navigate",
                "url": "https://app.example/checkout",
            }
        ],
        assertions=[
            {"id": "checkout-loads", "type": "status_code", "expected": 200}
        ],
        review_note="Made deterministic before accepting.",
        accept=True,
    )

    assert status == 200
    assert payload["ok"] is True
    assert payload["draft_status"] == "accepted"
    assert payload["changed_fields"] == [
        "assertions",
        "draft_status",
        "exact_steps",
        "name",
        "prompt",
        "review_notes",
    ]
    saved = payload["spec"]
    assert saved["name"] == "Accepted checkout draft"
    assert saved["exact_steps"][0]["id"] == "checkout"
    assert saved["assertions"][0]["id"] == "checkout-loads"
    assert saved["fixtures"]["review_notes"] == ["Made deterministic before accepting."]
    assert saved["fixtures"]["last_review_edit"]["fields"] == payload["changed_fields"]


def test_edit_ui_draft_payload_rejects_invalid_json_shape(tmp_path: Path) -> None:
    spec = create_spec(
        specs_dir=specs_dir_for_data_dir(tmp_path),
        name="Generated draft",
        prompt="Explore",
        app_url="https://app.example",
        start_command="",
        harness_command=DEFAULT_HARNESS_COMMAND,
        fixtures={"draft_status": "draft"},
    )

    payload, status = _edit_ui_draft_payload(
        data_dir=tmp_path,
        spec_id=spec.spec_id,
        steps={"id": "not-a-list"},
    )

    assert status == 400
    assert payload["ok"] is False
    assert "steps must be a JSON list of objects" in payload["error"]


def test_create_sdk_key_payload_creates_browser_ingest_key(
    tmp_path: Path,
) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()

    payload, status = _create_sdk_key_payload(
        store=store,
        project_name="Web",
        environment_name="production",
        name="Website capture",
    )

    assert status == 200
    assert payload["ok"] is True
    assert payload["name"] == "Website capture"
    assert payload["project"] == "Web"
    assert payload["environment"] == "production"
    assert payload["key"].startswith("rtpk_")
    assert payload["last4"] == payload["key"][-4:]
    assert payload["ingest_path"] == "/api/sdk/replay"
    assert payload["ingest_url"] == "http://127.0.0.1:8788/api/sdk/replay"

    row = authenticate_sdk_key(store, payload["key"])
    assert row is not None
    assert row.id == payload["id"]
    assert row.project_id == payload["project_id"]
    assert row.environment_id == payload["environment_id"]


def test_replay_dashboard_payload_includes_failure_timeline(tmp_path: Path) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-timeline",
        sequence=0,
        events=[{"type": 4, "timestamp": 100, "data": {"href": "https://app.test/checkout"}}],
        flush_type="final",
    )
    created = store.upsert_replay_issue(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        fingerprint="checkout-network-500",
        session_ids=["sess-timeline"],
        signal_summary={"network_5xx": 1, "console_error": 1},
        first_seen_ms=100,
        last_seen_ms=500,
        title="Checkout failed",
        evidence={
            "signals": [
                {
                    "detector": "network_5xx",
                    "timestamp_ms": 300,
                    "confidence": "high",
                    "reason_codes": ["network_5xx.status_5xx"],
                    "details": {
                        "method": "POST",
                        "request_url": "/api/checkout",
                        "status": 500,
                        "duration_ms": 42,
                    },
                },
                {
                    "detector": "console_error",
                    "timestamp_ms": 400,
                    "details": {
                        "level": "error",
                        "message": "Checkout total is undefined",
                    },
                },
            ],
            "events": [
                {"type": 4, "timestamp_ms": 100, "href": "https://app.test/checkout"},
                {"type": 3, "timestamp_ms": 200, "source": 2, "data_type": 2, "id": 9},
            ],
        },
    )
    issue_row = store.get_replay_issue(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        issue_id=created.public_id,
    )
    assert issue_row is not None
    link_id = store.upsert_failure_test_link(
        failure_id=str(issue_row["canonical_failure_id"]),
        issue_id=str(issue_row["id"]),
        issue_public_id=str(issue_row["public_id"]),
        spec_id="checkout-replay-regression",
        spec_name="Checkout replay regression",
        spec_path="specs/checkout-replay-regression.json",
        source="replay_issue",
    )
    store.update_failure_test_link_run(
        spec_id="checkout-replay-regression",
        link_id=link_id,
        run_result=RetraceTesterRunResult(
            run_id="run_checkout_1",
            spec_id="checkout-replay-regression",
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
            error="Checkout still returns 500",
        ),
    )
    repair_task_id = store.upsert_repair_task(
        failure_id=str(issue_row["canonical_failure_id"]),
        title="Fix checkout failure",
        source_type="replay_issue",
        source_external_id=created.public_id,
        status="open",
        likely_files=["src/checkout.tsx"],
        validation_commands=["uv run pytest tests/test_checkout.py"],
        risk_notes="Checkout remains covered by a failing replay regression.",
    )

    payload = _to_replay_dashboard_payload(store)
    issue = payload["issues"][0]
    timeline = issue["timeline"]

    assert [event["title"] for event in timeline] == [
        "Navigation",
        "Click",
        "Network 500",
        "Console error",
    ]
    assert timeline[2]["kind"] == "network"
    assert timeline[2]["summary"] == "POST /api/checkout returned 500 in 42ms"
    assert timeline[2]["detector_hit"] is True
    assert timeline[2]["confidence"] == "high"
    assert timeline[2]["reason_codes"] == ["network_5xx.status_5xx"]
    assert timeline[3]["summary"] == "Checkout total is undefined"
    assert issue["confidence"] == "medium"
    assert issue["fingerprint"]
    assert issue["analysis_status"] == ""
    assert issue["sessions"][0]["stable_id"] == "sess-timeline"
    assert issue["sessions"][0]["public_id"].startswith("rpl_")
    assert issue["test_links"][0]["spec_id"] == "checkout-replay-regression"
    assert issue["test_links"][0]["coverage_state"] == "covered_failing"
    assert issue["test_links"][0]["latest_run_status"] == "failed"
    assert issue["test_links"][0]["latest_run_classification"] == "app_bug"
    assert issue["repair_task"]["id"] == repair_task_id
    assert issue["repair_task"]["likely_files"] == ["src/checkout.tsx"]
    assert issue["repair_task"]["validation_commands"] == [
        "uv run pytest tests/test_checkout.py"
    ]
    assert issue["workflow"]["coverage_state"] == "covered_failing"
    assert issue["workflow"]["primary_action"] == "run_tests"
    assert issue["workflow"]["stage_states"]["evidence"] == "complete"
    assert issue["workflow"]["stage_states"]["test"] == "complete"
    assert issue["workflow"]["stage_states"]["repair"] == "complete"
    assert issue["workflow"]["stage_states"]["verification"] == "blocked"
    assert issue["workflow"]["counts"]["timeline"] == 4
    assert issue["workflow"]["counts"]["repair_tasks"] == 1


def test_create_sdk_key_payload_reports_creation_error(
    tmp_path: Path,
) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()

    with patch(
        "retrace.commands.ui.create_sdk_key",
        side_effect=RuntimeError("database locked"),
    ):
        payload, status = _create_sdk_key_payload(store=store)

    assert status == 400
    assert payload == {"ok": False, "error": "database locked"}


def test_generate_replay_issue_spec_payload_creates_native_spec(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-ui-spec",
        sequence=0,
        events=[
            {
                "type": 4,
                "timestamp": 0,
                "data": {"href": "https://app.example/signup"},
            },
            {
                "type": 3,
                "timestamp": 100,
                "data": {"source": 2, "type": 2, "id": 42},
            },
            {
                "type": 6,
                "timestamp": 200,
                "data": {
                    "plugin": "retrace/console@1",
                    "payload": {"level": "error", "payload": ["Signup crashed"]},
                },
            },
        ],
        flush_type="final",
    )
    processed = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-ui-spec"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    issue_public_id = processed.issues[0].public_id

    payload, status = _generate_replay_issue_spec_payload(
        store=store,
        data_dir=tmp_path,
        issue_id=issue_public_id,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        app_url="",
    )

    assert status == 200
    assert payload["ok"] is True
    assert payload["spec"]["execution_engine"] == "native"
    assert payload["spec"]["fixtures"]["issue_public_id"] == issue_public_id
    assert payload["issue_public_id"] == issue_public_id
    assert payload["replay_public_id"].startswith("rpl_")
    assert payload["confidence"] == "low"
    assert payload["known_gaps"]
    assert payload["spec"]["fixtures"]["generation"]["quality"]["status"] == "needs_locator"
    assert (
        specs_dir_for_data_dir(tmp_path) / f"{payload['spec']['spec_id']}.json"
    ).exists()


def test_generate_replay_issue_spec_payload_reports_missing_issue(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)

    payload, status = _generate_replay_issue_spec_payload(
        store=store,
        data_dir=tmp_path,
        issue_id="bug_missing",
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        app_url="",
    )

    assert status == 404
    assert payload == {"ok": False, "error": "Replay issue not found: bug_missing"}


def test_generate_replay_issue_specs_payload_creates_missing_group_specs(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    issue_ids: list[str] = []
    for index in range(2):
        session_id = f"sess-group-{index}"
        store.insert_replay_batch(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            session_id=session_id,
            sequence=0,
            events=[
                {
                    "type": 4,
                    "timestamp": 0,
                    "data": {"href": f"https://app.example/flow-{index}"},
                },
                {
                    "type": 3,
                    "timestamp": 100,
                    "data": {"source": 2, "type": 2, "id": index + 10},
                },
            ],
            flush_type="final",
        )
        created = store.upsert_replay_issue(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            fingerprint=f"grouped-error-{index}",
            session_ids=[session_id],
            signal_summary={"dead_click": 1},
            first_seen_ms=100,
            last_seen_ms=200,
            title=f"Grouped error {index}",
        )
        issue_ids.append(created.public_id)

    payload, status = _generate_replay_issue_specs_payload(
        store=store,
        data_dir=tmp_path,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        issue_ids=issue_ids,
        app_url="",
    )

    assert status == 200
    assert payload["ok"] is True
    assert payload["considered"] == 2
    assert payload["generated"] == 2
    assert payload["skipped"] == []
    assert {item["issue_public_id"] for item in payload["results"]} == set(issue_ids)
    for item in payload["results"]:
        assert (specs_dir_for_data_dir(tmp_path) / f"{item['spec_id']}.json").exists()

    second_payload, second_status = _generate_replay_issue_specs_payload(
        store=store,
        data_dir=tmp_path,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        status="new",
    )

    assert second_status == 200
    assert second_payload["generated"] == 0
    assert {item["reason"] for item in second_payload["skipped"]} == {
        "already_covered"
    }


def test_generate_replay_issue_specs_payload_reports_partial_success(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-group-ok",
        sequence=0,
        events=[
            {
                "type": 4,
                "timestamp": 0,
                "data": {"href": "https://app.example/ok"},
            }
        ],
        flush_type="final",
    )
    valid = store.upsert_replay_issue(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        fingerprint="grouped-ok",
        session_ids=["sess-group-ok"],
        signal_summary={"console_error": 1},
        first_seen_ms=100,
        last_seen_ms=100,
        title="Grouped ok",
    )
    invalid = store.upsert_replay_issue(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        fingerprint="grouped-missing-playback",
        session_ids=["sess-missing-playback"],
        signal_summary={"console_error": 1},
        first_seen_ms=100,
        last_seen_ms=100,
        title="Grouped missing playback",
    )

    payload, status = _generate_replay_issue_specs_payload(
        store=store,
        data_dir=tmp_path,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        issue_ids=[valid.public_id, invalid.public_id],
    )

    assert status == 207
    assert payload["ok"] is False
    assert payload["generated"] == 1
    assert payload["results"][0]["issue_public_id"] == valid.public_id
    assert payload["failed"] == [
        {
            "issue_public_id": invalid.public_id,
            "error": "Replay session not found: sess-missing-playback",
        }
    ]


def test_generate_and_run_replay_issue_api_spec_payload_updates_coverage(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CheckoutHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base_url = f"http://{host}:{port}"
    try:
        store.insert_replay_batch(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            session_id="sess-api-ui",
            sequence=0,
            events=[
                {
                    "type": 4,
                    "timestamp": 0,
                    "data": {"href": f"{base_url}/checkout"},
                }
            ],
            flush_type="final",
        )
        created = store.upsert_replay_issue(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            fingerprint="checkout-api-ui",
            session_ids=["sess-api-ui"],
            signal_summary={"network_5xx": 1},
            first_seen_ms=100,
            last_seen_ms=500,
            title="Checkout API failed",
            evidence={
                "signals": [
                    {
                        "detector": "network_5xx",
                        "timestamp_ms": 300,
                        "confidence": "high",
                        "details": {
                            "method": "POST",
                            "request_url": "/api/checkout",
                            "status": 500,
                            "request_headers": {"content-type": "application/json"},
                            "request_body": '{"cart_id":"abc"}',
                        },
                    }
                ]
            },
        )

        payload, status = _generate_replay_issue_api_spec_payload(
            store=store,
            data_dir=tmp_path,
            issue_id=created.public_id,
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            app_url=base_url,
        )
        assert status == 200
        assert payload["ok"] is True
        spec_id = payload["spec"]["spec_id"]
        assert payload["spec"]["method"] == "POST"
        assert payload["spec"]["url"] == f"{base_url}/api/checkout"
        assert payload["spec"]["expected_status"] == 500
        assert (
            api_specs_dir_for_data_dir(tmp_path) / f"{spec_id}.json"
        ).exists()

        run_payload, run_status = _run_replay_issue_api_spec_payload(
            store=store,
            data_dir=tmp_path,
            spec_id=spec_id,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert run_status == 200
    assert run_payload["ok"] is True
    assert run_payload["result"]["status"] == "passed"
    issue = store.get_replay_issue(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        issue_id=created.public_id,
    )
    assert issue is not None
    links = store.list_failure_test_links(
        failure_id=str(issue["canonical_failure_id"]),
        spec_id=spec_id,
    )
    assert links[0].source == "replay_issue_api"
    assert links[0].coverage_state == "covered_passing"
    assert links[0].latest_run_status == "passed"


def test_generate_replay_issue_fix_prompts_payload_creates_agent_prompts(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    repo_dir = tmp_path / "repo"
    src_dir = repo_dir / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "checkout.tsx").write_text(
        "export function Checkout(){ throw new Error('Checkout crashed') }\n",
        encoding="utf-8",
    )
    store.upsert_github_repo(
        repo_full_name="acme/widgets",
        default_branch="main",
        local_path=str(repo_dir),
    )
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-ui-prompts",
        sequence=0,
        events=[
            {
                "type": 4,
                "timestamp": 0,
                "data": {"href": "https://app.example/checkout"},
            },
            {
                "type": 6,
                "timestamp": 200,
                "data": {
                    "plugin": "retrace/console@1",
                    "payload": {
                        "level": "error",
                        "payload": ["Checkout crashed in src/checkout.tsx"],
                    },
                },
            },
        ],
        flush_type="final",
    )
    processed = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-ui-prompts"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    issue_public_id = processed.issues[0].public_id

    payload, status = _generate_replay_issue_fix_prompts_payload(
        store=store,
        output_dir=tmp_path / "reports",
        issue_id=issue_public_id,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        repo_full_name="acme/widgets",
    )

    assert status == 200
    assert payload["ok"] is True
    assert payload["issue_public_id"] == issue_public_id
    assert payload["repo"] == "acme/widgets"
    assert payload["generated"] == 1
    assert payload["candidates"][0]["file_path"] == "src/checkout.tsx"
    assert issue_public_id in payload["prompts"]["codex"]
    assert "src/checkout.tsx" in payload["prompts"]["claude_code"]
    assert payload["repair_task_id"].startswith("rpr_")
    task = store.get_repair_task(payload["repair_task_id"])
    assert task is not None
    assert task.source_external_id == issue_public_id
    assert "src/checkout.tsx" in task.likely_files
    out_dir = tmp_path / "reports" / "fix-prompts"
    assert (out_dir / payload["artifact_json"]).exists()
    assert payload["artifact_manifest_json"]
    assert (out_dir / payload["artifact_manifest_json"]).is_file()
    assert (out_dir / payload["prompt_files"]["codex"]).exists()


def test_generate_replay_issue_fix_prompts_payload_skips_ignored_issue(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    store.upsert_github_repo(
        repo_full_name="acme/widgets",
        default_branch="main",
        local_path=str(tmp_path),
    )
    upsert = store.upsert_replay_issue(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        fingerprint="ignored-fix-prompts",
        session_ids=["sess-ignored"],
        signal_summary={"console_error": 1},
        first_seen_ms=100,
        last_seen_ms=100,
        title="Ignored issue",
    )
    assert store.ignore_replay_issue(upsert.issue_id) is True

    payload, status = _generate_replay_issue_fix_prompts_payload(
        store=store,
        output_dir=tmp_path / "reports",
        issue_id=upsert.public_id,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        repo_full_name="acme/widgets",
    )

    assert status == 409
    assert payload["ok"] is False
    assert "ignored" in payload["error"]


def test_transition_replay_issue_payload_marks_resolved_and_unresolved(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-ui-lifecycle",
        sequence=0,
        events=[
            {
                "type": 6,
                "timestamp": 200,
                "data": {
                    "plugin": "retrace/console@1",
                    "payload": {"level": "error", "payload": ["Lifecycle crashed"]},
                },
            },
        ],
        flush_type="final",
    )
    processed = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-ui-lifecycle"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    issue_public_id = processed.issues[0].public_id

    payload, status = _transition_replay_issue_payload(
        store=store,
        issue_id=issue_public_id,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        status="resolved",
    )

    assert status == 200
    assert payload["ok"] is True
    assert payload["issue"]["public_id"] == issue_public_id
    assert payload["issue"]["status"] == "resolved"

    payload, status = _transition_replay_issue_payload(
        store=store,
        issue_id=issue_public_id,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        status="unresolved",
    )

    assert status == 200
    assert payload["issue"]["status"] == "unresolved"

    payload, status = _transition_replay_issue_payload(
        store=store,
        issue_id=issue_public_id,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        status="ignored",
    )

    assert status == 200
    assert payload["issue"]["status"] == "ignored"


def test_transition_replay_issue_payload_reports_missing_issue(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)

    payload, status = _transition_replay_issue_payload(
        store=store,
        issue_id="bug_missing",
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        status="resolved",
    )

    assert status == 404
    assert payload == {"ok": False, "error": "Replay issue not found: bug_missing"}


def test_transition_replay_issue_payload_rejects_unknown_status(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)

    payload, status = _transition_replay_issue_payload(
        store=store,
        issue_id="bug_missing",
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        status="closed",
    )

    assert status == 400
    assert payload == {
        "ok": False,
        "error": "status must be resolved, unresolved, or ignored",
    }


def test_verify_resolved_issues_payload_dry_run_lists_linked_specs(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    issue_public_id = _resolved_replay_issue(store, workspace)
    create_spec(
        specs_dir=specs_dir_for_data_dir(tmp_path),
        name="Replay regression",
        prompt="",
        app_url="https://app.example",
        start_command="",
        harness_command=DEFAULT_HARNESS_COMMAND,
        execution_engine="native",
        exact_steps=[{"action": "navigate", "url": "https://app.example"}],
        fixtures={"issue_public_id": issue_public_id},
    )

    payload, status = _verify_resolved_issues_payload(
        store=store,
        data_dir=tmp_path,
        cwd=tmp_path,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        dry_run=True,
    )

    assert status == 200
    assert payload["ok"] is True
    assert payload["plan"][0]["public_id"] == issue_public_id
    assert payload["plan"][0]["has_spec"] is True
    assert payload["verified"] == []
    assert payload["regressed"] == []


def test_verify_resolved_issues_payload_marks_failed_specs_regressed(
    tmp_path: Path, monkeypatch
) -> None:
    store, workspace = _workspace(tmp_path)
    issue_public_id = _resolved_replay_issue(store, workspace)
    create_spec(
        specs_dir=specs_dir_for_data_dir(tmp_path),
        name="Replay regression",
        prompt="",
        app_url="https://app.example",
        start_command="",
        harness_command=DEFAULT_HARNESS_COMMAND,
        execution_engine="native",
        exact_steps=[{"action": "navigate", "url": "https://app.example"}],
        fixtures={"issue_public_id": issue_public_id},
    )

    def fake_run_spec(**_: object) -> RetraceTesterRunResult:
        return RetraceTesterRunResult(
            run_id="run_failed",
            spec_id="spec_failed",
            ok=False,
            exit_code=1,
            run_dir=str(tmp_path / "run_failed"),
            harness_log_path="",
            app_log_path="",
            command="",
            final_prompt="",
            attempts=1,
            flaky=False,
            flake_reason="",
            status="failed",
            error="still broken",
            execution_engine="native",
        )

    monkeypatch.setattr("retrace.commands.ui.run_spec", fake_run_spec)

    payload, status = _verify_resolved_issues_payload(
        store=store,
        data_dir=tmp_path,
        cwd=tmp_path,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )

    assert status == 200
    assert payload["verified"] == []
    assert payload["regressed"][0]["public_id"] == issue_public_id
    row = store.get_replay_issue(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        issue_id=issue_public_id,
    )
    assert row is not None
    assert row["status"] == "regressed"


def test_verify_resolved_issues_payload_marks_passing_ui_spec_verified(
    tmp_path: Path, monkeypatch
) -> None:
    store, workspace = _workspace(tmp_path)
    issue_public_id = _resolved_replay_issue(store, workspace)
    create_spec(
        specs_dir=specs_dir_for_data_dir(tmp_path),
        name="Replay passing regression",
        prompt="",
        app_url="https://app.example",
        start_command="",
        harness_command=DEFAULT_HARNESS_COMMAND,
        execution_engine="native",
        exact_steps=[{"action": "navigate", "url": "https://app.example"}],
        fixtures={"issue_public_id": issue_public_id},
    )

    def fake_run_spec(**_: object) -> RetraceTesterRunResult:
        return RetraceTesterRunResult(
            run_id="run_passed",
            spec_id="spec_passed",
            ok=True,
            exit_code=0,
            run_dir=str(tmp_path / "run_passed"),
            harness_log_path="",
            app_log_path="",
            command="",
            final_prompt="",
            attempts=1,
            flaky=False,
            flake_reason="",
            status="passed",
            execution_engine="native",
        )

    monkeypatch.setattr("retrace.commands.ui.run_spec", fake_run_spec)

    payload, status = _verify_resolved_issues_payload(
        store=store,
        data_dir=tmp_path,
        cwd=tmp_path,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )

    assert status == 200
    assert payload["verified"] == [issue_public_id]
    assert payload["regressed"] == []
    row = store.get_replay_issue(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        issue_id=issue_public_id,
    )
    assert row is not None
    assert row["status"] == "verified"
    assert row["resolved_at"]


def test_verify_resolved_issues_payload_runs_linked_api_specs(
    tmp_path: Path, monkeypatch
) -> None:
    store, workspace = _workspace(tmp_path)
    issue_public_id = _resolved_replay_issue(store, workspace)
    issue = store.get_replay_issue(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        issue_id=issue_public_id,
    )
    assert issue is not None
    api_spec = create_api_spec(
        specs_dir=api_specs_dir_for_data_dir(tmp_path),
        name="Replay API verification",
        method="GET",
        url="https://app.example/api/health",
        expected_status=200,
        fixtures={
            "issue_id": str(issue["id"]),
            "issue_public_id": issue_public_id,
            "canonical_failure_id": str(issue["canonical_failure_id"]),
            "source": "replay_issue_api",
        },
    )
    link_id = store.upsert_failure_test_link(
        failure_id=str(issue["canonical_failure_id"]),
        issue_id=str(issue["id"]),
        issue_public_id=issue_public_id,
        spec_id=api_spec.spec_id,
        spec_name=api_spec.name,
        spec_path=str(api_specs_dir_for_data_dir(tmp_path) / f"{api_spec.spec_id}.json"),
        source="replay_issue_api",
    )

    def fake_run_api_spec(**_: object) -> APITestRunResult:
        return APITestRunResult(
            run_id="api_run_passed",
            spec_id=api_spec.spec_id,
            ok=True,
            status="passed",
            status_code=200,
            elapsed_ms=12,
            run_dir=str(tmp_path / "api_run_passed"),
        )

    monkeypatch.setattr("retrace.commands.ui.run_api_spec", fake_run_api_spec)

    payload, status = _verify_resolved_issues_payload(
        store=store,
        data_dir=tmp_path,
        cwd=tmp_path,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )

    assert status == 200
    assert payload["plan"][0]["tests"] == [
        {"kind": "api", "spec_id": api_spec.spec_id, "coverage_link_id": link_id}
    ]
    assert payload["verified"] == [issue_public_id]
    row = store.get_replay_issue(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        issue_id=issue_public_id,
    )
    assert row is not None
    assert row["status"] == "verified"
    assert row["resolved_at"]
    links = store.list_failure_test_links(
        failure_id=str(issue["canonical_failure_id"]),
        spec_id=api_spec.spec_id,
    )
    assert links[0].coverage_state == "covered_passing"
    assert links[0].latest_run_id == "api_run_passed"


def _resolved_replay_issue(store: Storage, workspace: object) -> str:
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-ui-verify",
        sequence=0,
        events=[
            {
                "type": 6,
                "timestamp": 200,
                "data": {
                    "plugin": "retrace/console@1",
                    "payload": {"level": "error", "payload": ["Verify crashed"]},
                },
            },
        ],
        flush_type="final",
    )
    processed = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-ui-verify"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    issue_public_id = processed.issues[0].public_id
    store.transition_replay_issue(processed.issues[0].issue_id, status="resolved")
    return issue_public_id
