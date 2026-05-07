from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import click

from retrace.config import load_config
from retrace.replay_core import ReplaySignalConfig, process_replay_sessions
from retrace.replay_specs import generate_spec_from_replay_issue
from retrace.storage import Storage
from retrace.tester import specs_dir_for_data_dir


@click.group("demo")
def demo_group() -> None:
    """Seed local demo data for the capture-to-test workflow."""


@demo_group.command("seed")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--app-url", default="https://demo.retrace.local", show_default=True)
@click.option("--session-id", default="demo-checkout-crash", show_default=True)
@click.option("--project", "project_name", default="Default", show_default=True)
@click.option("--environment", "environment_name", default="production", show_default=True)
@click.option("--generate-spec/--no-generate-spec", default=True, show_default=True)
def seed_demo(
    config_path: Path,
    app_url: str,
    session_id: str,
    project_name: str,
    environment_name: str,
    generate_spec: bool,
) -> None:
    """Create a replay-backed demo issue and optional regression spec."""
    _write_demo_config_if_missing(config_path)
    data_dir = _data_dir_from_config(config_path)
    store = Storage(data_dir / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(
        org_name="Local",
        project_name=project_name,
        environment_name=environment_name,
    )

    events = _demo_checkout_events(app_url.rstrip("/"))
    batch = store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id=session_id,
        sequence=0,
        events=events,
        flush_type="final",
        distinct_id="demo-user-1",
        metadata={
            "source": "retrace_demo",
            "route": "/checkout",
            "scenario": "checkout_total_crash",
        },
    )
    processed = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=[session_id],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    if not processed.issues:
        raise click.ClickException("Demo replay did not produce an issue.")

    issue = processed.issues[0]
    generated_payload: dict[str, Any] = {}
    if generate_spec:
        generated = generate_spec_from_replay_issue(
            store=store,
            specs_dir=specs_dir_for_data_dir(data_dir),
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            issue_id=issue.public_id,
            app_url=app_url,
        )
        generated_payload = {
            "spec_id": generated.spec.spec_id,
            "replay_public_id": generated.replay_public_id,
            "confidence": generated.confidence,
            "known_gaps": generated.known_gaps,
        }

    click.echo(
        json.dumps(
            {
                "config_path": str(config_path),
                "project_id": workspace.project_id,
                "environment_id": workspace.environment_id,
                "session_id": session_id,
                "batch_inserted": batch.inserted,
                "sessions_scanned": processed.sessions_scanned,
                "signals_detected": processed.signals_detected,
                "issue_public_id": issue.public_id,
                "issue_inserted": issue.inserted,
                "issue_regressed": issue.regressed,
                "tester_spec": generated_payload or None,
                "next_commands": [
                    "retrace tester from-replay-issue "
                    f"--config {shlex.quote(str(config_path))} {issue.public_id}",
                    f"retrace ui --config {shlex.quote(str(config_path))}",
                ],
            },
            indent=2,
        )
    )


def _data_dir_from_config(config_path: Path) -> Path:
    if config_path.exists():
        return load_config(config_path).run.data_dir
    return Path("./data")


def _write_demo_config_if_missing(config_path: Path) -> None:
    if config_path.exists():
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        """posthog:
  host: https://us.i.posthog.com
  project_id: demo
  api_key: ""
llm:
  provider: openai_compatible
  base_url: http://127.0.0.1:8080/v1
  model: local-demo
  api_key: ""
run:
  lookback_hours: 6
  max_sessions_per_run: 50
  output_dir: ./reports
  data_dir: ./data
detectors:
  console_error: true
cluster:
  min_size: 1
""",
        encoding="utf-8",
    )


def _demo_checkout_events(base_url: str) -> list[dict[str, Any]]:
    checkout_url = f"{base_url}/checkout"
    return [
        {
            "type": 4,
            "timestamp": 0,
            "data": {"href": checkout_url},
        },
        {
            "type": 6,
            "timestamp": 100,
            "data": {
                "plugin": "retrace/click@1",
                "payload": {
                    "button": 0,
                    "target": {
                        "tagName": "button",
                        "testIdAttrName": "data-testid",
                        "testIdValue": "checkout-pay",
                        "text": "Pay now",
                    },
                    "url": checkout_url,
                },
            },
        },
        {
            "type": 3,
            "timestamp": 110,
            "data": {"source": 2, "type": 2, "id": 42},
        },
        {
            "type": 6,
            "timestamp": 250,
            "data": {
                "plugin": "retrace/console@1",
                "payload": {
                    "level": "error",
                    "payload": [
                        "TypeError: Cannot read properties of undefined (reading 'total')"
                    ],
                },
            },
        },
    ]
