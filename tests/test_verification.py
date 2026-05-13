import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from click.testing import CliRunner

from retrace.api_testing import api_specs_dir_for_data_dir, create_api_spec
from retrace.cli import main
from retrace.failures import CanonicalFailure, stable_failure_public_id
from retrace.storage import Storage
from retrace.verification import plan_repair_verification, run_repair_verification


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def _server_url() -> tuple[ThreadingHTTPServer, str, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}", thread


def _failure(project_id: str, environment_id: str) -> CanonicalFailure:
    return CanonicalFailure(
        public_id=stable_failure_public_id(
            project_id,
            environment_id,
            "test_run",
            "api:checkout",
        ),
        project_id=project_id,
        environment_id=environment_id,
        source_type="test_run",
        source_external_id="api:checkout",
        fingerprint="api-checkout",
        title="Checkout API failed",
        summary="API regression needs verification.",
        severity="high",
        confidence="high",
        status="in_progress",
    )


def test_repair_verification_runs_linked_api_spec_and_resolves_failure(
    tmp_path: Path,
) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")
    server, base_url, thread = _server_url()
    try:
        failure_id = store.upsert_failure(
            _failure(workspace.project_id, workspace.environment_id)
        )
        spec = create_api_spec(
            specs_dir=api_specs_dir_for_data_dir(tmp_path),
            name="Checkout verification",
            method="GET",
            url=f"{base_url}/api/checkout",
            expected_status=200,
            json_assertions=[{"id": "ok", "path": "$.ok", "equals": True}],
        )
        link_id = store.upsert_failure_test_link(
            failure_id=failure_id,
            spec_id=spec.spec_id,
            spec_name=spec.name,
            spec_path=f"api-tests/specs/{spec.spec_id}.json",
            source="api_test_run",
        )
        repair_task_id = store.upsert_repair_task(
            failure_id=failure_id,
            title="Repair checkout API",
            status="ready_for_validation",
            validation_commands=[f"retrace tester api-run {spec.spec_id}"],
        )

        plan = plan_repair_verification(
            store=store,
            data_dir=tmp_path,
            repair_task_id=repair_task_id,
        )
        result = run_repair_verification(
            store=store,
            data_dir=tmp_path,
            cwd=tmp_path,
            repair_task_id=repair_task_id,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert plan.tests[0].kind == "api"
    assert plan.tests[0].coverage_link_id == link_id
    assert result.status == "passed"
    assert result.tests[0].ok is True
    failure = store.get_failure_by_id(failure_id)
    assert failure is not None
    assert failure.status == "resolved"
    assert failure.metadata["last_verification"]["status"] == "passed"
    task = store.get_repair_task(repair_task_id)
    assert task is not None
    assert task.status == "resolved"
    link = store.list_failure_test_links(failure_id=failure_id)[0]
    assert link.coverage_state == "covered_passing"


def test_repair_verification_blocks_without_linked_specs(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")
    failure_id = store.upsert_failure(
        _failure(workspace.project_id, workspace.environment_id)
    )
    repair_task_id = store.upsert_repair_task(
        failure_id=failure_id,
        title="Repair checkout API",
    )
    before = store.get_failure_by_id(failure_id)
    assert before is not None

    result = run_repair_verification(
        store=store,
        data_dir=tmp_path,
        cwd=tmp_path,
        repair_task_id=repair_task_id,
    )

    assert result.status == "blocked"
    failure = store.get_failure_by_id(failure_id)
    assert failure is not None
    assert failure.status == before.status
    assert failure.metadata["last_verification"]["status"] == "blocked"
    task = store.get_repair_task(repair_task_id)
    assert task is not None
    assert task.status == "blocked"
    assert task.metadata["last_verification"]["error"] == "No linked Retrace specs found."


def test_repair_verification_preserves_blocked_linked_spec(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")
    failure_id = store.upsert_failure(
        _failure(workspace.project_id, workspace.environment_id)
    )
    link_id = store.upsert_failure_test_link(
        failure_id=failure_id,
        spec_id="missing-checkout-api",
        spec_name="Missing checkout API",
        spec_path="api-tests/specs/missing-checkout-api.json",
        source="manual",
    )
    repair_task_id = store.upsert_repair_task(
        failure_id=failure_id,
        title="Repair checkout API",
        status="ready_for_validation",
    )
    before = store.get_failure_by_id(failure_id)
    assert before is not None

    result = run_repair_verification(
        store=store,
        data_dir=tmp_path,
        cwd=tmp_path,
        repair_task_id=repair_task_id,
    )

    assert result.status == "blocked"
    assert result.error == "One or more linked specs could not run."
    assert result.tests[0].coverage_link_id == link_id
    assert result.tests[0].status == "blocked"
    failure = store.get_failure_by_id(failure_id)
    assert failure is not None
    assert failure.status == before.status
    assert failure.metadata["last_verification"]["status"] == "blocked"
    task = store.get_repair_task(repair_task_id)
    assert task is not None
    assert task.status == "blocked"


def test_repair_verification_plans_all_linked_specs(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")
    failure_id = store.upsert_failure(
        _failure(workspace.project_id, workspace.environment_id)
    )
    repair_task_id = store.upsert_repair_task(
        failure_id=failure_id,
        title="Repair checkout API",
        status="ready_for_validation",
    )
    for index in range(105):
        spec_id = f"checkout-api-{index}"
        store.upsert_failure_test_link(
            failure_id=failure_id,
            spec_id=spec_id,
            spec_name=f"Checkout API {index}",
            spec_path=f"api-tests/specs/{spec_id}.json",
            source="manual",
        )

    plan = plan_repair_verification(
        store=store,
        data_dir=tmp_path,
        repair_task_id=repair_task_id,
    )

    assert len(plan.tests) == 105


def test_repair_verify_cli_prints_verification_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
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
  data_dir: ./data
"""
    )
    monkeypatch.chdir(tmp_path)
    store = Storage(tmp_path / "data" / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")
    failure_id = store.upsert_failure(
        _failure(workspace.project_id, workspace.environment_id)
    )
    repair_task_id = store.upsert_repair_task(
        failure_id=failure_id,
        title="Repair checkout API",
    )

    result = CliRunner().invoke(
        main,
        [
            "repair",
            "verify",
            "--repair-task-id",
            repair_task_id,
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "blocked"
    assert payload["repair_task_id"] == repair_task_id
