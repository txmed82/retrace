from pathlib import Path

from retrace.commands.mcp import _handle_tool_call


_CONFIG_YAML = """posthog:
  host: https://us.i.posthog.com
  project_id: "42"
llm:
  provider: openai_compatible
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


def test_mcp_create_and_list_tester_specs(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CONFIG_YAML)
    monkeypatch.chdir(tmp_path)

    created = _handle_tool_call(
        "retrace.create_tester_spec",
        {
            "config": str(cfg),
            "name": "Smoke",
            "mode": "describe",
            "prompt": "Check homepage",
        },
    )
    assert created["ok"] is True
    assert created["spec"]["name"] == "Smoke"

    listed = _handle_tool_call("retrace.list_tester_specs", {"config": str(cfg)})
    assert listed["count"] == 1
    assert listed["specs"][0]["name"] == "Smoke"
