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


_CONFIG_WITH_SINKS = """posthog:
  host: https://us.i.posthog.com
  project_id: "42"
llm:
  base_url: http://localhost:8080/v1
  model: llama
run:
  output_dir: ./reports
  data_dir: ./data
linear:
  api_key: lin_api_test
github_sink:
  api_key: ghp_test
notifications:
  webhook_url: https://hook.example/x
  slack_webhook_url: https://hooks.slack.com/services/abc
"""


def test_doctor_validates_linear_github_and_webhook_when_configured(
    tmp_path: Path, httpx_mock: HTTPXMock, monkeypatch
):
    (tmp_path / "config.yaml").write_text(_CONFIG_WITH_SINKS)
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
    httpx_mock.add_response(
        method="POST",
        url="https://api.linear.app/graphql",
        json={"data": {"viewer": {"id": "u1", "name": "Test User"}}},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://api.github.com/user",
        json={"login": "test-user"},
    )
    httpx_mock.add_response(
        method="HEAD",
        url="https://hook.example/x",
        status_code=200,
    )

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "Linear sink" in result.output
    assert "Test User" in result.output
    assert "GitHub sink" in result.output
    assert "test-user" in result.output
    assert "Notifications: webhook" in result.output
    assert "Notifications: slack" in result.output
    assert "Playwright runtime" in result.output


def test_doctor_warns_when_playwright_missing_and_no_browser_specs(
    tmp_path: Path, httpx_mock: HTTPXMock, monkeypatch
):
    """No browser specs configured + playwright missing → WARN, exit 0."""
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

    # Force the import to fail regardless of whether playwright is installed.
    import importlib
    import sys as _sys

    real_import_module = importlib.import_module

    def fake_import(name: str, *args, **kwargs):
        if name == "playwright.sync_api":
            raise ImportError("simulated missing extra")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    # Clear any cached import.
    _sys.modules.pop("playwright", None)
    _sys.modules.pop("playwright.sync_api", None)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "[WARN] Playwright runtime" in result.output
    assert "pip install retrace[browser]" in result.output


def test_doctor_fails_when_playwright_missing_and_explore_spec_exists(
    tmp_path: Path, httpx_mock: HTTPXMock, monkeypatch
):
    """A configured explore spec turns the Playwright miss into a hard FAIL."""
    (tmp_path / "config.yaml").write_text(_CONFIG_YAML)
    (tmp_path / ".env").write_text("RETRACE_POSTHOG_API_KEY=phx_test\n")

    # Drop a spec into data/ui-tests/specs that needs the browser runtime.
    from retrace.tester import create_spec, specs_dir_for_data_dir

    specs_dir = specs_dir_for_data_dir(tmp_path / "data")
    create_spec(
        specs_dir=specs_dir,
        name="needs-browser",
        prompt="explore the app",
        app_url="http://example.com",
        start_command="",
        harness_command="",
        execution_engine="explore",
        exploratory_goals=["Sign up"],
    )

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

    import importlib
    import sys as _sys

    real_import_module = importlib.import_module

    def fake_import(name: str, *args, **kwargs):
        if name == "playwright.sync_api":
            raise ImportError("simulated missing extra")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    _sys.modules.pop("playwright", None)
    _sys.modules.pop("playwright.sync_api", None)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code != 0, result.output
    assert "[FAIL] Playwright runtime" in result.output
    assert "required for at least one configured spec" in result.output
