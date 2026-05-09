from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from retrace.failures import CanonicalFailure, stable_failure_public_id
from retrace.storage import Storage


SandboxCommand = str | Sequence[str]


@dataclass(frozen=True)
class SandboxCommandResult:
    command: str
    exit_code: int
    duration_ms: int
    stdout_path: str
    stderr_path: str
    combined_log_path: str
    stdout_tail: str = ""
    stderr_tail: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PRSandboxConfig:
    repo_url: str
    ref: str
    setup_commands: list[SandboxCommand] = field(default_factory=list)
    test_commands: list[SandboxCommand] = field(default_factory=list)
    artifacts_dir: Path | None = None
    workspace_parent: Path | None = None
    keep_workspace: bool = False
    timeout_seconds: int = 600
    clone_depth: int = 1
    run_id: str = ""


@dataclass(frozen=True)
class PRSandboxRunResult:
    run_id: str
    repo_url: str
    ref: str
    status: str
    workspace_path: str
    workspace_cleaned: bool
    artifacts_dir: str
    clone_result: SandboxCommandResult
    setup_results: list[SandboxCommandResult]
    test_results: list[SandboxCommandResult]
    artifacts: list[dict[str, Any]]

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "repo_url": self.repo_url,
            "ref": self.ref,
            "status": self.status,
            "workspace_path": self.workspace_path,
            "workspace_cleaned": self.workspace_cleaned,
            "artifacts_dir": self.artifacts_dir,
            "clone_result": self.clone_result.to_dict(),
            "setup_results": [item.to_dict() for item in self.setup_results],
            "test_results": [item.to_dict() for item in self.test_results],
            "artifacts": list(self.artifacts),
        }


def run_pr_review_sandbox(config: PRSandboxConfig) -> PRSandboxRunResult:
    if not config.repo_url.strip():
        raise ValueError("repo_url is required")
    if not config.ref.strip():
        raise ValueError("ref is required")
    run_id = config.run_id.strip() or f"sandbox_{uuid4().hex[:12]}"
    artifacts_dir = _resolve_artifacts_dir(config.artifacts_dir, run_id)
    workspace_parent = config.workspace_parent or Path(
        tempfile.mkdtemp(prefix="retrace-sandbox-workspaces-")
    )
    workspace_parent.mkdir(parents=True, exist_ok=True)
    workspace_path = workspace_parent / run_id / "checkout"
    workspace_path.parent.mkdir(parents=True, exist_ok=True)

    clone_command = _clone_command(config, workspace_path)
    clone_result = _run_command(
        clone_command,
        cwd=workspace_path.parent,
        artifacts_dir=artifacts_dir,
        label="clone",
        timeout_seconds=config.timeout_seconds,
    )
    setup_results: list[SandboxCommandResult] = []
    test_results: list[SandboxCommandResult] = []
    workspace_cleaned = False
    status = "clone_failed"
    artifacts: list[dict[str, Any]] = []
    try:
        if clone_result.ok:
            for index, command in enumerate(config.setup_commands, start=1):
                result = _run_command(
                    command,
                    cwd=workspace_path,
                    artifacts_dir=artifacts_dir,
                    label=f"setup-{index}",
                    timeout_seconds=config.timeout_seconds,
                )
                setup_results.append(result)
                if not result.ok:
                    break
            if all(result.ok for result in setup_results):
                for index, command in enumerate(config.test_commands, start=1):
                    result = _run_command(
                        command,
                        cwd=workspace_path,
                        artifacts_dir=artifacts_dir,
                        label=f"test-{index}",
                        timeout_seconds=config.timeout_seconds,
                    )
                    test_results.append(result)
        status = _sandbox_status(clone_result, setup_results, test_results)
        artifacts = _write_manifest(
            run_id=run_id,
            config=config,
            artifacts_dir=artifacts_dir,
            clone_result=clone_result,
            setup_results=setup_results,
            test_results=test_results,
            status=status,
        )
    finally:
        if not config.keep_workspace:
            shutil.rmtree(workspace_path.parent, ignore_errors=True)
            workspace_cleaned = True
            _update_manifest_cleanup(artifacts_dir=artifacts_dir, cleaned=True)
    return PRSandboxRunResult(
        run_id=run_id,
        repo_url=config.repo_url,
        ref=config.ref,
        status=status,
        workspace_path=str(workspace_path),
        workspace_cleaned=workspace_cleaned,
        artifacts_dir=str(artifacts_dir),
        clone_result=clone_result,
        setup_results=setup_results,
        test_results=test_results,
        artifacts=artifacts,
    )


