# STAGE: plan-c-stub — Native UI tester configuration.
#
# Plan C scope: GitHub code matching + AI fix prompts for Retrace findings.
# The tester module provides auth/env profiles for automated browser-based
# verification of fix candidates in real application environments.
#
# FUTURE:
#   - Expand auth profiles to support OAuth2, cookie-based, and multi-step flows.
#   - Add TesterRunConfig for per-run settings (timeouts, retries, screenshots).
#   - Add TesterResult model for structured test outcomes.
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


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
