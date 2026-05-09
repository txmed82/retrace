import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from retrace.api_testing import (
    api_runs_dir_for_data_dir,
    api_specs_dir_for_data_dir,
    create_api_spec,
    list_api_specs,
    load_api_spec,
    run_api_spec,
)


class _APIHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/api/private"):
            if self.headers.get("Authorization") != "Bearer test-token":
                body = b'{"error":"unauthorized"}'
                self.send_response(401)
            else:
                body = json.dumps(
                    {
                        "ok": True,
                        "user": {"id": 42, "email": "dev@example.com"},
                        "token": "server-secret",
                    }
                ).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "session=secret")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        payload = json.loads(self.rfile.read(length).decode() or "{}")
        body = json.dumps({"received": payload, "ok": True}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def _server_url() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _APIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def test_api_specs_can_be_saved_listed_and_loaded(tmp_path: Path) -> None:
    spec = create_api_spec(
        specs_dir=api_specs_dir_for_data_dir(tmp_path),
        name="Health check",
        method="GET",
        url="http://example.test/api/health",
        expected_status=200,
        json_assertions=[{"path": "$.ok", "equals": True}],
    )

    loaded = load_api_spec(api_specs_dir_for_data_dir(tmp_path), spec.spec_id)
    listed = list_api_specs(api_specs_dir_for_data_dir(tmp_path))

    assert loaded.spec_id == spec.spec_id
    assert listed[0].name == "Health check"


def test_api_spec_rejects_sensitive_static_headers(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sensitive static headers"):
        create_api_spec(
            specs_dir=api_specs_dir_for_data_dir(tmp_path),
            name="Leaky API",
            method="GET",
            url="http://example.test/api/health",
            headers={"Authorization": "Bearer secret"},
        )


def test_api_spec_runs_with_auth_assertions_and_redacted_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    server, base_url = _server_url()
    monkeypatch.setenv(
        "RETRACE_API_HEADERS",
        json.dumps(
            {"Authorization": "Bearer test-token", "X-Api-Key": "local-secret"}
        ),
    )
    try:
        spec = create_api_spec(
            specs_dir=api_specs_dir_for_data_dir(tmp_path),
            name="Private API",
            method="GET",
            url=f"{base_url}/api/private",
            headers={"X-Trace": "local"},
            auth={"type": "headers", "headers_env": "RETRACE_API_HEADERS"},
            expected_status=200,
            json_assertions=[
                {"id": "ok", "path": "$.ok", "equals": True},
                {"id": "email", "path": "$.user.email", "contains": "example.com"},
            ],
            schema_assertions=[
                {
                    "type": "object",
                    "required": ["ok", "user"],
                    "properties": {
                        "ok": {"type": "boolean"},
                        "user": {
                            "type": "object",
                            "required": ["id"],
                            "properties": {"id": {"type": "integer"}},
                        },
                    },
                }
            ],
            latency_ms=5000,
            setup_steps=[
                {
                    "id": "setup",
                    "action": "script",
                    "set": {"started": "True"},
                    "assert": ["vars.started == True"],
                }
            ],
            teardown_steps=[
                {
                    "id": "teardown",
                    "action": "script",
                    "assert": ["response.status_code == 200"],
                }
            ],
        )

        result = run_api_spec(
            spec=spec,
            runs_dir=api_runs_dir_for_data_dir(tmp_path),
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.ok is True
    assert result.status_code == 200
    assert any(item["artifact_type"] == "api_request" for item in result.artifacts)
    assert any(item["artifact_type"] == "api_response" for item in result.artifacts)
    request_artifact = next(
        item for item in result.artifacts if item["artifact_type"] == "api_request"
    )
    response_artifact = next(
        item for item in result.artifacts if item["artifact_type"] == "api_response"
    )
    request_payload = json.loads(Path(request_artifact["path"]).read_text())
    response_payload = json.loads(Path(response_artifact["path"]).read_text())
    assert request_payload["headers"]["Authorization"] == "[redacted]"
    assert request_payload["headers"]["X-Api-Key"] == "[redacted]"
    response_headers = {k.lower(): v for k, v in response_payload["headers"].items()}
    assert response_headers["set-cookie"] == "[redacted]"
    assert response_payload["body"]["token"] == "[redacted]"
    spec_text = (api_specs_dir_for_data_dir(tmp_path) / f"{spec.spec_id}.json").read_text()
    assert "test-token" not in spec_text
    assert "local-secret" not in spec_text


def test_api_spec_reports_failed_json_assertion(tmp_path: Path) -> None:
    server, base_url = _server_url()
    try:
        spec = create_api_spec(
            specs_dir=api_specs_dir_for_data_dir(tmp_path),
            name="Failing API",
            method="GET",
            url=f"{base_url}/api/health",
            expected_status=200,
            json_assertions=[{"id": "bad", "path": "$.ok", "equals": False}],
        )

        result = run_api_spec(
            spec=spec,
            runs_dir=api_runs_dir_for_data_dir(tmp_path),
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.ok is False
    assert any(
        item["assertion_id"] == "bad" and item["ok"] is False
        for item in result.assertion_results
    )
