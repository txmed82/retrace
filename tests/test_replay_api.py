from __future__ import annotations

from contextlib import closing, contextmanager
import gzip
import json
from http.server import ThreadingHTTPServer
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread

from click.testing import CliRunner

from retrace.commands.api import _handler
from retrace.cli import main
from retrace.replay_api import (
    MAX_REPLAY_BODY_BYTES,
    ReplayIngestError,
    decode_replay_body,
    ingest_replay_request,
)
from retrace.sdk_keys import (
    authenticate_service_token,
    create_sdk_key,
    create_service_token,
)
from retrace.storage import Storage, WorkspaceIds


@contextmanager
def _running_replay_api_server(store: Storage):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(store))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _store(tmp_path: Path) -> tuple[Storage, str, WorkspaceIds]:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(
        org_name="Acme",
        project_name="Web",
        environment_name="production",
    )
    created = create_sdk_key(
        store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        name="Browser SDK",
    )
    return store, created.key, workspace


def test_replay_ingest_accepts_and_dedupes_batches(tmp_path: Path) -> None:
    store, key, workspace = _store(tmp_path)
    body = json.dumps(
        {
            "sessionId": "sess-1",
            "sequence": 0,
            "flushType": "final",
            "distinctId": "user-1",
            "metadata": {"route": "/signup"},
            "events": [{"type": 4, "data": {"href": "https://example.com/signup"}}],
        }
    ).encode()

    first = ingest_replay_request(
        store=store,
        headers={"x-retrace-key": key},
        body=body,
    )
    second = ingest_replay_request(
        store=store,
        headers={"x-retrace-key": key},
        body=body,
    )

    assert first["accepted"] is True
    assert first["duplicate"] is False
    assert second["accepted"] is False
    assert second["duplicate"] is True
    session = store.get_replay_session(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-1",
    )
    assert session is not None
    assert session["event_count"] == 1
    assert session["status"] == "completed"
    assert (
        len(
            store.list_replay_batches(
                project_id=workspace.project_id,
                environment_id=workspace.environment_id,
                session_id="sess-1",
            )
        )
        == 1
    )
    jobs = store.list_processing_jobs(kind="replay.finalize")
    assert len(jobs) == 1
    assert jobs[0]["subject_id"] == session["id"]


def test_replay_ingest_accepts_gzip_payload(tmp_path: Path) -> None:
    store, key, _workspace = _store(tmp_path)
    body = gzip.compress(
        json.dumps(
            {
                "sessionId": "sess-gzip",
                "sequence": 1,
                "events": [{"type": 2, "data": {"node": {"id": 1}}}],
            }
        ).encode()
    )

    result = ingest_replay_request(
        store=store,
        headers={"authorization": f"Bearer {key}", "content-encoding": "gzip"},
        body=body,
    )

    assert result["accepted"] is True
    assert result["event_count"] == 1


