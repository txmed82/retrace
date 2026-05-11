from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

import click

from retrace.commands.api import api_group
from retrace.commands.demo import demo_group
from retrace.commands.digest import digest_command
from retrace.commands.doctor import doctor_command
from retrace.commands.github import github_group
from retrace.commands.qa import qa_group
from retrace.commands.init import init_command
from retrace.commands.mcp import mcp_group
from retrace.commands.monitor import monitor_group
from retrace.commands.quickstart import quickstart_command
from retrace.commands.repair import repair_group
from retrace.commands.review import review_command
from retrace.commands.suggest_fixes import suggest_fixes_command
from retrace.commands.tester import tester_group
from retrace.commands.ui import ui_command
from retrace.config import load_config
from retrace.ingester import PostHogIngester
from retrace.llm.client import LLMClient
from retrace.pipeline import run_pipeline
from retrace.storage import Storage


@click.group()
def main() -> None:
    """Retrace — find the bugs your users hit."""


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
def run(config_path: Path) -> None:
    """Pull recent sessions, detect issues, write a report."""
    cfg = load_config(config_path)

    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()

    ingester = PostHogIngester(cfg.posthog, store, data_dir=cfg.run.data_dir)
    llm_client = LLMClient(cfg.llm)

    try:
        summary = run_pipeline(
            cfg=cfg,
            store=store,
            ingester=ingester,
            llm_client=llm_client,
            now=datetime.now(timezone.utc),
        )
    finally:
        llm_client.close()

    click.echo(
        f"Status {summary.status}. "
        f"Scanned {summary.sessions_scanned} sessions. "
        f"{summary.sessions_with_signals} flagged into "
        f"{summary.clusters_found} cluster(s). "
        f"Session errors {summary.sessions_errored}. "
        f"Detector errors {summary.detector_errors}. "
        f"Report written to {cfg.run.output_dir}/"
    )
    if summary.error:
        click.echo(f"Error: {summary.error}")
    if summary.status == "error":
        sys.exit(1)


main.add_command(init_command)
main.add_command(quickstart_command)
main.add_command(doctor_command)
main.add_command(github_group)
main.add_command(suggest_fixes_command)
main.add_command(ui_command)
main.add_command(tester_group)
main.add_command(mcp_group)
main.add_command(api_group)
main.add_command(digest_command)
main.add_command(demo_group)
main.add_command(repair_group)
main.add_command(qa_group)
main.add_command(review_command)
main.add_command(monitor_group)
