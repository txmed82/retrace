from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from retrace.commands.ui import (
    _create_sdk_key_payload,
    _generate_replay_issue_fix_prompts_payload,
    _generate_replay_issue_spec_payload,
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
    out_dir = tmp_path / "reports" / "fix-prompts"
    assert (out_dir / payload["artifact_json"]).exists()
    assert (out_dir / payload["prompt_files"]["codex"]).exists()


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
    assert payload == {"ok": False, "error": "status must be resolved or unresolved"}


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
