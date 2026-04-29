"""End-to-end test for the full self-host loop.

Walks the path a self-host user lives on:
    SDK ingest → finalize job → process_replay_sessions → cluster + AI summary
        → repro spec generation → run spec (native HTTP) → file ticket via mocked
        Linear → resolve issue → verify-resolved transitions to regressed when
        the spec re-fails.

The aim is one canary test that wires the seams together. Detailed unit
behavior is covered in the per-module test files.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import httpx

from retrace.issue_sink_clients import LinearClient
from retrace.issue_sinks import promote_replay_issue
from retrace.replay_core import ReplaySignalConfig, process_queued_replay_jobs
from retrace.replay_specs import generate_spec_from_replay_issue
from retrace.storage import Storage
from retrace.tester import (
    run_spec,
    runs_dir_for_data_dir,
    specs_dir_for_data_dir,
)


def _navigation(url: str, ts: int = 0) -> dict[str, Any]:
    return {"type": 4, "timestamp": ts, "data": {"href": url}}


def _console_error(message: str, ts: int = 1000) -> dict[str, Any]:
    return {
        "type": 6,
        "timestamp": ts,
        "data": {
            "plugin": "retrace/console@1",
            "payload": {"level": "error", "payload": [message]},
        },
    }


class _AppHandler(BaseHTTPRequestHandler):
    """Tiny test HTTP app — returns 200 by default, 500 when toggled."""

    state = {"broken": False}

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.state.get("broken"):
            body = b"broken"
            self.send_response(500)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b"<html><body>Welcome</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


def _start_app() -> tuple[ThreadingHTTPServer, str, Thread]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _AppHandler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return httpd, f"http://{host}:{port}", thread


def test_full_self_host_loop_creates_issue_promotes_resolves_regresses(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    store = Storage(data_dir / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")

    # Start the test app first so we can wire its URL into the seeded replay.
    httpd, app_url, _thread = _start_app()
    try:
        # 1) SDK ingests a replay batch whose navigation points at our test app.
        store.insert_replay_batch(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            session_id="sess-e2e",
            sequence=0,
            events=[
                _navigation(f"{app_url}/checkout"),
                _console_error("TypeError: total is undefined"),
            ],
            flush_type="final",
        )
        # insert_replay_batch with flush_type="final" auto-enqueues the
        # replay.finalize job — same path the SDK ingest API uses.

        # 2) Worker processes the queue.  No LLM client → deterministic fallback.
        summary = process_queued_replay_jobs(
            store=store,
            config=ReplaySignalConfig.from_names(["console_error"]),
        )
        assert summary.jobs_processed == 1
        assert summary.issues_inserted == 1
        public_id = summary.inserted_public_ids[0]

        # 3) Generate a repro spec from the issue.
        specs_dir = specs_dir_for_data_dir(data_dir)
        generated = generate_spec_from_replay_issue(
            store=store,
            specs_dir=specs_dir,
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            issue_id=public_id,
            app_url=app_url,
        )
        assert generated.issue_public_id == public_id

        # The auto-generated spec includes an "issue-not-reproduced" model-consensus
        # assertion. We don't have an LLM in this test, so strip it and keep the
        # deterministic page-loads assertion.  Also drop unknown rrweb-derived
        # steps that the native runner can't replay (e.g. raw console-error
        # events that have no corresponding interaction).
        generated.spec.assertions[:] = [
            a for a in generated.spec.assertions if a.get("type") != "model_consensus"
        ]
        generated.spec.exact_steps[:] = [
            s for s in generated.spec.exact_steps if s.get("action") != "unknown"
        ]

        # 4) Run the generated spec against the live test app.  Status 200
        # gives us a passing run.
        runs_dir = runs_dir_for_data_dir(data_dir)
        result = run_spec(
            spec=generated.spec,
            runs_dir=runs_dir,
            cwd=tmp_path,
        )
        assert result.ok, result.error

        # 5) Promote to a mocked Linear issue (real client, mocked HTTP).
        def linear_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if "TeamLabels" in body["query"]:
                return httpx.Response(
                    200,
                    json={"data": {"team": {"labels": {"nodes": []}}}},
                )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "issueCreate": {
                            "success": True,
                            "issue": {
                                "id": "uuid-x",
                                "identifier": "ENG-1",
                                "url": "https://linear.app/acme/issue/ENG-1",
                                "title": "x",
                            },
                        }
                    }
                },
            )

        with httpx.Client(transport=httpx.MockTransport(linear_handler)) as raw:
            client = LinearClient(api_key="lin_api_test", client=raw)
            promote_result = promote_replay_issue(
                store=store,
                project_id=workspace.project_id,
                environment_id=workspace.environment_id,
                issue_id=public_id,
                provider="linear",
                linear_client=client,
                linear_team_id="team-uuid",
            )
        assert promote_result.created is True
        assert promote_result.external_id == "ENG-1"

        # 6) Mark the issue resolved (the human did the fix, ticket closed).
        issue_row = store.get_replay_issue(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            issue_id=public_id,
        )
        assert issue_row is not None
        store.transition_replay_issue(str(issue_row["id"]), status="resolved")

        # 7) Toggle the test app to broken; rerunning the same repro spec
        # should fail and verify-resolved should regress the issue.
        _AppHandler.state["broken"] = True
        result_after = run_spec(
            spec=generated.spec,
            runs_dir=runs_dir,
            cwd=tmp_path,
        )
        assert result_after.ok is False

        # Manually invoke the storage transition that verify-resolved would do
        # to keep this test focused on the data path (the CLI surface itself
        # is exercised in test_resolution_verification).
        store.transition_replay_issue(str(issue_row["id"]), status="regressed")
        regressed_row = store.get_replay_issue(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            issue_id=public_id,
        )
        assert regressed_row is not None
        assert str(regressed_row["status"]) == "regressed"
    finally:
        _AppHandler.state["broken"] = False
        httpd.shutdown()


def test_replay_upsert_surfaces_previous_and_regressed_status(tmp_path: Path) -> None:
    """Direct unit-level coverage of the new ReplayIssueUpsertResult fields."""
    from retrace.replay_core import process_replay_sessions

    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")

    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-1",
        sequence=0,
        events=[
            _navigation("https://app.example/checkout"),
            _console_error("TypeError: total is undefined"),
        ],
        flush_type="incremental",  # avoid auto-enqueue here; we drive process_replay_sessions ourselves
    )
    res1 = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-1"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    assert res1.issues[0].inserted is True
    assert res1.issues[0].previous_status == ""
    assert res1.issues[0].current_status == "new"
    assert res1.issues[0].regressed is False

    # Mark resolved, then re-process — should flip to regressed.
    store.transition_replay_issue(res1.issues[0].issue_id, status="resolved")
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-2",
        sequence=0,
        events=[
            _navigation("https://app.example/checkout"),
            _console_error("TypeError: total is undefined"),
        ],
        flush_type="incremental",
    )
    res2 = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-2"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    assert res2.issues[0].inserted is False
    assert res2.issues[0].previous_status == "resolved"
    assert res2.issues[0].current_status == "regressed"
    assert res2.issues[0].regressed is True
