from pathlib import Path

from retrace.commands.mcp import _handle_tool_call
from retrace.storage import Storage


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


def test_mcp_lists_and_processes_replay_sessions(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CONFIG_YAML)
    monkeypatch.chdir(tmp_path)
    store = Storage(tmp_path / "data" / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Web")
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-mcp",
        sequence=0,
        events=[
            {
                "type": 4,
                "timestamp": 0,
                "data": {"href": "https://example.com"},
            },
            {
                "type": 6,
                "timestamp": 100,
                "data": {
                    "plugin": "retrace/console@1",
                    "payload": {"level": "error", "payload": ["Error: mcp"]},
                },
            },
        ],
        flush_type="final",
    )

    sessions = _handle_tool_call(
        "retrace.list_replay_sessions",
        {"config": str(cfg)},
    )
    assert sessions["count"] == 1
    assert sessions["sessions"][0]["stable_id"] == "sess-mcp"

    processed = _handle_tool_call(
        "retrace.process_queued_replays",
        {"config": str(cfg), "limit": 5},
    )
    assert processed["jobs_processed"] == 1

    issues = _handle_tool_call(
        "retrace.list_replay_issues",
        {"config": str(cfg)},
    )
    assert issues["count"] == 1
    assert issues["issues"][0]["title"] == "Error: mcp on replay"
