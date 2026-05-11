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
from retrace.llm_pr_review import LLMReviewResult, llm_review
from retrace.pr_review import analyze_pr_diff, build_pr_review_comment_plan
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
    # `allow_dash=True` so the documented stdin form `--diff -` actually
    # works; without it Click rejects `-` because the path doesn't exist.
    type=click.Path(exists=True, dir_okay=False, path_type=Path, allow_dash=True),
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
    "--post-comment",
    is_flag=True,
    default=False,
    help=(
        "Post the analysis as a comment on the PR via `gh`. Requires --pr "
        "(or --repo + --pr <number>) and the `gh` CLI installed + authed."
    ),
)
@click.option(
    "--run-affected-tests/--no-run-affected-tests",
    "run_affected_tests",
    default=False,
    show_default=True,
    help=(
        "Run the tester specs that cover the affected flows. Fold the "
        "results into the PR comment summary."
    ),
)
@click.option(
    "--llm/--no-llm",
    "use_llm",
    default=None,
    help=(
        "Ask an LLM to produce a summary, walkthrough, and inline "
        "suggestions on top of the templated analysis. Defaults to ON "
        "when `config.yaml` has an LLM configured."
    ),
)
@click.option(
    "--llm-self-critique/--no-llm-self-critique",
    "llm_self_critique",
    default=False,
    show_default=True,
    help=(
        "After the LLM review, make one more LLM call to rank/dedupe "
        "the inline suggestions and risk notes. Doubles cost on PRs "
        "with overflow; cheap on quiet PRs (only runs when there's "
        "more than the suggestion/risk cap)."
    ),
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
    post_comment: bool,
    run_affected_tests: bool,
    use_llm: Optional[bool],
    llm_self_critique: bool,
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
    # Fail fast — we don't want `--run-affected-tests` to spend
    # 60 seconds before the late `--post-comment` validation rejects.
    if post_comment and not (repo and pr_number):
        raise click.UsageError(
            "--post-comment requires --pr (with a real PR reference)."
        )

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

    affected_test_results: list[dict[str, Any]] = []
    if run_affected_tests:
        affected_test_results = _run_affected_tests(
            cfg=cfg,
            analysis=analysis,
        )

    # LLM review (P0.1). Default on iff the user has configured an
    # LLM in config.yaml. Falls back to an empty result on error.
    llm_result = _maybe_llm_review(
        cfg=cfg,
        diff_text=diff_text,
        analysis=analysis,
        use_llm=use_llm,
        enable_self_critique=llm_self_critique,
        store=store,
        repo=repo,
        pr_number=pr_number,
    )

    # P0.1 follow-up: persist non-empty reviews so the *next* PR review
    # on overlapping files can fold the prior risk notes into its
    # prompt instead of re-flagging the same issue.
    if llm_result and not llm_result.is_empty and not llm_result.diff_too_large:
        _persist_llm_review(
            store=store,
            repo=repo,
            pr_number=pr_number,
            analysis=analysis,
            llm_result=llm_result,
        )

    posted_comment_url = ""
    if post_comment:
        # The fail-fast check above already validated `(repo, pr_number)`.
        posted_comment_url = _post_pr_comment(
            repo=repo,
            pr_number=pr_number,
            analysis=analysis,
            incidents_filed=incidents_filed,
            affected_test_results=affected_test_results,
            llm_result=llm_result,
        )

    if as_json:
        payload = analysis.to_dict()
        payload["incidents_filed"] = incidents_filed
        payload["affected_test_results"] = affected_test_results
        payload["posted_comment_url"] = posted_comment_url
        payload["llm_review"] = llm_result.to_dict()
        click.echo(json.dumps(payload, indent=2))
        return

    _render_human(
        analysis,
        repo=repo,
        pr_number=pr_number,
        incidents_filed=incidents_filed,
        affected_test_results=affected_test_results,
        posted_comment_url=posted_comment_url,
        llm_result=llm_result,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_pr_ref(pr_ref: str, repo_full_name: str) -> tuple[str, int]:
    """Accept full URL, `owner/repo#N`, or bare `N` (when --repo is given).

    Malformed inputs (`owner/repo#foo`, or a URL where the digit group
    somehow isn't a number) raise a clean `UsageError` rather than
    leaking a raw `ValueError` to the user.
    """
    pr_ref = (pr_ref or "").strip()
    repo_full_name = (repo_full_name or "").strip()
    if not pr_ref:
        return repo_full_name, 0

    def _to_int(raw: str) -> int:
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise click.UsageError(
                f"could not parse --pr {pr_ref!r}: pull number must be an integer"
            ) from exc

    m = _PR_URL_RE.search(pr_ref)
    if m:
        return m.group(1), _to_int(m.group(2))
    if "#" in pr_ref:
        rep, num = pr_ref.split("#", 1)
        return rep.strip(), _to_int(num)
    if pr_ref.isdigit():
        if not repo_full_name:
            raise click.UsageError(
                f"--pr {pr_ref} is a bare number; pass --repo owner/name as well."
            )
        return repo_full_name, _to_int(pr_ref)
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


def _maybe_llm_review(
    *,
    cfg: Any,
    diff_text: str,
    analysis: Any,
    use_llm: Optional[bool],
    enable_self_critique: bool = False,
    store: Optional[Storage] = None,
    repo: str = "",
    pr_number: int = 0,
) -> LLMReviewResult:
    """Run `llm_review` if the user has an LLM configured.

    `use_llm=None` means "auto": run iff `config.yaml` declares an LLM
    base URL. `True` forces ON (will error if no LLM); `False` forces
    OFF (returns an empty result).
    """
    if use_llm is False:
        return LLMReviewResult()

    llm_cfg = getattr(cfg, "llm", None)
    has_llm = bool(llm_cfg and getattr(llm_cfg, "base_url", "").strip())

    if use_llm is None and not has_llm:
        return LLMReviewResult()
    if use_llm is True and not has_llm:
        raise click.UsageError(
            "--llm requested but no LLM is configured. Run `retrace init` "
            "or set `llm.base_url` in config.yaml."
        )

    prior_summary = ""
    if store is not None and repo:
        prior_summary = _prior_review_hint(
            store=store,
            repo=repo,
            paths=[f.path for f in getattr(analysis, "changed_files", [])],
            exclude_pr_number=pr_number,
        )

    # Local import — keeps the CLI startup time when --no-llm is used.
    from retrace.llm.client import LLMClient

    try:
        with LLMClient(llm_cfg) as client:
            return llm_review(
                diff_text=diff_text,
                analysis=analysis,
                llm_client=client,
                enable_self_critique=enable_self_critique,
                prior_review_summary=prior_summary,
            )
    except Exception as exc:  # pragma: no cover - defensive
        # Don't fail the review just because the LLM is down.
        return LLMReviewResult(error=f"LLM review failed: {exc}")


def _prior_review_hint(
    *,
    store: Storage,
    repo: str,
    paths: list[str],
    exclude_pr_number: int,
) -> str:
    """Compact summary of past LLM reviews that touched these files.

    Folds the most-recent (up to 3) prior reviews' risk notes into a
    few short lines. Each line ends with `(PR #N)` so the model can see
    we're talking about a different PR, not the current one.
    """
    if not paths or not repo:
        return ""
    rows = store.list_llm_pr_reviews_for_paths(
        paths,
        repo=repo,
        exclude_pr_number=exclude_pr_number,
        limit=3,
    )
    if not rows:
        return ""
    lines: list[str] = []
    for row in rows:
        try:
            risks = list(json.loads(row["risk_notes_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            risks = []
        pr_n = int(row["pr_number"] or 0)
        for note in risks[:2]:  # up to 2 per prior review
            note = str(note or "").strip()
            if note:
                lines.append(f"- {note}  (PR #{pr_n})")
        if len(lines) >= 5:
            break
    return "\n".join(lines[:5])


def _persist_llm_review(
    *,
    store: Storage,
    repo: str,
    pr_number: int,
    analysis: Any,
    llm_result: LLMReviewResult,
) -> None:
    """Best-effort persistence — never raises into the caller."""
    try:
        paths = [f.path for f in getattr(analysis, "changed_files", [])]
        store.add_llm_pr_review(
            repo=repo,
            pr_number=pr_number,
            model=llm_result.model,
            summary=llm_result.summary,
            risk_notes=list(llm_result.risk_notes),
            suggestions=[s.to_dict() for s in llm_result.inline_suggestions],
            paths=paths,
        )
    except Exception:  # pragma: no cover - defensive
        # Persistence failure shouldn't break the review surface.
        pass


def _run_affected_tests(*, cfg: Any, analysis: Any) -> list[dict[str, Any]]:
    """Run any tester specs that cover an affected flow.

    `analysis.existing_tests` carries the spec ids we already know touch
    the changed surface; this just calls `tester.run_spec` on each. We
    swallow exceptions so one broken spec doesn't blow up the review.
    """
    from retrace.tester import (
        load_spec,
        run_spec,
        runs_dir_for_data_dir,
        specs_dir_for_data_dir,
    )

    specs_dir = specs_dir_for_data_dir(cfg.run.data_dir)
    runs_dir = runs_dir_for_data_dir(cfg.run.data_dir)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Cap at 6 *unique* specs — slicing `existing_tests[:6]` before the
    # `seen` filter could let duplicate spec_ids in the first 6 rows
    # silently drop later coverage.
    for t in analysis.existing_tests:
        spec_id = getattr(t, "spec_id", "") or ""
        if not spec_id or spec_id in seen:
            continue
        if len(seen) >= 6:
            break
        seen.add(spec_id)
        try:
            spec = load_spec(specs_dir, spec_id)
        except FileNotFoundError:
            out.append({"spec_id": spec_id, "status": "missing", "summary": "spec file not found"})
            continue
        except Exception as exc:
            # Any other spec-load failure (malformed JSON, schema drift,
            # unreadable file) must degrade per-spec, not abort the whole
            # review.
            out.append({"spec_id": spec_id, "status": "error", "summary": str(exc)})
            continue
        try:
            result = run_spec(spec=spec, runs_dir=runs_dir)
        except Exception as exc:
            out.append({"spec_id": spec_id, "status": "error", "summary": str(exc)})
            continue
        out.append(
            {
                "spec_id": spec_id,
                "spec_name": getattr(spec, "name", ""),
                "status": "pass" if getattr(result, "ok", False) else "fail",
                "summary": getattr(result, "status", "") or "",
                "run_dir": str(getattr(result, "run_dir", "") or ""),
            }
        )
    return out


def _format_comment_body(
    *,
    analysis: Any,
    incidents_filed: list[str],
    affected_test_results: list[dict[str, Any]],
    llm_result: Optional[LLMReviewResult] = None,
) -> str:
    """Render the PR comment body. Uses `build_pr_review_comment_plan` as
    the trusted summary and tacks on QA-specific additions."""
    plan = build_pr_review_comment_plan(analysis)
    lines: list[str] = []
    # LLM review goes FIRST when present — that's the meat for humans.
    if llm_result is not None and not llm_result.is_empty:
        lines.append("## Retrace LLM review")
        lines.append("")
        lines.append(llm_result.to_markdown().rstrip())
        lines.append("")
    lines.append(plan.summary_body.rstrip())
    if incidents_filed:
        lines.append("")
        lines.append(f"### Retrace QA — incidents filed ({len(incidents_filed)})")
        for pid in incidents_filed:
            lines.append(f"- `{pid}` — run `retrace qa show {pid}` locally for details")
    if affected_test_results:
        passes = sum(1 for r in affected_test_results if r.get("status") == "pass")
        fails = sum(1 for r in affected_test_results if r.get("status") == "fail")
        errors = sum(1 for r in affected_test_results if r.get("status") not in {"pass", "fail"})
        lines.append("")
        lines.append(
            f"### Retrace tests — ran {len(affected_test_results)}: "
            f"{passes} pass / {fails} fail / {errors} error"
        )
        for r in affected_test_results:
            icon = "✅" if r.get("status") == "pass" else ("❌" if r.get("status") == "fail" else "⚠️")
            name = r.get("spec_name") or r.get("spec_id", "?")
            lines.append(f"- {icon} `{r.get('spec_id', '')}` — {name}  _{r.get('status', '')}_")
    lines.append("")
    lines.append("_Posted by `retrace review --post-comment`._")
    return "\n".join(lines).rstrip() + "\n"


def _post_pr_comment(
    *,
    repo: str,
    pr_number: int,
    analysis: Any,
    incidents_filed: list[str],
    affected_test_results: list[dict[str, Any]],
    llm_result: Optional[LLMReviewResult] = None,
) -> str:
    if not shutil.which("gh"):
        raise click.ClickException(
            "`gh` is required to post a PR comment. Install it from https://cli.github.com."
        )
    body = _format_comment_body(
        analysis=analysis,
        incidents_filed=incidents_filed,
        affected_test_results=affected_test_results,
        llm_result=llm_result,
    )
    # `gh pr comment` doesn't dedupe; we leave de-duplication to the
    # GitHub-App webhook path (`publish_pr_review_comments`) which keys
    # off a marker. For ad-hoc CLI use, the user can re-edit manually.
    proc = subprocess.run(
        ["gh", "pr", "comment", str(pr_number), "--repo", repo, "--body-file", "-"],
        input=body,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise click.ClickException(
            f"gh pr comment failed: {(proc.stderr or proc.stdout).strip()}"
        )
    # `gh` prints the comment URL on stdout.
    for line in (proc.stdout or "").splitlines()[::-1]:
        line = line.strip()
        if line.startswith("https://"):
            return line
    return ""


def _render_human(
    analysis: Any,
    *,
    repo: str,
    pr_number: int,
    incidents_filed: list[str],
    affected_test_results: list[dict[str, Any]] | None = None,
    posted_comment_url: str = "",
    llm_result: Optional[LLMReviewResult] = None,
) -> None:
    header = "PR review"
    if repo and pr_number:
        header += f" — {repo}#{pr_number}"
    click.echo(header)
    click.echo("─" * 64)

    if llm_result is not None and not llm_result.is_empty:
        click.echo("Retrace LLM review:")
        if llm_result.summary:
            click.echo("  " + llm_result.summary.replace("\n", "\n  "))
        if llm_result.walkthrough:
            click.echo("")
            click.echo("  Walkthrough:")
            for w in llm_result.walkthrough[:20]:
                click.echo(f"    • {w}")
        if llm_result.inline_suggestions:
            click.echo("")
            click.echo("  Inline suggestions:")
            for s in llm_result.inline_suggestions[:20]:
                click.echo(f"    • {s.path}:{s.line} — {s.body[:120]}")
        if llm_result.risk_notes:
            click.echo("")
            click.echo("  Risk notes:")
            for r in llm_result.risk_notes[:20]:
                click.echo(f"    • {r}")
        click.echo("")

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

    if affected_test_results:
        click.echo("")
        click.echo(f"Ran {len(affected_test_results)} affected test(s):")
        for r in affected_test_results:
            icon = "✓" if r.get("status") == "pass" else ("✗" if r.get("status") == "fail" else "·")
            click.echo(f"  {icon} {r.get('spec_id', '')}  {r.get('spec_name', '')}  ({r.get('status', '')})")

    if posted_comment_url:
        click.echo("")
        click.echo(f"Posted PR comment: {posted_comment_url}")
