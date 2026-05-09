from __future__ import annotations

import json
from pathlib import Path

from retrace.artifacts import artifact_manifest_item, write_artifact_manifest


def test_write_artifact_manifest_records_portable_artifacts(tmp_path: Path) -> None:
    artifact_path = tmp_path / "assertions.json"
    artifact_path.write_text("[]\n", encoding="utf-8")
    manifest_path = tmp_path / "artifact-manifest.json"

    manifest_artifact = write_artifact_manifest(
        manifest_path=manifest_path,
        artifacts=[
            artifact_manifest_item(
                artifact_id="assertions",
                artifact_type="assertion_results",
                path=artifact_path,
                label="Assertions",
                source_failure="flr_123",
                source_run="run_123",
            )
        ],
        source_failure="flr_123",
        source_run="run_123",
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "artifact_manifest.v1"
    assert payload["source_failure"] == "flr_123"
    assert payload["source_run"] == "run_123"
    assert payload["artifacts"][0]["mime_type"] == "application/json"
    assert payload["artifacts"][0]["path"] == str(artifact_path)
    assert manifest_artifact["artifact_type"] == "artifact_manifest"
