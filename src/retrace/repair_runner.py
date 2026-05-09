from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from retrace.prompts import build_repair_bundle_prompt
from retrace.repair import RepairBundle


@dataclass(frozen=True)
class RepairRunnerConfig:
    repo_path: Path
    agent_command: list[str] = field(default_factory=list)
    validation_commands: list[str] = field(default_factory=list)
    dry_run: bool = True
    allow_draft_pr: bool = False
    create_draft_pr: bool = False
    branch_name: str = ""
    repo_full_name: str = ""
    github_token: str = ""
    timeout_seconds: int = 900


@dataclass(frozen=True)
class RepairCommandResult:
    command: str
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class RepairRunResult:
    status: str
    prompt: str
    planned_commands: list[str]
    tests_run: list[RepairCommandResult] = field(default_factory=list)
    agent_result: RepairCommandResult | None = None
    changed_files: list[str] = field(default_factory=list)
    diff: str = ""
    draft_pr_url: str = ""
    error: str = ""


def run_repair(
    bundle: RepairBundle,
    config: RepairRunnerConfig,
) -> RepairRunResult:
    repo_path = config.repo_path.resolve()
    if not repo_path.exists() or not repo_path.is_dir():
        raise ValueError(f"repo_path does not exist or is not a directory: {repo_path}")
    if config.create_draft_pr and not config.allow_draft_pr:
        raise ValueError("draft PR creation requires allow_draft_pr=True")

    prompt = build_repair_bundle_prompt(bundle)
    validation_commands = config.validation_commands or bundle.validation_commands
    planned_commands = []
    if config.agent_command:
        planned_commands.append(shlex.join(config.agent_command))
    planned_commands.extend(validation_commands)
    if config.create_draft_pr:
        planned_commands.append("create draft pull request")

    if config.dry_run:
        return RepairRunResult(
            status="dry_run",
            prompt=prompt,
            planned_commands=planned_commands,
        )

    if not config.agent_command:
        return RepairRunResult(
            status="blocked",
            prompt=prompt,
            planned_commands=planned_commands,
            error="agent_command is required for local repair execution",
        )

    agent_result = _run_command(
        config.agent_command,
        repo_path=repo_path,
        timeout_seconds=config.timeout_seconds,
        stdin=prompt,
    )
    tests_run = [
        _run_shell_command(
            command,
            repo_path=repo_path,
            timeout_seconds=config.timeout_seconds,
        )
        for command in validation_commands
    ]
    changed_files = _changed_files(repo_path)
    diff = _diff(repo_path)
    draft_pr_url = ""
    if config.create_draft_pr:
        draft_pr_url = _create_draft_pr(
            repo_path=repo_path,
            branch_name=config.branch_name,
            repo_full_name=config.repo_full_name,
            github_token=config.github_token,
            timeout_seconds=config.timeout_seconds,
        )
    status = "completed"
    if agent_result.returncode != 0:
        status = "agent_failed"
    elif any(result.returncode != 0 for result in tests_run):
        status = "validation_failed"
    return RepairRunResult(
        status=status,
        prompt=prompt,
        planned_commands=planned_commands,
        tests_run=tests_run,
        agent_result=agent_result,
        changed_files=changed_files,
        diff=diff,
        draft_pr_url=draft_pr_url,
    )


def _run_command(
    command: list[str],
    *,
    repo_path: Path,
    timeout_seconds: int,
    stdin: str = "",
) -> RepairCommandResult:
    result = subprocess.run(
        command,
        cwd=repo_path,
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )
    return RepairCommandResult(
        command=shlex.join(command),
        returncode=int(result.returncode),
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _run_shell_command(
    command: str,
    *,
    repo_path: Path,
    timeout_seconds: int,
) -> RepairCommandResult:
    result = subprocess.run(
        command,
        cwd=repo_path,
        shell=True,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )
    return RepairCommandResult(
        command=command,
        returncode=int(result.returncode),
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _changed_files(repo_path: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only"],
        cwd=repo_path,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _diff(repo_path: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "--binary"],
        cwd=repo_path,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def _create_draft_pr(
    *,
    repo_path: Path,
    branch_name: str,
    repo_full_name: str,
    github_token: str,
    timeout_seconds: int,
) -> str:
    if not branch_name.strip():
        raise ValueError("branch_name is required when create_draft_pr=True")
    env = os.environ.copy()
    if github_token.strip():
        env["GH_TOKEN"] = github_token.strip()
    subprocess.run(
        ["git", "switch", "-C", branch_name.strip()],
        cwd=repo_path,
        check=True,
        timeout=timeout_seconds,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", branch_name.strip()],
        cwd=repo_path,
        check=True,
        timeout=timeout_seconds,
        env=env,
    )
    command = [
        "gh",
        "pr",
        "create",
        "--draft",
        "--title",
        f"Repair {branch_name.strip()}",
        "--body",
        "Created by retrace repair runner.",
    ]
    if repo_full_name.strip():
        command.extend(["--repo", repo_full_name.strip()])
    result = subprocess.run(
        command,
        cwd=repo_path,
        text=True,
        capture_output=True,
        check=True,
        timeout=timeout_seconds,
        env=env,
    )
    return result.stdout.strip()