def test_metrics_endpoint_returns_local_observability(tmp_path: Path) -> None:
    store, key, workspace = _store(tmp_path)
    service = create_service_token(
        store,
        project_id=workspace.project_id,
        name="Admin",
        scopes=["admin"],
    )
    ingest_replay_request(
        store=store,
        headers={"x-retrace-key": key},
        body=json.dumps(
            {
                "sessionId": "sess-metrics",
                "sequence": 0,
                "flushType": "final",
                "events": [{"type": 4, "data": {"href": "https://example.com"}}],
            }
        ).encode(),
    )

    with _running_replay_api_server(store) as server:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/healthz")
        health = conn.getresponse()
        health.read()
        assert health.status == 200
        conn.request(
            "GET",
            "/api/metrics",
            headers={"Authorization": f"Bearer {service.token}"},
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 200
    assert response.getheader("X-Retrace-Trace-Id")
    assert payload["api"]["replay_sessions"] == 1
    assert payload["api"]["replay_batches"] == 1
    assert payload["replay_processing"]["jobs"]["replay.finalize"]["queued"] == 1
    assert payload["replay_processing"]["queue_depth"] == 1
    assert payload["runtime"]["api_requests"] >= 1
    assert payload["runtime"]["api_latency_ms"]["max"] >= 0


def test_replay_ingest_rejects_gzip_bomb() -> None:
    oversized = gzip.compress(
        json.dumps(
            {
                "sessionId": "bomb",
                "sequence": 0,
                "events": [{"x": "x" * (MAX_REPLAY_BODY_BYTES + 1)}],
            }
        ).encode()
    )

    try:
        decode_replay_body(oversized, content_encoding="gzip")
    except ReplayIngestError as exc:
        assert exc.status == 413
        assert exc.code == "body_too_large"
    else:
        raise AssertionError("expected ReplayIngestError")


def test_replay_ingest_accepts_query_param_key_for_beacon_fallback(
    tmp_path: Path,
) -> None:
    store, key, workspace = _store(tmp_path)
    body = json.dumps(
        {
            "sessionId": "sess-query",
            "sequence": 0,
            "events": [{"type": 4, "data": {"href": "https://example.com"}}],
        }
    ).encode()

    result = ingest_replay_request(
        store=store,
        headers={},
        query={"key": key},
        body=body,
    )

    assert result["accepted"] is True
    assert (
        store.get_replay_session(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            session_id="sess-query",
        )
        is not None
    )


def test_replay_ingest_merges_session_metadata(tmp_path: Path) -> None:
    store, key, workspace = _store(tmp_path)
    first = json.dumps(
        {
            "sessionId": "sess-meta",
            "sequence": 0,
            "metadata": {"route": "/signup", "plan": "pro"},
            "events": [{"type": 4}],
        }
    ).encode()
    second = json.dumps(
        {
            "sessionId": "sess-meta",
            "sequence": 1,
            "metadata": {},
            "events": [{"type": 3}],
        }
    ).encode()

    ingest_replay_request(store=store, headers={"x-retrace-key": key}, body=first)
    ingest_replay_request(store=store, headers={"x-retrace-key": key}, body=second)

    session = store.get_replay_session(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-meta",
    )
    assert session is not None
    assert json.loads(session["metadata_json"]) == {"route": "/signup", "plan": "pro"}


def test_replay_playback_reads_filesystem_blob_events_in_sequence(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db", replay_blob_dir=tmp_path / "replay-blobs")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Web")

    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-blob",
        sequence=1,
        events=[{"type": 3, "timestamp": 20, "data": {"source": 2, "type": 2}}],
        flush_type="final",
    )
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-blob",
        sequence=0,
        events=[
            {
                "type": 4,
                "timestamp": 10,
                "data": {"href": "https://example.com/start"},
            }
        ],
        flush_type="normal",
    )

    batches = store.list_replay_batches(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-blob",
    )
    assert [b["blob_backend"] for b in batches] == [
        "local_filesystem",
        "local_filesystem",
    ]
    assert all((tmp_path / "replay-blobs" / b["blob_key"]).exists() for b in batches)

    playback = store.get_replay_playback(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-blob",
    )
    assert playback is not None
    assert [event["type"] for event in playback.events] == [4, 3]
    session = store.get_replay_session(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-blob",
    )
    assert session is not None
    assert json.loads(session["preview_json"]) == {
        "event_count": 2,
        "first_timestamp_ms": 10,
        "last_timestamp_ms": 20,
        "url": "https://example.com/start",
    }


def test_replay_duplicate_batch_does_not_overwrite_blob_events(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db", replay_blob_dir=tmp_path / "replay-blobs")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Web")

    first = store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-dup-blob",
        sequence=0,
        events=[{"type": 4, "timestamp": 10, "data": {"href": "https://first.test"}}],
        flush_type="normal",
    )
    second = store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-dup-blob",
        sequence=0,
        events=[{"type": 4, "timestamp": 20, "data": {"href": "https://second.test"}}],
        flush_type="normal",
    )

    assert first.inserted is True
    assert second.inserted is False
    playback = store.get_replay_playback(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-dup-blob",
    )
    assert playback is not None
    assert playback.events == [
        {"type": 4, "timestamp": 10, "data": {"href": "https://first.test"}}
    ]


