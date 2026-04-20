from pathlib import Path
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
  network_5xx: true
  rage_click: true
cluster:
  min_size: 1
"""


def test_doctor_reports_ok_when_connections_work(
    tmp_path: Path, httpx_mock: HTTPXMock, monkeypatch
):
    (tmp_path / "config.yaml").write_text(_CONFIG_YAML)
    (tmp_path / ".env").write_text("RETRACE_POSTHOG_API_KEY=phx_test\n")

    httpx_mock.add_response(
        method="GET",
        url="https://us.i.posthog.com/api/projects/42/",
        json={"name": "ok"},
    )
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={"choices": [{"message": {"content": "ok"}}]},
    )

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "PostHog" in result.output and "OK" in result.output
    assert "LLM" in result.output


def test_doctor_reports_failure_when_llm_unreachable(
    tmp_path: Path, httpx_mock: HTTPXMock, monkeypatch
):
    (tmp_path / "config.yaml").write_text(_CONFIG_YAML)
    (tmp_path / ".env").write_text("RETRACE_POSTHOG_API_KEY=phx_test\n")

    httpx_mock.add_response(
        method="GET",
        url="https://us.i.posthog.com/api/projects/42/",
        json={"name": "ok"},
    )
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        status_code=500,
        json={"error": "boom"},
    )

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code != 0
    assert "FAIL" in result.output or "fail" in result.output.lower()
