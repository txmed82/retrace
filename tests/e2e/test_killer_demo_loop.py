"""E2E Test: The "Killer Demo Loop"

Covers the full pipeline:
1. Ingest signal (Sentry compat) -> Incident created.
2. Bridge promotes to QA Incident.
3. `retrace qa auto` loop:
   a. Auto-reproduce (mocked runner).
   b. Propose fix (mocked scorer).
   c. Open PR (mocked git/gh).
"""

from __future__ import annotations

import json
import uuid
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch, MagicMock

from click.testing import CliRunner

from retrace.cli import main
from retrace.matching.scorer import CodeCandidate


def _post_sentry_envelope(api, *, event_id: str, error_type: str = "E2EError") -> int:
    envelope_header = json.dumps({"event_id": event_id, "sent_at": "2026-05-14T10:00:00Z"})
    item_header = json.dumps({"type": "event"})
    item_body = json.dumps({
        "event_id": event_id,
        "level": "error",
        "platform": "javascript",
        "message": "Uncaught TypeError: Cannot read property 'id' of undefined",
        "exception": {
            "values": [{
                "type": error_type,
                "value": "Cannot read property 'id' of undefined",
                "stacktrace": {
                    "frames": [{
                        "filename": "client/src/components/UserCard.tsx",
                        "function": "render",
                        "lineno": 12,
                    }]
                },
            }]
        },
        "request": {"url": "http://localhost:3000/settings"},
        "breadcrumbs": {"values": [
            {"category": "ui.click", "message": "button.settings-btn", "timestamp": 1234567},
            {"category": "console", "message": "Fetching user data...", "level": "info"},
        ]}
    })
    body = ("\n".join([envelope_header, item_header, item_body])).encode("utf-8")

    host, port = api.base_url.removeprefix("http://").split(":")
    conn = HTTPConnection(host, int(port), timeout=5)
    conn.request(
        "POST",
        f"/api/sentry/{api.project_id}/envelope",
        body=body,
        headers={
            "X-Sentry-Auth": f"Sentry sentry_key={api.sdk_key}",
            "Content-Type": "application/x-sentry-envelope",
        },
    )
    response = conn.getresponse()
    response.read()
    status = response.status
    conn.close()
    return status


def test_killer_demo_loop(live_api, tmp_path, monkeypatch):
    # 1. Setup - create a dummy repo dir so the scorer doesn't crash
    repo_dir = tmp_path / "mock-repo"
    repo_dir.mkdir()
    (repo_dir / "client/src/components").mkdir(parents=True)
    (repo_dir / "client/src/components/UserCard.tsx").write_text("// dummy react component")
    (repo_dir / ".git").mkdir()

    # Create config
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"""
posthog:
  host: https://us.i.posthog.com
  project_id: "42"
  api_key: phk_mock
run:
  data_dir: {tmp_path}
  output_dir: {tmp_path}/reports
llm:
  provider: openai
  api_key: mock-key
  model: gpt-4o
  base_url: https://api.openai.com/v1
""")
    monkeypatch.chdir(tmp_path)

    # 2. Ingest
    event_id = uuid.uuid4().hex
    status = _post_sentry_envelope(live_api, event_id=event_id)
    assert status in (200, 202)

    # Verify incident exists
    incidents = live_api.store.list_incidents(
        project_id=live_api.project_id, environment_id=live_api.environment_id
    )
    assert len(incidents) >= 1
    
    # Verify QA incident exists (synced by bridge)
    qa_incidents = live_api.store.list_qa_incidents(limit=1)
    assert len(qa_incidents) == 1
    incident_public_id = qa_incidents[0]["public_id"]

    # Connect the repo in storage
    live_api.store.upsert_github_repo(
        repo_full_name="mocked/repo",
        local_path=str(repo_dir),
        default_branch="main"
    )

    # 3. Mocks for the auto loop
    # Mock Scorer
    mock_candidates = [
        CodeCandidate(file_path="client/src/components/UserCard.tsx", score=0.9, rationale="stack trace match")
    ]
    
    # Mock git/gh subprocess calls in retrace.auto_fix
    def mock_run(cmd, **kwargs):
        # Always return success for git/gh calls
        return MagicMock(returncode=0, stdout="https://github.com/mocked/repo/pull/1", stderr="")

    with patch("retrace.auto_repro.run_spec") as mock_run_spec, \
         patch("retrace.auto_fix.score_repo_for_finding", return_value=mock_candidates), \
         patch("retrace.auto_fix._run", side_effect=mock_run), \
         patch("retrace.auto_fix._gh_available", return_value=True), \
         patch("retrace.auto_fix._gh_pr_create", return_value=("https://github.com/mocked/repo/pull/1", "")):

        # Mock a confirmed bug reproduction
        mock_run_spec.return_value = MagicMock(
            ok=False,
            exit_code=1,
            run_id="run-123",
            run_dir=str(tmp_path / "run-123"),
            assertion_results=[{"assertion_type": "text_contains", "ok": False, "message": "error found"}],
            error=""
        )
        (tmp_path / "run-123").mkdir()

        # 4. Run retrace qa auto
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "qa", "auto",
                "--config", str(config_path),
                "--repo", "mocked/repo",
                "--project-id", live_api.project_id,
                "--environment-id", live_api.environment_id,
                "--engine", "native",
                "--ready" # not a draft
            ]
        )

        assert result.exit_code == 0, result.output
        assert "Step 1/2  Reproducing" in result.output
        assert "Step 2/2  Building fix prompt" in result.output
        assert "Done." in result.output

        # 5. Final Assertions
        updated_row = live_api.store.get_qa_incident(incident_public_id)
        assert updated_row["status"] == "fix_proposed"
        assert updated_row["fix_status"] == "pr_open"
        assert updated_row["fix_pr_url"] == "https://github.com/mocked/repo/pull/1"
        
        # Check that the prompt was actually written
        prompt_path = Path(updated_row["fix_prompt_path"])
        assert prompt_path.exists()
        prompt_text = prompt_path.read_text()
        assert "Retrace fix request" in prompt_text
        assert incident_public_id in prompt_text
        assert "client/src/components/UserCard.tsx" in prompt_text
        # Verify reproduction/trace is embedded
        assert "UserCard.tsx:render:12" in prompt_text