def test_replay_lookups_are_tenant_scoped(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    first = store.ensure_workspace(
        org_name="Acme",
        project_name="Web",
        environment_name="production",
    )
    second = store.ensure_workspace(
        org_name="Beta",
        project_name="Web",
        environment_name="production",
    )

    store.insert_replay_batch(
        project_id=first.project_id,
        environment_id=first.environment_id,
        session_id="shared-session",
        sequence=0,
        events=[{"type": 4}],
        flush_type="normal",
        metadata={"tenant": "first"},
    )
    store.insert_replay_batch(
        project_id=second.project_id,
        environment_id=second.environment_id,
        session_id="shared-session",
        sequence=0,
        events=[{"type": 5}],
        flush_type="normal",
        metadata={"tenant": "second"},
    )

    first_session = store.get_replay_session(
        project_id=first.project_id,
        environment_id=first.environment_id,
        session_id="shared-session",
    )
    second_session = store.get_replay_session(
        project_id=second.project_id,
        environment_id=second.environment_id,
        session_id="shared-session",
    )
    assert first_session is not None
    assert second_session is not None
    assert json.loads(first_session["metadata_json"]) == {"tenant": "first"}
    assert json.loads(second_session["metadata_json"]) == {"tenant": "second"}
    assert len(
        store.list_replay_batches(
            project_id=first.project_id,
            environment_id=first.environment_id,
            session_id="shared-session",
        )
    ) == 1


def test_replay_ingest_rejects_invalid_key(tmp_path: Path) -> None:
    store, _key, _workspace = _store(tmp_path)

    try:
        ingest_replay_request(
            store=store,
            headers={"x-retrace-key": "bad"},
            body=b"{}",
        )
    except ReplayIngestError as exc:
        assert exc.status == 401
        assert exc.code == "unauthorized"
    else:
        raise AssertionError("expected ReplayIngestError")


def test_api_create_sdk_key_command_outputs_secret_once(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
posthog:
  host: https://us.i.posthog.com
  project_id: "1"
llm:
  provider: openai_compatible
  base_url: http://localhost:8080/v1
  model: test
run:
  data_dir: {tmp_path / "data"}
  output_dir: {tmp_path / "reports"}
"""
    )

    result = CliRunner().invoke(
        main,
        ["api", "create-sdk-key", "--config", str(cfg), "--project", "Web"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["key"].startswith("rtpk_")
    assert payload["project_id"].startswith("proj_")


def test_api_rejects_oversized_content_length_before_reading(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    with _running_replay_api_server(store) as server:
        with closing(
            HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
        ) as conn:
            conn.request(
                "POST",
                "/api/sdk/replay",
                body=b"",
                headers={"Content-Length": str(MAX_REPLAY_BODY_BYTES + 1)},
            )
            response = conn.getresponse()
            payload = json.loads(response.read())
            assert response.status == 413
            assert payload["error"] == "body_too_large"


def test_api_rejects_negative_content_length_before_reading(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    with _running_replay_api_server(store) as server:
        with closing(
            HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
        ) as conn:
            conn.request(
                "POST",
                "/api/sdk/replay",
                body=b"",
                headers={"Content-Length": "-1"},
            )
            response = conn.getresponse()
            payload = json.loads(response.read())
            assert response.status == 400
            assert payload["error"] == "invalid_content_length"


def test_api_cors_preflight_for_replay_ingest(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    with _running_replay_api_server(store) as server:
        with closing(
            HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
        ) as conn:
            conn.request(
                "OPTIONS",
                "/api/sdk/replay",
                headers={
                    "Origin": "https://app.example.com",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "x-retrace-key,content-type",
                },
            )
            response = conn.getresponse()
            response.read()
            assert response.status == 204
            assert response.getheader("Access-Control-Allow-Origin") == "*"
            assert "x-retrace-key" in response.getheader(
                "Access-Control-Allow-Headers", ""
            )


def test_project_members_and_service_tokens_round_trip(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Web")

    member_id = store.add_project_member(
        project_id=workspace.project_id,
        email="USER@EXAMPLE.COM",
        role="admin",
    )
    members = store.list_project_members(workspace.project_id)
    assert members[0]["id"] == member_id
    assert members[0]["email"] == "user@example.com"
    assert members[0]["role"] == "admin"

    token = create_service_token(
        store,
        project_id=workspace.project_id,
        name="MCP",
        scopes=["mcp:read", "issues:write"],
    )
    authed = authenticate_service_token(store, token.token)
    assert authed is not None
    assert authed.id == token.id
    assert authed.scopes == ["mcp:read", "issues:write"]
    assert store.revoke_service_token(token.id) is True
    assert authenticate_service_token(store, token.token) is None


def test_api_lists_replays_and_fetches_playback_with_service_token(
    tmp_path: Path,
) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(
        org_name="Acme",
        project_name="Web",
        environment_name="production",
    )
    token = create_service_token(
        store,
        project_id=workspace.project_id,
        name="Dashboard",
        scopes=["replay:read"],
    )
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-api",
        sequence=0,
        events=[{"type": 4, "data": {"href": "https://example.com"}}],
        flush_type="final",
        distinct_id="user-1",
        metadata={"route": "/"},
    )
    session = store.get_replay_session(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-api",
    )
    assert session is not None

    with _running_replay_api_server(store) as server:
        port = server.server_address[1]
        with closing(HTTPConnection("127.0.0.1", port, timeout=2)) as conn:
            conn.request(
                "GET",
                f"/api/replays?environment_id={workspace.environment_id}",
                headers={"Authorization": f"Bearer {token.token}"},
            )
            response = conn.getresponse()
            payload = json.loads(response.read())
            assert response.status == 200
            assert payload["sessions"][0]["stable_id"] == "sess-api"
            assert payload["sessions"][0]["metadata"] == {"route": "/"}
            assert payload["sessions"][0]["preview"] == {
                "event_count": 1,
                "url": "https://example.com",
            }

        with closing(HTTPConnection("127.0.0.1", port, timeout=2)) as conn:
            conn.request(
                "GET",
                f"/api/replays/{session['public_id']}?environment_id={workspace.environment_id}",
                headers={"Authorization": f"Bearer {token.token}"},
            )
            response = conn.getresponse()
            payload = json.loads(response.read())
            assert response.status == 200
            assert payload["session"]["stable_id"] == "sess-api"
            assert payload["events"] == [
                {"type": 4, "data": {"href": "https://example.com"}}
            ]


def test_api_lists_replay_issues_with_service_token(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Web")
    token = create_service_token(
        store,
        project_id=workspace.project_id,
        name="Dashboard",
        scopes=["issues:read"],
    )
    store.upsert_replay_issue(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        fingerprint="dead-click:/signup",
        session_ids=["sess-1", "sess-2"],
        signal_summary={"dead_click": 2},
        first_seen_ms=10,
        last_seen_ms=20,
        title="Dead clicks on signup",
        reproduction_steps=["Open signup", "Click continue"],
        severity="high",
        priority="high",
    )

    with _running_replay_api_server(store) as server:
        with closing(
            HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
        ) as conn:
            conn.request(
                "GET",
                f"/api/issues?environment_id={workspace.environment_id}",
                headers={"Authorization": f"Bearer {token.token}"},
            )
            response = conn.getresponse()
            payload = json.loads(response.read())
            assert response.status == 200
            assert payload["issues"][0]["title"] == "Dead clicks on signup"
            assert payload["issues"][0]["signal_summary"] == {"dead_click": 2}
            assert payload["issues"][0]["reproduction_steps"] == [
                "Open signup",
                "Click continue",
            ]


def test_api_replay_reads_reject_browser_sdk_key(tmp_path: Path) -> None:
    store, key, workspace = _store(tmp_path)

    with _running_replay_api_server(store) as server:
        with closing(
            HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
        ) as conn:
            conn.request(
                "GET",
                f"/api/replays?environment_id={workspace.environment_id}",
                headers={"Authorization": f"Bearer {key}"},
            )
            response = conn.getresponse()
            payload = json.loads(response.read())
            assert response.status == 401
            assert payload["error"] == "unauthorized"


def test_api_processes_queued_replays_with_service_token(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Web")
    token = create_service_token(
        store,
        project_id=workspace.project_id,
        name="Worker",
        scopes=["replay:write"],
    )
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-process",
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
                    "payload": {"level": "error", "payload": ["Error: failed"]},
                },
            },
        ],
        flush_type="final",
    )

    with _running_replay_api_server(store) as server:
        with closing(
            HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
        ) as conn:
            conn.request(
                "POST",
                "/api/replays/process",
                body=json.dumps({"limit": 5}),
                headers={
                    "Authorization": f"Bearer {token.token}",
                    "Content-Type": "application/json",
                },
            )
            response = conn.getresponse()
            payload = json.loads(response.read())
            assert response.status == 200
            assert payload["jobs_processed"] == 1
            assert payload["sessions_processed"] == 1
            assert payload["issues_created_or_updated"] == 1

    issues = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )
    assert issues[0]["title"] == "Error: failed on replay"
