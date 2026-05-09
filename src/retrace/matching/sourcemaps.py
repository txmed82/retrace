from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceMapIndex:
    generated_path: str
    sources: tuple[str, ...]


_STACK_PATH_RE = re.compile(
    r"([A-Za-z0-9_./-]+\.(?:m?js|tsx?|jsx?|py|go|java|rb|php))(?::\d+)?(?::\d+)?"
)


def load_source_maps(repo_path: Path) -> list[SourceMapIndex]:
    maps: list[SourceMapIndex] = []
    for path in repo_path.rglob("*.map"):
        if "node_modules" in path.parts or ".git" in path.parts:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sources = data.get("sources") if isinstance(data, dict) else None
        if not isinstance(sources, list):
            continue
        generated = _generated_path(repo_path, path, data)
        normalized_sources = [
            source
            for source in (
                _normalize_source(repo_path, path.parent, str(item)) for item in sources
            )
            if source
        ]
        if generated and normalized_sources:
            maps.append(
                SourceMapIndex(
                    generated_path=generated,
                    sources=tuple(normalized_sources),
                )
            )
    return maps


def map_stack_paths(
    evidence_text: str,
    *,
    source_maps: list[SourceMapIndex],
) -> list[str]:
    mapped: list[str] = []
    seen: set[str] = set()
    for stack_path in _stack_paths(evidence_text):
        stack_l = stack_path.lower()
        for source_map in source_maps:
            generated_l = source_map.generated_path.lower()
            if stack_l == generated_l or stack_l.endswith("/" + generated_l):
                for source in source_map.sources:
                    if source not in seen:
                        seen.add(source)
                        mapped.append(source)
    return mapped


def _stack_paths(text: str) -> list[str]:
    paths: list[str] = []
    for match in _STACK_PATH_RE.finditer(text):
        value = match.group(1).strip().lstrip("./")
        if value not in paths:
            paths.append(value)
    return paths


def _generated_path(repo_path: Path, map_path: Path, data: object) -> str:
    if isinstance(data, dict) and data.get("file"):
        file_value = str(data["file"]).strip()
        try:
            return (map_path.parent / file_value).resolve().relative_to(
                repo_path.resolve()
            ).as_posix()
        except ValueError:
            candidate = _normalize_source(repo_path, map_path.parent, file_value)
            if candidate:
                return candidate
    name = map_path.name.removesuffix(".map")
    return (map_path.parent / name).relative_to(repo_path).as_posix()


def _normalize_source(repo_path: Path, base: Path, source: str) -> str:
    clean = source.strip()
    if not clean or clean.startswith(("webpack://", "node_modules/")):
        clean = clean.removeprefix("webpack://")
    clean = clean.split("?", 1)[0].lstrip("/")
    while clean.startswith("./"):
        clean = clean[2:]
    if clean.startswith("../"):
        try:
            path = (base / clean).resolve().relative_to(repo_path.resolve())
            return path.as_posix()
        except ValueError:
            while clean.startswith("../"):
                clean = clean[3:]
    if clean.startswith("webpack:/"):
        clean = clean.split("webpack:/", 1)[-1].lstrip("/")
    if (repo_path / clean).exists() or "/" in clean:
        return clean
    return ""
