"""`retrace quickstart` now mints a Sentry-compatible DSN alongside the
replay script tag. The DSN must:

  * embed the same SDK key the replay tag uses (one credential)
  * point at the configured API host/port
  * be a valid Sentry DSN shape (`scheme://key@host/project`)
  * round-trip back to the original public key + project
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from retrace.commands.quickstart import quickstart_command
from retrace.sentry_compat import build_sentry_dsn


def test_quickstart_emits_sentry_dsn(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    runner = CliRunner()
    result = runner.invoke(
        quickstart_command,
        [
            "--config", str(cfg),
            "--api-host", "127.0.0.1",
            "--api-port", "8788",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    assert payload["sentry_dsn"].startswith("http://rtpk_"), payload["sentry_dsn"]
    assert "@127.0.0.1:8788/" in payload["sentry_dsn"]
    # Snippet includes the DSN in a Sentry.init call.
    snippet = payload["sentry_snippet"]
    assert "Sentry.init" in snippet
    assert payload["sentry_dsn"] in snippet


def test_quickstart_sentry_dsn_uses_same_sdk_key(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    runner = CliRunner()
    result = runner.invoke(
        quickstart_command,
        ["--config", str(cfg), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    # The DSN's public key portion must be the same SDK key — one
    # credential, two ingest paths.
    sdk_key = payload["sdk_key"]
    assert sdk_key in payload["sentry_dsn"]


def test_build_sentry_dsn_shape() -> None:
    dsn = build_sentry_dsn(
        public_key="rtpk_xyz",
        base_url="http://127.0.0.1:8788",
        project_id="proj_abc",
    )
    assert dsn == "http://rtpk_xyz@127.0.0.1:8788/proj_abc"


def test_quickstart_write_snippet_includes_both(tmp_path: Path) -> None:
    """`--write-snippet` should capture BOTH the replay tag and the
    Sentry one in a single file."""
    cfg = tmp_path / "config.yaml"
    snippet_path = tmp_path / "snippet.html"
    runner = CliRunner()
    result = runner.invoke(
        quickstart_command,
        ["--config", str(cfg), "--write-snippet", str(snippet_path), "--json"],
    )
    assert result.exit_code == 0, result.output
    contents = snippet_path.read_text()
    assert "@retrace/browser" in contents  # replay
    assert "Sentry.init" in contents  # error monitor
