import json
from pathlib import Path

from click.testing import CliRunner

from retrace.cli import main
from retrace.storage import Storage


_CONFIG_YAML = """posthog:
  host: https://us.i.posthog.com
  project_id: "172523"
llm:
  base_url: http://localhost:8080/v1
  model: gemma
run:
  lookback_hours: 168
  max_sessions_per_run: 50
  output_dir: ./reports
  data_dir: ./data
detectors:
  console_error: true
  network_5xx: true
  network_4xx: true
  rage_click: true
  dead_click: true
  error_toast: true
  blank_render: true
  session_abandon_on_error: true
cluster:
  min_size: 1
"""


def test_github_connect_list_disconnect(tmp_path: Path, monkeypatch):
    (tmp_path / "config.yaml").write_text(_CONFIG_YAML)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["github", "connect", "--repo", "acme/widgets"])
    assert result.exit_code == 0, result.output
    assert "Connected acme/widgets" in result.output

    result = runner.invoke(main, ["github", "list"])
    assert result.exit_code == 0, result.output
    assert "acme/widgets" in result.output

    result = runner.invoke(main, ["github", "disconnect", "--repo", "acme/widgets"])
    assert result.exit_code == 0, result.output
    assert "Disconnected acme/widgets" in result.output


def test_suggest_fixes_parses_report_and_writes_scaffold(tmp_path: Path, monkeypatch):
    (tmp_path / "config.yaml").write_text(_CONFIG_YAML)
    report_dir = tmp_path / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report = report_dir / "2026-04-22-160630.md"
    report.write_text(
        """# Retrace report — 2026-04-22 16:06 UTC

Scanned 9 sessions.  2 flagged into 2 cluster(s).

## 🟠 High

### Unresponsive buttons in store

- **Sample session:** [sess-1](https://us.i.posthog.com/project/172523/replay/sess-1)
- **Category:** functional_error
"""
    )

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    connect = runner.invoke(main, ["github", "connect", "--repo", "acme/widgets"])
    assert connect.exit_code == 0, connect.output

    result = runner.invoke(
        main,
        [
            "suggest-fixes",
            "--latest",
            "--repo",
            "acme/widgets",
            "--out",
            "./reports/fix-prompts",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Parsed 1 findings" in result.output

    out_dir = tmp_path / "reports" / "fix-prompts"
    json_files = list(out_dir.glob("*.json"))
    codex_files = list(out_dir.glob("*.codex.md"))
    claude_files = list(out_dir.glob("*.claude.md"))
    assert len(json_files) == 1
    assert len(codex_files) == 1
    assert len(claude_files) == 1
    text = json_files[0].read_text()
    assert "phase_2_prompt_generation" in text

    store = Storage(tmp_path / "data" / "retrace.db")
    store.init_schema()
    rows = store.list_report_findings()
    assert len(rows) == 1
    assert rows[0].title == "Unresponsive buttons in store"


def test_suggest_fixes_generates_from_replay_issue(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    seeded = runner.invoke(main, ["demo", "seed"])
    assert seeded.exit_code == 0, seeded.output
    issue_id = json.loads(seeded.output)["issue_public_id"]

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "checkout.tsx").write_text(
        "export function Checkout(){ return <button data-testid='checkout-pay'>Pay now</button> }"
    )

    connected = runner.invoke(
        main,
        [
            "github",
            "connect",
            "--repo",
            "acme/widgets",
            "--local-path",
            str(tmp_path),
        ],
    )
    assert connected.exit_code == 0, connected.output

    result = runner.invoke(
        main,
        [
            "suggest-fixes",
            "--replay-issue",
            issue_id,
            "--repo",
            "acme/widgets",
            "--out",
            "./reports/fix-prompts",
        ],
    )

    assert result.exit_code == 0, result.output
    assert f"Parsed 1 findings from replay issue {issue_id}" in result.output
    out_dir = tmp_path / "reports" / "fix-prompts"
    json_files = list(out_dir.glob("replay-*.json"))
    codex_files = list(out_dir.glob("replay-*.codex.md"))
    claude_files = list(out_dir.glob("replay-*.claude.md"))
    assert len(json_files) == 1
    assert len(codex_files) == 1
    assert len(claude_files) == 1
    artifact = json_files[0].read_text()
    assert '"repo": "acme/widgets"' in artifact
    assert "checkout.tsx" in artifact
    assert issue_id in codex_files[0].read_text()

    store = Storage(tmp_path / "data" / "retrace.db")
    store.init_schema()
    tasks = store.list_repair_tasks()
    assert len(tasks) == 1
    assert tasks[0].source_type == "replay_issue"
    assert tasks[0].source_external_id == issue_id
    assert "src/checkout.tsx" in tasks[0].likely_files
    assert len(tasks[0].prompt_artifacts) == 3
    workspace = store.ensure_workspace(project_name="Default")
    failure = store.find_failure_by_source(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        source_type="replay_issue",
        source_external_id=issue_id,
    )
    assert failure is not None
    assert failure.linked_repair_task_id == tasks[0].id


def test_suggest_fixes_replay_issue_honors_project_environment_scope(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    seeded = runner.invoke(
        main,
        [
            "demo",
            "seed",
            "--project",
            "Scoped App",
            "--environment",
            "staging",
        ],
    )
    assert seeded.exit_code == 0, seeded.output
    seed_payload = json.loads(seeded.output)
    issue_id = seed_payload["issue_public_id"]

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "checkout.tsx").write_text(
        "export function Checkout(){ return <button data-testid='checkout-pay'>Pay now</button> }"
    )

    connected = runner.invoke(
        main,
        [
            "github",
            "connect",
            "--repo",
            "acme/widgets",
            "--local-path",
            str(tmp_path),
        ],
    )
    assert connected.exit_code == 0, connected.output

    result = runner.invoke(
        main,
        [
            "suggest-fixes",
            "--replay-issue",
            issue_id,
            "--project-id",
            seed_payload["project_id"],
            "--environment-id",
            seed_payload["environment_id"],
            "--repo",
            "acme/widgets",
            "--out",
            "./reports/fix-prompts",
        ],
    )

    assert result.exit_code == 0, result.output
    assert f"Parsed 1 findings from replay issue {issue_id}" in result.output
    out_dir = tmp_path / "reports" / "fix-prompts"
    json_files = list(out_dir.glob("replay-*.json"))
    codex_files = list(out_dir.glob("replay-*.codex.md"))
    claude_files = list(out_dir.glob("replay-*.claude.md"))
    assert len(json_files) == 1
    assert len(codex_files) == 1
    assert len(claude_files) == 1
    artifact = json_files[0].read_text()
    assert '"repo": "acme/widgets"' in artifact
    assert "checkout.tsx" in artifact
