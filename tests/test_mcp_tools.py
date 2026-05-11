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


def test_mcp_list_and_get_qa_incidents(tmp_path: Path, monkeypatch) -> None:
    """The MCP `qa.*` tools must surface unified incidents so editor
    agents (Cursor / Claude Desktop) can drive the killer demo without
    shelling out."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CONFIG_YAML)
    monkeypatch.chdir(tmp_path)

    # Seed a qa_incident directly. The bridge tests cover the wired-in
    # paths; here we just need a row to read.
    import secrets
    from retrace.qa_incidents import (
        Incident,
        IncidentEvidence,
        ReproductionStep,
        make_fingerprint,
        make_public_id,
        utc_now_iso,
    )

    store = Storage(tmp_path / "data" / "retrace.db")
    store.init_schema()
    inc = Incident(
        id=secrets.token_hex(16),
        public_id=make_public_id(),
        project_id="local",
        environment_id="production",
        fingerprint=make_fingerprint(["mcp-test"]),
        title="Sign-in fails under load",
        summary="500 from /api/login",
        suspected_cause="connection pool exhaustion",
        severity="high",
        confidence="high",
        status="open",
        primary_source_kind="api_test",
        sources=[],
        reproduction=[ReproductionStep(0, "describe", "POST /api/login")],
        expected_outcome="200",
        actual_outcome="500",
        app_url="https://api.example/login",
        evidence=IncidentEvidence(),
        affected_count=5,
        affected_users=4,
        first_seen_ms=0,
        last_seen_ms=0,
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
    )
    store.upsert_qa_incident(inc.to_row())

    listed = _handle_tool_call(
        "retrace.list_qa_incidents",
        {"config": str(cfg)},
    )
    assert listed["count"] == 1
    summary = listed["incidents"][0]
    assert summary["public_id"] == inc.public_id
    assert summary["title"] == "Sign-in fails under load"
    assert summary["primary_source_kind"] == "api_test"
    assert summary["severity"] == "high"

    fetched = _handle_tool_call(
        "retrace.get_qa_incident",
        {"config": str(cfg), "incident_id": inc.public_id},
    )
    assert fetched["found"] is True
    assert fetched["incident"]["public_id"] == inc.public_id

    missing = _handle_tool_call(
        "retrace.get_qa_incident",
        {"config": str(cfg), "incident_id": "INC-NOPE"},
    )
    assert missing["found"] is False


def test_mcp_qa_tools_are_advertised_in_tools_list(tmp_path: Path) -> None:
    """`tools/list` must expose the new qa.* tools so MCP clients can
    discover them."""
    from retrace.commands.mcp import _tools

    names = {t["name"] for t in _tools()}
    assert "retrace.list_qa_incidents" in names
    assert "retrace.get_qa_incident" in names
    assert "retrace.reproduce_qa_incident" in names
    assert "retrace.fix_qa_incident" in names
