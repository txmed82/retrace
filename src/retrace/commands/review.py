"""`retrace review ...` — code-review surface on top of `pr_review.py`.

Today PR-review only lives behind the GitHub-App webhook (`github_app.py`).
This CLI exposes the same analysis for users who don't want to set up the
webhook: feed a unified diff (file or `gh pr diff`), get back an
analysis, and optionally file `qa_incidents` for the findings so they
ride the same `qa list` / `qa auto` rails as everything else.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import click

from retrace.config import load_config
from retrace.pr_review import analyze_pr_diff
from retrace.qa_incident_bridge import sync_qa_incident_from_pr_review_finding
from retrace.storage import Storage


_PR_URL_RE = re.compile(r"github\.com/([^/]+/[^/]+)/pull/(\d+)")


@click.command("review")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option(
    "--diff",
    "diff_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to a unified-diff file. Use `-` to read from stdin.",
)
@click.option(
    "--pr",
    "pr_ref",
    default="",
    help="PR reference: a full GitHub URL, `owner/repo#NUMBER`, or just NUMBER if --repo is given.",
)
@click.option(
    "--repo",
    "repo_full_name",
    default="",
    help="`owner/name` of the repo (needed for --pr when only a number is supplied).",
)
@click.option(
    "--repo-path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Local repo path to inform route detection.",
)
@click.option(
    "--project-id",
    default="",
    help="Project id for linking prior failures + filing incidents (defaults to the local workspace).",
)
@click.option(
    "--environment-id",
    default="",
    help="Environment id for linking prior failures + filing incidents.",
)
@click.option(
    "--file-incidents/--no-file-incidents",
    default=True,
    show_default=True,
    help="File a qa_incident for each finding so `retrace qa list` picks them up.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the raw analysis as JSON.",
)
def review_command(
    config_path: Path,
    diff_path: Optional[Path],
    pr_ref: str,
    repo_full_name: str,
    repo_path: Optional[Path],
    project_id: str,
    environment_id: str,
    file_incidents: bool,
    as_json: bool,
) -> None:
    """Run Retrace's PR review against a diff and (optionally) file qa_incidents.

    Examples:

    \b
      retrace review --diff /tmp/pr.diff
      gh pr diff 42 | retrace review --diff -
      retrace review --pr https://github.com/org/repo/pull/42
      retrace review --pr 42 --repo org/repo --file-incidents
    """
    if not diff_path and not pr_ref:
        raise click.UsageError("provide --diff or --pr")

    repo, pr_number = _resolve_pr_ref(pr_ref, repo_full_name)

    diff_text = _read_diff(diff_path, repo=repo, pr_number=pr_number)
    if not diff_text.strip():
        raise click.ClickException("empty diff")

    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()

    if not project_id or not environment_id:
        workspace = store.ensure_workspace(project_name="Default")
        project_id = project_id or workspace.project_id
        environment_id = environment_id or workspace.environment_id

    analysis = analyze_pr_diff(
        diff_text=diff_text,
        repo_path=repo_path,
        store=store,
        project_id=project_id,
        environment_id=environment_id,
    )

    incidents_filed: list[str] = []
    if file_incidents and repo and pr_number:
        incidents_filed = _file_incidents_for_analysis(
            store=store,
            analysis=analysis,
            repo=repo,
            pr_number=pr_number,
            project_id=project_id,
            environment_id=environment_id,
        )

    if as_json:
        payload = analysis.to_dict()
        payload["incidents_filed"] = incidents_filed
        click.echo(json.dumps(payload, indent=2))
        return

    _render_human(analysis, repo=repo, pr_number=pr_number, incidents_filed=incidents_filed)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_pr_ref(pr_ref: str, repo_full_name: str) -> tuple[str, int]:
    """Accept full URL, `owner/repo#N`, or bare `N` (when --repo is given)."""
    pr_ref = (pr_ref or "").strip()
    repo_full_name = (repo_full_name or "").strip()
    if not pr_ref:
        return repo_full_name, 0
    m = _PR_URL_RE.search(pr_ref)
    if m:
        return m.group(1), int(m.group(2))
    if "#" in pr_ref:
        rep, num = pr_ref.split("#", 1)
        return rep.strip(), int(num)
    if pr_ref.isdigit():
        if not repo_full_name:
            raise click.UsageError(
                f"--pr {pr_ref} is a bare number; pass --repo owner/name as well."
            )
        return repo_full_name, int(pr_ref)
    raise click.UsageError(f"could not parse --pr {pr_ref!r}")


def _read_diff(
    diff_path: Optional[Path],
    *,
    repo: str,
    pr_number: int,
) -> str:
    if diff_path is not None:
        if str(diff_path) == "-":
            return sys.stdin.read()
        return diff_path.read_text(encoding="utf-8")
    if not repo or not pr_number:
        raise click.UsageError("no --diff supplied and --pr is incomplete")
    if not shutil.which("gh"):
        raise click.ClickException(
            "`gh` is required to fetch a PR diff. Install it from "
            "https://cli.github.com or pass --diff <path>."
        )
    proc = subprocess.run(
        ["gh", "pr", "diff", str(pr_number), "--repo", repo],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise click.ClickException(
            f"gh pr diff failed: {(proc.stderr or proc.stdout).strip()}"
        )
    return proc.stdout


def _file_incidents_for_analysis(
    *,
    store: Storage,
    analysis: Any,
    repo: str,
    pr_number: int,
    project_id: str,
    environment_id: str,
) -> list[str]:
    """Translate each surfaced concern into a `qa_incident`."""
    out: list[str] = []

    for prior in analysis.prior_failures[:8]:
        title = f"PR #{pr_number} touches code linked to prior failure: {prior.title}"
        summary = (
            f"Prior failure {prior.public_id} ({prior.status}) matches this diff "
            f"on files: {', '.join(prior.matched_files[:5]) or '—'}."
        )
        pid = sync_qa_incident_from_pr_review_finding(
            store=store,
            project_id=project_id,
            environment_id=environment_id,
            title=title,
            summary=summary,
            repo=repo,
            pr_number=pr_number,
            files=prior.matched_files or [],
            suspected_cause=f"Regression risk against {prior.public_id}",
            severity=prior.severity or "medium",
        )
        if pid:
            out.append(pid)

    for miss in analysis.missing_tests[:8]:
        title = f"PR #{pr_number}: changed flow lacks coverage — {miss.flow}"
        summary = miss.reason
        pid = sync_qa_incident_from_pr_review_finding(
            store=store,
            project_id=project_id,
            environment_id=environment_id,
            title=title,
            summary=summary,
            repo=repo,
            pr_number=pr_number,
            files=miss.files or [],
            suspected_cause="Missing coverage for changed surface",
            severity="medium",
        )
        if pid:
            out.append(pid)

    return out


def _render_human(
    analysis: Any,
    *,
    repo: str,
    pr_number: int,
    incidents_filed: list[str],
) -> None:
    header = "PR review"
    if repo and pr_number:
        header += f" — {repo}#{pr_number}"
    click.echo(header)
    click.echo("─" * 64)

    click.echo(f"Changed files: {len(analysis.changed_files)}")
    for f in analysis.changed_files[:20]:
        added = sum(len(h.added_lines) for h in f.hunks)
        click.echo(f"  • {f.path}  (+{added})")

    if analysis.affected_flows:
        click.echo("")
        click.echo("Affected flows:")
        for flow in analysis.affected_flows[:20]:
            click.echo(f"  • [{flow.kind}] {flow.name} — {flow.reason}")

    if analysis.prior_failures:
        click.echo("")
        click.echo("Prior failures touched:")
        for prior in analysis.prior_failures[:20]:
            click.echo(
                f"  • {prior.public_id}  [{prior.severity}, {prior.status}]  {prior.title}"
            )

    if analysis.existing_tests:
        click.echo("")
        click.echo("Existing tests that cover affected flows:")
        for t in analysis.existing_tests[:20]:
            click.echo(f"  • {t.spec_id}  {t.spec_name}  ({t.coverage_state})")

    if analysis.missing_tests:
        click.echo("")
        click.echo("Missing coverage:")
        for miss in analysis.missing_tests[:20]:
            click.echo(f"  • {miss.kind}  {miss.flow}  — {miss.reason}")
            if miss.command:
                click.echo(f"      try: {miss.command}")

    if incidents_filed:
        click.echo("")
        click.echo(f"Filed {len(incidents_filed)} qa_incident(s):")
        for pid in incidents_filed:
            click.echo(f"  • {pid}   (retrace qa show {pid})")
        click.echo("")
        click.echo("Next: retrace qa auto --repo " + (repo or "<org/name>"))
