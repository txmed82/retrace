"""`retrace monitor ...` — manage error-monitor alert rules from the CLI.

Alert rules gate which Sentry-compatible / OTel / generic-webhook events
become user-visible incidents. Until now they could only be inspected by
talking to SQLite directly. This command exposes them through the same
shape as `retrace qa` / `retrace api`.

The CLI is deliberately small: `list` / `show` / `set` (upsert; disable
with `set --disabled`) / `delete`. Rule fields mirror
`Storage.upsert_app_error_alert_rule` so options map 1:1 to behaviour.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from retrace.config import load_config
from retrace.storage import Storage


@click.group("monitor")
def monitor_group() -> None:
    """Manage error-monitor alert rules."""


def _open(config_path: Path) -> tuple[Storage, str, str]:
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")
    return store, workspace.project_id, workspace.environment_id


def _row_to_dict(row) -> dict:
    return {
        "id": row.id,
        "public_id": row.public_id,
        "name": row.name,
        "enabled": bool(row.enabled),
        "action": row.action,
        "precedence": row.precedence,
        "min_severity": row.min_severity,
        "provider": row.provider,
        "title_contains": row.title_contains,
        "fingerprint_contains": row.fingerprint_contains,
        "route_contains": row.route_contains,
        "metadata": row.metadata,
        "updated_at": row.updated_at.isoformat() if hasattr(row.updated_at, "isoformat") else str(row.updated_at),
    }


@monitor_group.group("rules")
def rules_group() -> None:
    """Create, list, and delete app-error alert rules."""


@rules_group.command("list")
@click.option(
    "--config", "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"), show_default=True,
)
@click.option("--project-id", default="")
@click.option("--environment-id", default="")
@click.option(
    "--enabled-only/--all",
    default=False,
    show_default=True,
    help="Restrict to enabled rules only.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def rules_list(
    config_path: Path,
    project_id: str,
    environment_id: str,
    enabled_only: bool,
    as_json: bool,
) -> None:
    """List app-error alert rules."""
    store, default_pid, default_eid = _open(config_path)
    pid = project_id.strip() or default_pid
    eid = environment_id.strip() or default_eid
    rows = store.list_app_error_alert_rules(
        project_id=pid,
        environment_id=eid,
        enabled=True if enabled_only else None,
        limit=500,
    )
    if as_json:
        click.echo(json.dumps([_row_to_dict(r) for r in rows], indent=2))
        return
    if not rows:
        click.echo("No alert rules. Try `retrace monitor rules set --name ...`.")
        return
    for r in rows:
        icon = "○" if r.enabled else "—"
        click.echo(
            f"  {icon} {r.public_id}  {r.name}  "
            f"action={r.action}  precedence={r.precedence}"
            f"  min_severity={r.min_severity or '-'}"
        )


@rules_group.command("show")
@click.argument("rule_name")
@click.option(
    "--config", "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"), show_default=True,
)
@click.option("--project-id", default="")
@click.option("--environment-id", default="")
def rules_show(rule_name: str, config_path: Path, project_id: str, environment_id: str) -> None:
    """Show one alert rule by name."""
    store, default_pid, default_eid = _open(config_path)
    pid = project_id.strip() or default_pid
    eid = environment_id.strip() or default_eid
    rows = store.list_app_error_alert_rules(project_id=pid, environment_id=eid, limit=500)
    for r in rows:
        if r.name == rule_name:
            click.echo(json.dumps(_row_to_dict(r), indent=2))
            return
    raise click.ClickException(f"alert rule not found: {rule_name}")


@rules_group.command("set")
@click.option(
    "--config", "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"), show_default=True,
)
@click.option("--project-id", default="")
@click.option("--environment-id", default="")
@click.option("--name", required=True, help="Unique rule name within (project, env).")
@click.option(
    "--enabled/--disabled",
    default=True,
    show_default=True,
)
@click.option(
    "--action",
    type=click.Choice(["alert", "suppress"], case_sensitive=False),
    default="alert",
    show_default=True,
    help="Whether matching events surface incidents (`alert`) or are dropped (`suppress`).",
)
@click.option("--precedence", type=int, default=0, show_default=True)
@click.option(
    "--min-severity",
    default="",
    help="Minimum severity to match (low/medium/high/critical). Empty = any.",
)
@click.option("--provider", default="", help="Restrict to one provider (sentry, posthog, otel, generic).")
@click.option("--title-contains", default="", help="Match if alert title contains this substring.")
@click.option("--fingerprint-contains", default="", help="Match if fingerprint contains this substring.")
@click.option("--route-contains", default="", help="Match if route/URL contains this substring.")
@click.option("--metadata-json", default="", help="JSON object of arbitrary metadata.")
def rules_set(
    config_path: Path,
    project_id: str,
    environment_id: str,
    name: str,
    enabled: bool,
    action: str,
    precedence: int,
    min_severity: str,
    provider: str,
    title_contains: str,
    fingerprint_contains: str,
    route_contains: str,
    metadata_json: str,
) -> None:
    """Create or update an alert rule (idempotent on name)."""
    store, default_pid, default_eid = _open(config_path)
    pid = project_id.strip() or default_pid
    eid = environment_id.strip() or default_eid

    metadata: dict = {}
    if metadata_json:
        try:
            metadata = json.loads(metadata_json)
        except json.JSONDecodeError as exc:
            raise click.BadParameter(f"--metadata-json is not valid JSON: {exc}") from exc
        if not isinstance(metadata, dict):
            raise click.BadParameter("--metadata-json must be a JSON object")

    try:
        rule_id = store.upsert_app_error_alert_rule(
            project_id=pid,
            environment_id=eid,
            name=name,
            enabled=enabled,
            precedence=precedence,
            action=action.lower(),
            min_severity=min_severity,
            provider=provider,
            title_contains=title_contains,
            fingerprint_contains=fingerprint_contains,
            route_contains=route_contains,
            metadata=metadata,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(json.dumps({"rule_id": rule_id, "name": name, "enabled": enabled}, indent=2))


@rules_group.command("delete")
@click.argument("rule_name")
@click.option(
    "--config", "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"), show_default=True,
)
@click.option("--project-id", default="")
@click.option("--environment-id", default="")
@click.option("--yes", is_flag=True, default=False, help="Skip confirmation prompt.")
def rules_delete(
    rule_name: str,
    config_path: Path,
    project_id: str,
    environment_id: str,
    yes: bool,
) -> None:
    """Delete an alert rule by name."""
    store, default_pid, default_eid = _open(config_path)
    pid = project_id.strip() or default_pid
    eid = environment_id.strip() or default_eid
    if not yes:
        if not click.confirm(f"Delete alert rule {rule_name!r}?", default=False):
            click.echo("Cancelled.")
            sys.exit(1)
    removed = store.delete_app_error_alert_rule(
        project_id=pid, environment_id=eid, name=rule_name
    )
    click.echo(json.dumps({"deleted": bool(removed), "name": rule_name}))
