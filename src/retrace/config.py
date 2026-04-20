from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, HttpUrl


class PostHogConfig(BaseModel):
    host: str
    project_id: str
    api_key: str


class LLMConfig(BaseModel):
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


class RetraceConfig(BaseModel):
    posthog: PostHogConfig
    llm: LLMConfig
    run: RunConfig = Field(default_factory=RunConfig)
    detectors: DetectorsConfig = Field(default_factory=DetectorsConfig)


def load_config(path: Path) -> RetraceConfig:
    load_dotenv(override=False)
    raw = yaml.safe_load(Path(path).read_text()) or {}

    posthog_key_env = os.environ.get("RETRACE_POSTHOG_API_KEY")
    if posthog_key_env:
        raw.setdefault("posthog", {})["api_key"] = posthog_key_env
    elif "api_key" not in raw.setdefault("posthog", {}):
        raw["posthog"]["api_key"] = ""

    llm_key_env = os.environ.get("RETRACE_LLM_API_KEY")
    if llm_key_env:
        raw.setdefault("llm", {})["api_key"] = llm_key_env

    return RetraceConfig.model_validate(raw)
