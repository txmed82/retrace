"""Adapt a `qa_incident` into a `RepairBundle` for `repair_runner.run_repair`.

`auto_fix.propose_fix_for_incident` historically ran a naive "shell out to
claude/codex" loop. Master's `repair_runner.run_repair` is strictly better:
it runs a real agent against a prompt, executes the project's validation
commands, captures changed files + diff, and surfaces all of that
structurally.

This module is the bridge — translate the QA-side Incident shape into the
master-side `RepairBundle` shape so `qa fix --apply <agent>` ends up
calling the same engine that powers `retrace repair run`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from retrace.qa_incidents import Incident
from retrace.repair import RepairBundle
from retrace.repo_inspection import infer_validation_commands


log = logging.getLogger(__name__)


def qa_incident_to_repair_bundle(
    inc: Incident,
    *,
    repo_path: Path | None,
    likely_files: list[str] | None = None,
    prompt_path: str | None = None,
    repo_full_name: str = "",
) -> RepairBundle:
    """Project a QA incident onto the master RepairBundle shape.

    `likely_files` comes from the auto_fix candidate scorer; pass them
    through so `repair_runner` can target validation commands sensibly.
    `prompt_path` is the on-disk fix-prompt that the worktree already
    wrote — repair_runner doesn't open it, but our consumer surfaces it
    in the PR body.
    """
    files = list(likely_files or [])
    validation_plan = [
        {"command": item.command, "reason": item.reason, "source": item.source}
        for item in infer_validation_commands(
            repo_path=repo_path if repo_path else None,
            likely_files=files,
        )
    ]
    validation_commands = [item["command"] for item in validation_plan]

    failure_summary = {
        "title": inc.title,
        "summary": inc.summary,
        "suspected_cause": inc.suspected_cause,
        "severity": inc.severity,
        "confidence": inc.confidence,
        "primary_source_kind": inc.primary_source_kind,
    }

    reproduction = {
        "expected": inc.expected_outcome,
        "actual": inc.actual_outcome,
        "steps": [
            {
                "index": step.index,
                "action": step.action,
                "description": step.description,
                "target": step.target,
                "value": step.value,
                "url": step.url,
            }
            for step in inc.reproduction
        ],
        "app_url": inc.app_url,
    }

    evidence: list[dict[str, Any]] = []
    if inc.evidence.top_stack_frame:
        evidence.append(
            {
                "type": "stack_frame",
                "value": inc.evidence.top_stack_frame,
            }
        )
    for line in inc.evidence.console_excerpts[:5]:
        evidence.append({"type": "console", "value": str(line)[:300]})
    for nf in inc.evidence.network_failures[:5]:
        if isinstance(nf, dict):
            evidence.append({"type": "network_failure", "value": nf})

    backend_context: dict[str, Any] = {}
    if prompt_path:
        backend_context["fix_prompt_path"] = prompt_path
    if repo_full_name:
        backend_context["repo_full_name"] = repo_full_name

    return RepairBundle(
        failure_id=inc.id,
        public_id=inc.public_id,
        source_type=inc.primary_source_kind,
        source_external_id=inc.public_id,
        failure_summary=failure_summary,
        evidence=evidence,
        reproduction=reproduction,
        linked_tests=[],
        backend_context=backend_context,
        likely_files=files,
        deploy_context={},
        external_thread_context={},
        validation_commands=validation_commands,
        validation_plan=validation_plan,
        prompt_injection_defenses=[],
    )


# ---------------------------------------------------------------------------
# Agent command resolution
# ---------------------------------------------------------------------------


def resolve_agent_command(name: str) -> list[str]:
    """Map the user-facing `--apply <name>` to a shell command.

    Returns an empty list when no matching agent is configured, signalling
    that we should fall back to the prompt-only PR path.
    """
    import shutil

    name = (name or "").strip().lower()
    if name in {"", "none"}:
        return []
    candidates: list[list[str]] = []
    if name in {"auto", "claude"} and shutil.which("claude"):
        candidates.append(
            ["claude", "--print", "--dangerously-skip-permissions"]
        )
    if name in {"auto", "codex"} and shutil.which("codex"):
        candidates.append(["codex", "exec", "--full-auto"])
    if candidates:
        return candidates[0]
    log.info(
        "qa_repair_adapter: no agent CLI available for --apply=%r (looked for: claude, codex)",
        name,
    )
    return []
