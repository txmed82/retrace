"""`retrace monitor rules` CLI tests."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from retrace.commands.monitor import monitor_group
from retrace.storage import Storage


_CONFIG = """posthog:
  host: https://us.i.posthog.com
  project_id: demo
llm:
  provider: openai_compatible
  base_url: http://127.0.0.1:8080/v1
  model: x
run:
  data_dir: {data_dir}
"""


def _config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CONFIG.format(data_dir=str(tmp_path / "data")))
    return cfg


def test_monitor_rules_set_then_list_then_delete(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    runner = CliRunner()

    # Empty list initially.
    r = runner.invoke(monitor_group, ["rules", "list", "--config", str(cfg)])
    assert r.exit_code == 0
    assert "No alert rules" in r.output

    # Create.
    r = runner.invoke(
        monitor_group,
        [
            "rules", "set",
            "--config", str(cfg),
            "--name", "high-severity-login-errors",
            "--action", "alert",
            "--min-severity", "high",
            "--title-contains", "login",
            "--precedence", "10",
            "--metadata-json", '{"reason": "wedge"}',
        ],
    )
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["name"] == "high-severity-login-errors"

    # List finds it.
    r = runner.invoke(monitor_group, ["rules", "list", "--config", str(cfg), "--json"])
    assert r.exit_code == 0
    rules = json.loads(r.output)
    assert len(rules) == 1
    assert rules[0]["name"] == "high-severity-login-errors"
    assert rules[0]["min_severity"] == "high"
    assert rules[0]["title_contains"] == "login"
    assert rules[0]["metadata"] == {"reason": "wedge"}

    # Show.
    r = runner.invoke(
        monitor_group,
        ["rules", "show", "high-severity-login-errors", "--config", str(cfg)],
    )
    assert r.exit_code == 0
    detail = json.loads(r.output)
    assert detail["precedence"] == 10

    # Delete.
    r = runner.invoke(
        monitor_group,
        ["rules", "delete", "high-severity-login-errors", "--config", str(cfg), "--yes"],
    )
    assert r.exit_code == 0
    assert json.loads(r.output)["deleted"] is True

    # Gone.
    r = runner.invoke(monitor_group, ["rules", "list", "--config", str(cfg)])
    assert r.exit_code == 0
    assert "No alert rules" in r.output


def test_monitor_rules_set_is_idempotent(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    runner = CliRunner()
    for _ in range(2):
        r = runner.invoke(
            monitor_group,
            [
                "rules", "set",
                "--config", str(cfg),
                "--name", "dup",
                "--action", "suppress",
            ],
        )
        assert r.exit_code == 0
    store = Storage(tmp_path / "data" / "retrace.db")
    workspace = store.ensure_workspace(project_name="Default")
    rules = store.list_app_error_alert_rules(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )
    assert len(rules) == 1
    assert rules[0].action == "suppress"


def test_monitor_rules_rejects_bad_metadata_json(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    runner = CliRunner()
    r = runner.invoke(
        monitor_group,
        [
            "rules", "set",
            "--config", str(cfg),
            "--name", "bad",
            "--metadata-json", "not-json",
        ],
    )
    assert r.exit_code != 0
    assert "not valid JSON" in r.output


def test_monitor_rules_delete_missing_is_not_an_error(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    runner = CliRunner()
    r = runner.invoke(
        monitor_group,
        ["rules", "delete", "nope", "--config", str(cfg), "--yes"],
    )
    assert r.exit_code == 0
    assert json.loads(r.output)["deleted"] is False
