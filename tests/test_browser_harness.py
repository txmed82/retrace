import json
import shlex
import sys
from pathlib import Path

from retrace.browser_harness import BrowserHarnessAdapter
from retrace.failures import canonical_failure_from_test_run
from retrace.tester import create_spec, run_spec, runs_dir_for_data_dir


def _python_harness_command(tmp_path: Path, script: str) -> str:
    script_path = tmp_path / f"harness-{abs(hash(script))}.py"
    script_path.write_text(script)
    return (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(script_path))} "
        "{run_dir_q} {app_url_q} {prompt_q}"
    )


def _structured_harness_script(*, exit_code: int = 0, status: str = "passed") -> str:
    return f"""
import json
import pathlib
import sys

run_dir = pathlib.Path(sys.argv[1])
artifacts_dir = run_dir / "artifacts"
artifacts_dir.mkdir(parents=True, exist_ok=True)
(artifacts_dir / "final.png").write_bytes(b"fake-png")
payload = {{
    "status": {status!r},
    "error": "" if {exit_code} == 0 else "Checkout confirmation missing",
    "steps": [
        {{"action": "navigate", "target": sys.argv[2], "ok": True}},
        {{"action": "click", "target": "text=Checkout", "ok": {exit_code} == 0}},
    ],
    "screenshots": [{{"path": "artifacts/final.png", "label": "Final state"}}],
    "console": [{{"level": "error", "message": "hydration warning"}}],
    "network": [{{"method": "POST", "url": "/api/checkout", "status": 500}}],
    "assertions": [
        {{
            "id": "checkout-confirmation",
            "ok": {exit_code} == 0,
            "expected": "confirmation",
            "actual": "missing",
            "message": "Checkout confirmation appears",
        }}
    ],
}}
(run_dir / "browser-harness-result.json").write_text(json.dumps(payload))
print(json.dumps({{"event": "harness.complete", "status": {status!r}}}))
sys.exit({exit_code})
"""


def test_browser_harness_adapter_normalizes_structured_output(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    log_path = run_dir / "harness.log"
    command = _python_harness_command(
        tmp_path,
        _structured_harness_script(exit_code=1, status="failed")
    ).format(
        run_dir_q=shlex.quote(str(run_dir)),
        app_url_q=shlex.quote("http://app.test"),
        prompt_q=shlex.quote("Verify checkout"),
    )

    result = BrowserHarnessAdapter(
        command=command,
        run_dir=run_dir,
        log_path=log_path,
    ).run()

    assert result.exit_code == 1
    assert result.final_status == "failed"
    assert result.error == "Checkout confirmation missing"
    artifact_types = {artifact["artifact_type"] for artifact in result.artifacts}
    assert "log" in artifact_types
    assert "browser_harness_output" in artifact_types
    assert "browser_harness_steps" in artifact_types
    assert "console_output" in artifact_types
    assert "network_output" in artifact_types
    assert "screenshot" in artifact_types
    assert result.assertion_results == [
        {
            "assertion_id": "checkout-confirmation",
            "assertion_type": "harness",
            "ok": False,
            "expected": "confirmation",
            "actual": "missing",
            "message": "Checkout confirmation appears",
            "source": "harness",
            "confidence": 1.0,
        }
    ]


def test_harness_engine_records_structured_artifacts_in_manifest(
    tmp_path: Path,
) -> None:
    spec = create_spec(
        specs_dir=tmp_path / "specs",
        name="Checkout smoke",
        prompt="Verify checkout",
        app_url="http://app.test",
        start_command="",
        harness_command=_python_harness_command(
            tmp_path,
            _structured_harness_script(exit_code=0, status="passed")
        ),
        mode="describe",
        execution_engine="harness",
    )

    result = run_spec(spec=spec, runs_dir=runs_dir_for_data_dir(tmp_path))

    assert result.ok is True
    assert result.execution_engine == "harness"
    artifact_types = {artifact["artifact_type"] for artifact in result.artifacts}
    assert "browser_harness_output" in artifact_types
    assert "browser_harness_steps" in artifact_types
    assert "console_output" in artifact_types
    assert "network_output" in artifact_types
    assert "artifact_manifest" in artifact_types
    assert result.assertion_results[0]["source"] == "harness"
    manifest_path = Path(result.run_dir) / "artifacts" / "artifact-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest_types = {artifact["artifact_type"] for artifact in manifest["artifacts"]}
    assert "browser_harness_output" in manifest_types
    assert "browser_harness_steps" in manifest_types
    assert "console_output" in manifest_types
    assert "network_output" in manifest_types


def test_failed_harness_run_can_be_mapped_to_canonical_failure(
    tmp_path: Path,
) -> None:
    spec = create_spec(
        specs_dir=tmp_path / "specs",
        name="Checkout smoke",
        prompt="Verify checkout",
        app_url="http://app.test",
        start_command="",
        harness_command=_python_harness_command(
            tmp_path,
            _structured_harness_script(exit_code=1, status="failed")
        ),
        mode="describe",
        execution_engine="harness",
    )

    result = run_spec(spec=spec, runs_dir=runs_dir_for_data_dir(tmp_path))
    failure = canonical_failure_from_test_run(
        project_id="proj_1",
        environment_id="env_1",
        run_result=result,
        spec_name=spec.name,
    )

    assert result.ok is False
    assert failure.source_type == "test_run"
    assert failure.source_external_id == result.run_id
    assert failure.status == "new"
    assert failure.metadata["assertion_results"][0]["source"] == "harness"
