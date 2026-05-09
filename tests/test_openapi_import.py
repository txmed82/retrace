import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from click.testing import CliRunner

from retrace.api_testing import api_runs_dir_for_data_dir, api_specs_dir_for_data_dir, run_api_spec
from retrace.cli import main
from retrace.openapi_import import import_openapi_specs


class _OpenAPIHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/v1/users/0"):
            body = json.dumps({"id": 0, "email": "dev@example.com"}).encode()
            self.send_response(200)
        elif self.path.startswith("/v1/health"):
            body = b'{"ok":true}'
            self.send_response(200)
        else:
            body = b'{"error":"not found"}'
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def _server_url() -> tuple[ThreadingHTTPServer, str, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAPIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}", thread


def test_openapi_import_creates_runnable_contract_specs(tmp_path: Path) -> None:
    openapi_path = tmp_path / "openapi.yaml"
    openapi_path.write_text(
        """
openapi: 3.0.3
info:
  title: Demo API
  version: "1.0"
paths:
  /v1/users/{user_id}:
    get:
      operationId: getUser
      parameters:
        - name: user_id
          in: path
          required: true
          schema:
            type: integer
            default: 0
        - name: include
          in: query
          required: true
          schema:
            type: string
            default: profile
      responses:
        200:
          description: User
          content:
            application/json:
              schema:
                type: object
                required: [id, email]
                properties:
                  id:
                    type: integer
                  email:
                    type: string
  /v1/admin:
    get:
      responses:
        "200":
          description: Admin
"""
    )
    server, base_url, thread = _server_url()
    try:
        result = import_openapi_specs(
            openapi_path=openapi_path,
            specs_dir=api_specs_dir_for_data_dir(tmp_path),
            base_url=base_url,
            path_filter=r"/users/",
            method_filter="GET",
        )
        assert result.specs
        spec = result.specs[0]
        run = run_api_spec(spec=spec, runs_dir=api_runs_dir_for_data_dir(tmp_path))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.skipped == []
    assert len(result.specs) == 1
    assert spec.method == "GET"
    assert spec.url == f"{base_url}/v1/users/0"
    assert spec.query == {"include": "profile"}
    assert spec.expected_status == 200
    assert spec.schema_assertions == [
        {
            "schema": {
                "type": "object",
                "required": ["id", "email"],
                "properties": {
                    "id": {"type": "integer"},
                    "email": {"type": "string"},
                },
            }
        }
    ]
    assert spec.fixtures["contract_derived"] is True
    assert spec.fixtures["source"] == "openapi_import"
    assert spec.fixtures["operation_id"] == "getUser"
    assert run.ok is True


def test_openapi_import_resolves_json_refs_and_filters_methods(tmp_path: Path) -> None:
    openapi_path = tmp_path / "openapi.json"
    openapi_path.write_text(
        json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "Demo API", "version": "1.0"},
                "paths": {
                    "/v1/health": {
                        "get": {
                            "responses": {
                                "200": {
                                    "description": "Health",
                                    "content": {
                                        "application/json": {
                                            "schema": {
                                                "$ref": "#/components/schemas/Health"
                                            }
                                        }
                                    },
                                }
                            }
                        },
                        "post": {"responses": {"201": {"description": "Ignored"}}},
                    }
                },
                "components": {
                    "schemas": {
                        "Health": {
                            "type": "object",
                            "required": ["ok"],
                            "properties": {"ok": {"type": "boolean"}},
                        }
                    }
                },
            }
        )
    )

    result = import_openapi_specs(
        openapi_path=openapi_path,
        specs_dir=api_specs_dir_for_data_dir(tmp_path),
        base_url="http://api.example.test",
        method_filter="GET",
    )

    assert len(result.specs) == 1
    assert result.specs[0].url == "http://api.example.test/v1/health"
    assert result.specs[0].schema_assertions[0]["schema"]["required"] == ["ok"]
    assert result.specs[0].fixtures["contract_derived"] is True


def test_cli_imports_openapi_specs_with_base_url(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.yaml").write_text(
        """
posthog:
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
    )
    openapi_path = tmp_path / "openapi.yaml"
    openapi_path.write_text(
        """
openapi: 3.0.3
info:
  title: Demo API
  version: "1.0"
paths:
  /v1/health:
    get:
      summary: Health
      responses:
        "200":
          description: OK
          content:
            application/json:
              schema:
                type: object
                required: [ok]
                properties:
                  ok:
                    type: boolean
servers:
  - url: http://api.example.test
"""
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        main,
        [
            "tester",
            "api-import-openapi",
            str(openapi_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["created_count"] == 1
    spec_paths = list((tmp_path / "data" / "api-tests" / "specs").glob("*.json"))
    assert len(spec_paths) == 1
    spec_payload = json.loads(spec_paths[0].read_text())
    assert spec_payload["url"] == "http://api.example.test/v1/health"
    assert spec_payload["fixtures"]["contract_derived"] is True
