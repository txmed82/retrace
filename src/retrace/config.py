from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, Optional

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


class GitHubAppConfig(BaseModel):
    app_id: str = ""
    webhook_secret: str = ""
    private_key: str = ""
    installation_id: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.app_id.strip() and self.webhook_secret.strip())


class NotificationConfig(BaseModel):
    webhook_url: str = ""
    webhook_secret: str = ""
    slack_webhook_url: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url.strip() or self.slack_webhook_url.strip())


class TesterAuthProfileConfig(BaseModel):
    mode: Literal["form", "jwt", "headers"] = "headers"
    login_url: str = ""
    username: str = ""
    password_env: str = "RETRACE_TESTER_AUTH_PASSWORD"
    jwt_env: str = "RETRACE_TESTER_AUTH_JWT"
    headers_env: str = "RETRACE_TESTER_AUTH_HEADERS"
    auth_setup_steps: list[dict[str, Any]] = Field(default_factory=list)
    browser_settings: dict[str, Any] = Field(default_factory=dict)


class TesterEnvProfileConfig(BaseModel):
    app_url: str = ""
    api_base_url: str = ""
    env_overrides: dict[str, str] = Field(default_factory=dict)
    headers_env: str = ""


class TesterConfig(BaseModel):
    auth_profiles: dict[str, TesterAuthProfileConfig] = Field(default_factory=dict)
    env_profiles: dict[str, TesterEnvProfileConfig] = Field(default_factory=dict)


class RetraceConfig(BaseModel):
    posthog: PostHogConfig
    llm: LLMConfig
    run: RunConfig = Field(default_factory=RunConfig)
    detectors: DetectorsConfig = Field(default_factory=DetectorsConfig)
    cluster: ClusterConfig = Field(default_factory=ClusterConfig)
    linear: LinearConfig = Field(default_factory=LinearConfig)
    github_sink: GitHubSinkConfig = Field(default_factory=GitHubSinkConfig)
    github_app: GitHubAppConfig = Field(default_factory=GitHubAppConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    tester: TesterConfig = Field(default_factory=TesterConfig)


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