def canonical_failure_from_sandbox_result(
    *,
    project_id: str,
    environment_id: str,
    result: PRSandboxRunResult,
    pr_number: int | None = None,
) -> CanonicalFailure | None:
    failed = _first_failed_command(result)
    if failed is None:
        return None
    source_external_id = f"{result.run_id}:{failed.command}:{failed.exit_code}"
    summary = failed.stderr_tail or failed.stdout_tail or f"exit code {failed.exit_code}"
    return CanonicalFailure(
        public_id=stable_failure_public_id(
            project_id,
            environment_id,
            "github_pr_review",
            source_external_id,
        ),
        project_id=project_id,
        environment_id=environment_id,
        source_type="github_pr_review",
        source_external_id=source_external_id,
        fingerprint=source_external_id,
        title=f"PR sandbox command failed: {failed.command}",
        summary=summary,
        severity="medium",
        confidence="high",
        status="new",
        related_pr_number=pr_number,
        metadata={
            "run_id": result.run_id,
            "repo_url": result.repo_url,
            "ref": result.ref,
            "status": result.status,
            "failed_command": failed.to_dict(),
            "artifacts": list(result.artifacts),
            "workspace_cleaned": result.workspace_cleaned,
        },
    )


def persist_sandbox_failure(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    result: PRSandboxRunResult,
    pr_number: int | None = None,
) -> str | None:
    failure = canonical_failure_from_sandbox_result(
        project_id=project_id,
        environment_id=environment_id,
        result=result,
        pr_number=pr_number,
    )
    if failure is None:
        return None
    return store.upsert_failure(failure)


def _clone_command(config: PRSandboxConfig, workspace_path: Path) -> list[str]:
    command = ["git", "clone"]
    if config.clone_depth > 0:
        command.extend(["--depth", str(config.clone_depth)])
    command.extend(["--branch", config.ref, config.repo_url, str(workspace_path)])
    return command


def _run_command(
    command: SandboxCommand,
    *,
    cwd: Path,
    artifacts_dir: Path,
    label: str,
    timeout_seconds: int,
) -> SandboxCommandResult:
    start = time.monotonic()
    shell = isinstance(command, str)
    display = command if isinstance(command, str) else " ".join(command)
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=shell,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
        exit_code = int(completed.returncode)
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = _decode_output(exc.stdout)
        stderr = _decode_output(exc.stderr) + f"\ncommand timed out after {timeout_seconds}s"
    duration_ms = int((time.monotonic() - start) * 1000)
    stdout_path = artifacts_dir / f"{label}.stdout.txt"
    stderr_path = artifacts_dir / f"{label}.stderr.txt"
    combined_path = artifacts_dir / f"{label}.log"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    combined_path.write_text(
        f"$ {display}\n\n[stdout]\n{stdout}\n[stderr]\n{stderr}",
        encoding="utf-8",
    )
    return SandboxCommandResult(
        command=display,
        exit_code=exit_code,
        duration_ms=duration_ms,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        combined_log_path=str(combined_path),
        stdout_tail=_tail(stdout),
        stderr_tail=_tail(stderr),
    )


def _resolve_artifacts_dir(artifacts_dir: Path | None, run_id: str) -> Path:
    if artifacts_dir is None:
        base = Path(tempfile.mkdtemp(prefix="retrace-sandbox-artifacts-"))
        path = base / run_id
    else:
        path = artifacts_dir / run_id
    path.mkdir(parents=True, exist_ok=False)
    return path


def _sandbox_status(
    clone_result: SandboxCommandResult,
    setup_results: list[SandboxCommandResult],
    test_results: list[SandboxCommandResult],
) -> str:
    if not clone_result.ok:
        return "clone_failed"
    if any(not result.ok for result in setup_results):
        return "setup_failed"
    if any(not result.ok for result in test_results):
        return "tests_failed"
    return "succeeded"


def _write_manifest(
    *,
    run_id: str,
    config: PRSandboxConfig,
    artifacts_dir: Path,
    clone_result: SandboxCommandResult,
    setup_results: list[SandboxCommandResult],
    test_results: list[SandboxCommandResult],
    status: str,
) -> list[dict[str, Any]]:
    command_results = [clone_result, *setup_results, *test_results]
    artifacts = [
        {
            "artifact_id": f"sandbox-log-{index}",
            "artifact_type": "sandbox_command_log",
            "path": result.combined_log_path,
            "command": result.command,
            "exit_code": result.exit_code,
        }
        for index, result in enumerate(command_results, start=1)
    ]
    manifest_path = artifacts_dir / "sandbox-manifest.json"
    artifacts.append(
        {
            "artifact_id": "sandbox-manifest",
            "artifact_type": "sandbox_manifest",
            "path": str(manifest_path),
        }
    )
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "repo_url": config.repo_url,
                "ref": config.ref,
                "status": status,
                "workspace_cleaned": False,
                "commands": [result.to_dict() for result in command_results],
                "artifacts": artifacts,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return artifacts


def _update_manifest_cleanup(*, artifacts_dir: Path, cleaned: bool) -> None:
    manifest_path = artifacts_dir / "sandbox-manifest.json"
    if not manifest_path.exists():
        return
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["workspace_cleaned"] = cleaned
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _first_failed_command(
    result: PRSandboxRunResult,
) -> SandboxCommandResult | None:
    for command_result in [
        result.clone_result,
        *result.setup_results,
        *result.test_results,
    ]:
        if not command_result.ok:
            return command_result
    return None


def _tail(text: str, limit: int = 2000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def _decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
