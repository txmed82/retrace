from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from retrace.storage import SourceMapRow, Storage


_BASE64_VLQ = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_BASE64_INDEX = {char: idx for idx, char in enumerate(_BASE64_VLQ)}


@dataclass(frozen=True)
class SourceMapMatch:
    artifact_url: str
    source: str
    line: int
    column: int
    name: str = ""

    def frame_text(self, function: str = "") -> str:
        label = self.name or function
        return ":".join(str(part) for part in (self.source, label, self.line) if part)


@dataclass(frozen=True)
class SourceMapDiagnostic:
    status: str
    reason: str
    release: str
    dist: str = ""
    generated_file: str = ""
    line: int = 0
    column: int = 0
    candidate_artifacts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "status": self.status,
                "reason": self.reason,
                "release": self.release,
                "dist": self.dist,
                "generated_file": self.generated_file,
                "line": self.line,
                "column": self.column,
                "candidate_artifacts": list(self.candidate_artifacts),
            }.items()
            if value not in ("", 0, [])
        }


def upload_source_map(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    release: str,
    artifact_url: str,
    source_map: dict[str, Any],
    dist: str = "",
) -> SourceMapRow:
    source_map_id = store.upsert_source_map(
        project_id=project_id,
        environment_id=environment_id,
        release=release,
        dist=dist,
        artifact_url=artifact_url,
        source_map=source_map,
    )
    rows = store.list_source_maps(
        project_id=project_id,
        environment_id=environment_id,
        release=release,
        dist=dist,
    )
    for row in rows:
        if row.id == source_map_id:
            return row
    raise ValueError("source map upload was not persisted")


def map_stack_frame(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    release: str,
    generated_file: str,
    line: int,
    column: int = 0,
    dist: str = "",
) -> SourceMapMatch | None:
    if not release.strip() or not generated_file.strip() or line <= 0:
        return None
    rows = store.list_source_maps(
        project_id=project_id,
        environment_id=environment_id,
        release=release,
        dist=dist,
    )
    generated_path = _normalize_artifact(generated_file)
    for row in rows:
        if not _artifact_matches(generated_path, row.artifact_url, row.source_map):
            continue
        match = _lookup_mapping(row.source_map, line=line, column=max(0, column))
        if match is not None:
            source, mapped_line, mapped_column, name = match
            return SourceMapMatch(
                artifact_url=row.artifact_url,
                source=source,
                line=mapped_line,
                column=mapped_column,
                name=name,
            )
    return None


def diagnose_stack_frame_mapping(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    release: str,
    generated_file: str,
    line: int,
    column: int = 0,
    dist: str = "",
) -> SourceMapDiagnostic:
    clean_release = release.strip()
    clean_dist = dist.strip()
    clean_generated = generated_file.strip()
    if not clean_release:
        return SourceMapDiagnostic(
            status="skipped",
            reason="missing_release",
            release=clean_release,
            dist=clean_dist,
            generated_file=clean_generated,
            line=line,
            column=max(0, column),
        )
    if not clean_generated or line <= 0:
        return SourceMapDiagnostic(
            status="skipped",
            reason="invalid_stack_frame",
            release=clean_release,
            dist=clean_dist,
            generated_file=clean_generated,
            line=line,
            column=max(0, column),
        )
    rows = store.list_source_maps(
        project_id=project_id,
        environment_id=environment_id,
        release=clean_release,
        dist=clean_dist,
    )
    candidate_artifacts = tuple(_diagnostic_artifact_url(row.artifact_url) for row in rows)
    if not rows:
        return SourceMapDiagnostic(
            status="unmapped",
            reason="no_source_maps_for_release_dist",
            release=clean_release,
            dist=clean_dist,
            generated_file=clean_generated,
            line=line,
            column=max(0, column),
        )
    generated_path = _normalize_artifact(clean_generated)
    matched_rows = [
        row
        for row in rows
        if _artifact_matches(generated_path, row.artifact_url, row.source_map)
    ]
    if not matched_rows:
        return SourceMapDiagnostic(
            status="unmapped",
            reason="no_matching_artifact",
            release=clean_release,
            dist=clean_dist,
            generated_file=clean_generated,
            line=line,
            column=max(0, column),
            candidate_artifacts=candidate_artifacts,
        )
    for row in matched_rows:
        if _lookup_mapping(row.source_map, line=line, column=max(0, column)) is not None:
            return SourceMapDiagnostic(
                status="mapped",
                reason="mapped",
                release=clean_release,
                dist=clean_dist,
                generated_file=clean_generated,
                line=line,
                column=max(0, column),
                candidate_artifacts=tuple(
                    _diagnostic_artifact_url(item.artifact_url)
                    for item in matched_rows
                ),
            )
    return SourceMapDiagnostic(
        status="unmapped",
        reason="no_mapping_for_position",
        release=clean_release,
        dist=clean_dist,
        generated_file=clean_generated,
        line=line,
        column=max(0, column),
        candidate_artifacts=tuple(
            _diagnostic_artifact_url(item.artifact_url) for item in matched_rows
        ),
    )


