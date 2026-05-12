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


# ---------------------------------------------------------------------------
# P1.1 — alert routes (fan-out destinations for fired alerts)
# ---------------------------------------------------------------------------


@monitor_group.group("route")
def route_group() -> None:
    """Create, list, test, and delete alert fan-out routes.

    A route is bound to a kind (`slack` / `discord` / `pagerduty` /
    `webhook`) and a target URL. When an alert rule fires
    (`action=alert`), every enabled matching route posts to its
    target. Use `--rule <name>` to scope a route to one rule, or
    omit it to match every alert at-or-above `--severity`.
    """


def _redact_target_url(url: str) -> str:
    """For Slack / Discord / generic-webhook routes the URL itself
    often embeds a webhook token in the path. Show only
    `scheme://host/…` so an operator can confirm "yes, this points at
    hooks.slack.com" without printing the credential. (CodeRabbit
    Major catch on PR #131.)
    """
    from urllib.parse import urlsplit, urlunsplit

    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if not parts.netloc:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, "/…", "", ""))


def _route_row_to_dict(row) -> dict:
    return {
        "id": row.id,
        "public_id": row.public_id,
        "name": row.name,
        "enabled": bool(row.enabled),
        "rule_name": row.rule_name,
        "target_kind": row.target_kind,
        # Redacted by default so `--json` output is safe to paste into
        # shared workflows.
        "target_url_redacted": _redact_target_url(row.target_url),
        # Never echo the routing key / signing secret.
        "target_secret_set": bool(row.target_secret),
        "min_severity": row.min_severity,
        "dedup_window_seconds": row.dedup_window_seconds,
        "updated_at": (
            row.updated_at.isoformat()
            if hasattr(row.updated_at, "isoformat")
            else str(row.updated_at)
        ),
    }


