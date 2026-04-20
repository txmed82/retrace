from __future__ import annotations

from pathlib import Path

import click
import httpx
import questionary


LLM_KINDS = [
    "Local (llama.cpp / ollama / LM Studio)",
    "OpenAI",
    "Anthropic (via LiteLLM proxy)",
    "Custom OpenAI-compatible endpoint",
]


@click.command("init")
def init_command() -> None:
    """Interactive setup: prompts for PostHog + LLM, live-validates, writes config."""
    click.echo("Welcome to Retrace. Let's get you set up.\n")

    ph_host = questionary.text(
        "PostHog host?", default="https://us.i.posthog.com"
    ).ask()
    ph_project_id = questionary.text("PostHog project ID?").ask()
    ph_api_key = questionary.password("PostHog personal API key (phx_...)?").ask()

    _validate_posthog(ph_host, ph_project_id, ph_api_key)

    llm_kind = questionary.select(
        "LLM backend?",
        choices=LLM_KINDS,
    ).ask()

    default_base = {
        LLM_KINDS[0]: "http://localhost:8080/v1",
        LLM_KINDS[1]: "https://api.openai.com/v1",
        LLM_KINDS[2]: "http://localhost:8000/v1",
        LLM_KINDS[3]: "",
    }[llm_kind]
    default_model = {
        LLM_KINDS[0]: "llama-3.1-8b-instruct",
        LLM_KINDS[1]: "gpt-4o-mini",
        LLM_KINDS[2]: "claude-3-5-sonnet",
        LLM_KINDS[3]: "",
    }[llm_kind]

    llm_base_url = questionary.text("LLM base URL?", default=default_base).ask()
    llm_model = questionary.text("LLM model?", default=default_model).ask()
    llm_api_key = questionary.password("LLM API key (empty for local)?").ask()

    _validate_llm(llm_base_url, llm_model, llm_api_key)

    lookback_hours = int(
        questionary.text("Lookback hours on first run?", default="6").ask()
    )
    max_sessions_per_run = int(
        questionary.text("Max sessions per run?", default="50").ask()
    )
    output_dir = questionary.text(
        "Report output directory?", default="./reports"
    ).ask()
    data_dir = questionary.text("Data directory?", default="./data").ask()

    _write_config(
        config_path=Path("config.yaml"),
        env_path=Path(".env"),
        ph_host=ph_host,
        ph_project_id=ph_project_id,
        ph_api_key=ph_api_key,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        lookback_hours=lookback_hours,
        max_sessions_per_run=max_sessions_per_run,
        output_dir=output_dir,
        data_dir=data_dir,
    )
    click.echo("\n\u2713 Wrote config.yaml")
    click.echo("\u2713 Wrote .env")

    run_now = questionary.confirm("Run `retrace run` now?", default=False).ask()
    if run_now:
        click.echo("Running... (use `retrace run` to run again later)")
        from retrace.cli import run as run_cmd
        ctx = click.Context(run_cmd)
        ctx.invoke(run_cmd, config_path=Path("config.yaml"))


def _validate_posthog(host: str, project_id: str, api_key: str) -> None:
    url = f"{host.rstrip('/')}/api/projects/{project_id}/"
    with httpx.Client(timeout=15) as c:
        resp = c.get(url, headers={"Authorization": f"Bearer {api_key}"})
        resp.raise_for_status()
    click.echo("  \u2713 PostHog connection OK")


def _validate_llm(base_url: str, model: str, api_key: str) -> None:
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 4,
    }
    with httpx.Client(timeout=30) as c:
        resp = c.post(url, headers=headers, json=body)
        resp.raise_for_status()
    click.echo("  \u2713 LLM endpoint OK")


def _write_config(
    *,
    config_path: Path,
    env_path: Path,
    ph_host: str,
    ph_project_id: str,
    ph_api_key: str,
    llm_base_url: str,
    llm_model: str,
    llm_api_key: str,
    lookback_hours: int,
    max_sessions_per_run: int,
    output_dir: str,
    data_dir: str,
) -> None:
    config_path.write_text(
        f"""posthog:
  host: {ph_host}
  project_id: "{ph_project_id}"

llm:
  base_url: {llm_base_url}
  model: {llm_model}

run:
  lookback_hours: {lookback_hours}
  max_sessions_per_run: {max_sessions_per_run}
  output_dir: {output_dir}
  data_dir: {data_dir}

detectors:
  console_error: true
  network_5xx: true
  network_4xx: true
  rage_click: true
  dead_click: true
  error_toast: true
  blank_render: true
  session_abandon_on_error: true

cluster:
  min_size: 1
"""
    )
    env_lines = [f"RETRACE_POSTHOG_API_KEY={ph_api_key}"]
    if llm_api_key:
        env_lines.append(f"RETRACE_LLM_API_KEY={llm_api_key}")
    env_path.write_text("\n".join(env_lines) + "\n")
