"""P3.1 — `retrace tester quarantine` CLI smoke tests."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from retrace.commands.tester import tester_group as tester_cli
from retrace.storage import Storage


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "posthog": {"host": "https://us.i.posthog.com", "project_id": ""},
                "llm": {
                    "provider": "openai_compatible",
                    "base_url": "http://127.0.0.1:8080/v1",
                    "model": "x",
                },
                "run": {"data_dir": str(tmp_path / "data")},
            },
            sort_keys=False,
        )
    )
    return cfg


def test_quarantine_list_empty(tmp_path):
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(tester_cli, ["quarantine", "list", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


def test_quarantine_force_then_list(tmp_path):
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        tester_cli,
        [
            "quarantine", "force",
            "--config", str(cfg),
            "--reason", "known flaky",
            "spec_a",
        ],
    )
    assert result.exit_code == 0, result.output
    state = json.loads(result.output)
    assert state["spec_id"] == "spec_a"
    assert state["status"] == "quarantined"
    assert state["quarantine_reason"] == "known flaky"

    listed = runner.invoke(tester_cli, ["quarantine", "list", "--config", str(cfg)])
    assert listed.exit_code == 0
    rows = json.loads(listed.output)
    assert [r["spec_id"] for r in rows] == ["spec_a"]


def test_quarantine_show_unknown_spec(tmp_path):
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        tester_cli, ["quarantine", "show", "--config", str(cfg), "never-seen"]
    )
    assert result.exit_code == 0, result.output
    state = json.loads(result.output)
    assert state["status"] == "active"
    assert state["recent_outcomes"] == []


def test_quarantine_release(tmp_path):
    cfg = _write_config(tmp_path)
    Storage(tmp_path / "data" / "retrace.db").init_schema()
    runner = CliRunner()
    runner.invoke(
        tester_cli,
        ["quarantine", "force", "--config", str(cfg), "--reason", "x", "spec_a"],
    )
    result = runner.invoke(
        tester_cli,
        ["quarantine", "release", "--config", str(cfg), "--reason", "fixed", "spec_a"],
    )
    assert result.exit_code == 0, result.output
    state = json.loads(result.output)
    assert state["status"] == "active"
