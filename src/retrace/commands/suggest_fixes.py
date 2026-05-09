from __future__ import annotations

from pathlib import Path

import click

from retrace.config import load_config
from retrace.fix_suggestions import (
    generate_fix_suggestions,
    parsed_finding_from_replay_issue,
    replay_issue_report_key,
    slugify,
)
from retrace.reports.parser import parse_report_findings
from retrace.storage import Storage


def _latest_report(report_dir: Path) -> Path:
    files = sorted(
        report_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not files:
        raise click.ClickException(f"No report files found in {report_dir}")
    return files[0]


@click.command("suggest-fixes")
@click.option(
    "--report",
    "report_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--latest",
    "use_latest",
    is_flag=True,
    default=False,
    help="Use latest markdown report.",
)
@click.option(
    "--replay-issue",
    "replay_issue_id",
    default="",
    help="Generate prompts directly from a replay-backed issue public ID or row ID.",
)
@click.option(
    "--project-id", default="", help="Project ID override for --replay-issue."
)
@click.option(
    "--environment-id",
    default="",
    help="Environment ID override for --replay-issue.",
)
@click.option(
    "--repo", "repo_full_name", required=True, help="Connected repo in org/name format."
)
@click.option(
    "--repo-path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Optional local checkout path override.",
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./reports/fix-prompts"),
    show_default=True,
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
def suggest_fixes_command(
    *,
    report_path: Path | None,
    use_latest: bool,
    replay_issue_id: str,
    project_id: str,
    environment_id: str,
    repo_full_name: str,
    repo_path: Path | None,
    out_dir: Path,
    config_path: Path,
) -> None:
    source_count = sum(
        1
        for enabled in (
            bool(report_path),
            bool(use_latest),
            bool(replay_issue_id.strip()),
        )
        if enabled
    )
    if source_count != 1:
        raise click.ClickException(
            "Provide exactly one of --report, --latest, or --replay-issue."
        )

    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()

    repo = store.get_github_repo(repo_full_name)
    if repo is None:
        raise click.ClickException(
            f"Repo not connected: {repo_full_name}. Run `retrace github connect --repo {repo_full_name}` first."
        )

    effective_repo_path = repo_path or (
        Path(repo.local_path) if repo.local_path else None
    )
    if repo_path:
        store.upsert_github_repo(
            repo_full_name=repo.repo_full_name,
            default_branch=repo.default_branch,
            remote_url=repo.remote_url,
            local_path=str(repo_path),
            provider=repo.provider,
        )
        repo = store.get_github_repo(repo_full_name) or repo

    source_label = ""
    artifact_stem = ""
    effective_project_id = ""
    effective_environment_id = ""
    if replay_issue_id.strip():
        if project_id.strip() and environment_id.strip():
            effective_project_id = project_id.strip()
            effective_environment_id = environment_id.strip()
            issue = store.get_replay_issue(
                project_id=effective_project_id,
                environment_id=effective_environment_id,
                issue_id=replay_issue_id.strip(),
            )
        else:
            issue = store.find_replay_issue(replay_issue_id.strip())
            if issue is not None:
                effective_project_id = str(issue["project_id"])
                effective_environment_id = str(issue["environment_id"])
        if issue is None:
            raise click.ClickException(f"Replay issue not found: {replay_issue_id}")
        findings = [parsed_finding_from_replay_issue(issue)]
        source_label = f"replay issue {issue['public_id']}"
        artifact_stem = f"replay-{slugify(str(issue['public_id']))}"
        report_key = replay_issue_report_key(str(issue["public_id"]))
    else:
        target_report = _latest_report(cfg.run.output_dir) if use_latest else report_path
        assert target_report is not None
        findings = parse_report_findings(target_report)
        source_label = str(target_report)
        artifact_stem = target_report.stem
        report_key = str(target_report)

    if not findings:
        click.echo(f"No findings parsed from {source_label}")
        return

    result = generate_fix_suggestions(
        store=store,
        repo=repo,
        repo_path=effective_repo_path,
        out_dir=out_dir,
        report_key=report_key,
        source_label=source_label,
        artifact_stem=artifact_stem,
        findings=findings,
        project_id=effective_project_id,
        environment_id=effective_environment_id,
    )

    click.echo(
        f"Parsed {len(findings)} findings from {source_label}. "
        f"Stored {result.stored} findings. Wrote {result.generated} "
        f"fix-prompt artifact set(s) to {out_dir}. "
        f"Regression states: new={result.regression_counts['new']}, "
        f"ongoing={result.regression_counts['ongoing']}, "
        f"regressed={result.regression_counts['regressed']}."
    )
