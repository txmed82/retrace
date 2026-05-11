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


def _git_clean(cwd: Path) -> bool:
    r = _run(["git", "status", "--porcelain"], cwd=cwd)
    return r.returncode == 0 and not r.stdout.strip()


def _git_current_branch(cwd: Path) -> str:
    r = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    return r.stdout.strip() if r.returncode == 0 else ""


def _checkout_branch(cwd: Path, branch: str, base: str) -> tuple[bool, str]:
    # Make sure base is up to date locally; ignore failures (offline ok).
    _run(["git", "fetch", "origin", base], cwd=cwd)
    r = _run(["git", "checkout", "-b", branch, f"origin/{base}"], cwd=cwd)
    if r.returncode != 0:
        # Fall back to local base if origin/<base> isn't there.
        r = _run(["git", "checkout", "-b", branch, base], cwd=cwd)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    return True, ""


def _commit_and_push(cwd: Path, branch: str, message: str) -> tuple[bool, str]:
    _run(["git", "add", "-A"], cwd=cwd)
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


def _try_run_agent(cwd: Path, prompt_path: Path, agent: str) -> tuple[bool, str, str]:
    """Best-effort: run a local coding agent against the prompt.

    Returns (ran, agent_used, error_or_summary). Failure is non-fatal — the
    PR still ships with the prompt for a human or a CI agent.
    """
    agent = (agent or "auto").lower()
    candidates: list[tuple[str, list[str]]] = []
    if agent in {"auto", "claude"}:
        if shutil.which("claude"):
            candidates.append((
                "claude",
                ["claude", "--print", "--dangerously-skip-permissions", f"@{prompt_path}"],
            ))
    if agent in {"auto", "codex"}:
        if shutil.which("codex"):
            candidates.append((
                "codex",
                ["codex", "exec", "--full-auto", f"Read @{prompt_path} and follow it."],
            ))
    if not candidates:
        return False, "", "no agent CLI found (looked for: claude, codex)"

    for name, cmd in candidates:
        try:
            r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=1800)
            if r.returncode == 0:
                return True, name, (r.stdout or "")[-2000:]
            # if first attempt errors, try the next
            log.warning("agent %s exited %d: %s", name, r.returncode, (r.stderr or "")[-500:])
        except Exception as exc:
            log.warning("agent %s failed: %s", name, exc)
    return False, "", "all agents returned non-zero"


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

    if not repo_path or not repo_path.exists():
        outcome.status = "error"
        outcome.error = f"repo path not found: {repo_path}"
        return outcome

    if not _git_clean(repo_path):
        outcome.status = "error"
        outcome.error = "working tree is dirty; commit or stash before requesting a fix PR"
        return outcome

    branch = f"retrace/{_sanitize_branch_part(inc.public_id)}-{_sanitize_branch_part(inc.title)}"
    outcome.branch = branch
    starting_branch = _git_current_branch(repo_path)

    ok, err = _checkout_branch(repo_path, branch, base_branch)
    if not ok:
        outcome.status = "error"
        outcome.error = f"checkout failed: {err}"
        return outcome

    try:
        # Always check the prompt into the branch so the PR is self-describing.
        pr_prompt_path = repo_path / ".retrace" / f"{inc.public_id}.fix.md"
        pr_prompt_path.parent.mkdir(parents=True, exist_ok=True)
        pr_prompt_path.write_text(prompt_md)

        applied = False
        agent_used = ""
        if apply_with_agent:
            applied, agent_used, agent_msg = _try_run_agent(
                repo_path, pr_prompt_path, apply_with_agent
            )
            outcome.applied = applied
            outcome.agent = agent_used
            if not applied:
                log.info("agent did not apply changes (%s); shipping prompt-only PR", agent_msg)

        commit_msg = f"retrace: open fix request {inc.public_id} — {inc.title[:64]}"
        if applied:
            commit_msg = f"retrace: AI fix for {inc.public_id} — {inc.title[:64]}"

        ok, err = _commit_and_push(repo_path, branch, commit_msg)
        if not ok:
            outcome.status = "error"
            outcome.error = f"commit/push failed: {err}"
            return outcome

        pr_url = ""
        if _gh_available():
            pr_title = f"Retrace fix: {inc.title[:80]} ({inc.public_id})"
            url, err = _gh_pr_create(
                cwd=repo_path,
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
                return outcome
        else:
            outcome.error = "gh not installed; branch pushed without PR"

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
        if starting_branch and starting_branch != branch:
            _run(["git", "checkout", starting_branch], cwd=repo_path)
