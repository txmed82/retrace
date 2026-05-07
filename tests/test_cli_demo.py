from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from retrace.cli import main
from retrace.storage import Storage


def test_demo_seed_creates_replay_issue_and_spec_without_config(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["demo", "seed"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["config_path"] == "config.yaml"
    assert payload["session_id"] == "demo-checkout-crash"
    assert (tmp_path / "config.yaml").exists()
    assert payload["signals_detected"] == 1
    assert payload["issue_public_id"].startswith("bug_")
    assert payload["tester_spec"]["spec_id"]
    assert payload["tester_spec"]["confidence"] == "high"
    assert payload["tester_spec"]["known_gaps"] == []
    assert payload["fix_prompts"]["repo"] == "local/demo-checkout"
    assert payload["fix_prompts"]["candidates"][0]["file_path"] == "src/checkout.tsx"
    assert (
        tmp_path / "data" / "ui-tests" / "specs" / f"{payload['tester_spec']['spec_id']}.json"
    ).exists()
    assert (
        tmp_path
        / "reports"
        / "fix-prompts"
        / payload["fix_prompts"]["artifact_json"]
    ).exists()
    assert (
        tmp_path
        / "reports"
        / "fix-prompts"
        / payload["fix_prompts"]["prompt_files"]["codex"]
    ).exists()

    store = Storage(tmp_path / "data" / "retrace.db")
    workspace = store.ensure_workspace(
        org_name="Local",
        project_name="Default",
        environment_name="production",
    )
    issues = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )
    assert len(issues) == 1
    assert "Cannot read properties" in issues[0]["title"]

    generated_again = runner.invoke(
        main, ["tester", "from-replay-issue", payload["issue_public_id"]]
    )
    assert generated_again.exit_code == 0, generated_again.output
    assert json.loads(generated_again.output)["issue_public_id"] == payload["issue_public_id"]


def test_demo_seed_can_skip_spec_generation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["demo", "seed", "--no-generate-spec"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["tester_spec"] is None
    assert payload["fix_prompts"]["repo"] == "local/demo-checkout"
    assert not (tmp_path / "data" / "ui-tests" / "specs").exists()


def test_demo_seed_can_skip_fix_prompt_generation(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["demo", "seed", "--no-generate-fix-prompts"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["tester_spec"]["spec_id"]
    assert payload["fix_prompts"] is None
    assert not (tmp_path / "reports" / "fix-prompts").exists()


def test_demo_seed_next_commands_preserve_custom_config(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    config_path = tmp_path / "custom config.yaml"

    result = runner.invoke(main, ["demo", "seed", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["config_path"] == str(config_path)
    assert str(config_path) in payload["next_commands"][0]
    assert str(config_path) in payload["next_commands"][1]
