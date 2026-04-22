from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import click

from retrace.commands.doctor import doctor_command
from retrace.commands.github import github_group
from retrace.commands.init import init_command
from retrace.commands.suggest_fixes import suggest_fixes_command
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
        f"Scanned {summary.sessions_scanned} sessions. "
        f"{summary.sessions_with_signals} flagged into "
        f"{summary.clusters_found} cluster(s). "
        f"Report written to {cfg.run.output_dir}/"
    )


main.add_command(init_command)
main.add_command(doctor_command)
main.add_command(github_group)
main.add_command(suggest_fixes_command)
main.add_command(ui_command)
