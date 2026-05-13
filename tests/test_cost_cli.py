"""P3.5 — `retrace cost summary` CLI tests."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from retrace.commands.cost import cost_group as cost_cli
from retrace.storage import Storage


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "posthog": {
                    "host": "https://us.i.posthog.com",
                    "project_id": "",
                },
                "llm": {
                    "provider": "openai_compatible",
                    "base_url": "http://127.0.0.1:8080/v1",
                    "model": "gpt-4o-mini",
                },
                "run": {"data_dir": str(tmp_path / "data")},
            },
            sort_keys=False,
        )
    )
    return cfg


def _seed_reviews(tmp_path: Path) -> Storage:
    store = Storage(tmp_path / "data" / "retrace.db")
    store.init_schema()
    store.add_llm_pr_review(
        repo="org/app",
        pr_number=42,
        model="gpt-4o-mini",
        summary="ok",
        risk_notes=[],
        suggestions=[],
        paths=["a.ts"],
        input_tokens=100_000,
        output_tokens=10_000,
        estimated_cost_usd=0.021,
    )
    store.add_llm_pr_review(
        repo="org/app",
        pr_number=43,
        model="gpt-4o",
        summary="ok",
        risk_notes=[],
        suggestions=[],
        paths=["b.ts"],
        input_tokens=50_000,
        output_tokens=20_000,
        estimated_cost_usd=0.325,
    )
    return store


def test_cost_summary_empty(tmp_path):
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cost_cli, ["summary", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "No LLM PR reviews recorded" in result.output


def test_cost_summary_json_groups_by_model(tmp_path):
    cfg = _write_config(tmp_path)
    _seed_reviews(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cost_cli,
        ["summary", "--config", str(cfg), "--by", "model", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["group_by"] == "model"
    models = {g["model"] for g in payload["groups"]}
    assert models == {"gpt-4o", "gpt-4o-mini"}
    assert payload["totals"]["reviews"] == 2
    assert payload["totals"]["input_tokens"] == 150_000
    assert payload["totals"]["output_tokens"] == 30_000
    # 0.021 + 0.325 = 0.346
    assert abs(payload["totals"]["estimated_cost_usd"] - 0.346) < 1e-6


def test_cost_summary_json_groups_by_repo(tmp_path):
    cfg = _write_config(tmp_path)
    _seed_reviews(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cost_cli, ["summary", "--config", str(cfg), "--by", "repo", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # Same repo on both rows → one group, summed.
    assert len(payload["groups"]) == 1
    g = payload["groups"][0]
    assert g["repo"] == "org/app"
    assert g["reviews"] == 2


def test_cost_summary_json_groups_by_pr(tmp_path):
    cfg = _write_config(tmp_path)
    _seed_reviews(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cost_cli, ["summary", "--config", str(cfg), "--by", "pr", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    prs = {g["pr"] for g in payload["groups"]}
    assert prs == {"org/app#42", "org/app#43"}


def test_cost_summary_human_table_renders(tmp_path):
    cfg = _write_config(tmp_path)
    _seed_reviews(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cost_cli, ["summary", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "gpt-4o-mini" in result.output
    assert "gpt-4o" in result.output
    assert "TOTAL" in result.output
    # Includes the disclaimer about the estimate methodology.
    assert "chars/4" in result.output


def test_cost_summary_invalid_since_rejected(tmp_path):
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cost_cli, ["summary", "--config", str(cfg), "--since", "0"]
    )
    assert result.exit_code != 0
