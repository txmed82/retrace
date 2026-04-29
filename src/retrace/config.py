from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class PostHogConfig(BaseModel):
    host: str
    app_host: Optional[str] = None
    project_id: str
    api_key: str


class LLMConfig(BaseModel):
    provider: Literal["openai_compatible", "openai", "anthropic", "openrouter"] = (
        "openai_compatible"
    )
    base_url: str
    model: str
    api_key: Optional[str] = None
    timeout_seconds: int = 120


class RunConfig(BaseModel):
    lookback_hours: int = 6
    max_sessions_per_run: int = 50
    output_dir: Path = Path("./reports")
    data_dir: Path = Path("./data")


class DetectorsConfig(BaseModel):
    console_error: bool = True
    network_5xx: bool = True
    network_4xx: bool = True
    rage_click: bool = True
    dead_click: bool = True
    error_toast: bool = True
    blank_render: bool = True
    session_abandon_on_error: bool = True


class ClusterConfig(BaseModel):
    min_size: int = 1


class LinearConfig(BaseModel):
    api_key: str = ""
    team_id: str = ""
    team_key: str = ""
    labels: list[str] = Field(default_factory=list)
    endpoint: str = "https://api.linear.app/graphql"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key.strip())


class GitHubSinkConfig(BaseModel):
    api_key: str = ""
    repo: str = ""
    labels: list[str] = Field(default_factory=list)
    base_url: str = "https://api.github.com"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key.strip())


class NotificationConfig(BaseModel):
    webhook_url: str = ""
    webhook_secret: str = ""
    slack_webhook_url: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url.strip() or self.slack_webhook_url.strip())


class RetraceConfig(BaseModel):
    posthog: PostHogConfig
    llm: LLMConfig
    run: RunConfig = Field(default_factory=RunConfig)
    detectors: DetectorsConfig = Field(default_factory=DetectorsConfig)
    cluster: ClusterConfig = Field(default_factory=ClusterConfig)
    linear: LinearConfig = Field(default_factory=LinearConfig)
    github_sink: GitHubSinkConfig = Field(default_factory=GitHubSinkConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)


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
