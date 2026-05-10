from __future__ import annotations

import json
from pathlib import Path
import shlex
from typing import Any

import click

from retrace.config import load_config
from retrace.repair import build_repair_bundle
from retrace.repair_runner import RepairRunnerConfig, run_repair
from retrace.storage import Storage
from retrace.verification import plan_repair_verification, run_repair_verification


@click.group("repair")
def repair_group() -> None:
    """Build repair bundles and run local repair agents."""


def _store_and_config(config_path: Path) -> tuple[Any, Storage]:
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    return cfg, store


@repair_group.command("list")
@click.option("--status", default="", help="Filter by repair task status.")
@click.option("--failure-id", default="", help="Filter by failure row ID.")
@click.option("--limit", default=25, show_default=True, type=int)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Print machine-readable JSON.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
def repair_list_command(
    *,
    status: str,
    failure_id: str,
    limit: int,
    json_output: bool,
    config_path: Path,
) -> None:
    _cfg, store = _store_and_config(config_path)
    tasks = store.list_repair_tasks(
        failure_id=failure_id.strip() or None,
        status=status.strip() or None,
        limit=limit,
    )
    payload = [_repair_task_payload(task) for task in tasks]
    if json_output:
        click.echo(json.dumps({"repair_tasks": payload}, indent=2, sort_keys=True))
        return
    if not tasks:
        click.echo("No repair tasks found.")
        return
    for task in payload:
        click.echo(
            f"{task['id']}\t{task['status']}\t{task['failure_id']}\t{task['title']}"
        )


@repair_group.command("show")
@click.argument("repair_task_id")
@click.option("--include-sensitive", is_flag=True, default=False)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
def repair_show_command(
    *,
    repair_task_id: str,
    include_sensitive: bool,
    config_path: Path,
) -> None:
    _cfg, store = _store_and_config(config_path)
    task = store.get_repair_task(repair_task_id)
    if task is None:
        raise click.ClickException(f"Repair task not found: {repair_task_id}")
    bundle = build_repair_bundle(
        store,
        task.failure_id,
        include_sensitive=include_sensitive,
    )
    plan = plan_repair_verification(
        store=store,
        data_dir=_cfg.run.data_dir,
        repair_task_id=task.id,
    )
    click.echo(
        json.dumps(
            {
                "repair_task": _repair_task_payload(task),
                "bundle": bundle.__dict__,
                "verification_plan": plan.to_dict(),
            },
            indent=2,
            sort_keys=True,
        )
    )


@repair_group.command("verify")
@click.option("--repair-task-id", default="", help="Repair task ID or public ID.")
@click.option("--failure-id", default="", help="Failure row ID or public ID.")
@click.option("--dry-run", is_flag=True, default=False)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
def repair_verify_command(
    *,
    repair_task_id: str,
    failure_id: str,
    dry_run: bool,
    config_path: Path,
) -> None:
    if not repair_task_id.strip() and not failure_id.strip():
        raise click.ClickException("Provide --repair-task-id or --failure-id.")
    cfg, store = _store_and_config(config_path)
    try:
        result = run_repair_verification(
            store=store,
            data_dir=cfg.run.data_dir,
            cwd=config_path.parent,
            repair_task_id=repair_task_id,
            failure_id=failure_id,
            dry_run=dry_run,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    if result.status in {"failed", "blocked"} and not dry_run:
        raise click.ClickException(f"verification {result.status}")


@repair_group.command("run")
@click.option("--failure-id", required=True, help="Failure row ID or public ID.")
@click.option("--repo", "repo_full_name", default="", help="Connected repo in org/name.")
@click.option(
    "--repo-path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Local checkout path override.",
)
@click.option(
    "--agent-command",
    default="",
    help="Command used to run the local repair agent. The bundle prompt is sent on stdin.",
)
@click.option(
    "--validation-command",
    multiple=True,
    help="Validation command to run after the agent. Defaults to bundle commands.",
)
@click.option("--dry-run/--no-dry-run", default=True, show_default=True)
@click.option("--create-draft-pr", is_flag=True, default=False)
@click.option("--allow-draft-pr", is_flag=True, default=False)
@click.option("--branch-name", default="", help="Branch name for draft PR creation.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
def repair_run_command(
    *,
    failure_id: str,
    repo_full_name: str,
    repo_path: Path | None,
    agent_command: str,
    validation_command: tuple[str, ...],
    dry_run: bool,
    create_draft_pr: bool,
    allow_draft_pr: bool,
    branch_name: str,
    config_path: Path,
) -> None:
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    bundle = build_repair_bundle(
        store,
        failure_id,
        validation_commands=list(validation_command) or None,
    )
    repo = store.get_github_repo(repo_full_name) if repo_full_name.strip() else None
    effective_repo_path = repo_path or (
        Path(repo.local_path) if repo and repo.local_path else None
    )
    if effective_repo_path is None:
        raise click.ClickException(
            "Provide --repo-path or a connected --repo with local_path."
        )
    try:
        result = run_repair(
            bundle,
            RepairRunnerConfig(
                repo_path=effective_repo_path,
                agent_command=(
                    shlex.split(agent_command) if agent_command.strip() else []
                ),
                validation_commands=list(validation_command),
                dry_run=dry_run,
                allow_draft_pr=allow_draft_pr,
                create_draft_pr=create_draft_pr,
                branch_name=branch_name,
                repo_full_name=repo_full_name,
                github_token=cfg.github_sink.api_key,
            ),
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"status={result.status}")
    click.echo(f"planned_commands={len(result.planned_commands)}")
    if result.changed_files:
        click.echo("changed_files=" + ",".join(result.changed_files))
    if result.draft_pr_url:
        click.echo(f"draft_pr_url={result.draft_pr_url}")
    if result.error:
        raise click.ClickException(result.error)


def _repair_task_payload(task: Any) -> dict[str, Any]:
    return {
        "id": task.id,
        "public_id": task.public_id,
        "failure_id": task.failure_id,
        "source_type": task.source_type,
        "source_external_id": task.source_external_id,
        "title": task.title,
        "status": task.status,
        "likely_files": list(task.likely_files or []),
        "validation_commands": list(task.validation_commands or []),
        "branch": task.branch,
        "pr_url": task.pr_url,
        "risk_notes": task.risk_notes,
        "metadata": dict(task.metadata or {}),
        "evidence_ids": list(task.evidence_ids or []),
    }
