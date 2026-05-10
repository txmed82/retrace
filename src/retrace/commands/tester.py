from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import click
import yaml

from retrace.api_testing import (
    api_runs_dir_for_data_dir,
    api_specs_dir_for_data_dir,
    create_api_spec,
    list_api_specs,
    load_api_spec,
    persist_api_failure,
    run_api_spec,
)
from retrace.config import load_config
from retrace.evidence import EvidenceItem, evidence_dedupe_key
from retrace.failures import canonical_failure_from_harness_run
from retrace.openapi_import import import_openapi_specs
from retrace.replay_specs import (
    generate_api_spec_from_replay_issue,
    generate_spec_from_replay_issue,
)
from retrace.storage import Storage
from retrace.test_profiles import (
    apply_api_profiles,
    resolve_auth_profile,
    resolve_env_profile,
    validate_profiles,
)
from retrace.tester import (
    DEFAULT_APP_URL,
    DEFAULT_HARNESS_COMMAND,
    create_spec,
    enqueue_spec_run,
    list_specs,
    load_run_summaries,
    load_spec,
    now_iso,
    queue_dir_for_data_dir,
    run_queued_spec_once,
    run_spec,
    runs_dir_for_data_dir,
    save_spec,
    specs_dir_for_data_dir,
    validate_spec,
)


@click.group("tester")
def tester_group() -> None:
    """Browser Harness-first local UI tester workflows."""


