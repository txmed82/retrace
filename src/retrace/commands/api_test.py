"""`retrace api-test ...` — first-party API contract tests.

Failures upsert into the `qa_incidents` table so the same
`retrace qa reproduce|fix|auto` flow handles backend regressions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from retrace.api_tester import (
    APITestSpec,
    api_runs_dir_for_data_dir,
    api_specs_dir_for_data_dir,
    create_spec,
    list_specs,
    load_run_summaries,
    load_spec,
    run_spec_and_record,
)
from retrace.config import load_config
from retrace.storage import Storage


@click.group("api-test")
def api_test_group() -> None:
    """Define and run HTTP API contract tests against your services."""


def _open(config_path: Path) -> tuple[Any, Storage]:
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    return cfg, store


def _parse_kv(items: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in items:
        if "=" not in raw:
            raise click.BadParameter(f"expected key=value, got {raw!r}")
        k, v = raw.split("=", 1)
        if not k.strip():
            raise click.BadParameter(f"empty key in {raw!r}")
        out[k.strip()] = v
    return out


def _parse_assertions(raw_items: tuple[str, ...]) -> list[dict[str, Any]]:
    """Each item is a JSON object describing one assertion."""
    out: list[dict[str, Any]] = []
    for raw in raw_items:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise click.BadParameter(f"--assert must be JSON, got {raw!r}: {exc}") from exc
        if not isinstance(obj, dict):
            raise click.BadParameter(f"--assert must be a JSON object, got {type(obj).__name__}")
        out.append(obj)
    return out


@api_test_group.command("create")
@click.option(
    "--config", "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"), show_default=True,
)
@click.option("--name", required=True, help="Friendly test name.")
@click.option(
    "--method",
    type=click.Choice(["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"], case_sensitive=False),
    default="GET", show_default=True,
)
@click.option("--url", required=True)
@click.option("--header", "headers", multiple=True, help="Repeatable `Name=Value` request header.")
@click.option("--query", "queries", multiple=True, help="Repeatable `name=value` query param.")
@click.option("--body", default="", help="Raw request body string. Mutually exclusive with --json-body.")
@click.option("--json-body", default="", help="JSON-encoded body. Mutually exclusive with --body.")
@click.option("--auth-bearer-env", default="", help="Env var holding a bearer token.")
@click.option("--auth-header-env", default="", help="Env var holding a full Authorization header value.")
@click.option(
    "--assert", "assertions", multiple=True,
    help='Repeatable JSON assertion, e.g. \'{"assertion_type":"status_equals","value":200}\'.',
)
@click.option("--timeout-seconds", default=30.0, type=float, show_default=True)
@click.option("--project-id", default="local", show_default=True)
@click.option("--environment-id", default="production", show_default=True)
def api_test_create(
    config_path: Path,
    name: str,
    method: str,
    url: str,
    headers: tuple[str, ...],
    queries: tuple[str, ...],
    body: str,
    json_body: str,
    auth_bearer_env: str,
    auth_header_env: str,
    assertions: tuple[str, ...],
    timeout_seconds: float,
    project_id: str,
    environment_id: str,
) -> None:
    """Create and save a reusable API test spec."""
    cfg, _ = _open(config_path)
    if body and json_body:
        raise click.BadParameter("pass only one of --body / --json-body")
    parsed_json_body: Any = None
    if json_body:
        try:
            parsed_json_body = json.loads(json_body)
        except json.JSONDecodeError as exc:
            raise click.BadParameter(f"--json-body is not valid JSON: {exc}") from exc

    spec = create_spec(
        specs_dir=api_specs_dir_for_data_dir(cfg.run.data_dir),
        name=name,
        method=method.upper(),
        url=url,
        headers=_parse_kv(headers),
        query=_parse_kv(queries),
        body=body,
        json_body=parsed_json_body,
        auth_bearer_env=auth_bearer_env,
        auth_header_env=auth_header_env,
        assertions=_parse_assertions(assertions),
        timeout_seconds=timeout_seconds,
        project_id=project_id,
        environment_id=environment_id,
    )
    click.echo(json.dumps({"spec_id": spec.spec_id, "name": spec.name, "url": spec.url}, indent=2))


@api_test_group.command("list")
@click.option(
    "--config", "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"), show_default=True,
)
@click.option("--json", "as_json", is_flag=True, default=False)
def api_test_list(config_path: Path, as_json: bool) -> None:
    """List saved API test specs."""
    cfg, _ = _open(config_path)
    specs = list_specs(api_specs_dir_for_data_dir(cfg.run.data_dir))
    if as_json:
        click.echo(json.dumps(
            [{"spec_id": s.spec_id, "name": s.name, "method": s.method, "url": s.url} for s in specs],
            indent=2,
        ))
        return
    if not specs:
        click.echo("No api-test specs.")
        return
    for s in specs:
        click.echo(f"  {s.spec_id}  {s.method:<6}  {s.url}  — {s.name}")


def _resolve_spec(spec_id: str, specs_dir: Path) -> APITestSpec:
    """Allow `spec_id` to be either the canonical id or a name slug prefix."""
    try:
        return load_spec(specs_dir, spec_id)
    except FileNotFoundError:
        # Best-effort prefix match against existing specs.
        for s in list_specs(specs_dir):
            if s.spec_id == spec_id or s.name == spec_id or s.spec_id.startswith(spec_id):
                return s
        raise click.ClickException(f"api-test spec not found: {spec_id}")


@api_test_group.command("run")
@click.argument("spec_id")
@click.option(
    "--config", "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"), show_default=True,
)
@click.option(
    "--suspected-cause",
    default="",
    help="Optional free-text hint stored on any resulting incident.",
)
def api_test_run(spec_id: str, config_path: Path, suspected_cause: str) -> None:
    """Run a single saved API test spec; record an incident on failure."""
    cfg, store = _open(config_path)
    specs_dir = api_specs_dir_for_data_dir(cfg.run.data_dir)
    runs_dir = api_runs_dir_for_data_dir(cfg.run.data_dir)
    spec = _resolve_spec(spec_id, specs_dir)
    result = run_spec_and_record(
        spec=spec,
        runs_dir=runs_dir,
        store=store,
        suspected_cause=suspected_cause,
    )

    summary = {
        "run_id": result.run_id,
        "spec_id": result.spec_id,
        "status": result.status,
        "method": result.method,
        "url": result.url,
        "response_status": result.response_status,
        "duration_ms": result.duration_ms,
        "incident_id": result.incident_id,
        "run_dir": result.run_dir,
        "error": result.error,
    }
    click.echo(json.dumps(summary, indent=2))
    if result.incident_id:
        click.echo("")
        click.echo(f"Incident filed: {result.incident_id}")
        click.echo(f"Next: retrace qa show {result.incident_id}")
        click.echo(f"Or:   retrace qa auto --repo <org/name> --id {result.incident_id}")


@api_test_group.command("run-all")
@click.option(
    "--config", "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"), show_default=True,
)
@click.option(
    "--stop-on-fail",
    is_flag=True,
    default=False,
    help="Stop at the first failure; otherwise run every spec.",
)
def api_test_run_all(config_path: Path, stop_on_fail: bool) -> None:
    """Run every saved API test spec in sequence."""
    cfg, store = _open(config_path)
    specs_dir = api_specs_dir_for_data_dir(cfg.run.data_dir)
    runs_dir = api_runs_dir_for_data_dir(cfg.run.data_dir)
    specs = list_specs(specs_dir)
    if not specs:
        click.echo("No api-test specs to run.")
        return
    passed = 0
    failed = 0
    incidents_filed: list[str] = []
    for s in specs:
        click.echo(f"→ {s.method} {s.url}  ({s.spec_id})")
        result = run_spec_and_record(spec=s, runs_dir=runs_dir, store=store)
        if result.status == "pass":
            passed += 1
            click.echo(f"  pass  {result.response_status}  {result.duration_ms}ms")
        else:
            failed += 1
            click.echo(f"  {result.status}  {result.response_status}  {result.duration_ms}ms")
            if result.incident_id:
                incidents_filed.append(result.incident_id)
                click.echo(f"  incident: {result.incident_id}")
            if stop_on_fail:
                break

    click.echo("")
    click.echo(f"Done: {passed} pass / {failed} fail / {len(incidents_filed)} incident(s) filed")
    if incidents_filed:
        click.echo("Try: retrace qa auto --repo <org/name>")


@api_test_group.command("runs")
@click.option(
    "--config", "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"), show_default=True,
)
@click.option("--limit", type=int, default=20, show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
def api_test_runs(config_path: Path, limit: int, as_json: bool) -> None:
    """Show recent api-test runs."""
    cfg, _ = _open(config_path)
    runs_dir = api_runs_dir_for_data_dir(cfg.run.data_dir)
    summaries = load_run_summaries(runs_dir, limit=limit)
    if as_json:
        click.echo(json.dumps(summaries, indent=2))
        return
    if not summaries:
        click.echo("No api-test runs.")
        return
    for r in summaries:
        line = (
            f"  {r['run_id']}  {r['status']:<5}  "
            f"{r['method']:<6}  {r['response_status']:<3}  "
            f"{r['duration_ms']:>5}ms  {r['url']}"
        )
        if r["incident_id"]:
            line += f"  -> {r['incident_id']}"
        click.echo(line)
