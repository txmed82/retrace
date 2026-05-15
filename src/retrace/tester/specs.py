from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from .models import (
    ALLOWED_AUTH_MODES,
    ALLOWED_EXECUTION_ENGINES,
    ALLOWED_MODES,
    DEFAULT_APP_URL,
    DEFAULT_HARNESS_COMMAND,
    SPEC_SCHEMA_VERSION,
    EngineSelection,
    TesterSpec,
    now_iso,
    slugify,
)

if TYPE_CHECKING:
    from .harness import _should_use_playwright


def _spec_path(specs_dir: Path, spec_id: str) -> Path:
    # Validate spec_id to prevent path traversal
    if not spec_id or not re.match(r"^[a-zA-Z0-9_-]+$", spec_id):
        raise ValueError(
            "Invalid spec_id: must contain only alphanumeric characters, hyphens, and underscores"
        )

    # Build the path and verify it's contained within specs_dir
    candidate_path = (specs_dir / f"{spec_id}.json").resolve()
    try:
        specs_dir_resolved = specs_dir.resolve()
        candidate_path.relative_to(specs_dir_resolved)
    except (ValueError, RuntimeError):
        raise ValueError("Invalid spec_id: path traversal detected")

    return candidate_path


def save_spec(specs_dir: Path, spec: TesterSpec) -> Path:
    specs_dir.mkdir(parents=True, exist_ok=True)
    p = _spec_path(specs_dir, spec.spec_id)
    p.write_text(json.dumps(asdict(spec), indent=2) + "\n")
    return p


def load_spec(specs_dir: Path, spec_id: str) -> TesterSpec:
    p = _spec_path(specs_dir, spec_id)
    data = json.loads(p.read_text())
    _apply_spec_defaults(data)
    return _spec_from_data(data)


def list_specs(specs_dir: Path) -> list[TesterSpec]:
    if not specs_dir.exists():
        return []
    out: list[TesterSpec] = []
    for p in sorted(specs_dir.glob("*.json"), key=lambda x: x.stat().st_mtime):
        try:
            data = json.loads(p.read_text())
            _apply_spec_defaults(data)
            out.append(_spec_from_data(data))
        except Exception:
            continue
    return out


def _spec_from_data(data: dict[str, Any]) -> TesterSpec:
    allowed = {f.name for f in fields(TesterSpec)}
    return TesterSpec(**{k: v for k, v in data.items() if k in allowed})


def _apply_spec_defaults(data: dict[str, Any]) -> None:
    data.setdefault("schema_version", SPEC_SCHEMA_VERSION)
    data.setdefault("mode", "describe")
    data.setdefault("auth_required", False)
    data.setdefault("auth_mode", "none")
    data.setdefault("auth_login_url", "")
    data.setdefault("auth_username", "")
    data.setdefault("auth_password_env", "RETRACE_TESTER_AUTH_PASSWORD")
    data.setdefault("auth_jwt_env", "RETRACE_TESTER_AUTH_JWT")
    data.setdefault("auth_headers_env", "RETRACE_TESTER_AUTH_HEADERS")
    data.setdefault("auth_profile", "")
    data.setdefault("auth_setup_steps", [])
    data.setdefault("execution_engine", "harness")
    data.setdefault("exact_steps", [])
    data.setdefault("exploratory_goals", [])
    data.setdefault("assertions", [])
    data.setdefault("env_overrides", {})
    data.setdefault("browser_settings", {})
    data.setdefault("fixtures", {})
    data.setdefault("data_extraction", [])
    data.setdefault("schedules", [])
    data.setdefault("step_cache_enabled", True)
    data.setdefault("assertion_consensus_enabled", True)
    try:
        if int(data.get("schema_version") or 1) < SPEC_SCHEMA_VERSION:
            data["schema_version"] = SPEC_SCHEMA_VERSION
    except (TypeError, ValueError):
        data["schema_version"] = SPEC_SCHEMA_VERSION
    # Legacy mode migration.
    mode = str(data.get("mode") or "describe").strip().lower()
    if mode in {"prompt", "video"}:
        data["mode"] = "describe"
    elif mode in {"explore", "suite"}:
        data["mode"] = "explore_suite"
    engine = str(data.get("execution_engine") or "harness").strip().lower()
    data["execution_engine"] = engine


def _auth_needed_for_engine(spec: TesterSpec) -> bool:
    return bool(spec.auth_required or spec.auth_profile or spec.auth_setup_steps)


