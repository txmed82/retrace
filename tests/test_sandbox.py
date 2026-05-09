from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

from retrace.sandbox import (
    PRSandboxConfig,
    canonical_failure_from_sandbox_result,
    persist_sandbox_failure,
    run_pr_review_sandbox,
)
from retrace.storage import Storage


def _git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=path, check=True)
    subprocess.run(["git", "add", "app.py"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True)


def test_pr_review_sandbox_runs_command_captures_artifacts_and_cleans_workspace(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _git_repo(repo)

    result = run_pr_review_sandbox(
        PRSandboxConfig(
            repo_url=str(repo),
            ref="feature",
            setup_commands=[
                [sys.executable, "-c", "from pathlib import Path; Path('deps.txt').write_text('ok')"]
            ],
            test_commands=[[sys.executable, "-c", "print('sandbox test passed')"]],
            artifacts_dir=tmp_path / "artifacts",
            workspace_parent=tmp_path / "workspaces",
            run_id="run_success",
        )
    )

    assert result.status == "succeeded"
    assert result.workspace_cleaned is True
    assert not Path(result.workspace_path).exists()
    assert result.test_results[0].exit_code == 0
    log_path = Path(result.test_results[0].combined_log_path)
    assert "sandbox test passed" in log_path.read_text(encoding="utf-8")
    manifest = json.loads(
        (Path(result.artifacts_dir) / "sandbox-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["workspace_cleaned"] is True
    assert any(item["artifact_type"] == "sandbox_command_log" for item in result.artifacts)


def test_pr_review_sandbox_cleans_auto_workspace_parent_and_splits_string_commands(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _git_repo(repo)

    result = run_pr_review_sandbox(
        PRSandboxConfig(
            repo_url=str(repo),
            ref="feature",
            test_commands=[
                f"{shlex.quote(sys.executable)} -c \"print('string command passed')\""
            ],
            artifacts_dir=tmp_path / "artifacts",
            run_id="run_auto_workspace",
        )
    )

    auto_parent = Path(result.workspace_path).parent.parent
    assert result.status == "succeeded"
    assert result.workspace_cleaned is True
    assert not auto_parent.exists()
    assert "string command passed" in Path(
        result.test_results[0].combined_log_path
    ).read_text(encoding="utf-8")


def test_failed_sandbox_result_can_feed_canonical_failures(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _git_repo(repo)
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(
        org_name="Acme",
        project_name="Web",
        environment_name="pr",
    )

    result = run_pr_review_sandbox(
        PRSandboxConfig(
            repo_url=str(repo),
            ref="feature",
            test_commands=[
                [
                    sys.executable,
                    "-c",
                    "import sys; print('assertion failed', file=sys.stderr); sys.exit(2)",
                ]
            ],
            artifacts_dir=tmp_path / "artifacts",
            workspace_parent=tmp_path / "workspaces",
            run_id="run_failed",
        )
    )

    failure = canonical_failure_from_sandbox_result(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        result=result,
        pr_number=123,
    )
    failure_id = persist_sandbox_failure(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        result=result,
        pr_number=123,
    )

    assert result.status == "tests_failed"
    assert failure is not None
    assert failure.source_type == "github_pr_review"
    assert failure.related_pr_number == 123
    assert "assertion failed" in failure.summary
    assert failure_id
    stored = store.get_failure(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        failure_id=str(failure_id),
    )
    assert stored is not None
    assert stored.source_type == "github_pr_review"
