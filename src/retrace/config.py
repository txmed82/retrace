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


class RetraceConfig(BaseModel):
    posthog: PostHogConfig
    llm: LLMConfig
    run: RunConfig = Field(default_factory=RunConfig)
    detectors: DetectorsConfig = Field(default_factory=DetectorsConfig)
    cluster: ClusterConfig = Field(default_factory=ClusterConfig)


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

    return RetraceConfig.model_validate(raw)