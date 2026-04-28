from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import click
import yaml

from retrace.config import load_config
from retrace.tester import (
    DEFAULT_APP_URL,
    DEFAULT_HARNESS_COMMAND,
    create_spec,
    enqueue_spec_run,
    list_specs,
    load_run_summaries,
    load_spec,
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
@click.option(
    "--engine",
    "execution_engine",
    type=click.Choice(["harness", "native", "auto"], case_sensitive=False),
    default="harness",
    show_default=True,
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
    execution_engine: str,
) -> None:
    cfg = load_config(config_path)
    defaults = _tester_defaults(config_path)
    normalized_mode = "explore_suite" if mode.lower() == "explore_suite" else "describe"
    final_prompt = prompt.strip()
    if normalized_mode == "explore_suite" and not final_prompt:
        final_prompt = (
            "Systematically explore the app and propose a full regression test suite."
        )
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
        execution_engine=execution_engine.lower(),
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
) -> None:
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
        execution_engine="harness",
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
    type=click.Choice(["harness", "native", "auto"], case_sensitive=False),
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
                "error": result.error,
                "execution_engine": result.execution_engine,
                "artifacts": result.artifacts,
                "assertion_results": result.assertion_results,
            },
            indent=2,
        )
    )
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
            else max(0, int(defaults.get("max_retries") or 1))
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
            click.echo(json.dumps(job, indent=2))
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
