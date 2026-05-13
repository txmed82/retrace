# STAGE: core — PostHog, LLM, run, detectors, and cluster configuration.
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

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


class RetraceConfig(BaseModel):
    posthog: PostHogConfig
    llm: LLMConfig
    run: RunConfig = Field(default_factory=RunConfig)
    detectors: DetectorsConfig = Field(default_factory=DetectorsConfig)
    cluster: ClusterConfig = Field(default_factory=ClusterConfig)
    linear: LinearConfig = Field(default_factory=lambda: LinearConfig())
    github_sink: GitHubSinkConfig = Field(default_factory=lambda: GitHubSinkConfig())
    github_app: GitHubAppConfig = Field(default_factory=lambda: GitHubAppConfig())
    notifications: NotificationConfig = Field(default_factory=lambda: NotificationConfig())
    tester: TesterConfig = Field(default_factory=lambda: TesterConfig())
    retention: RetentionConfig = Field(default_factory=lambda: RetentionConfig())


# Resolve forward references by importing sibling modules at the bottom.
# Relative imports avoid re-triggering the parent package __init__.py
# (which would cause a circular import since __init__.py imports from us).
# With `from __future__ import annotations` active, type annotations are
# deferred strings — they only resolve at model_rebuild() time below.
from .retention import RetentionConfig  # noqa: E402
from .sinks import (  # noqa: E402
    GitHubAppConfig,
    GitHubSinkConfig,
    LinearConfig,
    NotificationConfig,
)
from .tester import TesterConfig  # noqa: E402

RetraceConfig.model_rebuild()
