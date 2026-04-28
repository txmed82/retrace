import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from retrace.tester import (
    create_spec,
    load_spec,
    run_spec,
    runs_dir_for_data_dir,
    specs_dir_for_data_dir,
)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/final")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/broken":
            body = b"broken"
            self.send_response(500)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b"<html><body>Welcome to Retrace</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Set-Cookie", "session=secret-token")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def _server_url() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def test_legacy_spec_loads_with_durable_defaults(tmp_path: Path) -> None:
    specs_dir = specs_dir_for_data_dir(tmp_path)
    specs_dir.mkdir(parents=True)
    (specs_dir / "legacy.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "spec_id": "legacy",
                "name": "Legacy",
                "mode": "describe",
                "prompt": "Open the app",
                "app_url": "http://127.0.0.1:3000",
                "start_command": "",
                "harness_command": "echo {app_url_q} {prompt_q} {run_dir_q}",
                "auth_required": False,
                "auth_mode": "none",
                "auth_login_url": "",
                "auth_username": "",
                "auth_password_env": "RETRACE_TESTER_AUTH_PASSWORD",
                "auth_jwt_env": "RETRACE_TESTER_AUTH_JWT",
                "auth_headers_env": "RETRACE_TESTER_AUTH_HEADERS",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        )
    )

    spec = load_spec(specs_dir, "legacy")

    assert spec.execution_engine == "harness"
    assert spec.exact_steps == []
    assert spec.assertions == []
    assert spec.env_overrides == {}
    assert spec.browser_settings == {}
    assert spec.fixtures == {}
    assert spec.data_extraction == []


def test_native_runner_writes_run_json_and_artifacts(tmp_path: Path) -> None:
    server, app_url = _server_url()
    try:
        spec = create_spec(
            specs_dir=specs_dir_for_data_dir(tmp_path),
            name="Native smoke",
            prompt="",
            app_url=app_url,
            start_command="",
            harness_command="",
            execution_engine="native",
            exact_steps=[
                {"id": "home", "action": "get", "path": "/"},
                {"id": "status", "action": "assert_status", "status": 200},
            ],
            assertions=[
                {
                    "id": "welcome-copy",
                    "type": "text_contains",
                    "expected": "Welcome to Retrace",
                    "consensus_group": "copy",
                }
            ],
            data_extraction=[{"id": "title-copy", "regex": "Welcome to ([A-Za-z]+)"}],
        )

        result = run_spec(spec=spec, runs_dir=runs_dir_for_data_dir(tmp_path))
    finally:
        server.shutdown()
        server.server_close()

    run_dir = Path(result.run_dir)
    run_json = json.loads((run_dir / "run.json").read_text())

    assert result.ok is True
    assert run_json["execution_engine"] == "native"
    assert len(run_json["assertion_results"]) == 2
    assert all(item["ok"] for item in run_json["assertion_results"])
    assert any(item["artifact_type"] == "assertion_results" for item in result.artifacts)
    assert (run_dir / "artifacts" / "native-summary.json").exists()
    assert (run_dir / "artifacts" / "assertions.json").exists()


def test_native_runner_records_consensus_and_step_cache(tmp_path: Path) -> None:
    server, app_url = _server_url()
    try:
        spec = create_spec(
            specs_dir=specs_dir_for_data_dir(tmp_path),
            name="Consensus cache",
            prompt="",
            app_url=app_url,
            start_command="",
            harness_command="",
            execution_engine="native",
            exact_steps=[{"id": "home", "action": "get", "path": "/redirect"}],
            assertions=[
                {
                    "id": "ai-copy",
                    "type": "model_consensus",
                    "consensus_group": "copy",
                    "model_votes": [
                        {"model": "primary", "ok": True},
                        {"model": "secondary", "ok": False},
                    ],
                    "arbiter_vote": "pass",
                }
            ],
        )

        first = run_spec(spec=spec, runs_dir=runs_dir_for_data_dir(tmp_path))
        second = run_spec(spec=spec, runs_dir=runs_dir_for_data_dir(tmp_path))
    finally:
        server.shutdown()
        server.server_close()

    assert first.ok is True
    assert second.ok is True
    first_artifacts = {item["artifact_type"] for item in first.artifacts}
    second_artifacts = {item["artifact_type"] for item in second.artifacts}
    assert "assertion_consensus" in first_artifacts
    assert "step_cache_events" in first_artifacts
    assert "step_cache_events" in second_artifacts

    first_cache = json.loads(
        (Path(first.run_dir) / "artifacts" / "step-cache-events.json").read_text()
    )
    second_cache = json.loads(
        (Path(second.run_dir) / "artifacts" / "step-cache-events.json").read_text()
    )
    assert any(item["status"] == "miss_store" for item in first_cache)
    assert any(item["status"] == "hit" for item in second_cache)
    assert any("/final" in item["cached_url"] for item in second_cache)
    consensus = json.loads(
        (Path(first.run_dir) / "artifacts" / "assertions.json").read_text()
    )
    assert consensus[0]["actual"]["decision"] == "arbiter"
    assert consensus[0]["actual"]["disagreement"] is True
    assert consensus[0]["actual"]["evidence"]["available"] is True
    assert consensus[0]["actual"]["evidence"]["status_code"] == 200
    assert consensus[0]["actual"]["evidence"]["headers"]["set-cookie"] == "[redacted]"
    assert consensus[0]["actual"]["evidence"]["body_capture"] is False
    assert "body_excerpt" not in consensus[0]["actual"]["evidence"]
    assert consensus[0]["confidence"] >= 0.67