def _lookup_mapping(
    source_map: dict[str, Any],
    *,
    line: int,
    column: int,
) -> tuple[str, int, int, str] | None:
    sources = source_map.get("sources")
    mappings = source_map.get("mappings")
    if not isinstance(sources, list) or not isinstance(mappings, str):
        return None
    names = source_map.get("names") if isinstance(source_map.get("names"), list) else []
    source_root = str(source_map.get("sourceRoot") or "").strip()
    target_line_index = max(0, line - 1)
    generated_line = 0
    previous_source = 0
    previous_original_line = 0
    previous_original_column = 0
    previous_name = 0
    best: tuple[int, int, int, int, int | None] | None = None

    for raw_line in mappings.split(";"):
        previous_generated_column = 0
        if generated_line == target_line_index:
            for segment in raw_line.split(","):
                if not segment:
                    continue
                decoded = _decode_segment(segment)
                if not decoded:
                    continue
                previous_generated_column += decoded[0]
                if len(decoded) >= 4:
                    previous_source += decoded[1]
                    previous_original_line += decoded[2]
                    previous_original_column += decoded[3]
                    if len(decoded) >= 5:
                        previous_name += decoded[4]
                        name_index: int | None = previous_name
                    else:
                        name_index = None
                    if previous_generated_column <= column:
                        best = (
                            previous_generated_column,
                            previous_source,
                            previous_original_line,
                            previous_original_column,
                            name_index,
                        )
            break
        for segment in raw_line.split(","):
            if not segment:
                continue
            decoded = _decode_segment(segment)
            if not decoded:
                continue
            previous_generated_column += decoded[0]
            if len(decoded) >= 4:
                previous_source += decoded[1]
                previous_original_line += decoded[2]
                previous_original_column += decoded[3]
                if len(decoded) >= 5:
                    previous_name += decoded[4]
        generated_line += 1

    if best is None:
        return None
    _, source_index, original_line, original_column, name_index = best
    if source_index < 0 or source_index >= len(sources):
        return None
    source = _normalize_source(str(sources[source_index]), source_root=source_root)
    if not source:
        return None
    name = ""
    if name_index is not None and 0 <= name_index < len(names):
        name = str(names[name_index] or "")
    return source, original_line + 1, original_column, name


def _decode_segment(segment: str) -> list[int]:
    values: list[int] = []
    value = 0
    shift = 0
    for char in segment:
        digit = _BASE64_INDEX.get(char)
        if digit is None:
            return []
        continuation = digit & 32
        digit &= 31
        value += digit << shift
        if continuation:
            shift += 5
            continue
        negative = value & 1
        decoded = value >> 1
        values.append(-decoded if negative else decoded)
        value = 0
        shift = 0
    return values if shift == 0 else []


def _artifact_matches(
    generated_path: str,
    artifact_url: str,
    source_map: dict[str, Any],
) -> bool:
    candidates = {_normalize_artifact(artifact_url)}
    file_value = str(source_map.get("file") or "").strip()
    if file_value:
        candidates.add(_normalize_artifact(file_value))
    return any(
        generated_path == candidate
        or generated_path.endswith("/" + candidate)
        or candidate.endswith("/" + generated_path)
        for candidate in candidates
        if candidate
    )


def _normalize_artifact(value: str) -> str:
    clean = value.strip().split("?", 1)[0].split("#", 1)[0]
    parsed = urlparse(clean)
    if parsed.path:
        clean = parsed.path
    clean = clean.replace("\\", "/").lstrip("/")
    while clean.startswith("./"):
        clean = clean[2:]
    return clean


def _diagnostic_artifact_url(value: str) -> str:
    text = str(value or "").strip()
    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        return parsed._replace(query="", fragment="").geturl()
    return text.split("?", 1)[0].split("#", 1)[0]


def _normalize_source(source: str, *, source_root: str) -> str:
    clean = source.strip().split("?", 1)[0].replace("\\", "/")
    for prefix in ("webpack://", "webpack:/"):
        if clean.startswith(prefix):
            clean = clean.removeprefix(prefix)
    clean = clean.lstrip("/")
    while clean.startswith("./"):
        clean = clean[2:]
    if source_root and not clean.startswith(source_root.strip("/")):
        clean = "/".join(part for part in (source_root.strip("/"), clean) if part)
    return clean
