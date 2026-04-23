from __future__ import annotations

import json
import re
from pathlib import Path

import click

from retrace.config import load_config
from retrace.matching import score_repo_for_finding
from retrace.prompts import build_claude_code_prompt, build_codex_prompt
from retrace.reports.parser import parse_report_findings
from retrace.storage import Storage


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:64] or "finding"


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
    repo_full_name: str,
    repo_path: Path | None,
    out_dir: Path,
    config_path: Path,
) -> None:
    if bool(report_path) == bool(use_latest):
        raise click.ClickException("Provide exactly one of --report or --latest.")

    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()

    repo = store.get_github_repo(repo_full_name)
    if repo is None:
        raise click.ClickException(
            f"Repo not connected: {repo_full_name}. Run `retrace github connect --repo {repo_full_name}` first."
        )

    target_report = _latest_report(cfg.run.output_dir) if use_latest else report_path
    assert target_report is not None

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

    findings = parse_report_findings(target_report)
    if not findings:
        click.echo(f"No findings parsed from {target_report}")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    stored = 0
    generated = 0

    for idx, f in enumerate(findings, start=1):
        finding_id = store.upsert_report_finding(
            report_path=str(target_report),
            finding_hash=f.finding_hash(),
            title=f.title,
            severity=f.severity,
            category=f.category,
            session_url=f.session_url,
            evidence_text=f.evidence_text,
            distinct_id=str(getattr(f, "distinct_id", "") or ""),
            error_issue_ids=list(getattr(f, "error_issue_ids", []) or []),
            trace_ids=list(getattr(f, "trace_ids", []) or []),
            top_stack_frame=str(getattr(f, "top_stack_frame", "") or ""),
            error_tracking_url=str(getattr(f, "error_tracking_url", "") or ""),
            logs_url=str(getattr(f, "logs_url", "") or ""),
            first_error_ts_ms=int(getattr(f, "first_error_ts_ms", 0) or 0),
            last_error_ts_ms=int(getattr(f, "last_error_ts_ms", 0) or 0),
        )
        stored += 1

        candidates = []
        if effective_repo_path and effective_repo_path.exists():
            candidates = score_repo_for_finding(
                repo_path=effective_repo_path,
                title=f.title,
                category=f.category,
                evidence_text=f.evidence_text,
                top_n=8,
            )
        store.replace_code_candidates(
            finding_id=finding_id,
            repo_id=repo.id,
            candidates=[
                (c.file_path, c.symbol, c.score, json.dumps({"rationale": c.rationale}))
                for c in candidates
            ],
        )

        codex_prompt = build_codex_prompt(f, candidates)
        claude_prompt = build_claude_code_prompt(f, candidates)
        store.replace_fix_prompts(
            finding_id=finding_id,
            repo_id=repo.id,
            prompts=[
                ("codex", codex_prompt, json.dumps({"kind": "codex"})),
                ("claude_code", claude_prompt, json.dumps({"kind": "claude_code"})),
            ],
        )

        artifact = {
            "phase": "phase_2_prompt_generation",
            "status": "ok",
            "repo": repo.repo_full_name,
            "default_branch": repo.default_branch,
            "repo_path": str(effective_repo_path) if effective_repo_path else "",
            "finding_id": finding_id,
            "finding": {
                "title": f.title,
                "severity": f.severity,
                "category": f.category,
                "session_id": f.session_id,
                "session_url": f.session_url,
                "finding_hash": f.finding_hash(),
            },
            "candidates": [
                {
                    "file_path": c.file_path,
                    "score": c.score,
                    "rationale": c.rationale,
                }
                for c in candidates
            ],
            "prompt_files": {
                "codex": f"{target_report.stem}-{idx:02d}-{_slugify(f.title)}.codex.md",
                "claude_code": f"{target_report.stem}-{idx:02d}-{_slugify(f.title)}.claude.md",
            },
        }
        base = f"{target_report.stem}-{idx:02d}-{_slugify(f.title)}"
        (out_dir / f"{base}.json").write_text(json.dumps(artifact, indent=2) + "\n")
        (out_dir / f"{base}.codex.md").write_text(codex_prompt + "\n")
        (out_dir / f"{base}.claude.md").write_text(claude_prompt + "\n")
        generated += 1

    click.echo(
        f"Parsed {len(findings)} findings from {target_report}. "
        f"Stored {stored} findings. Wrote {generated} fix-prompt artifact set(s) to {out_dir}."
    )