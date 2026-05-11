"""Tests for the first-party API test runner."""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest

from retrace.api_tester import (
    APITestSpec,
    create_spec,
    list_specs,
    load_run_summaries,
    load_spec,
    run_spec,
    run_spec_and_record,
    validate_spec,
)
from retrace.qa_incidents import Incident
from retrace.storage import Storage


# ---------------------------------------------------------------------------
# httpx mock transport — never touches the network
# ---------------------------------------------------------------------------


class _Stub(httpx.MockTransport):
    """Programmable httpx transport. Calls registered handler by URL path."""

    def __init__(self, handler):
        super().__init__(handler)


def _client_for(handler):
    return httpx.Client(transport=_Stub(handler), follow_redirects=True)


# ---------------------------------------------------------------------------
# Spec lifecycle
# ---------------------------------------------------------------------------


def test_create_save_load_roundtrip(tmp_path: Path):
    specs_dir = tmp_path / "specs"
    spec = create_spec(
        specs_dir=specs_dir,
        name="health check",
        method="GET",
        url="https://api.example.test/health",
        assertions=[
            {"assertion_type": "status_equals", "value": 200},
            {"assertion_type": "json_path_equals", "target": "ok", "value": True},
        ],
    )
    on_disk = load_spec(specs_dir, spec.spec_id)
    assert on_disk.url == "https://api.example.test/health"
    assert on_disk.method == "GET"
    assert len(on_disk.assertions) == 2
    assert any(s.spec_id == spec.spec_id for s in list_specs(specs_dir))


def test_validate_spec_rejects_bad_inputs(tmp_path: Path):
    base = APITestSpec(
        schema_version=1,
        spec_id="x",
        name="x",
        method="GET",
        url="https://example.test",
    )
    validate_spec(base)

    bad_method = APITestSpec(
        schema_version=1,
        spec_id="y",
        name="y",
        method="HACK",
        url="https://example.test",
    )
    with pytest.raises(ValueError, match="method"):
        validate_spec(bad_method)

    body_and_json = APITestSpec(
        schema_version=1,
        spec_id="z",
        name="z",
        method="POST",
        url="https://example.test",
        body='{"hi":1}',
        json_body={"hi": 1},
    )
    with pytest.raises(ValueError, match="body or json_body"):
        validate_spec(body_and_json)


# ---------------------------------------------------------------------------
# Run execution — pass paths
# ---------------------------------------------------------------------------


def test_run_spec_passes_when_assertions_match(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"ok": True, "v": 1})

    spec = create_spec(
        specs_dir=tmp_path / "specs",
        name="ok",
        method="GET",
        url="https://api.example.test/x",
        assertions=[
            {"assertion_type": "status_equals", "value": 200},
            {"assertion_type": "json_path_equals", "target": "ok", "value": True},
            {"assertion_type": "response_time_ms_under", "value": 60000},
        ],
    )
    client = _client_for(handler)
    try:
        result = run_spec(spec=spec, runs_dir=tmp_path / "runs", client=client)
    finally:
        client.close()
    assert result.status == "pass"
    assert result.response_status == 200
    assert all(a["ok"] for a in result.assertion_results)


