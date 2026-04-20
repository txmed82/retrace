import os
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
    assert cfg.llm.api_key is None
    assert cfg.run.lookback_hours == 3
    assert cfg.detectors.rage_click is False
