import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from click.testing import CliRunner

from retrace.cli import main
from retrace.storage import Storage
from retrace.tester import set_explore_factories


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


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<html>ok</html>")

    def log_message(self, format: str, *args: object) -> None:
        return


def _server_url() -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


class _ExploreDriver:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.url = ""

    def navigate(self, url: str) -> None:
        self.url = url

    def click(self, selector: str) -> None:
        return

    def type(self, selector: str, text: str) -> None:
        return

    def press(self, key: str, selector: str = "") -> None:
        return

    def wait_for(self, selector: str, timeout_ms: int = 5000) -> None:
        return

    def screenshot(self, path: Path) -> None:
        path.write_bytes(b"png")

    def snapshot(self) -> dict:
        return {"url": self.url, "title": "Demo", "text": "Signup Checkout"}

    def close(self) -> None:
        return


class _ExploreLLM:
    def chat_json(self, *, system: str, user: str) -> dict:
        return {
            "tool": "finish",
            "args": {"status": "success", "summary": "Primary flows discovered"},
        }


def test_tester_create_list_and_run(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.yaml").write_text(_CONFIG_YAML)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    create = runner.invoke(
        main,
        [
            "tester",
            "create",
            "--name",
            "Signup flow",
            "--prompt",
            "Open the page and click sign up",
            "--harness-cmd",
            "echo RUN {app_url_q} {prompt_q} > {run_dir}/out.txt",
        ],
    )
    assert create.exit_code == 0, create.output
    assert "Created tester spec:" in create.output

    listed = runner.invoke(main, ["tester", "list"])
    assert listed.exit_code == 0, listed.output
    assert "Signup flow" in listed.output
    spec_id = listed.output.splitlines()[0].split("\t")[0]

    ran = runner.invoke(main, ["tester", "run", spec_id])
    assert ran.exit_code == 0, ran.output
    assert '"ok": true' in ran.output
    assert '"engine_reason": "explicit Browser Harness engine"' in ran.output

    specs_dir = tmp_path / "data" / "ui-tests" / "specs"
    runs_dir = tmp_path / "data" / "ui-tests" / "runs"
    assert any(specs_dir.glob("*.json"))
    assert any(runs_dir.glob("*/run.json"))


def test_tester_create_suite_defaults_to_explore_mode(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "config.yaml").write_text(_CONFIG_YAML)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    create = runner.invoke(main, ["tester", "create-suite"])
    assert create.exit_code == 0, create.output

    listed = runner.invoke(main, ["tester", "list"])
    assert listed.exit_code == 0, listed.output
    assert "\texplore_suite\t" in listed.output


