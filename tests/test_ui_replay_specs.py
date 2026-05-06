from __future__ import annotations

from pathlib import Path

from retrace.commands.ui import _generate_replay_issue_spec_payload
from retrace.replay_core import ReplaySignalConfig, process_replay_sessions
from retrace.storage import Storage
from retrace.tester import specs_dir_for_data_dir


def _workspace(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(
        org_name="Acme",
        project_name="Web",
        environment_name="production",
    )
    return store, workspace


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
