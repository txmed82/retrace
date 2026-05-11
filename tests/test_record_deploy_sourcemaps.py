"""`retrace api record-deploy --source-map-dir` smoke + integration test."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from retrace.commands.api import api_group
from retrace.storage import Storage


_CONFIG = """posthog:
  host: https://us.i.posthog.com
  project_id: demo
llm:
  provider: openai_compatible
  base_url: http://127.0.0.1:8080/v1
  model: x
run:
  data_dir: {data_dir}
"""


def _minimal_source_map(source_file: str = "app.ts") -> dict:
    return {
        "version": 3,
        "file": "app.js",
        "sources": [source_file],
        "names": [],
        "mappings": "AAAA",
        "sourcesContent": ["console.log('hi');\n"],
    }


def test_record_deploy_auto_uploads_source_maps(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CONFIG.format(data_dir=str(tmp_path / "data")))

    maps_dir = tmp_path / "dist" / "maps"
    maps_dir.mkdir(parents=True)
    (maps_dir / "app.js.map").write_text(json.dumps(_minimal_source_map("app.ts")))
    (maps_dir / "vendor.js.map").write_text(json.dumps(_minimal_source_map("vendor.ts")))

    runner = CliRunner()
    result = runner.invoke(
        api_group,
        [
            "record-deploy",
            "--config", str(cfg),
            "--sha", "deadbeef",
            "--source-map-dir", str(maps_dir),
            "--source-map-artifact-prefix", "https://cdn.example.com/static",
        ],
    )
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    assert payload["deploy"]["sha"] == "deadbeef"
    uploaded = payload["uploaded_source_maps"]
    assert len(uploaded) == 2
    artifact_urls = {u["artifact_url"] for u in uploaded}
    assert "https://cdn.example.com/static/app.js" in artifact_urls
    assert "https://cdn.example.com/static/vendor.js" in artifact_urls
    assert payload["skipped_source_maps"] == []

    # Confirm they made it to storage.
    store = Storage(tmp_path / "data" / "retrace.db")
    workspace = store.ensure_workspace(project_name="Default")
    rows = store.list_source_maps(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        release="deadbeef",
    )
    assert len(rows) == 2


def test_record_deploy_skips_malformed_source_maps(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CONFIG.format(data_dir=str(tmp_path / "data")))

    maps_dir = tmp_path / "dist"
    maps_dir.mkdir()
    (maps_dir / "ok.js.map").write_text(json.dumps(_minimal_source_map()))
    (maps_dir / "bad.js.map").write_text("{not json")
    (maps_dir / "wrong_shape.js.map").write_text(json.dumps([1, 2, 3]))

    runner = CliRunner()
    result = runner.invoke(
        api_group,
        [
            "record-deploy",
            "--config", str(cfg),
            "--sha", "abc",
            "--source-map-dir", str(maps_dir),
        ],
    )
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    assert len(payload["uploaded_source_maps"]) == 1
    assert len(payload["skipped_source_maps"]) == 2
    reasons = {item["reason"].split(":")[0] for item in payload["skipped_source_maps"]}
    assert "unreadable" in reasons or "not a JSON object" in {
        item["reason"] for item in payload["skipped_source_maps"]
    }


def test_record_deploy_without_source_map_dir_still_works(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CONFIG.format(data_dir=str(tmp_path / "data")))

    runner = CliRunner()
    result = runner.invoke(
        api_group,
        ["record-deploy", "--config", str(cfg), "--sha", "noop"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["uploaded_source_maps"] == []
    assert payload["skipped_source_maps"] == []
