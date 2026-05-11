from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import click

from retrace.config import load_config
from retrace.fix_suggestions import (
    generate_fix_suggestions,
    parsed_finding_from_replay_issue,
    replay_issue_report_key,
    slugify,
)
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
@click.option(
    "--environment", "environment_name", default="production", show_default=True
)
@click.option("--generate-spec/--no-generate-spec", default=True, show_default=True)
@click.option(
    "--generate-fix-prompts/--no-generate-fix-prompts",
    default=True,
    show_default=True,
)
@click.option("--demo-repo", default="local/demo-checkout", show_default=True)
@click.option(
    "--demo-repo-path",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Directory for the generated local demo source tree.",
)
def seed_demo(
    config_path: Path,
    app_url: str,
    session_id: str,
    project_name: str,
    environment_name: str,
    generate_spec: bool,
    generate_fix_prompts: bool,
    demo_repo: str,
    demo_repo_path: Path | None,
) -> None:
    """Create a replay-backed demo issue, regression spec, and fix prompts."""
    _write_demo_config_if_missing(config_path)
    cfg = load_config(config_path)
    data_dir = cfg.run.data_dir
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
    fix_prompts_payload: dict[str, Any] = {}
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

    if generate_fix_prompts:
        repo_path = demo_repo_path or (data_dir / "demo-repo")
        _write_demo_repo(repo_path)
        repo_id = store.upsert_github_repo(
            repo_full_name=demo_repo,
            default_branch="main",
            remote_url=f"https://github.com/{demo_repo}.git",
            local_path=str(repo_path),
            provider="github",
        )
        repo = store.get_github_repo(demo_repo)
        if repo is None:
            raise click.ClickException(f"Demo repo could not be connected: {repo_id}")
        issue_row = store.get_replay_issue(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            issue_id=issue.public_id,
        )
        if issue_row is None:
            raise click.ClickException(f"Demo replay issue missing: {issue.public_id}")
        suggestions = generate_fix_suggestions(
            store=store,
            repo=repo,
            repo_path=repo_path,
            out_dir=cfg.run.output_dir / "fix-prompts",
            report_key=replay_issue_report_key(issue.public_id),
            source_label=f"replay issue {issue.public_id}",
            artifact_stem=f"replay-{slugify(issue.public_id)}",
            findings=[parsed_finding_from_replay_issue(issue_row)],
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
        )
        artifact = suggestions.artifacts[0] if suggestions.artifacts else None
        fix_prompts_payload = {
            "repo": suggestions.repo_full_name,
            "repo_path": suggestions.repo_path,
            "out_dir": str(suggestions.out_dir),
            "artifact_json": artifact.artifact_json if artifact else "",
            "prompt_files": artifact.prompt_files if artifact else {},
            "candidates": [
                {
                    "file_path": c.file_path,
                    "score": c.score,
                    "rationale": c.rationale,
                }
                for c in (artifact.candidates if artifact else [])
            ],
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
                "fix_prompts": fix_prompts_payload or None,
                "next_commands": [
                    "retrace tester from-replay-issue "
                    f"--config {shlex.quote(str(config_path))} {issue.public_id}",
                    "retrace suggest-fixes "
                    f"--config {shlex.quote(str(config_path))} "
                    f"--replay-issue {issue.public_id} "
                    f"--project-id {workspace.project_id} "
                    f"--environment-id {workspace.environment_id} "
                    f"--repo {shlex.quote(demo_repo)}",
                    f"retrace ui --config {shlex.quote(str(config_path))}",
                ],
            },
            indent=2,
        )
    )


@demo_group.command("all")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--project", "project_name", default="Default", show_default=True)
@click.option("--environment", "environment_name", default="production", show_default=True)
def demo_all(
    config_path: Path,
    project_name: str,
    environment_name: str,
) -> None:
    """Seed every pillar of the QA incident pipeline at once.

    Produces incidents from all five sources — replay, UI test, API test,
    error monitor (Sentry-compat), and PR review — so a fresh install
    can immediately run `retrace qa list` and see the unified queue in
    action.
    """
    from retrace.commands.demo_all import seed_all_pillars

    _write_demo_config_if_missing(config_path)
    seed_all_pillars(
        config_path=config_path,
        project_name=project_name,
        environment_name=environment_name,
    )


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
                        "TypeError: Cannot read properties of undefined "
                        "(reading 'total') at src/checkout.tsx:8"
                    ],
                },
            },
        },
    ]


def _write_demo_repo(repo_path: Path) -> None:
    src_dir = repo_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "checkout.tsx").write_text(
        """export function Checkout({ cart }: { cart?: { total?: number } }) {
  const total = cart!.total!.toFixed(2);
  return (
    <button data-testid="checkout-pay" onClick={() => submitPayment(total)}>
      Pay now
    </button>
  );
}

function submitPayment(total: string) {
  return fetch("/api/payments", { method: "POST", body: JSON.stringify({ total }) });
}
""",
        encoding="utf-8",
    )
