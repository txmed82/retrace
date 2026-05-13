# STAGE: plan-c-stub — Lightweight repository indexer for GitHub code matching.
# FUTURE: Implement _iter_source_files, _build_lexical_index, _extract_symbols,
#         _detect_routes, and _index_repo for Plan C matching MVP.
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class IndexedFile:
    """A single file entry in the repository index (stub model)."""

    file_path: str
    extension: str
    exported_symbols: list[str] = field(default_factory=list)
    route_hints: list[str] = field(default_factory=list)


class RepoIndexer(Protocol):
    """Protocol for repository indexers (Plan C MVP).

    Implementations index a local repository checkout for fast code matching,
    producing lightweight metadata (file paths, symbols, routes) without
    heavy AST parsing.
    """

    def index(self, repo_path: Path) -> list[IndexedFile]: ...


__all__ = ["IndexedFile", "RepoIndexer"]
