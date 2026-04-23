from __future__ import annotations

from pathlib import Path

import click
import httpx
import questionary

from retrace.llm.client import build_llm_http_request, fetch_llm_models

LLM_KINDS = {
    "Local (OpenAI-compatible: llama.cpp / ollama / LM Studio)": "openai_compatible",
    "OpenAI API (cloud)": "openai",
    "Anthropic API (cloud)": "anthropic",
    "OpenRouter API (cloud)": "openrouter",
    "Custom OpenAI-compatible endpoint": "openai_compatible",
}

LLM_DEFAULTS = {
    "Local (OpenAI-compatible: llama.cpp / ollama / LM Studio)": {
        "base_url": "http://localhost:8080/v1",
        "model": "llama-3.1-8b-instruct",
    },
    "OpenAI API (cloud)": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "Anthropic API (cloud)": {
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-3-5-sonnet-latest",
    },
    "OpenRouter API (cloud)": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4o-mini",
    },
    "Custom OpenAI-compatible endpoint": {
        "base_url": "",
        "model": "",
    },
}
_CLOUD_PROVIDERS = {"openai", "anthropic", "openrouter"}
_CUSTOM_MODEL_SENTINEL = "<Enter custom model>"


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

    llm_kind_label = questionary.select(
        "LLM backend?",
        choices=list(LLM_KINDS.keys()),
    ).ask()
    llm_provider = LLM_KINDS[llm_kind_label]

    default_base = LLM_DEFAULTS[llm_kind_label]["base_url"]
    default_model = LLM_DEFAULTS[llm_kind_label]["model"]

    llm_base_url = questionary.text("LLM base URL?", default=default_base).ask()
    key_label = {
        "openai": "OpenAI API key (sk-...)?",
        "anthropic": "Anthropic API key (sk-ant-...)?",
        "openrouter": "OpenRouter API key (sk-or-...)?",
        "openai_compatible": "LLM API key (empty for local)?",
    }[llm_provider]
    llm_api_key = questionary.password(key_label).ask()
    if llm_provider in _CLOUD_PROVIDERS and not (llm_api_key or "").strip():
        raise click.ClickException(f"{llm_provider} requires an API key.")

    fetch_models = questionary.confirm(
        "Fetch available models from provider/API?",
        default=(llm_provider in _CLOUD_PROVIDERS),
    ).ask()
    models: list[str] = []
    if fetch_models:
        try:
            models = fetch_llm_models(
                provider=llm_provider,
                base_url=llm_base_url,
                api_key=llm_api_key or None,
            )
            if models:
                click.echo(f"  ✓ Found {len(models)} model(s)")
            else:
                click.echo("  ! No models returned, falling back to manual entry.")
        except Exception as exc:
            click.echo(
                f"  ! Model discovery failed ({exc}), falling back to manual entry."
            )

    if models:
        choices = list(models) + [_CUSTOM_MODEL_SENTINEL]
        selected = questionary.select(
            "LLM model?",
            choices=choices,
            default=default_model if default_model in models else choices[0],
        ).ask()
        if selected == _CUSTOM_MODEL_SENTINEL:
            llm_model = questionary.text("LLM model?", default=default_model).ask()
        else:
            llm_model = selected
    else:
        llm_model = questionary.text("LLM model?", default=default_model).ask()

    _validate_llm(llm_provider, llm_base_url, llm_model, llm_api_key)

    lookback_hours = int(
        questionary.text("Lookback hours on first run?", default="6").ask()
    )
    max_sessions_per_run = int(
        questionary.text("Max sessions per run?", default="50").ask()
    )
    output_dir = questionary.text("Report output directory?", default="./reports").ask()
    data_dir = questionary.text("Data directory?", default="./data").ask()

    _write_config(
        config_path=Path("config.yaml"),
        env_path=Path(".env"),
        ph_host=ph_host,
        ph_project_id=ph_project_id,
        ph_api_key=ph_api_key,
        llm_provider=llm_provider,
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


def _validate_llm(provider: str, base_url: str, model: str, api_key: str) -> None:
    url, headers, body = build_llm_http_request(
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=api_key,
        system="You are a test assistant.",
        user="reply with ping",
        temperature=0.0,
        response_json=False,
        max_tokens=8,
    )
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
    llm_provider: str,
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
  provider: {llm_provider}
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