from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from retrace.artifacts import artifact_manifest_item, write_artifact_manifest
from retrace.matching import CodeCandidate, score_repo_for_finding
from retrace.prompts import build_claude_code_prompt, build_codex_prompt
from retrace.repair import repair_task_from_fix_suggestion
from retrace.reports.parser import ParsedFinding
from retrace.storage import GitHubRepoRow, Storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FixSuggestionArtifact:
    finding_id: int
    finding_hash: str
    title: str
    candidates: list[CodeCandidate]
    prompts: dict[str, str]
    prompt_files: dict[str, str]
    artifact_json: str
    artifact_manifest_json: str = ""
    repair_task_id: str = ""


@dataclass(frozen=True)
class FixSuggestionResult:
    source_label: str
    report_key: str
    repo_full_name: str
    repo_path: str
    stored: int
    generated: int
    out_dir: Path
    regression_counts: dict[str, int]
    artifacts: list[FixSuggestionArtifact]


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:64] or "finding"


def replay_issue_report_key(issue_public_id: str) -> str:
    return f"replay://issue/{issue_public_id}"


def parsed_finding_from_replay_issue(issue: Any) -> ParsedFinding:
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


def generate_fix_suggestions(
    *,
    store: Storage,
    repo: GitHubRepoRow,
    repo_path: Path | None,
    out_dir: Path,
    report_key: str,
    source_label: str,
    artifact_stem: str,
    findings: list[ParsedFinding],
    project_id: str = "",
    environment_id: str = "",
) -> FixSuggestionResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    stored = 0
    generated = 0
    finding_hashes: list[str] = []
    artifacts: list[FixSuggestionArtifact] = []
    effective_repo_path = repo_path if repo_path and repo_path.exists() else None

    for idx, finding in enumerate(findings, start=1):
        finding_hash = finding.finding_hash()
        finding_hashes.append(finding_hash)
        finding_id = store.upsert_report_finding(
            report_path=report_key,
            finding_hash=finding_hash,
            title=finding.title,
            severity=finding.severity,
            category=finding.category,
            session_url=finding.session_url,
            evidence_text=finding.evidence_text,
            distinct_id=str(getattr(finding, "distinct_id", "") or ""),
            error_issue_ids=list(getattr(finding, "error_issue_ids", []) or []),
            trace_ids=list(getattr(finding, "trace_ids", []) or []),
            top_stack_frame=str(getattr(finding, "top_stack_frame", "") or ""),
            error_tracking_url=str(getattr(finding, "error_tracking_url", "") or ""),
            logs_url=str(getattr(finding, "logs_url", "") or ""),
            first_error_ts_ms=int(getattr(finding, "first_error_ts_ms", 0) or 0),
            last_error_ts_ms=int(getattr(finding, "last_error_ts_ms", 0) or 0),
        )
        stored += 1

        candidates: list[CodeCandidate] = []
        if effective_repo_path is not None:
            candidates = score_repo_for_finding(
                repo_path=effective_repo_path,
                title=finding.title,
                category=finding.category,
                evidence_text=finding.evidence_text,
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

        prompts = {
            "codex": build_codex_prompt(finding, candidates),
            "claude_code": build_claude_code_prompt(finding, candidates),
        }
        store.replace_fix_prompts(
            finding_id=finding_id,
            repo_id=repo.id,
            prompts=[
                ("codex", prompts["codex"], json.dumps({"kind": "codex"})),
                (
                    "claude_code",
                    prompts["claude_code"],
                    json.dumps({"kind": "claude_code"}),
                ),
            ],
        )

        base = f"{artifact_stem}-{idx:02d}-{slugify(finding.title)}"
        prompt_files = {
            "codex": f"{base}.codex.md",
            "claude_code": f"{base}.claude.md",
        }
        artifact = {
            "phase": "phase_2_prompt_generation",
            "status": "ok",
            "repo": repo.repo_full_name,
            "default_branch": repo.default_branch,
            "repo_path": str(effective_repo_path) if effective_repo_path else "",
            "finding_id": finding_id,
            "finding": {
                "title": finding.title,
                "severity": finding.severity,
                "category": finding.category,
                "session_id": finding.session_id,
                "session_url": finding.session_url,
                "finding_hash": finding_hash,
            },
            "candidates": [
                {
                    "file_path": c.file_path,
                    "score": c.score,
                    "rationale": c.rationale,
                }
                for c in candidates
            ],
            "prompt_files": prompt_files,
        }
        artifact_json = f"{base}.json"
        (out_dir / artifact_json).write_text(
            json.dumps(artifact, indent=2) + "\n", encoding="utf-8"
        )
        (out_dir / prompt_files["codex"]).write_text(
            prompts["codex"] + "\n", encoding="utf-8"
        )
        (out_dir / prompt_files["claude_code"]).write_text(
            prompts["claude_code"] + "\n", encoding="utf-8"
        )
        manifest_items = [
            artifact_manifest_item(
                artifact_id=f"{base}-manifest-json",
                artifact_type="repair_manifest",
                path=out_dir / artifact_json,
                label="Repair prompt manifest",
                source_failure=report_key,
                metadata={"finding_id": finding_id, "repo": repo.repo_full_name},
            ),
            artifact_manifest_item(
                artifact_id=f"{base}-codex-prompt",
                artifact_type="repair_prompt",
                path=out_dir / prompt_files["codex"],
                label="Codex repair prompt",
                source_failure=report_key,
                metadata={"agent_target": "codex", "repo": repo.repo_full_name},
                mime_type="text/markdown",
            ),
            artifact_manifest_item(
                artifact_id=f"{base}-claude-prompt",
                artifact_type="repair_prompt",
                path=out_dir / prompt_files["claude_code"],
                label="Claude Code repair prompt",
                source_failure=report_key,
                metadata={"agent_target": "claude_code", "repo": repo.repo_full_name},
                mime_type="text/markdown",
            ),
        ]
        artifact_manifest_json = f"{base}.artifacts.json"
        write_artifact_manifest(
            manifest_path=out_dir / artifact_manifest_json,
            artifacts=manifest_items,
            source_failure=report_key,
            metadata={"finding_id": finding_id, "repo": repo.repo_full_name},
        )
        try:
            repair_task_id = _upsert_replay_issue_repair_task(
                store=store,
                report_key=report_key,
                repo=repo,
                repo_path=str(effective_repo_path) if effective_repo_path else "",
                out_dir=out_dir,
                finding=finding,
                candidates=candidates,
                prompt_files=prompt_files,
                artifact_json=artifact_json,
                project_id=project_id,
                environment_id=environment_id,
            )
        except Exception:
            logger.exception("failed to persist repair task for %s", report_key)
            repair_task_id = ""
        generated += 1
        artifacts.append(
            FixSuggestionArtifact(
                finding_id=finding_id,
                finding_hash=finding_hash,
                title=finding.title,
                candidates=candidates,
                prompts=prompts,
                prompt_files=prompt_files,
                artifact_json=artifact_json,
                artifact_manifest_json=artifact_manifest_json,
                repair_task_id=repair_task_id,
            )
        )

    regression = store.reconcile_regression_states(
        report_path=report_key,
        finding_hashes=finding_hashes,
    )
    regression_counts = {
        "new": sum(1 for state, _ in regression.values() if state == "new"),
        "ongoing": sum(1 for state, _ in regression.values() if state == "ongoing"),
        "regressed": sum(1 for state, _ in regression.values() if state == "regressed"),
    }
    return FixSuggestionResult(
        source_label=source_label,
        report_key=report_key,
        repo_full_name=repo.repo_full_name,
        repo_path=str(effective_repo_path) if effective_repo_path else "",
        stored=stored,
        generated=generated,
        out_dir=out_dir,
        regression_counts=regression_counts,
        artifacts=artifacts,
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


def _upsert_replay_issue_repair_task(
    *,
    store: Storage,
    report_key: str,
    repo: GitHubRepoRow,
    repo_path: str,
    out_dir: Path,
    finding: ParsedFinding,
    candidates: list[CodeCandidate],
    prompt_files: dict[str, str],
    artifact_json: str,
    project_id: str = "",
    environment_id: str = "",
) -> str:
    prefix = "replay://issue/"
    if not report_key.startswith(prefix) or not project_id or not environment_id:
        return ""
    issue_public_id = report_key.removeprefix(prefix)
    failure = store.find_failure_by_source(
        project_id=project_id,
        environment_id=environment_id,
        source_type="replay_issue",
        source_external_id=issue_public_id,
    )
    if failure is None:
        return ""
    evidence = store.list_failure_evidence(
        failure_id=failure.id,
        include_sensitive=False,
    )
    draft = repair_task_from_fix_suggestion(
        failure_id=failure.id,
        issue_public_id=issue_public_id,
        title=finding.title,
        repo_full_name=repo.repo_full_name,
        repo_path=repo_path,
        out_dir=out_dir,
        candidates=candidates,
        prompt_files=prompt_files,
        artifact_json=artifact_json,
        evidence_ids=[item.id for item in evidence],
    )
    return store.upsert_repair_task(
        failure_id=draft.failure_id,
        title=draft.title,
        source_type=draft.source_type,
        source_external_id=draft.source_external_id,
        status=draft.status,
        likely_files=draft.likely_files,
        prompt_artifacts=draft.prompt_artifacts,
        validation_commands=draft.validation_commands,
        branch=draft.branch,
        pr_url=draft.pr_url,
        risk_notes=draft.risk_notes,
        metadata=draft.metadata,
        evidence_ids=draft.evidence_ids,
    )
