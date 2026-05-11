"""Auto-fix-PR: Incident -> branch + draft PR with AI prompt.

This is the second half of the killer-demo flow:

    user bug  ->  auto-generated test  ->  AI FIX PR

We do *not* assume any specific AI agent is wired up. The flow is:

  1. Score the connected repo against the incident's symptoms to suggest
     likely culprit files.
  2. Produce a high-quality, agent-agnostic fix prompt that embeds the
     reproduction recipe, repro-test artifact paths, and candidate files.
  3. Optionally invoke a local agent CLI (`claude`, `codex`) inside the
     repo to apply changes.
  4. Open a draft PR (`gh pr create`) carrying the prompt as the body and
     the candidate files as attachments. The PR is the durable surface:
     the user (or a CI agent) can take it from there.

The output is the product: an open PR with a clear repro, clear suspects,
and a clear ask.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from retrace.qa_incidents import Incident, reproduction_prompt_for_incident
from retrace.matching.scorer import CodeCandidate, score_repo_for_finding
from retrace.storage import Storage


log = logging.getLogger(__name__)


@dataclass
class FixOutcome:
    incident_id: str
    repo: str
    branch: str
    prompt_path: str
    pr_url: str
    applied: bool
    agent: str
    candidate_files: list[str] = field(default_factory=list)
    status: str = ""           # "pr_open" | "prompt_ready" | "applied" | "error"
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "repo": self.repo,
            "branch": self.branch,
            "prompt_path": self.prompt_path,
            "pr_url": self.pr_url,
            "applied": self.applied,
            "agent": self.agent,
            "candidate_files": self.candidate_files,
            "status": self.status,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Prompt rendering.
# ---------------------------------------------------------------------------


def _candidate_block(candidates: list[CodeCandidate]) -> str:
    if not candidates:
        return "_No high-confidence candidates found. Trace from the reproduction steps backwards through the UI / routes / handlers._"
    return "\n".join(
        f"- `{c.file_path}` (score={c.score}; why: {c.rationale})"
        for c in candidates
    )


def _evidence_block(inc: Incident) -> str:
    ev = inc.evidence
    lines: list[str] = []
    if ev.top_stack_frame:
        lines.append(f"- Top stack frame: `{ev.top_stack_frame}`")
    if ev.console_excerpts:
        for c in ev.console_excerpts[:5]:
            lines.append(f"- Console: `{str(c)[:200]}`")
    if ev.network_failures:
        for nf in ev.network_failures[:5]:
            try:
                lines.append(
                    f"- Network failure: `{nf.get('method', 'GET')} {nf.get('url', '')}` "
                    f"-> `{nf.get('status', '')}`"
                )
            except Exception:
                continue
    if ev.replay_url:
        lines.append(f"- Replay: {ev.replay_url}")
    if ev.error_issue_ids:
        lines.append(f"- Error issue ids: {', '.join(ev.error_issue_ids[:5])}")
    return "\n".join(lines) or "_No structured evidence beyond the reproduction steps._"


def render_fix_prompt(
    *,
    inc: Incident,
    candidates: list[CodeCandidate],
    repro_run_dir: str = "",
    repro_log_path: str = "",
) -> str:
    """Render the agent-agnostic fix prompt as Markdown."""
    sections: list[str] = []
    sections.append(f"# Retrace fix request — {inc.public_id}\n")
    sections.append("## Bug\n")
    sections.append(f"**Title:** {inc.title}\n")
    if inc.summary:
        sections.append(f"**What users see:** {inc.summary}\n")
    if inc.suspected_cause:
        sections.append(f"**Suspected cause:** {inc.suspected_cause}\n")
    sections.append(f"**Severity:** {inc.severity}\n")
    sections.append(f"**Confidence:** {inc.confidence}\n")
    sections.append(f"**Affected users (observed):** {inc.affected_users}\n")
    sections.append("")

    sections.append("## Reproduction (auto-generated test)\n")
    sections.append("```")
    sections.append(reproduction_prompt_for_incident(inc))
    sections.append("```")
    if repro_run_dir:
        sections.append(f"\nRetrace ran this test for you. Artifacts: `{repro_run_dir}`")
    if repro_log_path:
        sections.append(f"Harness log: `{repro_log_path}`")
    sections.append("")

    sections.append("## Evidence\n")
    sections.append(_evidence_block(inc))
    sections.append("")

    sections.append("## Likely code locations\n")
    sections.append(_candidate_block(candidates))
    sections.append("")

    sections.append("## Task")
    sections.append("")
    sections.append("1. Identify the root cause. Confirm it against the reproduction recipe above.")
    sections.append("2. Apply the smallest safe fix.")
    sections.append("3. Add or update an automated test that would have caught this. The Retrace-generated test is a starting point — promote it or add a unit/integration test, whichever fits the code.")
    sections.append("4. Run the project's test suite locally.")
    sections.append("5. In the PR description, summarize: root cause, files changed, test added, and how to verify.")
    sections.append("")
    sections.append("## Acceptance criteria")
    sections.append("")
    sections.append("- The reproduction recipe no longer triggers the failure.")
    sections.append("- New test fails before the fix, passes after.")
    sections.append("- No unrelated changes.")
    sections.append("")
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Git / gh helpers.
# ---------------------------------------------------------------------------


def _sanitize_branch_part(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9-]+", "-", (text or "").strip().lower()).strip("-")
    return s[:48] or "fix"


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _local_branch_exists(cwd: Path, branch: str) -> bool:
    r = _run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=cwd)
    return r.returncode == 0


def _worktree_dir_for(repo_path: Path, branch: str) -> Path:
    """Pick a worktree path adjacent to (but outside) the repo.

    Sibling of the repo so paths stay short on filesystems with limits, and
    deterministic for the given (repo, branch) pair so manual cleanup is
    easy.
    """
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", branch).strip("-") or "fix"
    return repo_path.parent / f".retrace-worktree-{repo_path.name}-{safe}"


def _add_worktree(repo_path: Path, branch: str, base: str) -> tuple[Optional[Path], str]:
    """Materialise a worktree on `branch` (creating the branch if missing).

    The user's main checkout is never disturbed. We make a best-effort
    `git fetch origin <base>` first so the worktree starts from up-to-date
    code when online; offline runs fall back to the local base ref.
    """
    _run(["git", "fetch", "origin", base], cwd=repo_path)
    worktree = _worktree_dir_for(repo_path, branch)

    # If a previous run left the worktree behind, reuse it cleanly.
    if worktree.exists():
        prune = _run(["git", "worktree", "remove", "--force", str(worktree)], cwd=repo_path)
        # `prune` may fail if git has lost track of it; force removal in that
        # case so we start from a known-clean state.
        if prune.returncode != 0 and worktree.exists():
            import shutil
            shutil.rmtree(worktree, ignore_errors=True)
        _run(["git", "worktree", "prune"], cwd=repo_path)

    if _local_branch_exists(repo_path, branch):
        r = _run(["git", "worktree", "add", str(worktree), branch], cwd=repo_path)
    else:
        # Try origin/<base>; fall back to the local base ref.
        r = _run(
            ["git", "worktree", "add", "-b", branch, str(worktree), f"origin/{base}"],
            cwd=repo_path,
        )
        if r.returncode != 0:
            r = _run(
                ["git", "worktree", "add", "-b", branch, str(worktree), base],
                cwd=repo_path,
            )
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip()
    return worktree, ""


def _remove_worktree(repo_path: Path, worktree: Path) -> None:
    if worktree.exists():
        _run(["git", "worktree", "remove", "--force", str(worktree)], cwd=repo_path)
        if worktree.exists():
            import shutil
            shutil.rmtree(worktree, ignore_errors=True)
    _run(["git", "worktree", "prune"], cwd=repo_path)


def _commit_and_push(cwd: Path, branch: str, message: str) -> tuple[bool, str]:
    """Stage everything, commit if there's anything new, then push.

    The fix flow is intentionally re-runnable, so a repeat run with no
    changes must succeed silently rather than fail at `git commit` (which
    exits non-zero on "nothing to commit").
    """
    _run(["git", "add", "-A"], cwd=cwd)
    status = _run(["git", "status", "--porcelain"], cwd=cwd)
    if status.stdout.strip():
        r = _run(["git", "commit", "-m", message], cwd=cwd)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout).strip()
    r = _run(["git", "push", "-u", "origin", branch], cwd=cwd)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    return True, ""


def _gh_available() -> bool:
    return bool(shutil.which("gh"))


def _gh_pr_create(
    *,
    cwd: Path,
    title: str,
    body_path: Path,
    base: str,
    draft: bool,
) -> tuple[Optional[str], str]:
    args = ["gh", "pr", "create", "--title", title, "--body-file", str(body_path), "--base", base]
    if draft:
        args.append("--draft")
    r = _run(args, cwd=cwd)
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip()
    # `gh pr create` prints the URL on stdout (last line).
    url = ""
    for line in out.splitlines()[::-1]:
        line = line.strip()
        if line.startswith("https://"):
            url = line
            break
    return url or out, ""


# ---------------------------------------------------------------------------
# Optional local agent invocation.
# ---------------------------------------------------------------------------


def _run_agent_via_repair_runner(
    *,
    worktree: Path,
    inc: "Incident",
    candidates: list[CodeCandidate],
    prompt_path: Path,
    repo_full_name: str,
    apply_with_agent: str,
) -> tuple[bool, str, str, Any]:
    """Run the local coding agent through `repair_runner.run_repair`.

    Returns ``(applied, agent_used, summary, repair_result)``. We delegate
    to repair_runner so the QA fix flow gets validation-command execution
    and a structured `RepairRunResult` (changed_files + diff) for free —
    the prior bespoke `_try_run_agent` only knew "did it exit 0".

    Failure is non-fatal: the worktree path still ships a prompt-only
    branch, just without applied changes.
    """
    from retrace.qa_repair_adapter import (
        qa_incident_to_repair_bundle,
        resolve_agent_command,
    )
    from retrace.repair_runner import RepairRunnerConfig, run_repair

    agent_command = resolve_agent_command(apply_with_agent)
    if not agent_command:
        return False, "", "no agent CLI found (looked for: claude, codex)", None
    agent_name = agent_command[0]

    bundle = qa_incident_to_repair_bundle(
        inc,
        repo_path=worktree,
        likely_files=[c.file_path for c in candidates],
        prompt_path=str(prompt_path),
        repo_full_name=repo_full_name,
    )
    cfg = RepairRunnerConfig(
        repo_path=worktree,
        agent_command=agent_command,
        validation_commands=bundle.validation_commands,
        dry_run=False,
        allow_draft_pr=False,
        create_draft_pr=False,
        repo_full_name=repo_full_name,
    )
    try:
        result = run_repair(bundle, cfg)
    except Exception as exc:
        log.warning("repair_runner failed: %s", exc)
        return False, agent_name, f"repair_runner errored: {exc}", None

    if result.status == "applied" and result.changed_files:
        return True, agent_name, result.diff[-2000:] if result.diff else "applied", result

    summary = result.error or result.status
    return False, agent_name, summary, result


def _render_repair_appendix(repair_result: Any) -> str:
    """Format the validation + diff summary appended to the PR prompt."""
    lines: list[str] = []
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Repair runner output")
    lines.append("")
    lines.append(f"- Status: `{repair_result.status}`")
    if getattr(repair_result, "agent_result", None) is not None:
        ar = repair_result.agent_result
        lines.append(f"- Agent exit: `{ar.returncode}`")
    if getattr(repair_result, "changed_files", None):
        lines.append(f"- Changed files: {len(repair_result.changed_files)}")
        for f in repair_result.changed_files[:25]:
            lines.append(f"  - `{f}`")
    if getattr(repair_result, "tests_run", None):
        lines.append("- Validation commands:")
        for t in repair_result.tests_run[:10]:
            status = "✓" if t.returncode == 0 else "✗"
            lines.append(f"  - {status} `{t.command}` (exit {t.returncode})")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration.
# ---------------------------------------------------------------------------


def propose_fix_for_incident(
    *,
    store: Storage,
    incident_id: str,
    repo_full_name: str,
    repo_path: Path,
    base_branch: str = "main",
    prompts_out_dir: Optional[Path] = None,
    open_pr: bool = True,
    draft: bool = True,
    apply_with_agent: str = "",   # "" | "auto" | "claude" | "codex"
) -> FixOutcome:
    """Build the fix PR for a single incident.

    Even when `open_pr` is False or `gh` is missing, we always produce a
    prompt file on disk so the user can pipe it to their agent of choice.
    """
    row = store.get_qa_incident(incident_id)
    if row is None:
        raise ValueError(f"incident not found: {incident_id}")
    inc = Incident.from_row(row)

    candidates: list[CodeCandidate] = []
    if repo_path and repo_path.exists():
        candidates = score_repo_for_finding(
            repo_path=repo_path,
            title=inc.title,
            category=inc.primary_source_kind,
            evidence_text="\n".join(
                [inc.summary, inc.suspected_cause, inc.actual_outcome, inc.evidence.top_stack_frame or ""]
            ),
            top_n=8,
        )

    prompt_md = render_fix_prompt(inc=inc, candidates=candidates)

    out_dir = prompts_out_dir or (Path("reports") / "fix-prompts")
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = out_dir / f"{inc.public_id}.fix.md"
    prompt_file.write_text(prompt_md)

    outcome = FixOutcome(
        incident_id=inc.public_id,
        repo=repo_full_name,
        branch="",
        prompt_path=str(prompt_file),
        pr_url="",
        applied=False,
        agent="",
        candidate_files=[c.file_path for c in candidates],
        status="prompt_ready",
    )
    store.update_qa_incident_state(
        inc.public_id,
        fix_status="prompt_ready",
        fix_repo=repo_full_name,
        fix_prompt_path=str(prompt_file),
    )

    if not open_pr:
        return outcome

    def _persist_error(reason: str, branch_name: str = "") -> None:
        """Mirror an error outcome back to storage so `qa show` is honest."""
        store.update_qa_incident_state(
            inc.public_id,
            fix_status="error",
            fix_repo=repo_full_name,
            fix_branch=branch_name or None,
            fix_prompt_path=str(prompt_file),
        )

    if not repo_path or not repo_path.exists():
        outcome.status = "error"
        outcome.error = f"repo path not found: {repo_path}"
        _persist_error(outcome.error)
        return outcome

    branch = f"retrace/{_sanitize_branch_part(inc.public_id)}-{_sanitize_branch_part(inc.title)}"
    outcome.branch = branch

    # Use a git worktree so the user's checked-out branch and working tree
    # are never touched. This removes the "clean tree required" constraint
    # entirely and makes repeat runs idempotent.
    worktree, err = _add_worktree(repo_path, branch, base_branch)
    if worktree is None:
        outcome.status = "error"
        outcome.error = f"worktree add failed: {err}"
        _persist_error(outcome.error, branch_name=branch)
        return outcome

    try:
        # Always check the prompt into the branch so the PR is self-describing.
        pr_prompt_path = worktree / ".retrace" / f"{inc.public_id}.fix.md"
        pr_prompt_path.parent.mkdir(parents=True, exist_ok=True)
        pr_prompt_path.write_text(prompt_md)

        applied = False
        agent_used = ""
        repair_result = None
        if apply_with_agent:
            applied, agent_used, agent_msg, repair_result = _run_agent_via_repair_runner(
                worktree=worktree,
                inc=inc,
                candidates=candidates,
                prompt_path=pr_prompt_path,
                repo_full_name=repo_full_name,
                apply_with_agent=apply_with_agent,
            )
            outcome.applied = applied
            outcome.agent = agent_used
            if not applied:
                log.info("agent did not apply changes (%s); shipping prompt-only PR", agent_msg)

        commit_msg = f"retrace: open fix request {inc.public_id} — {inc.title[:64]}"
        if applied:
            commit_msg = f"retrace: AI fix for {inc.public_id} — {inc.title[:64]}"
            # Append validation-command summary to the prompt file so the
            # PR body documents what we ran.
            if repair_result is not None:
                pr_prompt_path.write_text(
                    prompt_md + _render_repair_appendix(repair_result)
                )

        ok, err = _commit_and_push(worktree, branch, commit_msg)
        if not ok:
            outcome.status = "error"
            outcome.error = f"commit/push failed: {err}"
            _persist_error(outcome.error, branch_name=branch)
            return outcome

        pr_url = ""
        if _gh_available():
            pr_title = f"Retrace fix: {inc.title[:80]} ({inc.public_id})"
            url, err = _gh_pr_create(
                cwd=worktree,
                title=pr_title,
                body_path=pr_prompt_path,
                base=base_branch,
                draft=draft,
            )
            if url:
                pr_url = url
            elif err:
                outcome.status = "error"
                outcome.error = f"gh pr create failed: {err}"
                _persist_error(outcome.error, branch_name=branch)
                return outcome
        else:
            outcome.error = (
                "gh not installed; branch pushed. Open a PR manually or install "
                "https://cli.github.com and re-run `retrace qa fix`."
            )

        outcome.pr_url = pr_url
        outcome.status = "pr_open" if pr_url else ("applied" if applied else "prompt_ready")
        store.update_qa_incident_state(
            inc.public_id,
            fix_status=outcome.status,
            fix_repo=repo_full_name,
            fix_branch=branch,
            fix_pr_url=pr_url,
            fix_prompt_path=str(prompt_file),
            status="fix_proposed" if pr_url else None,
        )
        return outcome
    finally:
        # Always clean up the worktree, even on early return paths above.
        _remove_worktree(repo_path, worktree)