def test_native_consensus_uses_retry_votes_after_failure(tmp_path: Path) -> None:
    server, app_url = _server_url()
    try:
        spec = create_spec(
            specs_dir=specs_dir_for_data_dir(tmp_path),
            name="Consensus retry",
            prompt="",
            app_url=app_url,
            start_command="",
            harness_command="",
            execution_engine="native",
            assertions=[
                {
                    "id": "retry-copy",
                    "type": "model_consensus",
                    "model_votes": [
                        {"model": "primary", "ok": False, "reasoning": "stale"},
                        {"model": "secondary", "ok": True, "reasoning": "present"},
                    ],
                    "retry_votes": [
                        {"model": "primary", "ok": True, "reasoning": "fresh evidence"}
                    ],
                    "confidence": 0.0,
                    "evidence": {
                        "snapshot_path": "snapshot.json",
                        "screenshot_path": "shot.png",
                    },
                }
            ],
        )

        result = run_spec(spec=spec, runs_dir=runs_dir_for_data_dir(tmp_path))
    finally:
        server.shutdown()
        server.server_close()

    assert result.ok is True
    assertions = json.loads((Path(result.run_dir) / "artifacts" / "assertions.json").read_text())
    assert assertions[0]["actual"]["pass_votes"] == 2
    assert assertions[0]["actual"]["retry_count"] == 1
    assert assertions[0]["actual"]["evidence"]["snapshot_path"] == "snapshot.json"
    assert assertions[0]["confidence"] == 0.0


def test_native_consensus_handles_null_and_invalid_confidence(tmp_path: Path) -> None:
    server, app_url = _server_url()
    try:
        spec = create_spec(
            specs_dir=specs_dir_for_data_dir(tmp_path),
            name="Consensus null confidence",
            prompt="",
            app_url=app_url,
            start_command="",
            harness_command="",
            execution_engine="native",
            assertions=[
                {
                    "id": "null-confidence",
                    "type": "model_consensus",
                    "model_votes": [{"model": "primary", "ok": True}],
                    "confidence": None,
                },
                {
                    "id": "invalid-confidence",
                    "type": "text_contains",
                    "expected": "Welcome",
                    "confidence": "not-a-number",
                },
            ],
        )

        result = run_spec(spec=spec, runs_dir=runs_dir_for_data_dir(tmp_path))
    finally:
        server.shutdown()
        server.server_close()

    assert result.ok is True
    assertions = json.loads(
        (Path(result.run_dir) / "artifacts" / "assertions.json").read_text()
    )
    assert assertions[0]["confidence"] == 1.0
    assert assertions[1]["confidence"] == 1.0


def test_native_step_cache_auto_heals_stale_effective_url(tmp_path: Path) -> None:
    server, app_url = _server_url()
    try:
        spec = create_spec(
            specs_dir=specs_dir_for_data_dir(tmp_path),
            name="Stale cache",
            prompt="",
            app_url=app_url,
            start_command="",
            harness_command="",
            execution_engine="native",
            exact_steps=[{"id": "home", "action": "get", "path": "/"}],
        )

        first = run_spec(spec=spec, runs_dir=runs_dir_for_data_dir(tmp_path))
        cache_dir = tmp_path / "ui-tests" / "cache" / "native-steps"
        cache_file = next(cache_dir.glob("*.json"))
        payload = json.loads(cache_file.read_text())
        payload["effective_url"] = f"{app_url}/broken"
        cache_file.write_text(json.dumps(payload))
        second = run_spec(spec=spec, runs_dir=runs_dir_for_data_dir(tmp_path))
    finally:
        server.shutdown()
        server.server_close()

    assert first.ok is True
    assert second.ok is True
    second_cache = json.loads(
        (Path(second.run_dir) / "artifacts" / "step-cache-events.json").read_text()
    )
    assert any(item["status"] == "auto_heal" for item in second_cache)


