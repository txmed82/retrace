import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from retrace.tester import (
    create_spec,
    load_spec,
    run_spec,
    runs_dir_for_data_dir,
    specs_dir_for_data_dir,
)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"<html><body>Welcome to Retrace</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
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

    run_dir = Path(result.run_dir)
    run_json = json.loads((run_dir / "run.json").read_text())

    assert result.ok is True
    assert run_json["execution_engine"] == "native"
    assert len(run_json["assertion_results"]) == 2
    assert all(item["ok"] for item in run_json["assertion_results"])
    assert any(item["artifact_type"] == "assertion_results" for item in result.artifacts)
    assert (run_dir / "artifacts" / "native-summary.json").exists()
    assert (run_dir / "artifacts" / "assertions.json").exists()
