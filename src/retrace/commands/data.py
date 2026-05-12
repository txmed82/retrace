"""P2.3 — `retrace data` CLI surface.

Two subcommands:

  - `retention apply [--dry-run]` — purge old rows + files per
    config.yaml's `retention:` block.
  - `backup --to PATH` — tarball the sqlite db + data_dir for
    offsite storage.

Both write JSON to stdout so the output is parseable by scripts /
cron wrappers.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from retrace.backup import create_backup, result_to_dict as backup_to_dict
from retrace.config import load_config
from retrace.retention import (
    RetentionPolicy,
    apply_retention,
    result_to_dict as retention_to_dict,
)
from retrace.storage import Storage


@click.group("data")
def data_group() -> None:
    """Retention sweeps + install backups."""


@data_group.group("retention")
def data_retention_group() -> None:
    """Apply configured retention policies."""


@data_retention_group.command("apply")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would be purged without modifying anything.",
)
def data_retention_apply(config_path: Path, dry_run: bool) -> None:
    """Purge rows + run artifacts older than configured TTLs."""
    cfg = load_config(config_path)
    policy = RetentionPolicy(
        failures_days=cfg.retention.failures_days,
        evidence_days=cfg.retention.evidence_days,
        source_maps_days=cfg.retention.source_maps_days,
        rate_limit_hours=cfg.retention.rate_limit_hours,
        replay_batches_days=cfg.retention.replay_batches_days,
        otel_events_days=cfg.retention.otel_events_days,
        run_artifact_days=cfg.retention.run_artifact_days,
    )
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    result = apply_retention(
        store=store,
        data_dir=Path(cfg.run.data_dir),
        policy=policy,
        dry_run=dry_run,
    )
    click.echo(json.dumps(retention_to_dict(result), indent=2, sort_keys=True))


@data_group.command("backup")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option(
    "--to",
    "output_path",
    required=True,
    type=click.Path(path_type=Path, dir_okay=False),
    help="Output `.tar.gz` path.",
)
def data_backup(config_path: Path, output_path: Path) -> None:
    """Snapshot sqlite + data_dir into a tarball."""
    cfg = load_config(config_path)
    db_path = Path(cfg.run.data_dir) / "retrace.db"
    if not db_path.exists():
        raise click.ClickException(
            f"database not found: {db_path} — run `retrace init` first?"
        )
    try:
        result = create_backup(
            db_path=db_path,
            data_dir=Path(cfg.run.data_dir),
            output_path=Path(output_path),
        )
    except (OSError, IsADirectoryError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(backup_to_dict(result), indent=2, sort_keys=True))
