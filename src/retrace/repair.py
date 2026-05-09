from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REPAIR_TASK_STATUSES = {
    "open",
    "in_progress",
    "blocked",
    "ready_for_validation",
    "resolved",
    "ignored",
}


@dataclass(frozen=True)
class RepairTaskDraft:
    failure_id: str
    title: str
    source_type: str = ""
    source_external_id: str = ""
    status: str = "open"
    likely_files: list[str] = field(default_factory=list)
    prompt_artifacts: list[dict[str, Any]] = field(default_factory=list)
    validation_commands: list[str] = field(default_factory=list)
    branch: str = ""
    pr_url: str = ""
    risk_notes: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)


def normalize_repair_task_status(value: object) -> str:
    status = str(value or "open").strip().lower()
    return status if status in REPAIR_TASK_STATUSES else "open"


def repair_task_from_fix_suggestion(
    *,
    failure_id: str,
    issue_public_id: str,
    title: str,
    repo_full_name: str,
    repo_path: str,
    out_dir: Path,
    candidates: list[Any],
    prompt_files: dict[str, str],
    artifact_json: str,
    evidence_ids: list[str],
) -> RepairTaskDraft:
    likely_files = _unique_strings(
        str(getattr(candidate, "file_path", "") or "") for candidate in candidates
    )
    prompt_artifacts = [
        {
            "artifact_type": "repair_manifest",
            "path": str(out_dir / artifact_json),
            "label": "Repair prompt manifest",
            "metadata": {"repo": repo_full_name},
        }
    ]
    for agent_target, relative_path in sorted(prompt_files.items()):
        prompt_artifacts.append(
            {
                "artifact_type": "repair_prompt",
                "path": str(out_dir / relative_path),
                "label": f"{agent_target} prompt",
                "metadata": {"agent_target": agent_target, "repo": repo_full_name},
            }
        )
    return RepairTaskDraft(
        failure_id=failure_id,
        title=f"Repair {title}".strip(),
        source_type="replay_issue",
        source_external_id=issue_public_id,
        status="open",
        likely_files=likely_files,
        prompt_artifacts=prompt_artifacts,
        validation_commands=[],
        risk_notes="Review generated prompts and linked evidence before applying fixes.",
        metadata={
            "repo": repo_full_name,
            "repo_path": repo_path,
            "issue_public_id": issue_public_id,
        },
        evidence_ids=_unique_strings(evidence_ids),
    )


def _unique_strings(values: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