def test_run_spec_uses_implicit_2xx_check_when_no_assertions(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    spec = create_spec(
        specs_dir=tmp_path / "specs",
        name="no-assertions",
        method="DELETE",
        url="https://api.example.test/thing/1",
    )
    client = _client_for(handler)
    try:
        result = run_spec(spec=spec, runs_dir=tmp_path / "runs", client=client)
    finally:
        client.close()
    assert result.status == "pass"
    assert len(result.assertion_results) == 1


# ---------------------------------------------------------------------------
# Run execution — failure paths file an incident
# ---------------------------------------------------------------------------


def test_run_spec_and_record_files_incident_on_failure(tmp_path: Path):
    db = tmp_path / "retrace.db"
    store = Storage(db)
    store.init_schema()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"error": "internal", "token": "secret-bearer-eyJABCDEF0123456789012345"},
        )

    spec = create_spec(
        specs_dir=tmp_path / "specs",
        name="login should be 200",
        method="POST",
        url="https://api.example.test/login",
        json_body={"email": "user@example.com", "password": "hunter2"},
        assertions=[{"assertion_type": "status_equals", "value": 200}],
    )

    # We can't easily inject the client into run_spec_and_record, so monkey-
    # patch httpx.Client to use the mock transport for the duration of the call.
    import httpx as _httpx
    original_client = _httpx.Client

    def _patched_client(*args, **kwargs):
        return original_client(*args, transport=_Stub(handler), **kwargs)

    _httpx.Client = _patched_client  # type: ignore[assignment]
    try:
        result = run_spec_and_record(
            spec=spec,
            runs_dir=tmp_path / "runs",
            store=store,
        )
    finally:
        _httpx.Client = original_client  # type: ignore[assignment]

    assert result.status == "fail"
    assert result.incident_id.startswith("INC-")
    # The stored incident exists and carries the API source kind.
    row = store.get_qa_incident(result.incident_id)
    assert row is not None
    inc = Incident.from_row(row)
    assert inc.primary_source_kind == "api_test"
    assert inc.evidence.api_test_run_ids == [result.run_id]

    # Both the captured response body in the result and the persisted
    # actual_outcome have been redacted (no raw bearer token survives).
    assert "secret-bearer-eyJABCDEF0123456789012345" not in result.response_body
    assert "secret-bearer-eyJABCDEF0123456789012345" not in inc.actual_outcome


def test_run_summaries_include_recent_runs(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    spec = create_spec(
        specs_dir=tmp_path / "specs",
        name="ping",
        method="GET",
        url="https://api.example.test/ping",
    )
    client = _client_for(handler)
    try:
        for _ in range(3):
            run_spec(spec=spec, runs_dir=tmp_path / "runs", client=client)
    finally:
        client.close()

    summaries = load_run_summaries(tmp_path / "runs", limit=10)
    assert len(summaries) == 3
    assert all(s["status"] == "pass" for s in summaries)


# ---------------------------------------------------------------------------
# Helper-path coverage: dotted JSON paths, list indices, regex
# ---------------------------------------------------------------------------


def test_json_path_with_list_index(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"users": [{"id": 1, "email": "a@b"}, {"id": 2}]})

    spec = create_spec(
        specs_dir=tmp_path / "specs",
        name="users",
        method="GET",
        url="https://api.example.test/users",
        assertions=[
            {"assertion_type": "json_path_equals", "target": "users.0.id", "value": 1},
            {"assertion_type": "json_path_equals", "target": "users.1.id", "value": 2},
        ],
    )
    client = _client_for(handler)
    try:
        result = run_spec(spec=spec, runs_dir=tmp_path / "runs", client=client)
    finally:
        client.close()
    assert result.status == "pass"


def test_body_matches_regex(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="request id: 7c3-xy9")

    spec = create_spec(
        specs_dir=tmp_path / "specs",
        name="rid",
        method="GET",
        url="https://api.example.test/rid",
        assertions=[
            {"assertion_type": "body_matches", "value": r"request id: [0-9a-z\-]+"},
        ],
    )
    client = _client_for(handler)
    try:
        result = run_spec(spec=spec, runs_dir=tmp_path / "runs", client=client)
    finally:
        client.close()
    assert result.status == "pass"


# ---------------------------------------------------------------------------
# Smoke-test CLI surface (don't shell out — just verify wiring)
# ---------------------------------------------------------------------------


def test_cli_help_lists_api_test_subcommands():
    proc = subprocess.run(
        ["uv", "run", "retrace", "api-test", "--help"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
        timeout=60,
    )
    assert proc.returncode == 0
    assert "run" in proc.stdout
    assert "create" in proc.stdout
    assert "runs" in proc.stdout