def validate_spec(spec: TesterSpec) -> None:
    if spec.schema_version != SPEC_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported tester schema_version={spec.schema_version}. "
            f"Expected {SPEC_SCHEMA_VERSION}."
        )
    if not spec.spec_id.strip():
        raise ValueError("spec_id is required")
    if not spec.name.strip():
        raise ValueError("name is required")
    if spec.mode not in ALLOWED_MODES:
        raise ValueError(f"mode must be one of: {sorted(ALLOWED_MODES)}")
    if not spec.app_url.strip():
        raise ValueError("app_url is required")
    if spec.execution_engine not in ALLOWED_EXECUTION_ENGINES:
        raise ValueError(
            f"execution_engine must be one of: {sorted(ALLOWED_EXECUTION_ENGINES)}"
        )
    explore_selected = spec.execution_engine == "explore" or (
        spec.execution_engine == "auto"
        and bool(spec.exploratory_goals)
        and not (spec.exact_steps or spec.assertions)
    )
    auto_explore_needs_harness_auth = (
        spec.execution_engine == "auto"
        and bool(spec.exploratory_goals)
        and _auth_needed_for_engine(spec)
    )
    needs_harness_command = (
        spec.execution_engine == "harness"
        or auto_explore_needs_harness_auth
        or (
            spec.execution_engine == "auto"
            and not (spec.exact_steps or spec.assertions or spec.exploratory_goals)
        )
    )
    native_selected = spec.execution_engine == "native" or (
        spec.execution_engine == "auto" and bool(spec.exact_steps or spec.assertions)
    )
    if native_selected and spec.auth_required and spec.auth_mode == "form":
        raise ValueError("native execution does not support form auth profiles")
    if explore_selected and not spec.exploratory_goals:
        raise ValueError("explore execution requires at least one exploratory_goal")
    if spec.execution_engine == "explore" and (spec.exact_steps or spec.assertions):
        # The explore engine doesn't read exact_steps or assertions; refuse
        # specs that set them so the silent-ignore footgun stays closed.
        raise ValueError(
            "explore execution does not support exact_steps/assertions; "
            "use execution_engine='native' or 'auto' for deterministic specs"
        )
    if spec.execution_engine == "visual":
        if not spec.exploratory_goals:
            raise ValueError("visual execution requires at least one exploratory_goal")
        if spec.exact_steps or spec.assertions:
            # Same footgun guard as explore -- visual mode is exploratory; if
            # someone wants deterministic steps they should pick native.
            raise ValueError(
                "visual execution does not support exact_steps/assertions; "
                "use execution_engine='native' for deterministic specs"
            )
    if needs_harness_command:
        if not spec.harness_command.strip():
            raise ValueError("harness_command is required")
        if "{app_url" not in spec.harness_command:
            raise ValueError("harness_command must include {app_url} or {app_url_q}")
        if "{run_dir" not in spec.harness_command:
            raise ValueError("harness_command must include {run_dir} or {run_dir_q}")
        if "{prompt" not in spec.harness_command:
            raise ValueError("harness_command must include {prompt} or {prompt_q}")
    if spec.auth_mode not in ALLOWED_AUTH_MODES:
        raise ValueError(f"auth_mode must be one of: {sorted(ALLOWED_AUTH_MODES)}")
    if spec.auth_required and spec.auth_mode == "none":
        raise ValueError("auth_mode cannot be 'none' when auth_required=true")
    if spec.auth_mode == "form" and spec.auth_required and not spec.auth_login_url:
        raise ValueError("auth_login_url is required for form auth")
    if not isinstance(spec.exact_steps, list):
        raise ValueError("exact_steps must be a list")
    if not isinstance(spec.exploratory_goals, list):
        raise ValueError("exploratory_goals must be a list")
    if not isinstance(spec.assertions, list):
        raise ValueError("assertions must be a list")
    if not isinstance(spec.env_overrides, dict):
        raise ValueError("env_overrides must be an object")
    if not isinstance(spec.auth_setup_steps, list):
        raise ValueError("auth_setup_steps must be a list")
    if not isinstance(spec.browser_settings, dict):
        raise ValueError("browser_settings must be an object")
    if not isinstance(spec.fixtures, dict):
        raise ValueError("fixtures must be an object")
    if not isinstance(spec.data_extraction, list):
        raise ValueError("data_extraction must be a list")
    if not isinstance(spec.schedules, list):
        raise ValueError("schedules must be a list")
    if not isinstance(spec.step_cache_enabled, bool):
        raise ValueError("step_cache_enabled must be a boolean")
    if not isinstance(spec.assertion_consensus_enabled, bool):
        raise ValueError("assertion_consensus_enabled must be a boolean")


