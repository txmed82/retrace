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
    persist_api_failure,
    run_api_spec,
)
from retrace.storage import Storage


class _APIHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/api/checkout/42"):
            body = (
                b'{"error":"checkout exploded for dev@example.com at '
                b'555-123-4567, 123 Main Street"}'
            )
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/private"):
            if self.headers.get("Authorization") != "Bearer test-token":
                body = b'{"error":"unauthorized"}'
                self.send_response(401)
            else:
                body = json.dumps(
                    {
                        "ok": True,
                        "user": {"id": 42, "email": "dev@example.com"},
                        "optional": None,
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

    def do_PATCH(self) -> None:
        if self.path.startswith("/api/cart/42"):
            body = json.dumps({"ok": True, "cartId": 42, "state": "updated"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


def _server_url() -> tuple[ThreadingHTTPServer, str, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _APIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}", thread


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
    server, base_url, thread = _server_url()
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
                {"id": "optional", "path": "$.optional", "exists": True},
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
            timeout_seconds=3.5,
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
        thread.join(timeout=2)

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
    server, base_url, thread = _server_url()
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
        thread.join(timeout=2)

    assert result.ok is False
    assert any(
        item["assertion_id"] == "bad" and item["ok"] is False
        for item in result.assertion_results
    )


def test_api_spec_runs_request_sequence_with_extracted_values(
    tmp_path: Path,
) -> None:
    server, base_url, thread = _server_url()
    try:
        spec = create_api_spec(
            specs_dir=api_specs_dir_for_data_dir(tmp_path),
            name="Cart sequence",
            method="GET",
            url=f"{base_url}/api/health",
            steps=[
                {
                    "id": "create-cart",
                    "method": "POST",
                    "url": f"{base_url}/api/cart",
                    "body": {"cartId": 42},
                    "expected_status": 201,
                    "extract": [{"name": "cart_id", "path": "$.received.cartId"}],
                    "json_assertions": [{"path": "$.ok", "equals": True}],
                },
                {
                    "id": "update-cart",
                    "method": "PATCH",
                    "url": f"{base_url}/api/cart/{{{{ vars.cart_id }}}}",
                    "expected_status": 200,
                    "json_assertions": [
                        {"id": "cart", "path": "$.cartId", "equals": 42},
                        {"id": "state", "path": "$.state", "equals": "updated"},
                    ],
                },
            ],
        )

        result = run_api_spec(spec=spec, runs_dir=api_runs_dir_for_data_dir(tmp_path))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.ok is True
    assert [item["assertion_id"] for item in result.assertion_results] == [
        "create-cart:expected-status",
        "create-cart:json-0",
        "update-cart:expected-status",
        "update-cart:cart",
        "update-cart:state",
    ]
    assert sum(item["artifact_type"] == "api_request" for item in result.artifacts) == 2


def test_failed_api_run_creates_failure_evidence_and_repair_task(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "server/routes").mkdir(parents=True)
    (repo / "server/routes/checkout.ts").write_text(
        "router.get('/api/checkout/:cartId', checkoutHandler);"
    )
    server, base_url, thread = _server_url()
    try:
        spec = create_api_spec(
            specs_dir=api_specs_dir_for_data_dir(tmp_path),
            name="Checkout API",
            method="GET",
            url=f"{base_url}/api/checkout/42",
            expected_status=200,
        )
        run = run_api_spec(
            spec=spec,
            runs_dir=api_runs_dir_for_data_dir(tmp_path),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert run.ok is False
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")

    persisted = persist_api_failure(
        store=store,
        spec=spec,
        result=run,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        repo_path=repo,
    )

    failure = store.get_failure(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        failure_id=persisted.failure_id,
    )
    assert failure is not None
    assert failure.source_type == "test_run"
    assert failure.source_external_id.startswith("api:")
    assert failure.linked_repair_task_id == persisted.repair_task_id
    evidence = store.list_failure_evidence(failure_id=persisted.failure_id)
    assert {item.evidence_type for item in evidence} >= {
        "api_request",
        "api_response",
        "test_transcript",
    }
    evidence_text = json.dumps([item.payload for item in evidence])
    assert "dev@example.com" not in evidence_text
    assert "555-123-4567" not in evidence_text
    assert "123 Main Street" not in evidence_text
    repair = store.get_repair_task(persisted.repair_task_id)
    assert repair is not None
    assert repair.likely_files == ["server/routes/checkout.ts"]
    assert repair.validation_commands == [f"retrace tester api-run {spec.spec_id}"]
    prompt = Path(persisted.prompt_path).read_text()
    assert f"URL: `{base_url}/api/checkout/42`" in prompt
    assert "Expected status: `200`" in prompt
    assert "Actual status: `500`" in prompt
    assert "dev@example.com" not in prompt
