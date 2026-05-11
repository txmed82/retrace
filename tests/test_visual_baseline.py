"""Visual baseline accept/compare lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from retrace.commands.tester import tester_group
from retrace.visual_baseline import (
    accept_baseline,
    baseline_dir_for_spec,
    compare_run_to_baseline,
    list_baselines,
)


_PNG_RED = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xcf"
    b"\xc0\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)

_PNG_BLUE = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x00"
    b"\xff\x00\x00\x00\x05\x00\x01\xab\xb1\x8f.\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _stage_run(run_dir: Path, screenshots: dict[str, bytes]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    for name, data in screenshots.items():
        (run_dir / name).write_bytes(data)


def test_accept_then_compare_unchanged(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    run = tmp_path / "runs" / "r1"
    _stage_run(run, {"step1.png": _PNG_RED, "step2.png": _PNG_RED})

    accept_baseline(data_dir=data_dir, spec_id="spec-a", run_dir=run)
    result = compare_run_to_baseline(data_dir=data_dir, spec_id="spec-a", run_dir=run)
    assert result.compared == 2
    assert sorted(result.unchanged) == [str(run / "step1.png"), str(run / "step2.png")]
    assert result.new == []
    assert result.diffs == []


def test_compare_emits_diff_for_changed_screenshot(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    baseline_run = tmp_path / "runs" / "baseline"
    new_run = tmp_path / "runs" / "new"
    _stage_run(baseline_run, {"step1.png": _PNG_RED})
    _stage_run(new_run, {"step1.png": _PNG_BLUE})

    accept_baseline(data_dir=data_dir, spec_id="spec-a", run_dir=baseline_run)
    result = compare_run_to_baseline(data_dir=data_dir, spec_id="spec-a", run_dir=new_run)

    assert result.compared == 1
    assert len(result.diffs) == 1
    diff_path = Path(result.diffs[0])
    # The diff artifact lives next to the original screenshot in the run dir.
    assert diff_path.parent == new_run
    assert diff_path.name == "step1-diff.png"
    assert diff_path.exists()
    # auto_repro._scan_run_dir_signals already treats `*-diff*.png` as a
    # confirmed-failure signal — make sure we're producing that exact
    # shape so the classifier picks it up.
    assert "diff" in diff_path.name.lower()


def test_compare_flags_new_screenshots_with_no_baseline(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    new_run = tmp_path / "runs" / "new"
    _stage_run(new_run, {"first.png": _PNG_RED})

    result = compare_run_to_baseline(data_dir=data_dir, spec_id="spec-fresh", run_dir=new_run)
    assert result.compared == 1
    assert len(result.new) == 1
    assert result.diffs == []
    assert result.unchanged == []


def test_invalid_spec_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        baseline_dir_for_spec(tmp_path, "../escape")
    with pytest.raises(ValueError):
        baseline_dir_for_spec(tmp_path, "")


def test_list_baselines_returns_per_spec_counts(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    run = tmp_path / "runs" / "r1"
    _stage_run(run, {"a.png": _PNG_RED, "b.png": _PNG_BLUE})

    accept_baseline(data_dir=data_dir, spec_id="spec-a", run_dir=run)
    accept_baseline(data_dir=data_dir, spec_id="spec-b", run_dir=run)

    summary = list_baselines(data_dir)
    by_spec = {item["spec_id"]: item for item in summary}
    assert by_spec["spec-a"]["image_count"] == 2
    assert sorted(by_spec["spec-a"]["images"]) == ["a.png", "b.png"]


def test_cli_lifecycle_smoke(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""posthog:
  host: https://us.i.posthog.com
  project_id: demo
llm:
  provider: openai_compatible
  base_url: http://127.0.0.1:8080/v1
  model: x
run:
  data_dir: {tmp_path / "data"}
"""
    )
    run = tmp_path / "runs" / "r1"
    _stage_run(run, {"home.png": _PNG_RED})

    runner = CliRunner()

    # accept
    r = runner.invoke(
        tester_group,
        ["baseline", "accept", "spec-x", "--config", str(cfg), "--run-dir", str(run)],
    )
    assert r.exit_code == 0, r.output
    accepted = json.loads(r.output)
    assert accepted["spec_id"] == "spec-x"
    assert accepted["accepted"] and accepted["accepted"][0].endswith("home.png")

    # list shows the new baseline
    r = runner.invoke(tester_group, ["baseline", "list", "--config", str(cfg)])
    assert r.exit_code == 0, r.output
    assert "spec-x" in r.output

    # compare against an unchanged run -> no diffs
    r = runner.invoke(
        tester_group,
        ["baseline", "compare", "spec-x", "--config", str(cfg), "--run-dir", str(run)],
    )
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["diffs"] == []

    # compare against a different image -> exactly one diff
    new_run = tmp_path / "runs" / "r2"
    _stage_run(new_run, {"home.png": _PNG_BLUE})
    r = runner.invoke(
        tester_group,
        ["baseline", "compare", "spec-x", "--config", str(cfg), "--run-dir", str(new_run)],
    )
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert len(payload["diffs"]) == 1
