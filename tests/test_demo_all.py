"""Tests for `retrace demo all` — every pillar lands an incident."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from retrace.commands.demo import demo_group
from retrace.commands.demo_all import seed_all_pillars
from retrace.storage import Storage


_CONFIG = """posthog:
  host: https://us.i.posthog.com
  project_id: demo
llm:
  provider: openai_compatible
  base_url: http://127.0.0.1:8080/v1
  model: demo
run:
  data_dir: {data_dir}
"""


def _make_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CONFIG.format(data_dir=str(tmp_path / "data")))
    return cfg


def test_demo_all_seeds_every_pillar(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    results = seed_all_pillars(config_path=cfg)

    # Every pillar must produce an INC-... id.
    assert set(results.keys()) >= {"replay", "ui_test", "api_test", "error_monitor", "pr_review"}
    for kind, pid in results.items():
        assert pid.startswith("INC-"), f"{kind} returned {pid!r}"

    # Confirm they're queryable as qa_incidents.
    store = Storage(tmp_path / "data" / "retrace.db")
    rows = store.list_qa_incidents(limit=50)
    by_source: dict[str, int] = {}
    for r in rows:
        kind = str(r["primary_source_kind"] or "")
        by_source[kind] = by_source.get(kind, 0) + 1
    # We expect at least one of each pillar source kind.
    assert by_source.get("replay", 0) >= 1
    assert by_source.get("ui_test", 0) >= 1
    assert by_source.get("api_test", 0) >= 1
    assert by_source.get("error_monitor", 0) >= 1


def test_demo_all_cli_runs(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(demo_group, ["all", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    # Each pillar prints its INC- id.
    for label in ("replay", "ui_test", "api_test", "error_monitor", "pr_review"):
        assert label in result.output


def test_demo_all_is_idempotent(tmp_path: Path) -> None:
    """Running `demo all` twice must not multiply incidents (each pillar
    is fingerprint-keyed)."""
    cfg = _make_config(tmp_path)
    seed_all_pillars(config_path=cfg)
    seed_all_pillars(config_path=cfg)

    store = Storage(tmp_path / "data" / "retrace.db")
    rows = store.list_qa_incidents(limit=50)
    # 5 deterministic fingerprints → 5 rows even on a second seed.
    assert len(rows) == 5
