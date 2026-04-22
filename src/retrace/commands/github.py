from __future__ import annotations

from pathlib import Path

import click

from retrace.config import load_config
from retrace.storage import Storage


def _store_from_config(config_path: Path) -> Storage:
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    return store


@click.group("github")
def github_group() -> None:
    """Manage GitHub repository connections for fix suggestions."""


@github_group.command("connect")
@click.option("--repo", "repo_full_name", required=True, help="GitHub repo in org/name format.")
@click.option("--branch", "default_branch", default="main", show_default=True)
@click.option(
    "--local-path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Local checkout path for matching/scoring.",
)
@click.option("--token-env", default="GITHUB_TOKEN", show_default=True, help="Reserved for future auth checks.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
def github_connect(
    *,
    repo_full_name: str,
    default_branch: str,
    local_path: Path | None,
    token_env: str,
    config_path: Path,
) -> None:
    _ = token_env
    store = _store_from_config(config_path)
    remote_url = f"https://github.com/{repo_full_name}.git"
    store.upsert_github_repo(
        repo_full_name=repo_full_name,
        default_branch=default_branch,
        remote_url=remote_url,
        local_path=str(local_path) if local_path else "",
        provider="github",
    )
    if local_path:
        click.echo(f"Connected {repo_full_name} (branch={default_branch}, local_path={local_path})")
    else:
        click.echo(f"Connected {repo_full_name} (branch={default_branch})")


@github_group.command("list")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
def github_list(*, config_path: Path) -> None:
    store = _store_from_config(config_path)
    repos = store.list_github_repos()
    if not repos:
        click.echo("No connected repos.")
        return
    for r in repos:
        lp = f"  local_path={r.local_path}" if r.local_path else ""
        click.echo(f"- {r.repo_full_name}  branch={r.default_branch}  provider={r.provider}{lp}")


@github_group.command("disconnect")
@click.option("--repo", "repo_full_name", required=True, help="GitHub repo in org/name format.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
def github_disconnect(*, repo_full_name: str, config_path: Path) -> None:
    store = _store_from_config(config_path)
    removed = store.delete_github_repo(repo_full_name)
    if removed:
        click.echo(f"Disconnected {repo_full_name}")
    else:
        click.echo(f"Repo not found: {repo_full_name}")