def test_tester_api_create_list_and_run(tmp_path: Path, monkeypatch) -> None:
    server, server_thread, app_url = _server_url()
    try:
        (tmp_path / "config.yaml").write_text(_CONFIG_YAML)
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()

        create = runner.invoke(
            main,
            [
                "tester",
                "api-create",
                "--name",
                "Health API",
                "--url",
                app_url,
                "--json-assertion",
                '{"path":"$.ok","equals":false}',
            ],
        )
        assert create.exit_code == 0, create.output
        spec_id = create.output.strip().split(": ")[1]

        listed = runner.invoke(main, ["tester", "api-list"])
        assert listed.exit_code == 0, listed.output
        assert spec_id in listed.output

        ran = runner.invoke(main, ["tester", "api-run", spec_id])
        assert ran.exit_code != 0
        assert '"status_code": 200' in ran.output
        assert '"assertion_id": "json-0"' in ran.output
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_tester_create_uses_auth_profile_without_persisting_secret(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "config.yaml").write_text(
        _CONFIG_YAML
        + """
tester:
  auth_profiles:
    local-jwt:
      mode: jwt
      jwt_env: RETRACE_LOCAL_JWT
      browser_settings:
        viewport_width: 1200
      auth_setup_steps:
        - id: auth-marker
          action: script
          set:
            auth_profile: "'local-jwt'"
"""
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RETRACE_LOCAL_JWT", "secret-token")
    runner = CliRunner()

    create = runner.invoke(
        main,
        [
            "tester",
            "create",
            "--name",
            "Private flow",
            "--auth-profile",
            "local-jwt",
            "--engine",
            "native",
        ],
    )
    assert create.exit_code == 0, create.output

    spec_path = next((tmp_path / "data" / "ui-tests" / "specs").glob("*.json"))
    payload = json.loads(spec_path.read_text())
    assert payload["auth_required"] is True
    assert payload["auth_mode"] == "jwt"
    assert payload["auth_profile"] == "local-jwt"
    assert payload["auth_jwt_env"] == "RETRACE_LOCAL_JWT"
    assert payload["auth_setup_steps"][0]["id"] == "auth-marker"
    assert payload["browser_settings"]["viewport_width"] == 1200
    assert "secret-token" not in spec_path.read_text()


def test_tester_api_create_and_run_use_shared_profiles(
    tmp_path: Path, monkeypatch
) -> None:
    server, server_thread, app_url = _server_url()
    try:
        (tmp_path / "config.yaml").write_text(
            _CONFIG_YAML
            + """
tester:
  auth_profiles:
    api-jwt:
      mode: jwt
      jwt_env: RETRACE_API_JWT
  env_profiles:
    local-api:
      api_base_url: REPLACE_BASE_URL
      env_overrides:
        RETRACE_API_JWT: test-token
""".replace("REPLACE_BASE_URL", app_url)
        )
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()

        create = runner.invoke(
            main,
            [
                "tester",
                "api-create",
                "--name",
                "Private profile API",
                "--url",
                "/api/private",
                "--auth-profile",
                "api-jwt",
                "--env-profile",
                "local-api",
            ],
        )
        assert create.exit_code == 0, create.output
        spec_id = create.output.strip().split(": ")[1]

        ran = runner.invoke(main, ["tester", "api-run", spec_id])
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    assert ran.exit_code == 0, ran.output
    assert '"status_code": 200' in ran.output
    spec_payload = json.loads(
        next((tmp_path / "data" / "api-tests" / "specs").glob("*.json")).read_text()
    )
    assert spec_payload["auth_profile"] == "api-jwt"
    assert spec_payload["env_profile"] == "local-api"
    assert "test-token" not in json.dumps(spec_payload)


def test_tester_profiles_outputs_redacted_shared_profiles(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "config.yaml").write_text(
        _CONFIG_YAML
        + """
tester:
  auth_profiles:
    api-jwt:
      mode: jwt
      jwt_env: RETRACE_API_JWT
  env_profiles:
    local-api:
      api_base_url: http://127.0.0.1:3000
      env_overrides:
        FEATURE_FLAG: enabled
"""
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["tester", "profiles"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["auth_profiles"][0]["jwt_env"] == "[secret-env]"
    assert payload["env_profiles"][0]["api_base_url"] == "http://127.0.0.1:3000"
    assert "RETRACE_API_JWT" not in result.output


def test_create_suite_run_generates_accepts_and_runs_draft_specs(
    tmp_path: Path, monkeypatch
) -> None:
    server, server_thread, app_url = _server_url()
    try:
        (tmp_path / "config.yaml").write_text(
            _CONFIG_YAML
            + """
tester:
  auth_profiles:
    local-jwt:
      mode: jwt
      jwt_env: RETRACE_LOCAL_JWT
      auth_setup_steps:
        - id: auth-marker
          action: script
          set:
            auth_profile: "'local-jwt'"
"""
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("RETRACE_LOCAL_JWT", "test-token")
        set_explore_factories(
            driver_factory=lambda **kwargs: _ExploreDriver(),
            llm_factory=lambda: _ExploreLLM(),
        )
        runner = CliRunner()

        create = runner.invoke(
            main,
            [
                "tester",
                "create-suite",
                "--app-url",
                app_url,
                "--auth-profile",
                "local-jwt",
                "--goal",
                "Signup flow",
                "--goal",
                "Checkout payment",
            ],
        )
        assert create.exit_code == 0, create.output
        suite_spec_id = create.output.strip().split(": ")[1]

        ran_suite = runner.invoke(main, ["tester", "run", suite_spec_id])
        assert ran_suite.exit_code == 0, ran_suite.output
        suite_payload = json.loads(ran_suite.output)
        run_id = suite_payload["run_id"]
        assert any(
            artifact["artifact_type"] == "suite_proposal"
            for artifact in suite_payload["artifacts"]
        )

        specs_dir = tmp_path / "data" / "ui-tests" / "specs"
        draft_specs = [
            json.loads(path.read_text())
            for path in specs_dir.glob("*.json")
            if json.loads(path.read_text()).get("fixtures", {}).get("draft_status")
            == "draft"
        ]
        assert len(draft_specs) == 2
        assert {spec["fixtures"]["criticality"] for spec in draft_specs} == {"high"}
        assert all(
            spec["fixtures"]["source_exploration_run"] == run_id
            for spec in draft_specs
        )
        assert all(spec["fixtures"]["draft_reason"] for spec in draft_specs)
        assert all("source_auth" in spec["fixtures"] for spec in draft_specs)
        assert all(spec["auth_profile"] == "local-jwt" for spec in draft_specs)
        assert all(
            spec["auth_setup_steps"][0]["id"] == "auth-marker"
            for spec in draft_specs
        )

        draft_id = draft_specs[0]["spec_id"]
        accepted = runner.invoke(
            main,
            ["tester", "accept-draft", draft_id, "--name", "Accepted draft"],
        )
        assert accepted.exit_code == 0, accepted.output
        assert '"draft_status": "accepted"' in accepted.output
        accepted_again = runner.invoke(main, ["tester", "accept-draft", draft_id])
        assert accepted_again.exit_code != 0
        assert "not an unaccepted draft" in accepted_again.output

        ran_draft = runner.invoke(main, ["tester", "run", draft_id, "--retries", "0"])
        assert ran_draft.exit_code == 0, ran_draft.output
        assert '"execution_engine": "native"' in ran_draft.output
    finally:
        set_explore_factories(driver_factory=None, llm_factory=None)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_tester_run_retries_and_marks_flaky(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.yaml").write_text(_CONFIG_YAML)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    create = runner.invoke(
        main,
        [
            "tester",
            "create",
            "--name",
            "Flaky flow",
            "--prompt",
            "Run flow",
            "--harness-cmd",
            "if [ -f {run_dir_q}/ok ]; then echo ok {app_url_q} {prompt_q}; exit 0; else touch {run_dir_q}/ok; echo timeout {app_url_q} {prompt_q}; exit 1; fi",
        ],
    )
    assert create.exit_code == 0, create.output

    listed = runner.invoke(main, ["tester", "list"])
    assert listed.exit_code == 0, f"tester list failed: {listed.output}"
    spec_id = listed.output.splitlines()[0].split("\t")[0]

    ran = runner.invoke(main, ["tester", "run", spec_id, "--retries", "1"])
    assert ran.exit_code == 0, ran.output
    assert '"status": "flaky_passed"' in ran.output
    assert '"flaky": true' in ran.output
    assert '"failure_classification": "timeout"' in ran.output


def test_failed_harness_run_persists_failure_evidence_and_repair(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "config.yaml").write_text(_CONFIG_YAML)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    harness_payload_path = tmp_path / "harness-payload.json"
    harness_payload_path.write_text(
        json.dumps(
            {
                "status": "failed",
                "error": "checkout failed",
                "network": [{"url": "/api/checkout", "status": 500}],
                "console": [{"level": "error", "message": "boom"}],
            }
        )
    )

    create = runner.invoke(
        main,
        [
            "tester",
            "create",
            "--name",
            "Checkout failure",
            "--prompt",
            "Run checkout",
            "--harness-cmd",
            (
                "echo {app_url_q} {prompt_q} >/dev/null; cp "
                + str(harness_payload_path)
                + " {run_dir_q}/browser-harness-result.json; exit 1"
            ),
        ],
    )
    assert create.exit_code == 0, create.output
    spec_id = create.output.strip().split(": ")[1]

    first = runner.invoke(main, ["tester", "run", spec_id, "--retries", "0"])
    assert first.exit_code != 0
    first_payload = json.loads(first.output.split("\nError:", 1)[0])
    assert first_payload["canonical_failure_id"].startswith("flr_")
    assert first_payload["repair_task_id"].startswith("rpr_")

    second = runner.invoke(main, ["tester", "run", spec_id, "--retries", "0"])
    assert second.exit_code != 0
    second_payload = json.loads(second.output.split("\nError:", 1)[0])
    assert second_payload["canonical_failure_id"] == first_payload["canonical_failure_id"]
    assert second_payload["repair_task_id"] == first_payload["repair_task_id"]

    store = Storage(tmp_path / "data" / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")
    failures = store.list_failures(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )
    assert len(failures) == 1
    assert failures[0].linked_repair_task_id == first_payload["repair_task_id"]
    evidence = store.list_failure_evidence(failure_id=failures[0].id)
    evidence_types = {item.evidence_type for item in evidence}
    assert {"test_transcript", "network_request", "console_log"} <= evidence_types
    repair = store.get_repair_task(first_payload["repair_task_id"])
    assert repair is not None
    assert repair.evidence_ids
    links = store.list_failure_test_links(failure_id=failures[0].id)
    assert links[0].latest_run_id == second_payload["run_id"]


def test_tester_enqueue_and_worker_runs_queued_spec(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "config.yaml").write_text(_CONFIG_YAML)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    create = runner.invoke(
        main,
        [
            "tester",
            "create",
            "--name",
            "Queued flow",
            "--prompt",
            "Open queued page",
            "--harness-cmd",
            "echo QUEUED {app_url_q} {prompt_q} > {run_dir}/queued.txt",
        ],
    )
    assert create.exit_code == 0, create.output
    spec_id = create.output.strip().split(": ")[1]

    enqueue = runner.invoke(main, ["tester", "enqueue", spec_id])
    assert enqueue.exit_code == 0, enqueue.output
    assert '"status": "queued"' in enqueue.output
    queue_dir = tmp_path / "data" / "ui-tests" / "queue"
    assert not list(queue_dir.glob("*.tmp"))

    worker = runner.invoke(main, ["tester", "worker", "--once"])
    assert worker.exit_code == 0, worker.output
    assert '"status": "succeeded"' in worker.output

    assert any((tmp_path / "data" / "ui-tests" / "queue" / "done").glob("*.json"))
    assert any((tmp_path / "data" / "ui-tests" / "runs").glob("*/run.json"))


def test_tester_enqueue_honors_zero_retry_default(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "config.yaml").write_text(
        _CONFIG_YAML
        + """
tester:
  max_retries: 0
"""
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    create = runner.invoke(
        main,
        [
            "tester",
            "create",
            "--name",
            "No retries",
            "--prompt",
            "Open the page",
            "--harness-cmd",
            "echo RUN {app_url_q} {prompt_q} > {run_dir}/out.txt",
        ],
    )
    assert create.exit_code == 0, create.output
    spec_id = create.output.strip().split(": ")[1]

    enqueue = runner.invoke(main, ["tester", "enqueue", spec_id])
    assert enqueue.exit_code == 0, enqueue.output
    assert '"retries": 0' in enqueue.output
