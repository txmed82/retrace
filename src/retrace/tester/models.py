from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_HARNESS_COMMAND = (
    "browser-harness run --url {app_url} --task {prompt_q} --output {run_dir_q}"
)
DEFAULT_APP_URL = "http://127.0.0.1:3000"
SPEC_SCHEMA_VERSION = 2
ALLOWED_MODES = {"describe", "explore_suite"}
ALLOWED_AUTH_MODES = {"none", "form", "jwt", "headers"}
ALLOWED_EXECUTION_ENGINES = {"harness", "native", "explore", "visual", "auto"}
SUITE_PROPOSAL_SCHEMA_VERSION = "suite_proposal.v1"
FAILURE_CLASSIFICATIONS = {
    "app_bug",
    "test_bug",
    "environment_failure",
    "auth_failure",
    "timeout",
    "selector_drift",
    "unknown",
}

logger = logging.getLogger(__name__)


@dataclass
class TesterSpec:
    schema_version: int
    spec_id: str
    name: str
    mode: str
    prompt: str
    app_url: str
    start_command: str
    harness_command: str
    auth_required: bool
    auth_mode: str
    auth_login_url: str
    auth_username: str
    auth_password_env: str
    auth_jwt_env: str
    auth_headers_env: str
    created_at: str
    updated_at: str
    auth_profile: str = ""
    auth_setup_steps: list[dict[str, Any]] = field(default_factory=list)
    execution_engine: str = "harness"
    exact_steps: list[dict[str, Any]] = field(default_factory=list)
    exploratory_goals: list[str] = field(default_factory=list)
    assertions: list[dict[str, Any]] = field(default_factory=list)
    env_overrides: dict[str, str] = field(default_factory=dict)
    browser_settings: dict[str, Any] = field(default_factory=dict)
    fixtures: dict[str, Any] = field(default_factory=dict)
    data_extraction: list[dict[str, Any]] = field(default_factory=list)
    schedules: list[dict[str, Any]] = field(default_factory=list)
    step_cache_enabled: bool = True
    assertion_consensus_enabled: bool = True


@dataclass
class TesterAssertionResult:
    assertion_id: str
    assertion_type: str
    ok: bool
    expected: Any
    actual: Any
    message: str
    source: str = "native"
    confidence: float = 1.0
    consensus_group: str = ""
    model_votes: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TesterArtifact:
    artifact_id: str
    artifact_type: str
    path: str
    label: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TesterStepCacheEvent:
    step_id: str
    cache_key: str
    status: str
    cached_url: str
    resolved_url: str
    message: str


@dataclass
class TesterRunResult:
    run_id: str
    spec_id: str
    ok: bool
    exit_code: int
    run_dir: str
    harness_log_path: str
    app_log_path: str
    command: str
    final_prompt: str
    attempts: int
    flaky: bool
    flake_reason: str
    status: str
    failure_classification: str = "unknown"
    error: str = ""
    execution_engine: str = "harness"
    engine_reason: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    assertion_results: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class EngineSelection:
    execution_engine: str
    reason: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return s or "ui-test"


def specs_dir_for_data_dir(data_dir: Path) -> Path:
    return data_dir / "ui-tests" / "specs"


def runs_dir_for_data_dir(data_dir: Path) -> Path:
    return data_dir / "ui-tests" / "runs"


def queue_dir_for_data_dir(data_dir: Path) -> Path:
    return data_dir / "ui-tests" / "queue"


def skills_dir_for_data_dir(data_dir: Path) -> Path:
    return data_dir / "ui-tests" / "skills"
