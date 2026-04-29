from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from click.testing import CliRunner

from retrace.cli import main
from retrace.digest import (
    DigestIssueRow,
    build_digest,
    render_digest_markdown,
    write_digest_report,
)
from retrace.replay_core import ReplaySignalConfig, process_replay_sessions
from retrace.storage import Storage


def _navigation(url: str, ts: int = 0) -> dict[str, object]:
    return {"type": 4, "timestamp": ts, "data": {"href": url}}


def _console_error(message: str, ts: int = 1000) -> dict[str, object]:
    return {
        "type": 6,
        "timestamp": ts,
        "data": {
            "plugin": "retrace/console@1",
            "payload": {"level": "error", "payload": [message]},
        },
    }


def _seed_one_issue(tmp_path: Path) -> tuple[Storage, str, str, str]:
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
        flush_type="final",
    )
    processed = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-1"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    return store, workspace.project_id, workspace.environment_id, processed.issues[0].public_id


def test_build_digest_buckets_new_issues_in_window(tmp_path: Path) -> None:
    store, pid, eid, public_id = _seed_one_issue(tmp_path)
    digest = build_digest(store=store, project_id=pid, environment_id=eid)
    assert any(r.public_id == public_id for r in digest.new_issues)
    # The same issue is open and high-impact (1 session); appears in top_impact_open too.
    assert any(r.public_id == public_id for r in digest.top_impact_open)


def test_build_digest_excludes_old_updated_at(tmp_path: Path) -> None:
    store, pid, eid, _ = _seed_one_issue(tmp_path)
    # Push the window 24h forward — the seeded issue (just-created) falls out.
    future = datetime.now(timezone.utc) + timedelta(hours=48)
    digest = build_digest(
        store=store, project_id=pid, environment_id=eid, lookback_hours=1, now=future
    )
    assert digest.new_issues == []
    assert digest.regressed_issues == []
    assert digest.resolved_issues == []


def test_render_digest_markdown_handles_empty() -> None:
    from retrace.digest import DigestPayload

    text = render_digest_markdown(
        DigestPayload(
            project_id="p",
            environment_id="e",
            window_start="2026-01-01T00:00:00+00:00",
            window_end="2026-01-02T00:00:00+00:00",
        )
    )
    assert "No replay activity" in text


def test_render_digest_markdown_includes_sections() -> None:
    from retrace.digest import DigestPayload

    rows = [
        DigestIssueRow(
            public_id="bug_a",
            title="Crash",
            severity="high",
            status="new",
            affected_count=10,
            affected_users=4,
            updated_at="2026-01-01T00:00:00+00:00",
        )
    ]
    text = render_digest_markdown(
        DigestPayload(
            project_id="p",
            environment_id="e",
            window_start="2026-01-01T00:00:00+00:00",
            window_end="2026-01-02T00:00:00+00:00",
            new_issues=rows,
            top_impact_open=rows,
        )
    )
    assert "New issues" in text
    assert "bug_a" in text
    assert "Top open issues" in text


def test_write_digest_report_creates_file(tmp_path: Path) -> None:
    from retrace.digest import DigestPayload

    out_dir = tmp_path / "reports"
    digest = DigestPayload(
        project_id="p",
        environment_id="e",
        window_start="2026-01-01T00:00:00+00:00",
        window_end="2026-01-02T00:00:00+00:00",
    )
    path = write_digest_report(digest=digest, reports_dir=out_dir)
    assert path.exists()
    assert path.parent == out_dir
    assert path.name.startswith("digest-")


_CONFIG_YAML = """posthog:
  host: https://us.i.posthog.com
  project_id: "42"
  api_key: phx_test
llm:
  base_url: http://localhost:8080/v1
  model: llama
run:
  data_dir: ./data
  output_dir: ./reports
"""


def test_cli_digest_writes_report_and_emits_json(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "config.yaml").write_text(_CONFIG_YAML)
    (tmp_path / ".env").write_text("")
    (tmp_path / "data").mkdir()

    # Seed an issue using the same data_dir layout the CLI will read.
    store = Storage(tmp_path / "data" / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-cli",
        sequence=0,
        events=[
            _navigation("https://app.example/checkout"),
            _console_error("TypeError: total is undefined"),
        ],
        flush_type="final",
    )
    process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-cli"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["digest", "--format", "json"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["counts"]["new"] >= 1
    assert out["report_path"].endswith(".md")
    assert (tmp_path / "reports").exists()
