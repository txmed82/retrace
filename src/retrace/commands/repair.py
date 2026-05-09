from __future__ import annotations

from pathlib import Path
import shlex

import click

from retrace.config import load_config
from retrace.repair import build_repair_bundle
from retrace.repair_runner import RepairRunnerConfig, run_repair
from retrace.storage import Storage


@click.group("repair")
def repair_group() -> None:
    """Build repair bundles and run local repair agents."""


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
    effective_repo_path = repo_path or (Path(repo.local_path) if repo and repo.local_path else None)
    if effective_repo_path is None:
        raise click.ClickException("Provide --repo-path or a connected --repo with local_path.")
    result = run_repair(
        bundle,
        RepairRunnerConfig(
            repo_path=effective_repo_path,
            agent_command=shlex.split(agent_command) if agent_command.strip() else [],
            validation_commands=list(validation_command),
            dry_run=dry_run,
            allow_draft_pr=allow_draft_pr,
            create_draft_pr=create_draft_pr,
            branch_name=branch_name,
            repo_full_name=repo_full_name,
            github_token=cfg.github_sink.api_key,
        ),
    )
    click.echo(f"status={result.status}")
    click.echo(f"planned_commands={len(result.planned_commands)}")
    if result.changed_files:
        click.echo("changed_files=" + ",".join(result.changed_files))
    if result.draft_pr_url:
        click.echo(f"draft_pr_url={result.draft_pr_url}")
    if result.error:
        raise click.ClickException(result.error)
