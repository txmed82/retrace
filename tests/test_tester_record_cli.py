"""P1.4 follow-up — CLI tests for `retrace tester record` (HAR
import) and `retrace tester env` (profile management).
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from retrace.commands.tester import tester_group as tester_cli
from retrace.test_profiles import resolve_env_profile


def _write_config(tmp_path: Path, env_profiles: dict | None = None) -> Path:
    cfg = tmp_path / "config.yaml"
    body: dict = {
        "posthog": {
            "host": "https://us.i.posthog.com",
            "project_id": "",
        },
        "llm": {
            "provider": "openai_compatible",
            "base_url": "http://127.0.0.1:8080/v1",
            "model": "x",
        },
        "run": {"data_dir": str(tmp_path / "data")},
    }
    if env_profiles is not None:
        body["tester"] = {"env_profiles": env_profiles}
    cfg.write_text(yaml.safe_dump(body, sort_keys=False))
    return cfg


def _write_har(tmp_path: Path, entries: list) -> Path:
    har = {"log": {"version": "1.2", "entries": entries}}
    p = tmp_path / "capture.har"
    p.write_text(json.dumps(har))
    return p


def _har_entry(method: str, url: str, *, status: int = 200) -> dict:
    return {
        "request": {
            "method": method,
            "url": url,
            "headers": [],
            "queryString": [],
        },
        "response": {"status": status, "statusText": "OK"},
    }


# ---------------------------------------------------------------------------
# tester record (HAR import)
# ---------------------------------------------------------------------------


def test_tester_record_dry_run_does_not_write_specs(tmp_path):
    cfg = _write_config(tmp_path)
    har = _write_har(
        tmp_path,
        [
            _har_entry("GET", "https://api.example.com/v1/users"),
            _har_entry("POST", "https://api.example.com/v1/orders"),
        ],
    )
    runner = CliRunner()
    result = runner.invoke(
        tester_cli,
        ["record", "--config", str(cfg), "--har", str(har), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["summary"] == {"total_entries": 2, "kept": 2}
    methods = {s["method"] for s in payload["specs"]}
    assert methods == {"GET", "POST"}
    # No spec files written on dry-run.
    specs_dir = tmp_path / "data" / "api-tests" / "specs"
    assert not specs_dir.exists() or not list(specs_dir.glob("*.json"))


def test_tester_record_persists_specs(tmp_path):
    cfg = _write_config(tmp_path)
    har = _write_har(
        tmp_path,
        [
            _har_entry("GET", "https://api.example.com/v1/users"),
            _har_entry("POST", "https://api.example.com/v1/orders"),
        ],
    )
    runner = CliRunner()
    result = runner.invoke(
        tester_cli,
        ["record", "--config", str(cfg), "--har", str(har)],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["summary"]["created"] == 2
    assert len(payload["created"]) == 2
    specs_dir = tmp_path / "data" / "api-tests" / "specs"
    spec_files = list(specs_dir.glob("*.json"))
    assert len(spec_files) == 2


def test_tester_record_with_filters(tmp_path):
    cfg = _write_config(tmp_path)
    har = _write_har(
        tmp_path,
        [
            _har_entry("GET", "https://api.example.com/v1/users"),
            _har_entry("GET", "https://api.example.com/static/app.js"),
            _har_entry("POST", "https://api.example.com/v1/orders"),
            _har_entry("GET", "https://www.unrelated.com/x"),
        ],
    )
    runner = CliRunner()
    result = runner.invoke(
        tester_cli,
        [
            "record",
            "--config", str(cfg),
            "--har", str(har),
            "--include-host", "api.example.com",
            "--include-method", "GET",
            "--exclude-path", "/static/*",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # Only `GET /v1/users` survives all three filters.
    assert payload["summary"]["kept"] == 1
    assert payload["specs"][0]["url"].endswith("/v1/users")


def test_tester_record_invalid_har_file(tmp_path):
    cfg = _write_config(tmp_path)
    bad = tmp_path / "not.har"
    bad.write_text('{"random": "json"}')
    runner = CliRunner()
    result = runner.invoke(
        tester_cli, ["record", "--config", str(cfg), "--har", str(bad)]
    )
    assert result.exit_code != 0
    assert "does not look like a HAR" in result.output


def test_tester_record_invalid_json(tmp_path):
    cfg = _write_config(tmp_path)
    bad = tmp_path / "broken.har"
    # Has the HAR-shape sigil but is broken JSON — must surface a
    # readable error, not crash.
    bad.write_text('{"log": {entries: [garbage')
    runner = CliRunner()
    result = runner.invoke(
        tester_cli, ["record", "--config", str(cfg), "--har", str(bad)]
    )
    assert result.exit_code != 0
    assert "invalid JSON" in result.output


def test_tester_record_non_utf8_file(tmp_path):
    """Binary or unusual-encoding HAR file → clean Click error, no
    stack trace. (CodeRabbit major finding on PR #137.)"""
    cfg = _write_config(tmp_path)
    bad = tmp_path / "binary.har"
    bad.write_bytes(b"\xff\xfe\x00\x00binary garbage")
    runner = CliRunner()
    result = runner.invoke(
        tester_cli, ["record", "--config", str(cfg), "--har", str(bad)]
    )
    assert result.exit_code != 0
    assert "UTF-8" in result.output


def test_tester_record_unknown_env_profile_fails_fast(tmp_path):
    """If --env-profile references a missing profile, we surface the
    error before writing any spec files."""
    cfg = _write_config(tmp_path, env_profiles={})  # tester block, no profiles
    har = _write_har(tmp_path, [_har_entry("GET", "https://x/api/x")])
    runner = CliRunner()
    result = runner.invoke(
        tester_cli,
        [
            "record",
            "--config", str(cfg),
            "--har", str(har),
            "--env-profile", "does-not-exist",
        ],
    )
    assert result.exit_code != 0
    assert "unknown env profile" in result.output
    # No specs persisted.
    specs_dir = tmp_path / "data" / "api-tests" / "specs"
    assert not specs_dir.exists() or not list(specs_dir.glob("*.json"))


def test_tester_record_with_valid_env_profile(tmp_path):
    cfg = _write_config(
        tmp_path,
        env_profiles={
            "staging": {
                "api_base_url": "https://api.staging.example.com",
            }
        },
    )
    har = _write_har(
        tmp_path,
        [_har_entry("GET", "https://api.example.com/v1/users")],
    )
    runner = CliRunner()
    result = runner.invoke(
        tester_cli,
        [
            "record",
            "--config", str(cfg),
            "--har", str(har),
            "--env-profile", "staging",
        ],
    )
    assert result.exit_code == 0, result.output
    spec_files = list((tmp_path / "data" / "api-tests" / "specs").glob("*.json"))
    assert len(spec_files) == 1
    spec = json.loads(spec_files[0].read_text())
    assert spec["env_profile"] == "staging"


def test_tester_record_empty_har_creates_nothing(tmp_path):
    cfg = _write_config(tmp_path)
    har = _write_har(tmp_path, [])
    runner = CliRunner()
    result = runner.invoke(
        tester_cli, ["record", "--config", str(cfg), "--har", str(har)]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["summary"]["total_entries"] == 0
    assert payload["created"] == []


# ---------------------------------------------------------------------------
# tester env (profile management)
# ---------------------------------------------------------------------------


def test_tester_env_list_empty(tmp_path):
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(tester_cli, ["env", "list", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


def test_tester_env_list_returns_redacted_previews(tmp_path):
    cfg = _write_config(
        tmp_path,
        env_profiles={
            "prod": {
                "api_base_url": "https://api.example.com",
                "headers_env": "RETRACE_PROD_HEADERS",
            },
            "staging": {
                "api_base_url": "https://api.staging.example.com",
            },
        },
    )
    runner = CliRunner()
    result = runner.invoke(tester_cli, ["env", "list", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    profiles = json.loads(result.output)
    names = sorted(p["name"] for p in profiles)
    assert names == ["prod", "staging"]
    # `headers_env` value is redacted (env-var-name only, never the value).
    flat = json.dumps(profiles)
    assert "[secret-env]" in flat


def test_tester_env_show_known_profile(tmp_path):
    cfg = _write_config(
        tmp_path,
        env_profiles={
            "prod": {
                "api_base_url": "https://api.example.com",
            }
        },
    )
    runner = CliRunner()
    result = runner.invoke(
        tester_cli, ["env", "show", "--config", str(cfg), "prod"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["api_base_url"] == "https://api.example.com"


def test_tester_env_show_unknown_profile(tmp_path):
    cfg = _write_config(tmp_path, env_profiles={})
    runner = CliRunner()
    result = runner.invoke(
        tester_cli, ["env", "show", "--config", str(cfg), "nope"]
    )
    assert result.exit_code != 0
    assert "unknown env profile" in result.output


def test_tester_env_yaml_emits_paste_ready_stanza(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        tester_cli,
        [
            "env", "yaml",
            "--name", "staging",
            "--api-base-url", "https://api.staging.example.com",
            "--headers-env", "RETRACE_STAGING_HEADERS",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = yaml.safe_load(result.output)
    assert parsed == {
        "tester": {
            "env_profiles": {
                "staging": {
                    "api_base_url": "https://api.staging.example.com",
                    "headers_env": "RETRACE_STAGING_HEADERS",
                }
            }
        }
    }


def test_tester_env_yaml_round_trips_through_resolve_env_profile(tmp_path):
    """The YAML emitted by `env yaml` is a valid env profile —
    `resolve_env_profile` accepts it without complaint."""
    runner = CliRunner()
    result = runner.invoke(
        tester_cli,
        [
            "env", "yaml",
            "--name", "qa",
            "--api-base-url", "https://api.qa.example.com",
            "--app-url", "https://app.qa.example.com",
            "--override", "FEATURE_FLAG=on",
            "--override", "DEBUG=1",
        ],
    )
    assert result.exit_code == 0, result.output
    stanza = yaml.safe_load(result.output)
    defaults = stanza["tester"]
    resolved = resolve_env_profile(defaults, "qa")
    assert resolved.api_base_url == "https://api.qa.example.com"
    assert resolved.app_url == "https://app.qa.example.com"
    assert resolved.env_overrides == {"FEATURE_FLAG": "on", "DEBUG": "1"}


def test_tester_env_yaml_rejects_malformed_override():
    runner = CliRunner()
    result = runner.invoke(
        tester_cli,
        [
            "env", "yaml",
            "--name", "x",
            "--api-base-url", "https://x",
            "--override", "no_equals_sign",
        ],
    )
    assert result.exit_code != 0
    assert "KEY=VALUE" in result.output


def test_tester_env_yaml_requires_at_least_one_field():
    runner = CliRunner()
    result = runner.invoke(tester_cli, ["env", "yaml", "--name", "x"])
    assert result.exit_code != 0
    assert "Provide at least one" in result.output
