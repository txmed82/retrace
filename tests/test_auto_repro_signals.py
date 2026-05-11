"""Artifact-aware classifier in auto_repro.

`_classify_outcome` should promote a not-otherwise-failing run to
"confirmed" when the tester dropped strong evidence into the run dir
(screenshot diff, DOM diff, captured runtime errors, captured network
failures), and should leave clean runs as "not_confirmed".
"""

from __future__ import annotations

import json
from pathlib import Path

from retrace.auto_repro import _classify_outcome


class _Run:
    """Minimal stand-in for `TesterRunResult` — only the fields the
    classifier reads."""

    def __init__(
        self,
        *,
        exit_code: int = 0,
        error: str = "",
        assertion_results=None,
        run_dir: str = "",
    ) -> None:
        self.exit_code = exit_code
        self.error = error
        self.assertion_results = assertion_results or []
        self.run_dir = run_dir
        self.run_id = "run-x"


def test_screenshot_diff_promotes_clean_run_to_confirmed(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "step3-screenshot-diff.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    confirmed, status, summary = _classify_outcome(
        _Run(exit_code=0, run_dir=str(run_dir)),
        exact_steps_count=2,
    )
    assert confirmed is True
    assert status == "confirmed"
    assert "screenshot diff" in summary


def test_dom_diff_promotes_to_confirmed(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "dom-diff.txt").write_text("<div class='error'>...</div>\n")

    confirmed, status, _ = _classify_outcome(
        _Run(exit_code=0, run_dir=str(run_dir)),
        exact_steps_count=0,
    )
    assert confirmed is True
    assert status == "confirmed"


def test_captured_errors_promote_to_confirmed(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "errors.json").write_text(json.dumps([
        {"message": "TypeError: undefined is not a function", "stack": "..."},
    ]))

    confirmed, status, summary = _classify_outcome(
        _Run(exit_code=0, run_dir=str(run_dir)),
        exact_steps_count=0,
    )
    assert confirmed is True
    assert status == "confirmed"
    assert "errors" in summary.lower()


def test_captured_network_failures_promote_to_confirmed(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "network-failures.json").write_text(
        json.dumps([{"url": "/api/login", "status": 500}])
    )

    confirmed, status, summary = _classify_outcome(
        _Run(exit_code=0, run_dir=str(run_dir)),
        exact_steps_count=0,
    )
    assert confirmed is True
    assert status == "confirmed"
    assert "network" in summary.lower()


def test_empty_network_failures_json_is_not_a_signal(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "network-failures.json").write_text("[]")

    confirmed, status, _ = _classify_outcome(
        _Run(exit_code=0, run_dir=str(run_dir)),
        exact_steps_count=0,
    )
    assert confirmed is False
    assert status == "not_confirmed"


def test_missing_run_dir_is_handled(tmp_path: Path):
    # run_dir points at a nonexistent path — must not crash.
    confirmed, status, _ = _classify_outcome(
        _Run(exit_code=0, run_dir=str(tmp_path / "does-not-exist")),
        exact_steps_count=0,
    )
    assert confirmed is False
    assert status == "not_confirmed"


def test_failed_assertion_still_wins_over_artifact_scan(tmp_path: Path):
    """A real failed assertion is the highest-confidence signal — it
    must short-circuit before we go scanning the filesystem."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "screenshot-diff.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    confirmed, status, summary = _classify_outcome(
        _Run(
            exit_code=1,
            assertion_results=[
                {"assertion_type": "text_visible", "ok": False, "message": "btn missing"}
            ],
            run_dir=str(run_dir),
        ),
        exact_steps_count=2,
    )
    assert confirmed is True
    assert status == "confirmed"
    assert "btn missing" in summary
