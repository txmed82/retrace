"""AI-enhanced code candidate reranking."""

from __future__ import annotations

import logging

from retrace.llm.client import LLMClient
from retrace.matching.scorer import CodeCandidate

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a software engineering expert. Your task is to rerank a list of potential culprit files based on a bug report.

You will be given:
1. A bug title and summary.
2. Structured evidence (stack traces, network failures, console logs).
3. A list of up to 50 potential candidate files found via keyword search.

Evaluate the relationship between the evidence and each file path. 
- Files appearing in the stack trace should be ranked highest.
- Files related to the failing API route or UI component mentioned in breadcrumbs should be ranked high.
- Files that match the functional domain of the bug (e.g., "auth", "checkout") should be prioritized.

Return a JSON object with a single key "ranked_files", which is a list of file paths in descending order of relevance. Include up to 12 files.
"""


def rerank_candidates_with_ai(
    *,
    llm: LLMClient,
    candidates: list[CodeCandidate],
    title: str,
    category: str,
    evidence_text: str,
    top_n: int = 8,
) -> list[CodeCandidate]:
    """Use the LLM to rerank a list of potential code candidates."""
    if not candidates:
        return []

    # Take top 50 candidates from keyword search to avoid context bloat
    pool = candidates[:50]

    user_prompt = f"""Bug Title: {title}
Category: {category}

Evidence:
{evidence_text}

Candidate Files:
"""
    for c in pool:
        user_prompt += f"- {c.file_path} (rationale: {c.rationale})\n"

    try:
        response = llm.chat_json(system=SYSTEM_PROMPT, user=user_prompt)
        ranked_paths = response.get("ranked_files") or []

        if not ranked_paths:
            return candidates[:top_n]

        # Map paths back to CodeCandidate objects
        path_to_candidate = {c.file_path: c for c in pool}

        final_candidates: list[CodeCandidate] = []
        seen_paths: set[str] = set()
        for path in ranked_paths:
            # Handle cases where LLM might return paths with leading slashes or slightly different casing
            clean_path = str(path).strip().lstrip("/")
            if clean_path in path_to_candidate and clean_path not in seen_paths:
                final_candidates.append(path_to_candidate[clean_path])
                seen_paths.add(clean_path)

        # Fill remaining slots with original top candidates if LLM returned fewer than top_n
        seen = {c.file_path for c in final_candidates}
        for c in pool:
            if len(final_candidates) >= top_n:
                break
            if c.file_path not in seen:
                final_candidates.append(c)
                seen.add(c.file_path)

        return final_candidates[:top_n]

    except Exception as exc:
        logger.warning("AI reranking failed, falling back to keyword ranking: %s", exc)
        return candidates[:top_n]
