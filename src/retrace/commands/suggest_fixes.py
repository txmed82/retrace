from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import click

from retrace.config import load_config
from retrace.matching import score_repo_for_finding
from retrace.prompts import build_claude_code_prompt, build_codex_prompt
from retrace.reports.parser import ParsedFinding, parse_report_findings
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
    "--replay-issue",
    "replay_issue_id",
    default="",
    help="Generate prompts directly from a replay-backed issue public ID or row ID.",
)
@click.option("--project-id", default="", help="Project ID override for --replay-issue.")
@click.option("--environment-id", default="", help="Environment ID override for --replay-issue.")
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
        1 for enabled in (bool(report_path), bool(use_latest), bool(replay_issue_id.strip())) if enabled
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
    if replay_issue_id.strip():
        workspace = store.ensure_workspace(project_name="Default")
        effective_project_id = project_id.strip() or workspace.project_id
        effective_environment_id = environment_id.strip() or workspace.environment_id
        issue = store.get_replay_issue(
            project_id=effective_project_id,
            environment_id=effective_environment_id,
            issue_id=replay_issue_id.strip(),
        )
        if issue is None:
            raise click.ClickException(f"Replay issue not found: {replay_issue_id}")
        findings = [_parsed_finding_from_replay_issue(issue)]
        source_label = f"replay issue {issue['public_id']}"
        artifact_stem = f"replay-{_slugify(str(issue['public_id']))}"
        report_key = f"replay://issue/{issue['public_id']}"
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

    out_dir.mkdir(parents=True, exist_ok=True)
    stored = 0
    generated = 0
    finding_hashes: list[str] = []

    for idx, f in enumerate(findings, start=1):
        f_hash = f.finding_hash()
        finding_hashes.append(f_hash)
        finding_id = store.upsert_report_finding(
            report_path=report_key,
            finding_hash=f_hash,
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
                "codex": f"{artifact_stem}-{idx:02d}-{_slugify(f.title)}.codex.md",
                "claude_code": f"{artifact_stem}-{idx:02d}-{_slugify(f.title)}.claude.md",
            },
        }
        base = f"{artifact_stem}-{idx:02d}-{_slugify(f.title)}"
        (out_dir / f"{base}.json").write_text(json.dumps(artifact, indent=2) + "\n")
        (out_dir / f"{base}.codex.md").write_text(codex_prompt + "\n")
        (out_dir / f"{base}.claude.md").write_text(claude_prompt + "\n")
        generated += 1

    regression = store.reconcile_regression_states(
        report_path=report_key,
        finding_hashes=finding_hashes,
    )
    new_count = sum(1 for state, _ in regression.values() if state == "new")
    regressed_count = sum(1 for state, _ in regression.values() if state == "regressed")
    ongoing_count = sum(1 for state, _ in regression.values() if state == "ongoing")

    click.echo(
        f"Parsed {len(findings)} findings from {source_label}. "
        f"Stored {stored} findings. Wrote {generated} fix-prompt artifact set(s) to {out_dir}. "
        f"Regression states: new={new_count}, ongoing={ongoing_count}, regressed={regressed_count}."
    )


def _parsed_finding_from_replay_issue(issue: Any) -> ParsedFinding:
    signal_summary = _json_obj(issue["signal_summary_json"])
    evidence = _json_obj(issue["evidence_json"])
    reproduction_steps = _json_list(issue["reproduction_steps_json"])
    representative_session_id = str(issue["representative_session_id"] or "")
    evidence_text = json.dumps(
        {
            "issue_public_id": str(issue["public_id"] or ""),
            "summary": str(issue["summary"] or ""),
            "likely_cause": str(issue["likely_cause"] or ""),
            "reproduction_steps": reproduction_steps,
            "signal_summary": signal_summary,
            "evidence": evidence,
        },
        sort_keys=True,
    )
    return ParsedFinding(
        title=str(issue["title"] or "Replay issue"),
        severity=str(issue["severity"] or "medium"),
        category="replay_issue",
        session_url=f"retrace://replay/{representative_session_id}",
        evidence_text=evidence_text,
        distinct_id=str(issue["distinct_id"] or "") or None,
        error_issue_ids=_json_list(issue["error_issue_ids_json"]),
        trace_ids=_json_list(issue["trace_ids_json"]),
        top_stack_frame=str(issue["top_stack_frame"] or "") or None,
        error_tracking_url=str(issue["error_tracking_url"] or "") or None,
        logs_url=str(issue["logs_url"] or "") or None,
    )


def _json_obj(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(raw: Any) -> list[str]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item)]
