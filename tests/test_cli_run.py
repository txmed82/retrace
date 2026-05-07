from pathlib import Path
import re

import httpx
from click.testing import CliRunner
from pytest_httpx import HTTPXMock

from retrace.cli import main


_CONFIG_YAML = """posthog:
  host: https://us.i.posthog.com
  project_id: "42"
llm:
  base_url: http://localhost:8080/v1
  model: llama
run:
  lookback_hours: 6
  max_sessions_per_run: 50
  output_dir: ./reports
  data_dir: ./data
detectors:
  console_error: true
cluster:
  min_size: 1
"""


def test_run_reports_network_errors_without_traceback(
    tmp_path: Path, httpx_mock: HTTPXMock, monkeypatch
):
    (tmp_path / "config.yaml").write_text(_CONFIG_YAML)
    (tmp_path / ".env").write_text("RETRACE_POSTHOG_API_KEY=phx_test\n")
    httpx_mock.add_exception(
        httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known"),
        method="GET",
        url=re.compile(
            r"https://us\.i\.posthog\.com/api/projects/42/session_recordings.*"
        ),
    )

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["run"])

    assert result.exit_code != 0
    assert "network unavailable or host could not be resolved" in result.output
    assert "Traceback" not in result.output
    assert "nodename nor servname" not in result.output