def test_native_step_cache_bypasses_on_retry(tmp_path: Path) -> None:
    server, app_url = _server_url()
    try:
        spec = create_spec(
            specs_dir=specs_dir_for_data_dir(tmp_path),
            name="Retry bypass",
            prompt="",
            app_url=app_url,
            start_command="",
            harness_command="",
            execution_engine="native",
            exact_steps=[
                {
                    "id": "home",
                    "action": "get",
                    "path": "/redirect",
                    "expected_text": "missing on purpose",
                }
            ],
            assertions=[
                {"id": "force-retry", "type": "text_contains", "expected": "not here"}
            ],
        )

        first = run_spec(spec=spec, runs_dir=runs_dir_for_data_dir(tmp_path))
        second = run_spec(
            spec=spec,
            runs_dir=runs_dir_for_data_dir(tmp_path),
            max_retries=1,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert first.ok is False
    assert second.ok is False
    cache_events = json.loads(
        (Path(second.run_dir) / "artifacts" / "step-cache-events.json").read_text()
    )
    assert any(item["status"] == "bypass" for item in cache_events)


def test_native_step_cache_does_not_emit_cold_bypass_event(tmp_path: Path) -> None:
    server, app_url = _server_url()
    try:
        spec = create_spec(
            specs_dir=specs_dir_for_data_dir(tmp_path),
            name="Cold bypass",
            prompt="",
            app_url=app_url,
            start_command="",
            harness_command="",
            execution_engine="native",
            exact_steps=[
                {"id": "home", "action": "get", "path": "/", "cache_bypass": True}
            ],
        )

        result = run_spec(spec=spec, runs_dir=runs_dir_for_data_dir(tmp_path))
    finally:
        server.shutdown()
        server.server_close()

    assert result.ok is True
    cache_events = json.loads(
        (Path(result.run_dir) / "artifacts" / "step-cache-events.json").read_text()
    )
    assert not any(item["status"] == "bypass" for item in cache_events)


def test_native_auth_required_specs_fail_fast(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="native execution"):
        create_spec(
            specs_dir=specs_dir_for_data_dir(tmp_path),
            name="Native auth",
            prompt="",
            app_url="http://127.0.0.1:3000",
            start_command="",
            harness_command="",
            execution_engine="native",
            auth_required=True,
            auth_mode="jwt",
            exact_steps=[{"id": "home", "action": "get", "path": "/"}],
        )


def test_harness_retry_terminates_timed_out_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    procs: list[object] = []

    class FakeProc:
        def __init__(self, should_timeout: bool) -> None:
            self.should_timeout = should_timeout
            self.stopped = False
            self.terminated = False
            self.killed = False

        def wait(self, timeout: int | None = None) -> int:
            if self.should_timeout and not self.stopped:
                raise TimeoutError("hung")
            return 0

        def poll(self) -> int | None:
            return 0 if self.stopped or not self.should_timeout else None

        def terminate(self) -> None:
            self.terminated = True
            self.stopped = True

        def kill(self) -> None:
            self.killed = True
            self.stopped = True

    def fake_run_shell(*args: object, **kwargs: object) -> FakeProc:
        proc = FakeProc(should_timeout=not procs)
        procs.append(proc)
        return proc

    monkeypatch.setattr("retrace.tester._run_shell", fake_run_shell)
    spec = create_spec(
        specs_dir=specs_dir_for_data_dir(tmp_path),
        name="Retry cleanup",
        prompt="Open the app",
        app_url="http://127.0.0.1:3000",
        start_command="",
        harness_command="echo {app_url_q} {prompt_q} {run_dir_q}",
    )

    result = run_spec(
        spec=spec,
        runs_dir=runs_dir_for_data_dir(tmp_path),
        max_retries=1,
    )

    assert result.ok is True
    assert len(procs) == 2
    assert getattr(procs[0], "terminated") is True
    assert getattr(procs[0], "killed") is False
