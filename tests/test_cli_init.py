from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner
from pytest_httpx import HTTPXMock

from retrace.cli import main


_ANSWERS = {
    "ph_host": "https://us.i.posthog.com",
    "ph_project_id": "42",
    "ph_api_key": "phx_test",
    "llm_kind": "Local (OpenAI-compatible: llama.cpp / ollama / LM Studio)",
    "llm_base_url": "http://localhost:8080/v1",
    "llm_model": "llama-3.1-8b-instruct",
    "llm_api_key": "",
    "lookback_hours": "6",
    "max_sessions_per_run": "50",
    "output_dir": "./reports",
    "data_dir": "./data",
    "run_now": False,
}


_PROMPT_KEYWORDS = [
    ("PostHog host", "ph_host"),
    ("PostHog project ID", "ph_project_id"),
    ("PostHog personal API key", "ph_api_key"),
    ("LLM backend", "llm_kind"),
    ("LLM base URL", "llm_base_url"),
    ("LLM model", "llm_model"),
    ("LLM API key", "llm_api_key"),
    ("Lookback hours", "lookback_hours"),
    ("Max sessions per run", "max_sessions_per_run"),
    ("Report output directory", "output_dir"),
    ("Data directory", "data_dir"),
    ("Run `retrace run` now", "run_now"),
]


def _match_key(message: str) -> str:
    for needle, key in _PROMPT_KEYWORDS:
        if needle.lower() in message.lower():
            return key
    raise KeyError(f"unexpected prompt: {message!r}")


def _fake_prompt(key: str):
    m = MagicMock()
    m.ask.return_value = _ANSWERS[key]
    return m


def test_init_writes_config_and_env_with_validated_connections(
    tmp_path: Path, httpx_mock: HTTPXMock, monkeypatch
):
    httpx_mock.add_response(
        method="GET",
        url="https://us.i.posthog.com/api/projects/42/",
        json={"name": "My Project"},
    )
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={"choices": [{"message": {"content": "ok"}}]},
    )

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    with patch("retrace.commands.init.questionary") as q:
        q.text.side_effect = lambda message, default="": _fake_prompt(_match_key(message))
        q.password.side_effect = lambda message: _fake_prompt(_match_key(message))
        q.select.side_effect = lambda message, choices: _fake_prompt(_match_key(message))
        q.confirm.side_effect = lambda message, default=False: _fake_prompt(_match_key(message))

        result = runner.invoke(main, ["init"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "config.yaml").exists()
    assert (tmp_path / ".env").exists()

    env_text = (tmp_path / ".env").read_text()
    assert "RETRACE_POSTHOG_API_KEY=phx_test" in env_text

    cfg_text = (tmp_path / "config.yaml").read_text()
    assert "project_id: \"42\"" in cfg_text or "project_id: '42'" in cfg_text
    assert "us.i.posthog.com" in cfg_text
    assert "provider: openai_compatible" in cfg_text
    assert "llama-3.1-8b-instruct" in cfg_text