def _tester_defaults(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    raw = yaml.safe_load(config_path.read_text()) or {}
    return (raw.get("tester") or {}) if isinstance(raw, dict) else {}


@tester_group.command("profiles")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
def tester_profiles(config_path: Path) -> None:
    """Validate shared auth/env profiles and print a redacted preview."""
    try:
        payload = validate_profiles(_tester_defaults(config_path))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@tester_group.command("review-spec")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option(
    "--kind",
    type=click.Choice(["ui", "api"], case_sensitive=False),
    default="ui",
    show_default=True,
)
@click.argument("spec_id")
def tester_review_spec(config_path: Path, kind: str, spec_id: str) -> None:
    """Print review/edit metadata for a generated UI or API test spec."""
    cfg = load_config(config_path)
    if kind.lower() == "api":
        spec = load_api_spec(api_specs_dir_for_data_dir(cfg.run.data_dir), spec_id)
        payload = {
            "kind": "api",
            "spec_id": spec.spec_id,
            "name": spec.name,
            "method": spec.method,
            "url": spec.url,
            "route_path": _route_path_for_review(spec.url),
            "auth_profile": spec.auth_profile,
            "env_profile": spec.env_profile,
            "request_count": len(spec.steps) if spec.steps else 1,
            "steps": spec.steps,
            "assertions": {
                "expected_status": spec.expected_status,
                "json": spec.json_assertions,
                "schema": spec.schema_assertions,
            },
            "api_regression": spec.fixtures.get("api_regression", {}),
            "fixture_notes": spec.fixtures.get("fixture_notes", []),
        }
    else:
        spec = load_spec(specs_dir_for_data_dir(cfg.run.data_dir), spec_id)
        generation = spec.fixtures.get("generation", {})
        payload = {
            "kind": "ui",
            "spec_id": spec.spec_id,
            "name": spec.name,
            "app_url": spec.app_url,
            "auth_profile": spec.auth_profile,
            "execution_engine": spec.execution_engine,
            "quality": generation.get("quality", {}),
            "review": generation.get("review", {}),
            "steps": spec.exact_steps,
            "assertions": spec.assertions,
            "known_gaps": generation.get("known_gaps", []),
            "api_regression_candidate": generation.get("api_regression_candidate", {}),
        }
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


def _route_path_for_review(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return parsed.path or url


def _json_option(value: str, *, label: str, default: Any) -> Any:
    raw = value.strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"{label} must be valid JSON: {exc}") from exc


def _auth_profile(defaults: dict[str, Any], name: str) -> dict[str, Any]:
    if not name:
        return {}
    try:
        resolve_auth_profile(defaults, name)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    profiles = defaults.get("auth_profiles") or {}
    return dict(profiles.get(name) or {})


def _profile_setup_steps(profile: dict[str, Any]) -> list[dict[str, Any]]:
    steps = profile.get("auth_setup_steps", profile.get("setup_steps", []))
    if steps in (None, ""):
        return []
    if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
        raise click.ClickException("auth profile setup_steps must be a list of objects")
    return [dict(step) for step in steps]


def _profile_browser_settings(profile: dict[str, Any]) -> dict[str, Any]:
    settings = profile.get("browser_settings") or {}
    if not isinstance(settings, dict):
        raise click.ClickException("auth profile browser_settings must be an object")
    return dict(settings)


def _env_profile(defaults: dict[str, Any], name: str) -> dict[str, Any]:
    if not name:
        return {}
    try:
        resolve_env_profile(defaults, name)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    profiles = defaults.get("env_profiles") or {}
    return dict(profiles.get(name) or {})


def _apply_api_profiles(
    spec: Any,
    *,
    defaults: dict[str, Any],
    auth_profile_name: str = "",
    env_profile_name: str = "",
) -> Any:
    try:
        return apply_api_profiles(
            spec,
            defaults=defaults,
            auth_profile_name=auth_profile_name,
            env_profile_name=env_profile_name,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _single_failure_test_link_id(store: Storage, spec_id: str) -> str:
    links = store.list_failure_test_links(spec_id=spec_id, limit=2)
    return links[0].id if len(links) == 1 else ""


def _persist_harness_failure(
    *,
    store: Storage,
    result: Any,
    spec_name: str,
) -> dict[str, str]:
    workspace = store.ensure_workspace(project_name="Default")
    failure = canonical_failure_from_harness_run(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        run_result=result,
        spec_name=spec_name,
    )
    failure_id, _evidence_ids, repair_task_id = (
        store.upsert_failure_with_evidence_and_repair_task(
            failure=failure,
            evidence_items=_harness_evidence_items(failure_id="", result=result),
            repair_task={
                "title": f"Repair harness failure: {spec_name or result.spec_id}",
                "source_type": "test_run",
                "source_external_id": failure.source_external_id,
                "status": "open",
                "prompt_artifacts": list(getattr(result, "artifacts", []) or []),
                "validation_commands": [
                    f"retrace tester run {result.spec_id} --retries 0"
                ],
                "risk_notes": "Review harness artifacts before applying a repair.",
                "metadata": {
            "run_id": result.run_id,
            "spec_id": result.spec_id,
            "failure_classification": result.failure_classification,
                },
            },
        )
    )
    store.upsert_failure_test_link(
        failure_id=failure_id,
        spec_id=result.spec_id,
        spec_name=spec_name,
        source="test_run",
    )
    return {"failure_id": failure_id, "repair_task_id": repair_task_id}


def _harness_evidence_items(*, failure_id: str, result: Any) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    source = f"test_run:{result.run_id}"
    for artifact in list(getattr(result, "artifacts", []) or []):
        artifact_type = str(artifact.get("artifact_type") or "")
        evidence_type = _artifact_evidence_type(artifact_type)
        if not evidence_type:
            continue
        payload = {
            "run_id": result.run_id,
            "spec_id": result.spec_id,
            "artifact_id": str(artifact.get("artifact_id") or ""),
            "artifact_type": artifact_type,
            "label": str(artifact.get("label") or ""),
            "metadata": dict(artifact.get("metadata") or {}),
        }
        artifact_path = str(artifact.get("path") or "")
        items.append(
            EvidenceItem(
                failure_id=failure_id,
                evidence_type=evidence_type,
                occurred_at_ms=0,
                source=source,
                redaction_state="redacted",
                payload=payload,
                artifact_path=artifact_path,
                dedupe_key=evidence_dedupe_key(
                    failure_id=failure_id,
                    evidence_type=evidence_type,
                    source=source,
                    occurred_at_ms=0,
                    payload=payload,
                ),
            )
        )
    return items


def _artifact_evidence_type(artifact_type: str) -> str:
    return {
        "log": "test_transcript",
        "browser_harness_output": "dom_snapshot",
        "browser_harness_steps": "test_transcript",
        "console_output": "console_log",
        "network_output": "network_request",
        "screenshot": "screenshot",
    }.get(artifact_type, "")


@tester_group.command("create")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--name", required=True, help="Friendly test name.")
@click.option(
    "--prompt",
    required=False,
    default="",
    help="Task prompt for Browser Harness (empty allowed for suite explore).",
)
@click.option("--app-url", default="", help="App URL override.")
@click.option("--start-cmd", default="", help="Local app startup command.")
@click.option("--harness-cmd", default="", help="Harness command template.")
@click.option(
    "--mode",
    type=click.Choice(["describe", "explore_suite"], case_sensitive=False),
    default="describe",
    show_default=True,
)
@click.option("--auth-required/--no-auth-required", default=False, show_default=True)
@click.option(
    "--auth-mode",
    type=click.Choice(["none", "form", "jwt", "headers"], case_sensitive=False),
    default="none",
    show_default=True,
)
@click.option("--auth-login-url", default="")
@click.option("--auth-username", default="")
@click.option("--auth-password-env", default="RETRACE_TESTER_AUTH_PASSWORD")
@click.option("--auth-jwt-env", default="RETRACE_TESTER_AUTH_JWT")
@click.option("--auth-headers-env", default="RETRACE_TESTER_AUTH_HEADERS")
@click.option("--auth-profile", default="", help="Reusable auth profile from config.")
@click.option(
    "--engine",
    "execution_engine",
    type=click.Choice(
        ["harness", "native", "explore", "visual", "auto"], case_sensitive=False
    ),
    default="harness",
    show_default=True,
)
@click.option(
    "--goal",
    "goals",
    multiple=True,
    help="Exploratory goal (repeatable). Routes 'explore' engine.",
)
@click.option(
    "--max-steps",
    default=0,
    show_default=False,
    type=int,
    help="Max LLM-driven steps for the explore engine (0 keeps the explorer default).",
)
def tester_create(
    config_path: Path,
    name: str,
    prompt: str,
    app_url: str,
    start_cmd: str,
    harness_cmd: str,
    mode: str,
    auth_required: bool,
    auth_mode: str,
    auth_login_url: str,
    auth_username: str,
    auth_password_env: str,
    auth_jwt_env: str,
    auth_headers_env: str,
    auth_profile: str,
    execution_engine: str,
    goals: tuple[str, ...],
    max_steps: int,
) -> None:
    cfg = load_config(config_path)
    defaults = _tester_defaults(config_path)
    normalized_mode = "explore_suite" if mode.lower() == "explore_suite" else "describe"
    final_prompt = prompt.strip()
    if normalized_mode == "explore_suite" and not final_prompt:
        final_prompt = (
            "Systematically explore the app and propose a full regression test suite."
        )
    exploratory_goals = [g.strip() for g in goals if g.strip()]
    profile_name = auth_profile.strip()
    profile = _auth_profile(defaults, profile_name)
    if profile:
        auth_required = True
        auth_mode = str(profile.get("mode") or auth_mode).strip() or auth_mode
        auth_login_url = (
            auth_login_url.strip() or str(profile.get("login_url") or "")
        )
        auth_username = auth_username.strip() or str(profile.get("username") or "")
        auth_password_env = (
            auth_password_env.strip()
            if auth_password_env.strip() != "RETRACE_TESTER_AUTH_PASSWORD"
            else str(profile.get("password_env") or auth_password_env)
        )
        auth_jwt_env = (
            auth_jwt_env.strip()
            if auth_jwt_env.strip() != "RETRACE_TESTER_AUTH_JWT"
            else str(profile.get("jwt_env") or auth_jwt_env)
        )
        auth_headers_env = (
            auth_headers_env.strip()
            if auth_headers_env.strip() != "RETRACE_TESTER_AUTH_HEADERS"
            else str(profile.get("headers_env") or auth_headers_env)
        )
    browser_settings: dict[str, Any] = _profile_browser_settings(profile) if profile else {}
    if max_steps > 0:
        browser_settings["explore_max_steps"] = int(max_steps)
    spec = create_spec(
        specs_dir=specs_dir_for_data_dir(cfg.run.data_dir),
        name=name,
        prompt=final_prompt,
        app_url=app_url.strip() or str(defaults.get("app_url") or DEFAULT_APP_URL),
        start_command=start_cmd.strip() or str(defaults.get("start_command") or ""),
        harness_command=(
            harness_cmd.strip()
            or str(defaults.get("harness_command") or DEFAULT_HARNESS_COMMAND)
        ),
        mode=normalized_mode,
        auth_required=auth_required,
        auth_mode=auth_mode.lower(),
        auth_login_url=auth_login_url,
        auth_username=auth_username,
        auth_password_env=auth_password_env,
        auth_jwt_env=auth_jwt_env,
        auth_headers_env=auth_headers_env,
        auth_profile=profile_name,
        auth_setup_steps=_profile_setup_steps(profile) if profile else [],
        execution_engine=execution_engine.lower(),
        exploratory_goals=exploratory_goals,
        browser_settings=browser_settings,
    )
    click.echo(f"Created tester spec: {spec.spec_id}")


@tester_group.command("create-suite")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--name", default="Systematic Suite Draft", show_default=True)
@click.option("--app-url", default="", help="App URL override.")
@click.option("--start-cmd", default="", help="Local app startup command.")
@click.option("--harness-cmd", default="", help="Harness command template.")
@click.option("--auth-required/--no-auth-required", default=False, show_default=True)
@click.option(
    "--auth-mode",
    type=click.Choice(["none", "form", "jwt", "headers"], case_sensitive=False),
    default="none",
    show_default=True,
)
@click.option("--auth-login-url", default="")
@click.option("--auth-username", default="")
@click.option("--auth-profile", default="", help="Reusable auth profile from config.")
@click.option(
    "--goal",
    "goals",
    multiple=True,
    help="Exploratory goal to turn into a draft spec (repeatable).",
)
def tester_create_suite(
    config_path: Path,
    name: str,
    app_url: str,
    start_cmd: str,
    harness_cmd: str,
    auth_required: bool,
    auth_mode: str,
    auth_login_url: str,
    auth_username: str,
    auth_profile: str,
    goals: tuple[str, ...],
) -> None:
    exploratory_goals = tuple(g.strip() for g in goals if g.strip()) or (
        "Explore primary user journeys and propose regression tests.",
    )
    ctx = click.get_current_context()
    ctx.invoke(
        tester_create,
        config_path=config_path,
        name=name,
        prompt="",
        app_url=app_url,
        start_cmd=start_cmd,
        harness_cmd=harness_cmd,
        mode="explore_suite",
        auth_required=auth_required,
        auth_mode=auth_mode,
        auth_login_url=auth_login_url,
        auth_username=auth_username,
        auth_password_env="RETRACE_TESTER_AUTH_PASSWORD",
        auth_jwt_env="RETRACE_TESTER_AUTH_JWT",
        auth_headers_env="RETRACE_TESTER_AUTH_HEADERS",
        auth_profile=auth_profile,
        execution_engine="explore",
        goals=exploratory_goals,
        max_steps=0,
    )


@tester_group.command("accept-draft")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.argument("spec_id")
@click.option("--name", default="", help="Optional accepted spec name.")
@click.option("--prompt", default="", help="Optional accepted spec prompt.")
def tester_accept_draft(
    config_path: Path,
    spec_id: str,
    name: str,
    prompt: str,
) -> None:
    cfg = load_config(config_path)
    specs_dir = specs_dir_for_data_dir(cfg.run.data_dir)
    spec = load_spec(specs_dir, spec_id)
    if dict(spec.fixtures or {}).get("draft_status") != "draft":
        raise click.ClickException("Spec is not an unaccepted draft.")
    if name.strip():
        spec.name = name.strip()
    if prompt.strip():
        spec.prompt = prompt.strip()
    spec.fixtures = dict(spec.fixtures or {})
    spec.fixtures["draft_status"] = "accepted"
    if not spec.fixtures.get("accepted_at"):
        spec.fixtures["accepted_at"] = now_iso()
    save_spec(specs_dir, spec)
    click.echo(
        json.dumps(
            {
                "ok": True,
                "spec_id": spec.spec_id,
                "draft_status": spec.fixtures["draft_status"],
                "source_exploration_run": spec.fixtures.get("source_exploration_run", ""),
            },
            indent=2,
        )
    )


@tester_group.command("list")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
def tester_list(config_path: Path) -> None:
    cfg = load_config(config_path)
    specs = list_specs(specs_dir_for_data_dir(cfg.run.data_dir))
    if not specs:
        click.echo("No tester specs found.")
        return
    for s in specs:
        click.echo(f"{s.spec_id}\t{s.mode}\t{s.name}")


@tester_group.command("api-create")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--name", required=True, help="Friendly API test name.")
@click.option("--method", default="GET", show_default=True)
@click.option("--url", required=True, help="Absolute API URL.")
@click.option("--query-json", default="", help="JSON object for query params.")
@click.option("--headers-json", default="", help="JSON object for static headers.")
@click.option("--body-json", default="", help="JSON request body.")
@click.option("--auth-bearer-env", default="", help="Env var containing bearer token.")
@click.option("--auth-profile", default="", help="Shared tester auth profile.")
@click.option("--env-profile", default="", help="Shared tester environment profile.")
@click.option("--expected-status", default=200, show_default=True, type=int)
@click.option(
    "--json-assertion",
    "json_assertions",
    multiple=True,
    help="JSON assertion object, repeatable.",
)
@click.option(
    "--schema-assertion-json",
    default="",
    help="JSON schema assertion object.",
)
@click.option("--latency-ms", default=0, type=int, help="Latency budget in ms.")
@click.option("--timeout-seconds", default=15.0, show_default=True, type=float)
def tester_api_create(
    config_path: Path,
    name: str,
    method: str,
    url: str,
    query_json: str,
    headers_json: str,
    body_json: str,
    auth_bearer_env: str,
    auth_profile: str,
    env_profile: str,
    expected_status: int,
    json_assertions: tuple[str, ...],
    schema_assertion_json: str,
    latency_ms: int,
    timeout_seconds: float,
) -> None:
    cfg = load_config(config_path)
    defaults = _tester_defaults(config_path)
    auth = (
        {"type": "bearer", "token_env": auth_bearer_env.strip()}
        if auth_bearer_env.strip()
        else {}
    )
    if auth_profile.strip() and not auth:
        try:
            resolved_auth = resolve_auth_profile(defaults, auth_profile.strip())
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        if resolved_auth.mode == "form":
            raise click.ClickException("API specs support jwt, headers, and none auth profiles")
        auth = dict(resolved_auth.auth)
    env_profile_data = _env_profile(defaults, env_profile.strip()) if env_profile.strip() else {}
    final_url = url
    api_base_url = str(env_profile_data.get("api_base_url") or "").strip()
    if api_base_url and final_url.startswith("/"):
        final_url = api_base_url.rstrip("/") + "/" + final_url.lstrip("/")
    parsed_json_assertions = []
    for item in json_assertions:
        parsed = _json_option(item, label="json-assertion", default={})
        if parsed:
            parsed_json_assertions.append(parsed)
    schema_assertions = []
    if schema_assertion_json.strip():
        schema_assertions.append(
            _json_option(schema_assertion_json, label="schema-assertion-json", default={})
        )
    spec = create_api_spec(
        specs_dir=api_specs_dir_for_data_dir(cfg.run.data_dir),
        name=name,
        method=method,
        url=final_url,
        query=_json_option(query_json, label="query-json", default={}),
        headers=_json_option(headers_json, label="headers-json", default={}),
        body=_json_option(body_json, label="body-json", default=None),
        auth=auth,
        auth_profile=auth_profile.strip(),
        env_profile=env_profile.strip(),
        env_overrides={},
        expected_status=expected_status,
        json_assertions=parsed_json_assertions,
        schema_assertions=schema_assertions,
        latency_ms=latency_ms,
        timeout_seconds=timeout_seconds,
    )
    click.echo(f"Created API test spec: {spec.spec_id}")


@tester_group.command("api-list")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
def tester_api_list(config_path: Path) -> None:
    cfg = load_config(config_path)
    specs = list_api_specs(api_specs_dir_for_data_dir(cfg.run.data_dir))
    if not specs:
        click.echo("No API test specs found.")
        return
    for spec in specs:
        click.echo(f"{spec.spec_id}\t{spec.method}\t{spec.url}\t{spec.name}")


@tester_group.command("api-run")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--project-id", default="", help="Project ID override.")
@click.option("--environment-id", default="", help="Environment ID override.")
@click.option(
    "--repo-path",
    type=click.Path(path_type=Path),
    default=None,
    help="Local repo path for route-based repair file scoring.",
)
@click.option("--auth-profile", default="", help="Auth profile override for this run.")
@click.option("--env-profile", default="", help="Environment profile override for this run.")
@click.argument("spec_id")
def tester_api_run(
    config_path: Path,
    project_id: str,
    environment_id: str,
    repo_path: Optional[Path],
    auth_profile: str,
    env_profile: str,
    spec_id: str,
) -> None:
    cfg = load_config(config_path)
    defaults = _tester_defaults(config_path)
    spec = load_api_spec(api_specs_dir_for_data_dir(cfg.run.data_dir), spec_id)
    spec = _apply_api_profiles(
        spec,
        defaults=defaults,
        auth_profile_name=auth_profile,
        env_profile_name=env_profile,
    )
    result = run_api_spec(spec=spec, runs_dir=api_runs_dir_for_data_dir(cfg.run.data_dir))
    failure_metadata: dict[str, Any] = {}
    if not result.ok:
        try:
            store = Storage(cfg.run.data_dir / "retrace.db")
            store.init_schema()
            workspace = store.ensure_workspace(project_name="Default")
            persisted = persist_api_failure(
                store=store,
                spec=spec,
                result=result,
                project_id=project_id.strip() or workspace.project_id,
                environment_id=environment_id.strip() or workspace.environment_id,
                repo_path=repo_path,
            )
            failure_metadata = {
                "canonical_failure_id": persisted.failure_id,
                "repair_task_id": persisted.repair_task_id,
                "likely_files": persisted.likely_files,
                "repair_prompt_path": persisted.prompt_path,
            }
        except Exception as exc:
            click.echo(
                f"warning: failed to persist API failure metadata: {exc}",
                err=True,
            )
    click.echo(json.dumps({**result.__dict__, **failure_metadata}, indent=2))
    if not result.ok:
        raise click.ClickException("API test run failed.")


@tester_group.command("api-import-openapi")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option(
    "--base-url",
    default="",
    help="Base URL for generated runnable specs.",
)
@click.option(
    "--path-filter",
    default="",
    help="Regex filter for OpenAPI paths.",
)
@click.option(
    "--method",
    "method_filter",
    default="",
    help="HTTP method filter, for example GET or POST.",
)
@click.option("--auth-profile", default="", help="Auth profile to attach to imported specs.")
@click.option("--env-profile", default="", help="Environment profile to attach to imported specs.")
@click.argument("openapi_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def tester_api_import_openapi(
    config_path: Path,
    base_url: str,
    path_filter: str,
    method_filter: str,
    auth_profile: str,
    env_profile: str,
    openapi_path: Path,
) -> None:
    cfg = load_config(config_path)
    defaults = _tester_defaults(config_path)
    env_profile_data = _env_profile(defaults, env_profile.strip()) if env_profile.strip() else {}
    effective_base_url = base_url or str(env_profile_data.get("api_base_url") or "")
    result = import_openapi_specs(
        openapi_path=openapi_path,
        specs_dir=api_specs_dir_for_data_dir(cfg.run.data_dir),
        base_url=effective_base_url,
        path_filter=path_filter,
        method_filter=method_filter,
        auth_profile=auth_profile,
        env_profile=env_profile,
    )
    click.echo(
        json.dumps(
            {
                "created": [spec.spec_id for spec in result.specs],
                "created_count": len(result.specs),
                "skipped": result.skipped,
            },
            indent=2,
        )
    )


@tester_group.command("show")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.argument("spec_id")
def tester_show(config_path: Path, spec_id: str) -> None:
    cfg = load_config(config_path)
    spec = load_spec(specs_dir_for_data_dir(cfg.run.data_dir), spec_id)
    click.echo(json.dumps(spec.__dict__, indent=2))


@tester_group.command("update")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.argument("spec_id")
@click.option("--name", default=None)
@click.option("--prompt", default=None)
@click.option("--app-url", default=None)
@click.option("--start-cmd", default=None)
@click.option("--harness-cmd", default=None)
@click.option(
    "--mode",
    default=None,
    type=click.Choice(["describe", "explore_suite"], case_sensitive=False),
)
@click.option("--auth-required", default=None, type=bool)
@click.option(
    "--auth-mode",
    default=None,
    type=click.Choice(["none", "form", "jwt", "headers"], case_sensitive=False),
)
@click.option("--auth-login-url", default=None)
@click.option("--auth-username", default=None)
@click.option(
    "--engine",
    "execution_engine",
    default=None,
    type=click.Choice(
        ["harness", "native", "explore", "visual", "auto"], case_sensitive=False
    ),
)
def tester_update(
    config_path: Path,
    spec_id: str,
    name: Optional[str],
    prompt: Optional[str],
    app_url: Optional[str],
    start_cmd: Optional[str],
    harness_cmd: Optional[str],
    mode: Optional[str],
    auth_required: Optional[bool],
    auth_mode: Optional[str],
    auth_login_url: Optional[str],
    auth_username: Optional[str],
    execution_engine: Optional[str],
) -> None:
    from retrace.tester import now_iso

    cfg = load_config(config_path)
    specs_dir = specs_dir_for_data_dir(cfg.run.data_dir)
    spec = load_spec(specs_dir, spec_id)
    if name is not None:
        spec.name = name
    if prompt is not None:
        spec.prompt = prompt
    if app_url is not None:
        spec.app_url = app_url
    if start_cmd is not None:
        spec.start_command = start_cmd
    if harness_cmd is not None:
        spec.harness_command = harness_cmd
    if mode is not None:
        spec.mode = mode.lower()
    if auth_required is not None:
        spec.auth_required = bool(auth_required)
    if auth_mode is not None:
        spec.auth_mode = auth_mode.lower()
    if auth_login_url is not None:
        spec.auth_login_url = auth_login_url
    if auth_username is not None:
        spec.auth_username = auth_username
    if execution_engine is not None:
        spec.execution_engine = execution_engine.lower()
    spec.updated_at = now_iso()
    validate_spec(spec)
    save_spec(specs_dir, spec)
    click.echo(f"Updated tester spec: {spec.spec_id}")


@tester_group.command("from-replay-issue")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--project-id", default="", help="Project ID override.")
@click.option("--environment-id", default="", help="Environment ID override.")
@click.option("--app-url", default="", help="App URL override for generated spec.")
@click.argument("issue_id")
def tester_from_replay_issue(
    config_path: Path,
    project_id: str,
    environment_id: str,
    app_url: str,
    issue_id: str,
) -> None:
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")
    generated = generate_spec_from_replay_issue(
        store=store,
        specs_dir=specs_dir_for_data_dir(cfg.run.data_dir),
        project_id=project_id.strip() or workspace.project_id,
        environment_id=environment_id.strip() or workspace.environment_id,
        issue_id=issue_id,
        app_url=app_url,
    )
    click.echo(
        json.dumps(
            {
                "spec_id": generated.spec.spec_id,
                "issue_public_id": generated.issue_public_id,
                "replay_public_id": generated.replay_public_id,
                "confidence": generated.confidence,
                "known_gaps": generated.known_gaps,
            },
            indent=2,
        )
    )


@tester_group.command("api-from-replay-issue")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--project-id", default="", help="Project ID override.")
@click.option("--environment-id", default="", help="Environment ID override.")
@click.option("--app-url", default="", help="Base URL override for relative requests.")
@click.argument("issue_id")
def tester_api_from_replay_issue(
    config_path: Path,
    project_id: str,
    environment_id: str,
    app_url: str,
    issue_id: str,
) -> None:
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")
    generated = generate_api_spec_from_replay_issue(
        store=store,
        specs_dir=api_specs_dir_for_data_dir(cfg.run.data_dir),
        project_id=project_id.strip() or workspace.project_id,
        environment_id=environment_id.strip() or workspace.environment_id,
        issue_id=issue_id,
        app_url=app_url,
    )
    click.echo(
        json.dumps(
            {
                "spec_id": generated.spec.spec_id,
                "issue_public_id": generated.issue_public_id,
                "replay_public_id": generated.replay_public_id,
                "method": generated.spec.method,
                "url": generated.spec.url,
                "expected_status": generated.spec.expected_status,
                "auth": generated.spec.auth,
            },
            indent=2,
        )
    )


@tester_group.command("run")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.argument("spec_id")
@click.option("--prompt", default=None, help="Override prompt for this run.")
@click.option("--app-url", default=None, help="Override app URL for this run.")
@click.option("--start-cmd", default=None, help="Override app startup command.")
@click.option("--retries", default=None, type=int, help="Override retry count.")
def tester_run(
    config_path: Path,
    spec_id: str,
    prompt: Optional[str],
    app_url: Optional[str],
    start_cmd: Optional[str],
    retries: Optional[int],
) -> None:
    cfg = load_config(config_path)
    defaults = _tester_defaults(config_path)
    retries_v = (
        max(0, int(retries))
        if retries is not None
        else max(0, int(defaults.get("max_retries") or 1))
    )
    spec = load_spec(specs_dir_for_data_dir(cfg.run.data_dir), spec_id)
    result = run_spec(
        spec=spec,
        runs_dir=runs_dir_for_data_dir(cfg.run.data_dir),
        prompt_override=prompt,
        app_url_override=app_url,
        start_command_override=start_cmd,
        max_retries=retries_v,
        cwd=config_path.parent,
    )
    failure_metadata: dict[str, str] = {}
    try:
        store = Storage(cfg.run.data_dir / "retrace.db")
        store.init_schema()
        link_id = _single_failure_test_link_id(store, result.spec_id)
        if link_id:
            store.update_failure_test_link_run(
                spec_id=result.spec_id,
                run_result=result,
                link_id=link_id,
            )
        if not result.ok and result.execution_engine == "harness":
            failure_metadata = _persist_harness_failure(
                store=store,
                result=result,
                spec_name=spec.name,
            )
            link_id = _single_failure_test_link_id(store, result.spec_id)
            if link_id:
                store.update_failure_test_link_run(
                    spec_id=result.spec_id,
                    run_result=result,
                    link_id=link_id,
                )
    except Exception as exc:
        click.echo(
            f"warning: failed to persist tester failure metadata: {exc}",
            err=True,
        )
    click.echo(
        json.dumps(
            {
                "spec_id": result.spec_id,
                "run_id": result.run_id,
                "ok": result.ok,
                "exit_code": result.exit_code,
                "run_dir": result.run_dir,
                "harness_log_path": result.harness_log_path,
                "final_prompt": result.final_prompt,
                "attempts": result.attempts,
                "status": result.status,
                "flaky": result.flaky,
                "flake_reason": result.flake_reason,
                "failure_classification": result.failure_classification,
                "error": result.error,
                "execution_engine": result.execution_engine,
                "engine_reason": result.engine_reason,
                "canonical_failure_id": failure_metadata.get("failure_id", ""),
                "repair_task_id": failure_metadata.get("repair_task_id", ""),
                "artifacts": result.artifacts,
                "assertion_results": result.assertion_results,
            },
            indent=2,
        )
    )
    if not result.ok and cfg.notifications.enabled:
        from retrace.notification_sinks import (
            NotificationEvent,
            NotificationPayload,
            build_sinks_from_config,
            close_sinks,
            dispatch_notification,
        )

        sinks = build_sinks_from_config(cfg.notifications)
        try:
            dispatch_notification(
                sinks,
                NotificationPayload(
                    event=NotificationEvent.RUN_FAILED.value,
                    title=f"Tester run failed: {spec.name}",
                    summary=result.error or f"exit code {result.exit_code}",
                    public_id=result.run_id,
                    extra={
                        "spec_id": result.spec_id,
                        "execution_engine": result.execution_engine,
                        "attempts": result.attempts,
                        "flake_reason": result.flake_reason,
                        "failure_classification": result.failure_classification,
                    },
                ),
            )
        finally:
            close_sinks(sinks)
    if not result.ok:
        raise click.ClickException("Tester run failed.")


@tester_group.command("enqueue")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.argument("spec_id")
@click.option("--prompt", default=None, help="Override prompt for this queued run.")
@click.option("--app-url", default=None, help="Override app URL for this queued run.")
@click.option("--start-cmd", default=None, help="Override app startup command.")
@click.option("--retries", default=None, type=int, help="Override retry count.")
def tester_enqueue(
    config_path: Path,
    spec_id: str,
    prompt: Optional[str],
    app_url: Optional[str],
    start_cmd: Optional[str],
    retries: Optional[int],
) -> None:
    cfg = load_config(config_path)
    defaults = _tester_defaults(config_path)
    load_spec(specs_dir_for_data_dir(cfg.run.data_dir), spec_id)
    job = enqueue_spec_run(
        queue_dir=queue_dir_for_data_dir(cfg.run.data_dir),
        spec_id=spec_id,
        prompt_override=prompt,
        app_url_override=app_url,
        start_command_override=start_cmd,
        retries=(
            max(0, int(retries))
            if retries is not None
            else max(
                0,
                int(defaults["max_retries"]) if "max_retries" in defaults else 1,
            )
        ),
    )
    click.echo(json.dumps(job, indent=2))


@tester_group.command("worker")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--once", is_flag=True, help="Process at most one queued tester job.")
@click.option("--interval", default=30, show_default=True, type=int)
def tester_worker(config_path: Path, once: bool, interval: int) -> None:
    cfg = load_config(config_path)
    specs_dir = specs_dir_for_data_dir(cfg.run.data_dir)
    runs_dir = runs_dir_for_data_dir(cfg.run.data_dir)
    queue_dir = queue_dir_for_data_dir(cfg.run.data_dir)
    while True:
        job = run_queued_spec_once(
            specs_dir=specs_dir,
            runs_dir=runs_dir,
            queue_dir=queue_dir,
            cwd=config_path.parent,
        )
        if job is not None:
            result_payload = (
                job.get("result") if isinstance(job.get("result"), dict) else None
            )
            run_ok = (
                bool(result_payload["ok"])
                if result_payload is not None and "ok" in result_payload
                else job.get("status") == "succeeded"
            )
            try:
                store = Storage(cfg.run.data_dir / "retrace.db")
                store.init_schema()
                spec_id = str(job.get("spec_id") or "")
                link_id = _single_failure_test_link_id(store, spec_id)
                if link_id and result_payload is not None:
                    store.update_failure_test_link_run(
                        spec_id=spec_id,
                        run_result=SimpleNamespace(**result_payload),
                        link_id=link_id,
                    )
            except Exception as exc:
                click.echo(
                    f"warning: failed to persist failure_test_link metadata: {exc}",
                    err=True,
                )
            click.echo(json.dumps(job, indent=2))
            if not run_ok and cfg.notifications.enabled:
                from retrace.notification_sinks import (
                    NotificationEvent,
                    NotificationPayload,
                    build_sinks_from_config,
                    close_sinks,
                    dispatch_notification,
                )

                sinks = build_sinks_from_config(cfg.notifications)
                try:
                    dispatch_notification(
                        sinks,
                        NotificationPayload(
                            event=NotificationEvent.RUN_FAILED.value,
                            title=f"Queued tester run failed: {job.get('spec_id', '')}",
                            summary=str(
                                (result_payload or {}).get("error")
                                or job.get("error")
                                or ""
                            ),
                            public_id=str((result_payload or {}).get("run_id") or ""),
                            extra={
                                "spec_id": job.get("spec_id"),
                                "execution_engine": (result_payload or {}).get(
                                    "execution_engine"
                                ),
                                "attempts": (result_payload or {}).get("attempts"),
                                "failure_classification": (result_payload or {}).get(
                                    "failure_classification"
                                ),
                            },
                        ),
                    )
                finally:
                    close_sinks(sinks)
        if once:
            return
        time.sleep(max(1, int(interval)))


@tester_group.command("runs")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--limit", default=15, show_default=True, type=int)
def tester_runs(config_path: Path, limit: int) -> None:
    cfg = load_config(config_path)
    runs = load_run_summaries(runs_dir_for_data_dir(cfg.run.data_dir), limit=limit)
    if not runs:
        click.echo("No tester runs found.")
        return
    click.echo(json.dumps(runs, indent=2))
