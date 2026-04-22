from __future__ import annotations

from retrace.matching.scorer import CodeCandidate
from retrace.reports.parser import ParsedFinding


def _candidate_block(candidates: list[CodeCandidate]) -> str:
    if not candidates:
        return "- No high-confidence candidates found. Start by tracing route handlers from the reproduction steps."
    return "\n".join(
        f"- `{c.file_path}` (score={c.score}; why: {c.rationale})" for c in candidates
    )


def _base_prompt(f: ParsedFinding, candidates: list[CodeCandidate]) -> str:
    return (
        f"Bug finding:\n"
        f"- Title: {f.title}\n"
        f"- Severity: {f.severity}\n"
        f"- Category: {f.category}\n"
        f"- Replay: {f.session_url}\n\n"
        f"Evidence excerpt:\n{f.evidence_text}\n\n"
        f"Likely code locations:\n{_candidate_block(candidates)}\n\n"
        "Task:\n"
        "1. Identify the root cause for unresponsive click behavior.\n"
        "2. Implement a minimal, safe fix.\n"
        "3. Add/adjust tests to prevent regression.\n"
        "4. Summarize exact changed files and why.\n"
    )


def build_codex_prompt(f: ParsedFinding, candidates: list[CodeCandidate]) -> str:
    return (
        "You are working in a local git checkout.\n"
        "Make the fix directly in code and run tests.\n\n"
        + _base_prompt(f, candidates)
        + "\nAcceptance criteria:\n"
        "- Clicking affected UI elements triggers expected action.\n"
        "- No regressions in store/home routing and checkout flow.\n"
        "- Tests pass."
    )


def build_claude_code_prompt(f: ParsedFinding, candidates: list[CodeCandidate]) -> str:
    return (
        "Act as a senior full-stack engineer. Produce a patch and validation steps.\n\n"
        + _base_prompt(f, candidates)
        + "\nOutput format:\n"
        "- Root cause\n"
        "- Patch summary\n"
        "- Exact files touched\n"
        "- Test plan and results"
    )
