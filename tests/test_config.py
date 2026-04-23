from pathlib import Path
import textwrap

from retrace.config import RetraceConfig, load_config


def test_load_config_merges_yaml_and_env(tmp_path: Path, monkeypatch):
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        textwrap.dedent(
            """
            posthog:
              host: https://eu.i.posthog.com
              project_id: "42"
            llm:
              base_url: http://localhost:8080/v1
              model: llama-3.1-8b-instruct
            run:
              lookback_hours: 3
              max_sessions_per_run: 25
              output_dir: ./reports
              data_dir: ./data
            detectors:
              console_error: true
              network_5xx: true
              rage_click: false
            """
        )
    )
    monkeypatch.setenv("RETRACE_POSTHOG_API_KEY", "phx_test")
    monkeypatch.setenv("RETRACE_LLM_API_KEY", "")

    cfg = load_config(config_yaml)

    assert isinstance(cfg, RetraceConfig)
    assert cfg.posthog.host == "https://eu.i.posthog.com"
    assert cfg.posthog.project_id == "42"
    assert cfg.posthog.api_key == "phx_test"
    assert cfg.llm.base_url == "http://localhost:8080/v1"
    assert cfg.llm.provider == "openai_compatible"
    assert cfg.llm.api_key is None
    assert cfg.run.lookback_hours == 3
    assert cfg.detectors.rage_click is False


def test_load_config_keeps_yaml_keys_when_env_unset(tmp_path, monkeypatch):
    import textwrap
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        textwrap.dedent(
            """
            posthog:
              host: https://us.i.posthog.com
              project_id: "1"
              api_key: phx_from_yaml
            llm:
              base_url: http://localhost:8080/v1
              model: m
              api_key: sk_from_yaml
            """
        )
    )
    monkeypatch.delenv("RETRACE_POSTHOG_API_KEY", raising=False)
    monkeypatch.delenv("RETRACE_LLM_API_KEY", raising=False)

    from retrace.config import load_config
    cfg = load_config(config_yaml)

    assert cfg.posthog.api_key == "phx_from_yaml"
    assert cfg.llm.api_key == "sk_from_yaml"


def test_load_config_supports_provider_specific_llm_env(tmp_path, monkeypatch):
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        textwrap.dedent(
            """
            posthog:
              host: https://us.i.posthog.com
              project_id: "1"
            llm:
              provider: anthropic
              base_url: https://api.anthropic.com/v1
              model: claude-3-5-sonnet-latest
            """
        )
    )
    monkeypatch.delenv("RETRACE_LLM_API_KEY", raising=False)
    monkeypatch.setenv("RETRACE_ANTHROPIC_API_KEY", "sk-ant-test")

    cfg = load_config(config_yaml)

    assert cfg.llm.provider == "anthropic"
    assert cfg.llm.api_key == "sk-ant-test"


def test_load_config_normalizes_provider_before_env_lookup(tmp_path, monkeypatch):
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        textwrap.dedent(
            """
            posthog:
              host: https://us.i.posthog.com
              project_id: "1"
            llm:
              provider: Anthropic
              base_url: https://api.anthropic.com/v1
              model: claude-3-5-sonnet-latest
            """
        )
    )
    monkeypatch.delenv("RETRACE_LLM_API_KEY", raising=False)
    monkeypatch.setenv("RETRACE_ANTHROPIC_API_KEY", "sk-ant-normalized")

    cfg = load_config(config_yaml)

    assert cfg.llm.provider == "anthropic"
    assert cfg.llm.api_key == "sk-ant-normalized"