@route_group.command("add")
@click.option(
    "--config", "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"), show_default=True,
)
@click.option("--project-id", default="")
@click.option("--environment-id", default="")
@click.option("--name", required=True, help="Unique route name within (project, env).")
@click.option(
    "--kind",
    "target_kind",
    type=click.Choice(["slack", "discord", "pagerduty", "webhook"], case_sensitive=False),
    required=True,
    help="Target kind. Determines payload shape.",
)
@click.option(
    "--url",
    "target_url",
    required=True,
    help=(
        "Target URL. For PagerDuty, the events endpoint "
        "(default https://events.pagerduty.com/v2/enqueue is conventional)."
    ),
)
@click.option(
    "--secret",
    "target_secret",
    default="",
    help=(
        "Routing key / signing secret. Required for `pagerduty`; "
        "optional for `webhook` (sent as `X-Retrace-Signature` upstream "
        "in a future revision)."
    ),
)
@click.option(
    "--rule",
    "rule_name",
    default="",
    help="Restrict to one named alert rule. Empty = match every alert.",
)
@click.option(
    "--severity",
    "min_severity",
    type=click.Choice(["", "low", "medium", "high", "critical"], case_sensitive=False),
    default="",
    help="Suppress alerts below this severity. Empty = no floor.",
)
@click.option(
    "--dedup-window",
    "dedup_window_seconds",
    type=int,
    default=300,
    show_default=True,
    help="Window (seconds) within which a repeat fingerprint is suppressed.",
)
@click.option(
    "--disabled/--enabled",
    "disabled",
    default=False,
    help="Create the route disabled (re-run `route add --name ... --enabled` to flip it on).",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def route_add(
    config_path: Path,
    project_id: str,
    environment_id: str,
    name: str,
    target_kind: str,
    target_url: str,
    target_secret: str,
    rule_name: str,
    min_severity: str,
    dedup_window_seconds: int,
    disabled: bool,
    as_json: bool,
) -> None:
    """Create or update an alert fan-out route."""
    store, default_pid, default_eid = _open(config_path)
    pid = project_id.strip() or default_pid
    eid = environment_id.strip() or default_eid
    if target_kind.lower() == "pagerduty" and not target_secret:
        raise click.UsageError(
            "PagerDuty routes require --secret (the Events v2 routing key)."
        )
    try:
        row = store.upsert_alert_route(
            project_id=pid,
            environment_id=eid,
            name=name,
            target_kind=target_kind.lower(),
            target_url=target_url,
            target_secret=target_secret,
            rule_name=rule_name,
            min_severity=min_severity.lower(),
            dedup_window_seconds=dedup_window_seconds,
            enabled=not disabled,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(json.dumps(_route_row_to_dict(row), indent=2))
    else:
        icon = "○" if row.enabled else "—"
        click.echo(
            f"  {icon} {row.public_id}  {row.name}  ({row.target_kind})  "
            f"→ {_redact_target_url(row.target_url)}"
        )


@route_group.command("list")
@click.option(
    "--config", "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"), show_default=True,
)
@click.option("--project-id", default="")
@click.option("--environment-id", default="")
@click.option("--enabled-only/--all", default=False, show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
def route_list(
    config_path: Path,
    project_id: str,
    environment_id: str,
    enabled_only: bool,
    as_json: bool,
) -> None:
    """List alert fan-out routes."""
    store, default_pid, default_eid = _open(config_path)
    pid = project_id.strip() or default_pid
    eid = environment_id.strip() or default_eid
    rows = store.list_alert_routes(
        project_id=pid,
        environment_id=eid,
        enabled=True if enabled_only else None,
    )
    if as_json:
        click.echo(json.dumps([_route_row_to_dict(r) for r in rows], indent=2))
        return
    if not rows:
        click.echo("No alert routes. Try `retrace monitor route add --name ...`.")
        return
    for r in rows:
        icon = "○" if r.enabled else "—"
        rule_suffix = f"  rule={r.rule_name}" if r.rule_name else ""
        sev_suffix = f"  ≥{r.min_severity}" if r.min_severity else ""
        click.echo(
            f"  {icon} {r.public_id}  {r.name}  ({r.target_kind})"
            f"{rule_suffix}{sev_suffix}  → {_redact_target_url(r.target_url)}"
        )


@route_group.command("delete")
@click.argument("route_name")
@click.option(
    "--config", "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"), show_default=True,
)
@click.option("--project-id", default="")
@click.option("--environment-id", default="")
@click.option("--yes", is_flag=True, default=False, help="Skip confirmation prompt.")
def route_delete(
    route_name: str,
    config_path: Path,
    project_id: str,
    environment_id: str,
    yes: bool,
) -> None:
    """Delete an alert route by name."""
    store, default_pid, default_eid = _open(config_path)
    pid = project_id.strip() or default_pid
    eid = environment_id.strip() or default_eid
    if not yes:
        if not click.confirm(f"Delete alert route {route_name!r}?", default=False):
            click.echo("Cancelled.")
            sys.exit(1)
    removed = store.delete_alert_route(
        project_id=pid, environment_id=eid, name=route_name
    )
    click.echo(json.dumps({"deleted": bool(removed), "name": route_name}))


@route_group.command("test")
@click.argument("route_name")
@click.option(
    "--config", "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"), show_default=True,
)
@click.option("--project-id", default="")
@click.option("--environment-id", default="")
@click.option(
    "--severity",
    default="high",
    type=click.Choice(["low", "medium", "high", "critical"], case_sensitive=False),
    show_default=True,
)
@click.option("--title", default="Retrace test alert")
@click.option("--summary", default="This is a synthetic alert from `retrace monitor route test`.")
def route_test(
    route_name: str,
    config_path: Path,
    project_id: str,
    environment_id: str,
    severity: str,
    title: str,
    summary: str,
) -> None:
    """Send a synthetic alert through one route to verify reachability.

    Bypasses dedup so a fresh test always sends. The fingerprint is
    randomized per invocation.
    """
    import secrets

    from retrace.alert_dispatch import dispatch_alert
    from retrace.alert_rules import AlertRuleDecision
    from retrace.monitoring_ingest import MonitoringAlert

    store, default_pid, default_eid = _open(config_path)
    pid = project_id.strip() or default_pid
    eid = environment_id.strip() or default_eid
    route = store.get_alert_route(project_id=pid, environment_id=eid, name=route_name)
    if route is None:
        raise click.ClickException(
            f"No route named {route_name!r}. Run `retrace monitor route list`."
        )
    if not route.enabled:
        raise click.ClickException(
            f"Route {route_name!r} is disabled. Re-create with --enabled or "
            "delete + re-add."
        )

    fake_alert = MonitoringAlert(
        provider="retrace-test",
        external_id=f"test-{secrets.token_hex(4)}",
        title=title,
        summary=summary,
        severity=severity.lower(),
        fingerprint=f"test-{secrets.token_hex(8)}",
        occurred_at_ms=0,
        metadata={"environment": eid, "test": True},
        evidence={},
    )
    decision = AlertRuleDecision(
        state="active",
        action="alert",
        rule_name=route.rule_name,
    )
    # Scope dispatch to JUST this route so a synthetic test alert
    # cannot leak to other routes that match the (project, env, rule)
    # tuple. (CodeRabbit Major catch on PR #131.)
    results = dispatch_alert(
        store=store,
        project_id=pid,
        environment_id=eid,
        alert=fake_alert,
        decision=decision,
        only_route_ids=[route.id],
    )
    if not results:
        click.echo(
            json.dumps({
                "status": "no_dispatch",
                "hint": (
                    "Route didn't match dispatch (rule_name mismatch, severity "
                    "gate, or disabled). Run `retrace monitor route list`."
                ),
            }, indent=2)
        )
        sys.exit(2)
    me = results[0]
    click.echo(json.dumps(me.to_dict(), indent=2))
    if me.status != "sent":
        sys.exit(2)
