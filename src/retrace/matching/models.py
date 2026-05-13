# STAGE: plan-c-stub — Matching data models for GitHub code matching.
# FUTURE: Add FindingContext, RepoIndexSnapshot, IndexedFile, MatchResult,
#         PromptTemplate, and review-state tracking models.
from __future__ import annotations

from retrace.matching.scorer import CodeCandidate  # re-export for discoverability

__all__ = ["CodeCandidate"]
