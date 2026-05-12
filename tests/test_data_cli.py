"""P2.3 — tests for `retrace data` CLI subcommands."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import yaml
from click.testing import CliRunner

from retrace.commands.data import data_group as data_cli
from retrace.storage import Storage


def _write_config(tmp_path: Path, retention: dict | None = None) -> Path:
    cfg = tmp_path / "config.yaml"
    body: dict = {
        "posthog": {
            "host": "https://us.i.posthog.com",
            "project_id": "",
        },
        "llm": {
            "provider": "openai_compatible",
            "base_url": "http://127.0.0.1:8080/v1",
            "model": "x",
        },
        "run": {"data_dir": str(tmp_path / "data")},
    }
    if retention is not None:
        body["retention"] = retention
    cfg.write_text(yaml.safe_dump(body, sort_keys=False))
    return cfg


def _seed_store(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    store = Storage(data_dir / "retrace.db")
    store.init_schema()
    store.insert_replay_batch(
        project_id="proj_1",
        environment_id="env_1",
        session_id="sess_a",
        sequence=1,
        events=[{"type": 0, "timestamp": 1}],
        flush_type="normal",
    )


# ---------------------------------------------------------------------------
# data retention apply
# ---------------------------------------------------------------------------


def test_data_retention_apply_dry_run_on_fresh_install(tmp_path):
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        data_cli, ["retention", "apply", "--config", str(cfg), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    # No data → all zeros.
    assert all(v == 0 for v in payload["pruned"].values())


def test_data_retention_apply_uses_config_policy(tmp_path):
    """Retention TTLs come from `config.yaml`'s `retention:` block.
    Defaults apply when the section is omitted."""
    cfg = _write_config(
        tmp_path,
        retention={
            "failures_days": 7,
            "replay_batches_days": 7,
            "otel_events_days": 7,
        },
    )
    runner = CliRunner()
    result = runner.invoke(
        data_cli, ["retention", "apply", "--config", str(cfg), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["policy"]["failures_days"] == 7
    assert payload["policy"]["replay_batches_days"] == 7
    assert payload["policy"]["otel_events_days"] == 7
    # Other policy keys default to the config defaults.
    assert payload["policy"]["evidence_days"] == 90


def test_data_retention_apply_real_run_against_seeded_store(tmp_path):
    cfg = _write_config(
        tmp_path, retention={"replay_batches_days": 30}
    )
    _seed_store(tmp_path)
    # Fresh batch (now-ish) — not eligible at 30-day cutoff.
    runner = CliRunner()
    result = runner.invoke(data_cli, ["retention", "apply", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is False
    assert payload["pruned"]["replay_batches"] == 0


# ---------------------------------------------------------------------------
# data backup
# ---------------------------------------------------------------------------


def test_data_backup_writes_tarball(tmp_path):
    cfg = _write_config(tmp_path)
    _seed_store(tmp_path)
    out = tmp_path / "snapshot.tar.gz"
    runner = CliRunner()
    result = runner.invoke(
        data_cli, ["backup", "--config", str(cfg), "--to", str(out)]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["output_path"] == str(out)
    assert payload["bytes_written"] > 0
    assert out.exists()
    with tarfile.open(out, "r:gz") as tar:
        names = sorted(m.name for m in tar.getmembers())
    assert "retrace.db" in names


def test_data_backup_errors_on_missing_db(tmp_path):
    cfg = _write_config(tmp_path)  # data dir not seeded
    out = tmp_path / "snapshot.tar.gz"
    runner = CliRunner()
    result = runner.invoke(
        data_cli, ["backup", "--config", str(cfg), "--to", str(out)]
    )
    assert result.exit_code != 0
    assert "database not found" in result.output


def test_data_backup_errors_when_output_path_is_directory(tmp_path):
    cfg = _write_config(tmp_path)
    _seed_store(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(
        data_cli, ["backup", "--config", str(cfg), "--to", str(out_dir)]
    )
    assert result.exit_code != 0
