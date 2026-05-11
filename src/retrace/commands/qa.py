"""`retrace qa ...` — the unified QA surface across replay/UI/API.

The killer-demo flow lives here:

    retrace qa auto --repo org/name

This picks the highest-priority open QA incident, auto-generates a UI test
that reproduces it, and (if confirmed) opens a draft PR with the fix prompt
and suspected files. One command, three steps, one PR.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import click

from retrace.auto_fix import propose_fix_for_incident
from retrace.auto_repro import reproduce_incident
from retrace.config import load_config
from retrace.qa_incidents import Incident
from retrace.storage import Storage


@click.group("qa")
def qa_group() -> None:
    """Work with unified Retrace incidents (replay + UI test + API test)."""


def _open_store(config_path: Path) -> tuple[Storage, Any]:
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    return store, cfg


def _format_row(inc: Incident) -> str:
    icons = {
        "open": "○",
        "reproducing": "…",
        "reproduced": "✱",
        "not_reproduced": "?",
        "fixing": "▶",
        "fix_proposed": "→",
        "resolved": "✓",
        "ignored": "—",
    }
    icon = icons.get(inc.status, "·")
    sev = f"[{inc.severity}]"
    src = f"({inc.primary_source_kind})"
    return f"{icon} {inc.public_id}  {sev:<10} {src:<14} {inc.title}"


@qa_group.command("list")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=Path("config.yaml"))
@click.option("--status", default="", help="Filter by status (open, reproduced, ...).")
@click.option("--limit", type=int, default=25, show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
def incident_list(config_path: Path, status: str, limit: int, as_json: bool) -> None:
    """List incidents in priority order."""
    store, _ = _open_store(config_path)
    rows = store.list_qa_incidents(status=status or None, limit=limit)
    incidents = [Incident.from_row(r) for r in rows]
    if as_json:
        click.echo(json.dumps([inc.to_row() for inc in incidents], indent=2))
        return
    if not incidents:
        click.echo("No incidents.")
        return
    for inc in incidents:
        click.echo(_format_row(inc))


@qa_group.command("show")
@click.argument("incident_id")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=Path("config.yaml"))
@click.option("--json", "as_json", is_flag=True, default=False)
def incident_show(incident_id: str, config_path: Path, as_json: bool) -> None:
    """Render a single incident with its reproduction recipe and evidence."""
    store, _ = _open_store(config_path)
    row = store.get_qa_incident(incident_id)
    if row is None:
        raise click.ClickException(f"Incident not found: {incident_id}")
    inc = Incident.from_row(row)
    if as_json:
        click.echo(json.dumps(inc.to_row(), indent=2))
        return

    click.echo(f"{inc.public_id}  {inc.title}")
    click.echo(f"  severity:  {inc.severity}    confidence: {inc.confidence}    status: {inc.status}")
    click.echo(f"  source:    {inc.primary_source_kind}    affected: {inc.affected_users} user(s) / {inc.affected_count} event(s)")
    if inc.app_url:
        click.echo(f"  app:       {inc.app_url}")
    if inc.summary:
        click.echo("")
        click.echo(f"  {inc.summary}")
    if inc.suspected_cause:
        click.echo(f"  suspected: {inc.suspected_cause}")

    click.echo("")
    click.echo("  Reproduction:")
    for s in inc.reproduction:
        line = f"    {s.index + 1}. [{s.action}] {s.description}"
        if s.target:
            line += f"  target={json.dumps(s.target, separators=(',', ':'))[:80]}"
        if s.value and s.value != "<masked>":
            line += f"  value={s.value[:40]}"
        click.echo(line)
    if inc.expected_outcome:
        click.echo(f"    expected: {inc.expected_outcome}")
    if inc.actual_outcome:
        click.echo(f"    actual:   {inc.actual_outcome}")

    click.echo("")
    click.echo("  Pipeline:")
    click.echo(f"    repro: {inc.repro_status}    spec={inc.repro_spec_id or '-'}    run={inc.repro_run_id or '-'}")
    if inc.repro_summary:
        click.echo(f"      {inc.repro_summary}")
    click.echo(f"    fix:   {inc.fix_status}    repo={inc.fix_repo or '-'}    branch={inc.fix_branch or '-'}")
    if inc.fix_pr_url:
        click.echo(f"      PR: {inc.fix_pr_url}")
    if inc.fix_prompt_path:
        click.echo(f"      prompt: {inc.fix_prompt_path}")


@qa_group.command("reproduce")
@click.argument("incident_id")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=Path("config.yaml"))
@click.option("--app-url", default="", help="Override the URL of the app under test.")
@click.option("--harness-cmd", "harness_cmd", default="", help="Override the harness command template.")
@click.option("--engine", "execution_engine", type=click.Choice(["harness", "native", "auto"], case_sensitive=False), default="harness", show_default=True)
def incident_reproduce(
    incident_id: str,
    config_path: Path,
    app_url: str,
    harness_cmd: str,
    execution_engine: str,
) -> None:
    """Auto-generate a UI test that reproduces this incident, then run it."""
    store, cfg = _open_store(config_path)
    outcome = reproduce_incident(
        store=store,
        data_dir=cfg.run.data_dir,
        incident_id=incident_id,
        app_url=app_url,
        harness_command=harness_cmd,
        execution_engine=execution_engine.lower(),
    )
    click.echo(json.dumps(outcome.as_dict(), indent=2))


@qa_group.command("fix")
@click.argument("incident_id")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=Path("config.yaml"))
@click.option("--repo", "repo_full_name", required=True, help="Connected repo in org/name format.")
@click.option("--repo-path", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--base", "base_branch", default="", help="Base branch (defaults to the connected repo's default).")
@click.option("--out", "out_dir", type=click.Path(file_okay=False, path_type=Path), default=Path("./reports/fix-prompts"))
@click.option("--open-pr/--no-open-pr", default=True, show_default=True)
@click.option("--draft/--ready", "draft", default=True, show_default=True)
@click.option("--apply", "apply_with", default="", type=click.Choice(["", "auto", "claude", "codex"]), help="Optionally invoke a local coding agent to apply changes inside the branch.")
def incident_fix(
    incident_id: str,
    config_path: Path,
    repo_full_name: str,
    repo_path: Optional[Path],
    base_branch: str,
    out_dir: Path,
    draft: bool,
    apply_with: str,
    open_pr: bool,
) -> None:
    """Generate a fix prompt and (by default) open a draft PR for this incident."""
    store, _ = _open_store(config_path)
    repo = store.get_github_repo(repo_full_name)
    if repo is None:
        raise click.ClickException(
            f"Repo not connected: {repo_full_name}. Run `retrace github connect --repo {repo_full_name}` first."
        )
    effective_repo_path = repo_path or (Path(repo.local_path) if repo.local_path else None)
    if not effective_repo_path:
        raise click.ClickException(
            "No local checkout configured. Pass --repo-path or connect the repo with --local-path."
        )
    outcome = propose_fix_for_incident(
        store=store,
        incident_id=incident_id,
        repo_full_name=repo_full_name,
        repo_path=effective_repo_path,
        base_branch=base_branch or repo.default_branch or "main",
        prompts_out_dir=out_dir,
        open_pr=open_pr,
        draft=draft,
        apply_with_agent=apply_with,
    )
    click.echo(json.dumps(outcome.as_dict(), indent=2))


@qa_group.command("auto")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=Path("config.yaml"))
@click.option("--repo", "repo_full_name", required=True, help="Connected repo in org/name format.")
@click.option("--repo-path", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--base", "base_branch", default="")
@click.option("--app-url", default="")
@click.option("--engine", "execution_engine", type=click.Choice(["harness", "native", "auto"], case_sensitive=False), default="harness", show_default=True)
@click.option("--apply", "apply_with", default="", type=click.Choice(["", "auto", "claude", "codex"]))
@click.option("--draft/--ready", default=True, show_default=True)
@click.option("--no-pr", is_flag=True, default=False, help="Skip opening the PR; just produce the prompt.")
@click.option("--id", "explicit_id", default="", help="Specific incident id; otherwise picks the top open incident.")
def incident_auto(
    config_path: Path,
    repo_full_name: str,
    repo_path: Optional[Path],
    base_branch: str,
    app_url: str,
    execution_engine: str,
    apply_with: str,
    draft: bool,
    no_pr: bool,
    explicit_id: str,
) -> None:
    """The killer demo: pick top incident -> auto-generate test -> open fix PR.

    Steps:
      1. Pick the highest-priority open incident (or --id).
      2. Generate + run a UI test that reproduces it.
      3. If reproduced, score the repo, build the fix prompt, and open a
         draft PR. Optionally invoke a local agent to apply changes.
    """
    store, cfg = _open_store(config_path)

    if explicit_id:
        row = store.get_qa_incident(explicit_id)
    else:
        row = store.next_open_qa_incident()
    if row is None:
        raise click.ClickException("No open incident to work on.")
    inc = Incident.from_row(row)

    click.echo(f"→ Working on {inc.public_id}: {inc.title}")
    click.echo("Step 1/2  Reproducing with auto-generated UI test…")
    repro = reproduce_incident(
        store=store,
        data_dir=cfg.run.data_dir,
        incident_id=inc.public_id,
        app_url=app_url,
        execution_engine=execution_engine.lower(),
    )
    click.echo(f"  spec: {repro.spec_id}")
    click.echo(f"  run:  {repro.run_id}  ({repro.status}) {repro.summary}")
    if not repro.confirmed:
        click.echo("Stopping: bug did not reproduce. Inspect the run dir or refine the incident.")
        click.echo(json.dumps(repro.as_dict(), indent=2))
        return

    click.echo("")
    click.echo("Step 2/2  Building fix prompt and opening PR…")
    repo = store.get_github_repo(repo_full_name)
    if repo is None:
        raise click.ClickException(
            f"Repo not connected: {repo_full_name}. Run `retrace github connect --repo {repo_full_name}` first."
        )
    effective_repo_path = repo_path or (Path(repo.local_path) if repo.local_path else None)
    if not effective_repo_path:
        raise click.ClickException(
            "No local checkout configured. Pass --repo-path or connect the repo with --local-path."
        )

    fix = propose_fix_for_incident(
        store=store,
        incident_id=inc.public_id,
        repo_full_name=repo_full_name,
        repo_path=effective_repo_path,
        base_branch=base_branch or repo.default_branch or "main",
        prompts_out_dir=Path("./reports/fix-prompts"),
        open_pr=not no_pr,
        draft=draft,
        apply_with_agent=apply_with,
    )
    click.echo(f"  prompt: {fix.prompt_path}")
    if fix.branch:
        click.echo(f"  branch: {fix.branch}")
    if fix.pr_url:
        click.echo(f"  PR:     {fix.pr_url}")
    if fix.error:
        click.echo(f"  note:   {fix.error}")
    click.echo("")
    click.echo("Done.")
    click.echo(json.dumps({"incident": inc.public_id, "repro": repro.as_dict(), "fix": fix.as_dict()}, indent=2))
