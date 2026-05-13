# STAGE: core — Retrace configuration package.
#
# Domain models are split by concern:
#   base.py      — PostHog, LLM, run, detectors, cluster, and RetraceConfig
#   sinks.py     — Linear, GitHub, and notification sinks
#   tester.py    — Native UI tester auth/env profiles
#   retention.py — Data retention TTL configuration
#
# All models and the load_config helper are re-exported from here so
# existing callers (from retrace.config import X) continue to work.

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from retrace.config.base import (
    ClusterConfig,
    DetectorsConfig,
    LLMConfig,
    PostHogConfig,
    RetraceConfig,
    RunConfig,
)
from retrace.config.retention import RetentionConfig
from retrace.config.sinks import (
    GitHubAppConfig,
    GitHubSinkConfig,
    LinearConfig,
    NotificationConfig,
)
from retrace.config.tester import (
    TesterAuthProfileConfig,
    TesterConfig,
    TesterEnvProfileConfig,
)

__all__ = [
    "ClusterConfig",
    "DetectorsConfig",
    "GitHubAppConfig",
    "GitHubSinkConfig",
    "LinearConfig",
    "LLMConfig",
    "NotificationConfig",
    "PostHogConfig",
    "RetentionConfig",
    "RetraceConfig",
    "RunConfig",
    "TesterAuthProfileConfig",
    "TesterConfig",
    "TesterEnvProfileConfig",
    "load_config",
]


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def load_config(path: Path) -> RetraceConfig:
    config_path = Path(path)
    # Keep env scoping local to the chosen config directory.
    load_dotenv(dotenv_path=config_path.parent / ".env", override=False)
    raw = yaml.safe_load(config_path.read_text()) or {}

    posthog_key_env = os.environ.get("RETRACE_POSTHOG_API_KEY")
    if posthog_key_env:
        raw.setdefault("posthog", {})["api_key"] = posthog_key_env
    elif "api_key" not in raw.setdefault("posthog", {}):
        raw["posthog"]["api_key"] = ""

    llm_provider = (
        str(((raw.get("llm") or {}).get("provider") or "openai_compatible"))
        .strip()
        .lower()
    )
    raw.setdefault("llm", {})["provider"] = llm_provider
    llm_key_env = os.environ.get("RETRACE_LLM_API_KEY")
    if not llm_key_env:
        provider_env_map = {
            "openai": "RETRACE_OPENAI_API_KEY",
            "anthropic": "RETRACE_ANTHROPIC_API_KEY",
            "openrouter": "RETRACE_OPENROUTER_API_KEY",
        }
        provider_env = provider_env_map.get(llm_provider)
        if provider_env:
            llm_key_env = os.environ.get(provider_env)
    if llm_key_env:
        raw.setdefault("llm", {})["api_key"] = llm_key_env

    linear_key_env = os.environ.get("RETRACE_LINEAR_API_KEY")
    if linear_key_env:
        raw.setdefault("linear", {})["api_key"] = linear_key_env

    github_key_env = (
        os.environ.get("RETRACE_GITHUB_API_KEY")
        or os.environ.get("RETRACE_GITHUB_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
    )
    if github_key_env:
        raw.setdefault("github_sink", {})["api_key"] = github_key_env

    github_app_env_map = {
        "app_id": "RETRACE_GITHUB_APP_ID",
        "webhook_secret": "RETRACE_GITHUB_APP_WEBHOOK_SECRET",
        "private_key": "RETRACE_GITHUB_APP_PRIVATE_KEY",
        "installation_id": "RETRACE_GITHUB_APP_INSTALLATION_ID",
    }
    for key, env_name in github_app_env_map.items():
        value = os.environ.get(env_name)
        if value:
            raw.setdefault("github_app", {})[key] = value

    webhook_url_env = os.environ.get("RETRACE_NOTIFY_WEBHOOK_URL")
    if webhook_url_env:
        raw.setdefault("notifications", {})["webhook_url"] = webhook_url_env
    webhook_secret_env = os.environ.get("RETRACE_NOTIFY_WEBHOOK_SECRET")
    if webhook_secret_env:
        raw.setdefault("notifications", {})["webhook_secret"] = webhook_secret_env
    slack_webhook_env = os.environ.get("RETRACE_NOTIFY_SLACK_WEBHOOK_URL")
    if slack_webhook_env:
        raw.setdefault("notifications", {})["slack_webhook_url"] = slack_webhook_env

    return RetraceConfig.model_validate(raw)
