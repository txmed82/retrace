from __future__ import annotations

import json
from pathlib import Path

import click

from retrace.config import load_config
from retrace.digest import build_digest, render_digest_markdown, write_digest_report
from retrace.notification_sinks import (
    NotificationEvent,
    NotificationPayload,
    build_sinks_from_config,
    close_sinks,
    dispatch_notification,
)
from retrace.storage import Storage


@click.command("digest")
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
    "--lookback-hours",
    default=24,
    show_default=True,
    type=int,
    help="Window for new/regressed/resolved buckets (top-impact spans all open issues).",
)
@click.option(
    "--top",
    "top_impact_limit",
    default=5,
    show_default=True,
    type=int,
    help="Number of top-impact open issues to include.",
)
@click.option(
    "--write/--no-write",
    "write_report",
    default=True,
    help="Write the rendered digest under reports/.",
)
@click.option(
    "--notify/--no-notify",
    "notify_sinks_flag",
    default=False,
    help="Fan out a digest summary through configured notification sinks.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["markdown", "json"], case_sensitive=False),
    default="markdown",
    show_default=True,
)
def digest_command(
    config_path: Path,
    project_id: str,
    environment_id: str,
    lookback_hours: int,
    top_impact_limit: int,
    write_report: bool,
    notify_sinks_flag: bool,
    output_format: str,
) -> None:
    """Generate a markdown digest of replay-issue activity."""
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")
    digest = build_digest(
        store=store,
        project_id=project_id.strip() or workspace.project_id,
        environment_id=environment_id.strip() or workspace.environment_id,
        lookback_hours=lookback_hours,
        top_impact_limit=top_impact_limit,
    )

    report_path: Path | None = None
    if write_report:
        report_path = write_digest_report(
            digest=digest,
            reports_dir=cfg.run.output_dir,
        )

    if output_format.lower() == "json":
        click.echo(
            json.dumps(
                {
                    "project_id": digest.project_id,
                    "environment_id": digest.environment_id,
                    "window_start": digest.window_start,
                    "window_end": digest.window_end,
                    "report_path": str(report_path) if report_path else "",
                    "counts": {
                        "new": len(digest.new_issues),
                        "regressed": len(digest.regressed_issues),
                        "resolved": len(digest.resolved_issues),
                        "top_impact": len(digest.top_impact_open),
                    },
                },
                indent=2,
            )
        )
    else:
        click.echo(render_digest_markdown(digest))
        if report_path:
            click.echo(f"Wrote {report_path}", err=True)

    if notify_sinks_flag and cfg.notifications.enabled and not digest.is_empty:
        sinks = build_sinks_from_config(cfg.notifications)
        try:
            dispatch_notification(
                sinks,
                NotificationPayload(
                    event="digest.generated",
                    title=(
                        f"Retrace digest: {len(digest.new_issues)} new, "
                        f"{len(digest.regressed_issues)} regressed, "
                        f"{len(digest.resolved_issues)} resolved"
                    ),
                    summary=(
                        "Top open: "
                        + ", ".join(
                            r.public_id for r in digest.top_impact_open[:3]
                        )
                    ),
                    extra={
                        "report_path": str(report_path) if report_path else "",
                        "window_start": digest.window_start,
                        "window_end": digest.window_end,
                    },
                ),
            )
        finally:
            close_sinks(sinks)
    # silence unused-import lint when notify path is dead in tests
    _ = NotificationEvent