def create_spec(
    *,
    specs_dir: Path,
    name: str,
    prompt: str,
    app_url: str,
    start_command: str,
    harness_command: str,
    mode: str = "describe",
    auth_required: bool = False,
    auth_mode: str = "none",
    auth_login_url: str = "",
    auth_username: str = "",
    auth_password_env: str = "RETRACE_TESTER_AUTH_PASSWORD",
    auth_jwt_env: str = "RETRACE_TESTER_AUTH_JWT",
    auth_headers_env: str = "RETRACE_TESTER_AUTH_HEADERS",
    auth_profile: str = "",
    auth_setup_steps: Optional[list[dict[str, Any]]] = None,
    execution_engine: str = "harness",
    exact_steps: Optional[list[dict[str, Any]]] = None,
    exploratory_goals: Optional[list[str]] = None,
    assertions: Optional[list[dict[str, Any]]] = None,
    env_overrides: Optional[dict[str, str]] = None,
    browser_settings: Optional[dict[str, Any]] = None,
    fixtures: Optional[dict[str, Any]] = None,
    data_extraction: Optional[list[dict[str, Any]]] = None,
    schedules: Optional[list[dict[str, Any]]] = None,
    step_cache_enabled: bool = True,
    assertion_consensus_enabled: bool = True,
) -> TesterSpec:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    spec_id = f"{ts}-{slugify(name)[:40]}-{uuid.uuid4().hex[:8]}"
    created_at = now_iso()
    spec = TesterSpec(
        schema_version=SPEC_SCHEMA_VERSION,
        spec_id=spec_id,
        name=name.strip() or "UI test",
        mode=(mode.strip() or "describe"),
        prompt=prompt.strip(),
        app_url=app_url.strip() or DEFAULT_APP_URL,
        start_command=start_command.strip(),
        harness_command=harness_command.strip() or DEFAULT_HARNESS_COMMAND,
        auth_required=bool(auth_required),
        auth_mode=(auth_mode.strip() or "none"),
        auth_login_url=auth_login_url.strip(),
        auth_username=auth_username.strip(),
        auth_password_env=(auth_password_env.strip() or "RETRACE_TESTER_AUTH_PASSWORD"),
        auth_jwt_env=(auth_jwt_env.strip() or "RETRACE_TESTER_AUTH_JWT"),
        auth_headers_env=(auth_headers_env.strip() or "RETRACE_TESTER_AUTH_HEADERS"),
        auth_profile=auth_profile.strip(),
        auth_setup_steps=auth_setup_steps or [],
        created_at=created_at,
        updated_at=created_at,
        execution_engine=(execution_engine.strip().lower() or "harness"),
        exact_steps=exact_steps or [],
        exploratory_goals=exploratory_goals or [],
        assertions=assertions or [],
        env_overrides=env_overrides or {},
        browser_settings=browser_settings or {},
        fixtures=fixtures or {},
        data_extraction=data_extraction or [],
        schedules=schedules or [],
        step_cache_enabled=bool(step_cache_enabled),
        assertion_consensus_enabled=bool(assertion_consensus_enabled),
    )
    validate_spec(spec)
    save_spec(specs_dir, spec)
    return spec


def _api_only_spec(spec: TesterSpec) -> bool:
    spec_type = str(
        (spec.fixtures or {}).get("spec_type") or (spec.fixtures or {}).get("type") or ""
    ).strip().lower()
    if spec_type in {"api", "api_test", "api-only"}:
        return True
    if not spec.exact_steps:
        return False
    api_actions = {"get", "post", "put", "patch", "delete", "head", "options", "request"}
    for step in spec.exact_steps:
        action = str(step.get("action") or step.get("type") or "get").lower()
        if action not in api_actions:
            return False
        target = str(step.get("url") or step.get("path") or "")
        if "/api/" not in target and not target.rstrip("/").endswith("/api"):
            return False
        if step.get("selector") or step.get("target"):
            return False
    return True


def _needs_playwright_runtime(spec: TesterSpec) -> bool:
    from .harness import _should_use_playwright

    steps = [*spec.auth_setup_steps, *(spec.exact_steps or [])]
    return _should_use_playwright(spec, steps)


def select_execution_engine(spec: TesterSpec) -> EngineSelection:
    engine = spec.execution_engine
    if engine != "auto":
        if engine == "native":
            runtime = "Playwright" if _needs_playwright_runtime(spec) else "native HTTP"
            return EngineSelection(
                execution_engine="native",
                reason=f"explicit native engine; {runtime} runtime selected by steps",
            )
        if engine == "explore":
            return EngineSelection(
                execution_engine="explore",
                reason="explicit explore engine for exploratory goals",
            )
        if engine == "visual":
            return EngineSelection(
                execution_engine="visual",
                reason="explicit visual engine for screenshot-guided exploration",
            )
        return EngineSelection(
            execution_engine="harness",
            reason="explicit Browser Harness engine",
        )

    if _api_only_spec(spec):
        return EngineSelection(
            execution_engine="native",
            reason=(
                "auto selected native HTTP because the spec is API-only and "
                "does not need a browser"
            ),
        )
    if spec.exact_steps or spec.assertions:
        runtime = "Playwright" if _needs_playwright_runtime(spec) else "native HTTP"
        return EngineSelection(
            execution_engine="native",
            reason=(
                "auto selected native because deterministic steps/assertions are "
                f"present; {runtime} runtime selected by steps"
            ),
        )
    if spec.exploratory_goals and _auth_needed_for_engine(spec):
        return EngineSelection(
            execution_engine="harness",
            reason=(
                "auto selected Browser Harness because exploratory auth setup "
                "requires credential-aware execution"
            ),
        )
    if spec.exploratory_goals:
        return EngineSelection(
            execution_engine="explore",
            reason="auto selected explore for exploratory goals",
        )
    return EngineSelection(
        execution_engine="harness",
        reason="auto selected Browser Harness (default fallback)",
    )
