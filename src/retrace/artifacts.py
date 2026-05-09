from __future__ import annotations

import json
import mimetypes
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


ARTIFACT_MANIFEST_VERSION = "artifact_manifest.v1"


@dataclass(frozen=True)
class ArtifactManifestItem:
    artifact_id: str
    artifact_type: str
    path: str
    mime_type: str
    label: str
    source_failure: str = ""
    source_run: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def artifact_manifest_item(
    *,
    artifact_id: str,
    artifact_type: str,
    path: str | Path,
    label: str,
    source_failure: str = "",
    source_run: str = "",
    metadata: dict[str, Any] | None = None,
    mime_type: str = "",
) -> dict[str, Any]:
    path_text = str(path)
    return asdict(
        ArtifactManifestItem(
            artifact_id=str(artifact_id),
            artifact_type=str(artifact_type),
            path=path_text,
            mime_type=mime_type or guess_mime_type(path_text),
            label=str(label),
            source_failure=str(source_failure or ""),
            source_run=str(source_run or ""),
            metadata=dict(metadata or {}),
        )
    )


def tester_artifact_manifest_items(
    artifacts: list[dict[str, Any]],
    *,
    source_run: str,
    source_failure: str = "",
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for artifact in artifacts:
        items.append(
            artifact_manifest_item(
                artifact_id=str(artifact.get("artifact_id") or ""),
                artifact_type=str(artifact.get("artifact_type") or ""),
                path=str(artifact.get("path") or ""),
                label=str(artifact.get("label") or ""),
                source_failure=source_failure,
                source_run=source_run,
                metadata=dict(artifact.get("metadata") or {}),
            )
        )
    return items


def write_artifact_manifest(
    *,
    manifest_path: Path,
    artifacts: list[dict[str, Any]],
    source_failure: str = "",
    source_run: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": ARTIFACT_MANIFEST_VERSION,
        "source_failure": source_failure,
        "source_run": source_run,
        "artifacts": artifacts,
        "metadata": dict(metadata or {}),
    }
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return artifact_manifest_item(
        artifact_id="artifact-manifest",
        artifact_type="artifact_manifest",
        path=manifest_path,
        label="Artifact manifest",
        source_failure=source_failure,
        source_run=source_run,
        metadata={"count": len(artifacts), **dict(metadata or {})},
        mime_type="application/json",
    )


def guess_mime_type(path: str) -> str:
    if not path:
        return "application/octet-stream"
    guessed, _ = mimetypes.guess_type(path)
    if guessed:
        return guessed
    if path.endswith(".log"):
        return "text/plain"
    return "application/octet-stream"
