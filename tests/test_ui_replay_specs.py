from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from retrace.commands.ui import (
    _create_sdk_key_payload,
    _generate_replay_issue_fix_prompts_payload,
    _INDEX_HTML,
    _replay_api_check,
    _generate_replay_issue_spec_payload,
    _to_replay_dashboard_payload,
    _transition_replay_issue_payload,
    _verify_resolved_issues_payload,
)
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
    assert "Confidence:" in _INDEX_HTML
    assert "Reasons:" in _INDEX_HTML
    assert 'data-view="issues"' in _INDEX_HTML
    assert 'data-view="findings"' in _INDEX_HTML
    assert "linkedFailureTests" in _INDEX_HTML
    assert 'role="button" tabindex="0"' in _INDEX_HTML
    assert 'rel="noopener noreferrer"' in _INDEX_HTML
    assert "hashchange" in _INDEX_HTML
    assert "safeExternalUrl" in _INDEX_HTML
    assert "safeHashUrl(issue.share_url, '#issue=')" in _INDEX_HTML
    assert "#issue=${encodeURIComponent(issue.public_id)}" in _INDEX_HTML
    assert "#replay=${encodeURIComponent(replayId)}" in _INDEX_HTML


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
    assert issue["sessions"][0]["stable_id"] == "sess-timeline"
    assert issue["sessions"][0]["public_id"].startswith("rpl_")
    assert issue["test_links"][0]["spec_id"] == "checkout-replay-regression"
    assert issue["test_links"][0]["coverage_state"] == "covered_failing"
    assert issue["test_links"][0]["latest_run_status"] == "failed"
    assert issue["test_links"][0]["latest_run_classification"] == "app_bug"


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
    assert payload["confidence"] == "medium"
    assert payload["known_gaps"]
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
