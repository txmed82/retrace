from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from retrace.llm.client import build_llm_http_request, extract_llm_text_content


DEFAULT_HARNESS_COMMAND = (
    "browser-harness run --url {app_url} --task {prompt_q} --output {run_dir_q}"
)
DEFAULT_APP_URL = "http://127.0.0.1:3000"
SPEC_SCHEMA_VERSION = 2
ALLOWED_MODES = {"describe", "explore_suite"}
ALLOWED_AUTH_MODES = {"none", "form", "jwt", "headers"}
ALLOWED_EXECUTION_ENGINES = {"harness", "native", "explore", "auto"}


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
    error: str = ""
    execution_engine: str = "harness"
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    assertion_results: list[dict[str, Any]] = field(default_factory=list)


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


def _spec_path(specs_dir: Path, spec_id: str) -> Path:
    # Validate spec_id to prevent path traversal
    if not spec_id or not re.match(r'^[a-zA-Z0-9_-]+$', spec_id):
        raise ValueError("Invalid spec_id: must contain only alphanumeric characters, hyphens, and underscores")

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
    needs_harness_command = spec.execution_engine == "harness" or (
        spec.execution_engine == "auto"
        and not (spec.exact_steps or spec.assertions or spec.exploratory_goals)
    )
    native_selected = spec.execution_engine == "native" or (
        spec.execution_engine == "auto" and bool(spec.exact_steps or spec.assertions)
    )
    if native_selected and spec.auth_required:
        raise ValueError("native execution does not yet support auth_required specs")
    if explore_selected and not spec.exploratory_goals:
        raise ValueError("explore execution requires at least one exploratory_goal")
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
        auth_password_env=(
            auth_password_env.strip() or "RETRACE_TESTER_AUTH_PASSWORD"
        ),
        auth_jwt_env=(auth_jwt_env.strip() or "RETRACE_TESTER_AUTH_JWT"),
        auth_headers_env=(
            auth_headers_env.strip() or "RETRACE_TESTER_AUTH_HEADERS"
        ),
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


def _run_shell(
    command: str,
    *,
    stdout_fh: Any,
    stderr_fh: Any,
    cwd: Optional[Path] = None,
    auth_context: Optional[dict[str, str]] = None,
    env_overrides: Optional[dict[str, str]] = None,
) -> subprocess.Popen[Any]:
    shell = os.environ.get("SHELL", "").strip()
    shell_cmd = shell if shell and shutil.which(shell) else ""
    if not shell_cmd:
        shell_cmd = shutil.which("bash") or shutil.which("sh") or "/bin/sh"

    # Prepare environment with auth credentials if provided
    env = os.environ.copy()
    if auth_context:
        if auth_context.get("password"):
            env["RETRACE_TESTER_AUTH_PASSWORD"] = auth_context["password"]
        if auth_context.get("jwt"):
            env["RETRACE_TESTER_AUTH_JWT"] = auth_context["jwt"]
        if auth_context.get("headers_json"):
            env["RETRACE_TESTER_AUTH_HEADERS"] = auth_context["headers_json"]
    if env_overrides:
        env.update({str(k): str(v) for k, v in env_overrides.items()})

    return subprocess.Popen(
        [shell_cmd, "-lc", command],
        stdout=stdout_fh,
        stderr=stderr_fh,
        cwd=str(cwd) if cwd else None,
        env=env,
    )


def _wait_for_http(url: str, timeout_seconds: int = 45) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=2.5, follow_redirects=True) as c:
                r = c.get(url)
                if r.status_code < 500:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _format_harness_command(
    template: str,
    *,
    app_url: str,
    prompt: str,
    run_dir: Path,
) -> str:
    return template.format(
        app_url=app_url,
        prompt=prompt,
        run_dir=str(run_dir),
        prompt_q=shlex.quote(prompt),
        app_url_q=shlex.quote(app_url),
        run_dir_q=shlex.quote(str(run_dir)),
    )


def _auth_context_from_env(spec: TesterSpec) -> dict[str, str]:
    return {
        "required": "true" if spec.auth_required else "false",
        "mode": spec.auth_mode,
        "login_url": spec.auth_login_url,
        "username": spec.auth_username,
        "password": os.environ.get(spec.auth_password_env, "").strip(),
        "jwt": os.environ.get(spec.auth_jwt_env, "").strip(),
        "headers_json": os.environ.get(spec.auth_headers_env, "").strip(),
    }


def _compose_task_prompt(
    *,
    mode: str,
    prompt: str,
    auth_context: Optional[dict[str, str]] = None,
) -> str:
    base = prompt.strip()
    m = (mode or "describe").strip().lower()
    if m in {"explore", "explore_suite", "suite"} and not base:
        base = (
            "Systematically explore the app and draft a robust regression suite "
            "covering smoke paths, primary user journeys, key error states, and "
            "at least one edge case per major flow. Return concise test cases."
        )
    elif not base:
        base = "Run a targeted user-flow regression test for this app."

    if not auth_context:
        return base
    if str(auth_context.get("required", "")).lower() not in {"1", "true", "yes", "on"}:
        return f"{base}\n\nAuth: no authentication required."

    mode_name = (auth_context.get("mode") or "none").strip().lower()
    lines = [base, "", "Authentication context:"]
    if mode_name == "form":
        lines.append(f"- login_url: {auth_context.get('login_url', '')}")
        lines.append(f"- username: {auth_context.get('username', '')}")
        lines.append("- password: use provided runtime secret")
    elif mode_name == "jwt":
        lines.append("- auth type: bearer token")
        lines.append("- jwt: use provided runtime token")
    elif mode_name == "headers":
        lines.append("- auth type: custom headers")
        lines.append("- headers_json: use provided runtime JSON headers")
    else:
        lines.append("- auth required but mode unspecified; discover login path first")
    return "\n".join(lines)


def _join_url(base_url: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path:
        return base_url
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _assertion_result(
    *,
    assertion: dict[str, Any],
    ok: bool,
    expected: Any,
    actual: Any,
    message: str,
    confidence: float | None = None,
) -> TesterAssertionResult:
    assertion_type = str(
        assertion.get("type") or assertion.get("assertion_type") or "unknown"
    )
    selected_confidence = (
        confidence
        if confidence is not None
        else assertion.get("confidence")
        if assertion.get("confidence") is not None
        else 1.0
    )
    return TesterAssertionResult(
        assertion_id=str(
            assertion.get("id") or assertion.get("name") or uuid.uuid4().hex[:8]
        ),
        assertion_type=assertion_type,
        ok=ok,
        expected=expected,
        actual=actual,
        message=message,
        source=str(assertion.get("source") or "native"),
        confidence=_coerce_confidence(selected_confidence, default=1.0),
        consensus_group=str(assertion.get("consensus_group") or ""),
        model_votes=list(assertion.get("model_votes") or []),
    )


def _coerce_confidence(raw: Any, *, default: float = 1.0) -> float:
    try:
        value = default if raw is None else float(raw)
    except (TypeError, ValueError):
        value = default
    return max(0.0, min(1.0, value))


def _bool_from_vote(vote: dict[str, Any]) -> bool | None:
    for key in ("ok", "passed", "pass", "result"):
        if key not in vote:
            continue
        value = vote[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            clean = value.strip().lower()
            if clean in {"pass", "passed", "true", "ok", "yes"}:
                return True
            if clean in {"fail", "failed", "false", "no"}:
                return False
    return None


def _evaluate_consensus_assertion(
    assertion: dict[str, Any],
) -> TesterAssertionResult:
    votes = _collect_consensus_votes(assertion)
    parsed: list[bool] = []
    for vote in votes:
        value = _bool_from_vote(vote)
        if value is not None:
            parsed.append(value)
    if not parsed:
        return _assertion_result(
            assertion=assertion,
            ok=False,
            expected=assertion.get("expected", "model vote majority"),
            actual={"votes": votes, "error": "no_parseable_votes"},
            message="Consensus assertion did not include parseable model votes.",
        )

    pass_count = sum(1 for value in parsed if value)
    fail_count = len(parsed) - pass_count
    arbiter_vote = assertion.get("arbiter_vote")
    arbiter = _bool_from_vote({"result": arbiter_vote}) if arbiter_vote is not None else None
    disagreement = pass_count > 0 and fail_count > 0
    if pass_count == fail_count and arbiter is not None:
        ok = arbiter
        decision = "arbiter"
    else:
        ok = pass_count > fail_count
        decision = "majority"
    confidence = max(pass_count, fail_count) / len(parsed)
    if disagreement and decision == "arbiter":
        confidence = max(confidence, 0.67)
    return _assertion_result(
        assertion=assertion,
        ok=ok,
        expected=assertion.get("expected", "model vote majority"),
        actual={
            "decision": decision,
            "pass_votes": pass_count,
            "fail_votes": fail_count,
            "arbiter_vote": arbiter,
            "disagreement": disagreement,
            "evidence": assertion.get("evidence", {}),
            "retry_count": len(assertion.get("retry_votes") or []),
        },
        message=(
            f"Consensus {decision}: {pass_count} pass vote(s), "
            f"{fail_count} fail vote(s)."
        ),
        confidence=_coerce_confidence(
            assertion.get("confidence") if "confidence" in assertion else confidence,
            default=confidence,
        ),
    )


def _evaluate_model_backed_consensus_assertion(
    assertion: dict[str, Any],
    *,
    response: Optional[httpx.Response],
) -> TesterAssertionResult:
    if assertion.get("model_votes") or assertion.get("votes"):
        return _evaluate_consensus_assertion(assertion)

    models = _consensus_models(assertion)
    if not models:
        return _evaluate_consensus_assertion(assertion)

    provider = str(
        assertion.get("provider")
        or assertion.get("llm_provider")
        or os.environ.get("RETRACE_LLM_PROVIDER")
        or "openai_compatible"
    )
    base_url = str(
        assertion.get("base_url")
        or assertion.get("llm_base_url")
        or os.environ.get("RETRACE_LLM_BASE_URL")
        or ""
    ).strip()
    api_key = _consensus_api_key(assertion)
    timeout = float(assertion.get("timeout_seconds") or 30)
    prompt = str(
        assertion.get("prompt")
        or assertion.get("question")
        or assertion.get("expected")
        or "Decide whether the observed page satisfies this assertion."
    )
    evidence = _response_assertion_evidence(
        response,
        capture_body=bool(assertion.get("capture_body_evidence", True)),
    )
    snapshot = _assertion_snapshot_payload(
        assertion=assertion,
        evidence=evidence,
        prompt=prompt,
    )
    if not base_url:
        failed = dict(assertion)
        failed["model_votes"] = [
            {
                "model": model,
                "ok": False,
                "error": "missing_llm_base_url",
            }
            for model in models
        ]
        failed["evidence"] = evidence
        return _evaluate_consensus_assertion(failed)

    votes = _run_consensus_model_votes(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        models=models,
        prompt=prompt,
        snapshot=snapshot,
        timeout=timeout,
    )
    parsed = [_bool_from_vote(vote) for vote in votes]
    retry_votes: list[dict[str, Any]] = []
    if bool(assertion.get("retry_failed", True)) and any(value is False for value in parsed):
        retry_models = [
            model for model, value in zip(models, parsed) if value is False
        ]
        retry_votes = _run_consensus_model_votes(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            models=retry_models,
            prompt=f"{prompt}\n\nRetry with fresh evidence and verify carefully.",
            snapshot=snapshot,
            timeout=timeout,
            retry=True,
        )

    arbiter_vote: str | bool | None = assertion.get("arbiter_vote")
    combined = votes + retry_votes
    combined_parsed = [_bool_from_vote(vote) for vote in combined]
    has_disagreement = (
        any(value is True for value in combined_parsed)
        and any(value is False for value in combined_parsed)
    )
    arbiter_model = str(assertion.get("arbiter_model") or "").strip()
    if has_disagreement and arbiter_vote is None and arbiter_model:
        arbiter = _run_consensus_model_votes(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            models=[arbiter_model],
            prompt=(
                f"{prompt}\n\nAct as arbiter. Resolve disagreement from these "
                f"votes: {json.dumps(combined, ensure_ascii=True)}"
            ),
            snapshot=snapshot,
            timeout=timeout,
        )
        if arbiter:
            arbiter_vote = _bool_from_vote(arbiter[0])

    hydrated = dict(assertion)
    hydrated["model_votes"] = votes
    hydrated["retry_votes"] = retry_votes
    hydrated["arbiter_vote"] = arbiter_vote
    hydrated["evidence"] = evidence
    return _evaluate_consensus_assertion(hydrated)


def _consensus_models(assertion: dict[str, Any]) -> list[str]:
    raw = assertion.get("models")
    models: list[str] = []
    if isinstance(raw, list):
        models.extend(str(item).strip() for item in raw if str(item).strip())
    for key in ("primary_model", "secondary_model"):
        value = str(assertion.get(key) or "").strip()
        if value:
            models.append(value)
    out: list[str] = []
    for model in models:
        if model not in out:
            out.append(model)
    return out[:4]


def _consensus_api_key(assertion: dict[str, Any]) -> str | None:
    if assertion.get("api_key"):
        return str(assertion["api_key"])
    api_key_env = str(assertion.get("api_key_env") or "RETRACE_LLM_API_KEY")
    return os.environ.get(api_key_env) or None


def _assertion_snapshot_payload(
    *,
    assertion: dict[str, Any],
    evidence: dict[str, Any],
    prompt: str,
) -> dict[str, Any]:
    return {
        "assertion_id": assertion.get("id") or assertion.get("name") or "",
        "assertion": prompt,
        "expected": assertion.get("expected"),
        "url": evidence.get("url"),
        "status_code": evidence.get("status_code"),
        "headers": evidence.get("headers", {}),
        "body_excerpt": evidence.get("body_excerpt", ""),
        "body_length": evidence.get("body_length", 0),
        "screenshot_available": False,
    }


def _run_consensus_model_votes(
    *,
    provider: str,
    base_url: str,
    api_key: str | None,
    models: list[str],
    prompt: str,
    snapshot: dict[str, Any],
    timeout: float,
    retry: bool = False,
) -> list[dict[str, Any]]:
    if not models:
        return []
    max_workers = min(4, len(models))
    votes: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _call_assertion_model,
                provider=provider,
                base_url=base_url,
                api_key=api_key,
                model=model,
                prompt=prompt,
                snapshot=snapshot,
                timeout=timeout,
                retry=retry,
            ): model
            for model in models
        }
        for future in as_completed(futures):
            try:
                votes.append(future.result())
            except Exception as exc:
                votes.append(
                    {
                        "model": futures[future],
                        "ok": False,
                        "error": str(exc),
                        "retry": retry,
                    }
                )
    order = {model: idx for idx, model in enumerate(models)}
    return sorted(votes, key=lambda vote: order.get(str(vote.get("model")), 999))


def _call_assertion_model(
    *,
    provider: str,
    base_url: str,
    api_key: str | None,
    model: str,
    prompt: str,
    snapshot: dict[str, Any],
    timeout: float,
    retry: bool,
) -> dict[str, Any]:
    system = (
        "You are a strict UI test assertion judge. Return only JSON with keys "
        "ok (boolean), confidence (0-1), and reasoning (short string)."
    )
    user = (
        f"Assertion: {prompt}\n\n"
        f"Observed evidence JSON:\n{json.dumps(snapshot, indent=2, ensure_ascii=True)}"
    )
    url, headers, body = build_llm_http_request(
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=api_key,
        system=system,
        user=user,
        temperature=0.0,
        response_json=True,
        max_tokens=256,
    )
    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, headers=headers, json=body)
        response.raise_for_status()
        payload = response.json()
    raw_text = extract_llm_text_content(provider=provider, payload=payload)
    parsed = _parse_model_vote_json(raw_text)

    # Safely coerce "ok" field to boolean
    ok_val = parsed.get("ok")
    if isinstance(ok_val, bool):
        ok = ok_val
    elif isinstance(ok_val, str):
        normalized = ok_val.strip().lower()
        ok = normalized in {"true", "1", "yes"}
    elif isinstance(ok_val, (int, float)):
        ok = bool(ok_val)
    else:
        ok = False

    return {
        "model": model,
        "ok": ok,
        "confidence": _coerce_confidence(parsed.get("confidence"), default=0.5),
        "reasoning": str(parsed.get("reasoning") or ""),
        "retry": retry,
    }


def _parse_model_vote_json(content: str) -> dict[str, Any]:
    text = content.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(1))
    if not isinstance(parsed, dict):
        raise ValueError("model vote response must be a JSON object")
    return parsed


def _collect_consensus_votes(assertion: dict[str, Any]) -> list[dict[str, Any]]:
    votes = [
        vote
        for vote in list(assertion.get("model_votes") or assertion.get("votes") or [])
        if isinstance(vote, dict)
    ]
    parsed = [_bool_from_vote(vote) for vote in votes]
    has_failure = any(value is False for value in parsed)
    retry_votes = [
        vote for vote in list(assertion.get("retry_votes") or []) if isinstance(vote, dict)
    ]
    if has_failure and retry_votes:
        votes.extend(retry_votes)
    return votes


def _evaluate_native_assertion(
    assertion: dict[str, Any],
    *,
    response: Optional[httpx.Response],
) -> TesterAssertionResult:
    kind = str(assertion.get("type") or assertion.get("assertion_type") or "").lower()
    if kind in {"model_consensus", "consensus", "ai_consensus"}:
        consensus_assertion = dict(assertion)
        response_evidence = _response_assertion_evidence(
            response,
            capture_body=bool(assertion.get("capture_body_evidence")),
        )
        existing_evidence = consensus_assertion.get("evidence")
        if isinstance(existing_evidence, dict):
            consensus_assertion["evidence"] = {
                **response_evidence,
                **existing_evidence,
            }
        else:
            consensus_assertion["evidence"] = response_evidence
        return _evaluate_model_backed_consensus_assertion(
            consensus_assertion,
            response=response,
        )
    if response is None:
        return _assertion_result(
            assertion=assertion,
            ok=False,
            expected=assertion.get("expected"),
            actual=None,
            message="No response is available for assertion.",
        )

    if kind in {"status", "status_code", "assert_status"}:
        expected = int(assertion.get("expected", assertion.get("status", 200)))
        actual = response.status_code
        return _assertion_result(
            assertion=assertion,
            ok=actual == expected,
            expected=expected,
            actual=actual,
            message=f"Expected status {expected}, got {actual}.",
        )

    if kind in {"text_contains", "body_contains", "assert_text", "contains"}:
        expected = str(assertion.get("expected", assertion.get("text", "")))
        actual = response.text
        return _assertion_result(
            assertion=assertion,
            ok=expected in actual,
            expected=expected,
            actual={"contains": expected in actual, "body_length": len(actual)},
            message=f"Expected response body to contain {expected!r}.",
        )

    if kind in {"header_present", "assert_header"}:
        expected = str(assertion.get("expected", assertion.get("header", "")))
        actual = dict(response.headers)
        return _assertion_result(
            assertion=assertion,
            ok=expected.lower() in {k.lower() for k in response.headers.keys()},
            expected=expected,
            actual=actual,
            message=f"Expected header {expected!r} to be present.",
        )

    return _assertion_result(
        assertion=assertion,
        ok=False,
        expected=assertion.get("expected"),
        actual={"unsupported_type": kind},
        message=f"Unsupported native assertion type: {kind or 'unknown'}.",
    )


def _response_assertion_evidence(
    response: Optional[httpx.Response],
    *,
    capture_body: bool = False,
) -> dict[str, Any]:
    if response is None:
        return {"kind": "http_response", "available": False}
    text = response.text
    evidence = {
        "kind": "http_response",
        "available": True,
        "url": str(response.url),
        "status_code": response.status_code,
        "headers": _redacted_response_headers(dict(response.headers)),
        "body_capture": bool(capture_body),
        "body_length": len(text),
    }
    if capture_body:
        evidence["body_excerpt"] = text[:2000]
    return evidence


def _redacted_response_headers(headers: dict[str, str]) -> dict[str, str]:
    sensitive = {
        "authorization",
        "cookie",
        "set-cookie",
        "proxy-authorization",
        "x-csrf-token",
        "x-xsrf-token",
    }
    redacted: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in sensitive:
            redacted[key] = "[redacted]"
        else:
            redacted[key] = value
    return redacted


def _cache_key_for_step(*, spec: TesterSpec, app_url: str, step: dict[str, Any]) -> str:
    payload = {
        "spec_id": spec.spec_id,
        "app_url": app_url,
        "action": str(step.get("action") or step.get("type") or "get").lower(),
        "url": str(step.get("url") or ""),
        "path": str(step.get("path") or ""),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]


def _load_step_cache(cache_dir: Path, cache_key: str) -> dict[str, Any] | None:
    path = cache_dir / f"{cache_key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _save_step_cache(cache_dir: Path, cache_key: str, payload: dict[str, Any]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{cache_key}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _consensus_summary(results: list[TesterAssertionResult]) -> list[dict[str, Any]]:
    groups: dict[str, list[TesterAssertionResult]] = {}
    for result in results:
        if result.consensus_group:
            groups.setdefault(result.consensus_group, []).append(result)
    summary: list[dict[str, Any]] = []
    for group, items in sorted(groups.items()):
        passed = sum(1 for item in items if item.ok)
        failed = len(items) - passed
        summary.append(
            {
                "group": group,
                "assertion_count": len(items),
                "passed": passed,
                "failed": failed,
                "ok": failed == 0,
                "disagreement": passed > 0 and failed > 0,
            }
        )
    return summary


_EXPLORE_DRIVER_FACTORY: Optional[Callable[..., Any]] = None
_EXPLORE_LLM_FACTORY: Optional[Callable[..., Any]] = None


def set_explore_factories(
    *,
    driver_factory: Optional[Callable[..., Any]] = None,
    llm_factory: Optional[Callable[..., Any]] = None,
) -> None:
    """Override the default driver/LLM factories for tests.

    Pass `None` to reset.  Production callers never need this.
    """
    global _EXPLORE_DRIVER_FACTORY, _EXPLORE_LLM_FACTORY
    _EXPLORE_DRIVER_FACTORY = driver_factory
    _EXPLORE_LLM_FACTORY = llm_factory


def _run_explore_spec(
    *,
    spec: TesterSpec,
    app_url: str,
    run_dir: Path,
    runs_dir: Path,
    log_path: Path,
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]], str]:
    from retrace.explorer import run_explorer

    artifacts: list[dict[str, Any]] = []
    assertion_results: list[dict[str, Any]] = []
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as log_fh:
        log_fh.write(f"--- explore engine starting for {spec.spec_id} ---\n")

    try:
        if _EXPLORE_DRIVER_FACTORY is not None:
            driver = _EXPLORE_DRIVER_FACTORY(browser_settings=spec.browser_settings)
        else:
            from retrace.explorer import build_playwright_driver

            driver = build_playwright_driver(browser_settings=spec.browser_settings)
    except Exception as exc:
        return 1, artifacts, assertion_results, f"explore driver setup failed: {exc}"

    try:
        if _EXPLORE_LLM_FACTORY is not None:
            llm = _EXPLORE_LLM_FACTORY()
        else:
            from retrace.config import LLMConfig
            from retrace.llm.client import LLMClient

            base_cfg = spec.fixtures.get("__llm__")  # tests can stash a config here
            cfg = base_cfg if isinstance(base_cfg, LLMConfig) else None
            if cfg is None:
                raise RuntimeError(
                    "explore engine requires an LLM; provide one via set_explore_factories "
                    "or via the calling integration"
                )
            llm = LLMClient(cfg)
    except Exception as exc:
        try:
            driver.close()
        except Exception:
            pass
        return 1, artifacts, assertion_results, f"explore llm setup failed: {exc}"

    skills_dir = runs_dir.parent / "skills"
    explore_max_steps_raw = spec.browser_settings.get("explore_max_steps")
    try:
        run_kwargs: dict[str, Any] = {
            "spec_id": spec.spec_id,
            "spec_name": spec.name,
            "app_url": app_url,
            "exploratory_goals": list(spec.exploratory_goals),
            "run_dir": run_dir,
            "driver": driver,
            "llm": llm,
            "skills_dir": skills_dir,
        }
        if isinstance(explore_max_steps_raw, int) and explore_max_steps_raw > 0:
            run_kwargs["max_steps"] = explore_max_steps_raw
        result = run_explorer(**run_kwargs)
    except Exception as exc:
        return 1, artifacts, assertion_results, f"explore run failed: {exc}"
    finally:
        if hasattr(llm, "close"):
            try:
                llm.close()
            except Exception:
                pass

    artifacts = list(result.artifacts)
    error = result.error
    exit_code = 0 if result.ok else 1
    return exit_code, artifacts, assertion_results, error


def _run_native_spec(
    *,
    spec: TesterSpec,
    app_url: str,
    run_dir: Path,
    log_path: Path,
    attempt: int = 0,
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]], str]:
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[TesterArtifact] = []
    assertion_results: list[TesterAssertionResult] = []
    last_response: Optional[httpx.Response] = None
    error = ""
    timeout = float(spec.browser_settings.get("timeout_seconds") or 10)
    cache_events: list[TesterStepCacheEvent] = []
    cache_dir = run_dir.parent.parent / "cache" / "native-steps"

    steps = spec.exact_steps or [
        {"id": "default-get", "action": "get", "url": app_url}
    ]
    if _should_use_playwright(spec, steps):
        return _run_playwright_spec(
            spec=spec,
            app_url=app_url,
            run_dir=run_dir,
            log_path=log_path,
            steps=steps,
        )
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            for idx, step in enumerate(steps):
                action = str(step.get("action") or step.get("type") or "get").lower()
                if action in {"visit", "goto", "get", "navigate"}:
                    resolved_url = _join_url(
                        app_url, str(step.get("url") or step.get("path") or "")
                    )
                    cache_key = _cache_key_for_step(
                        spec=spec,
                        app_url=app_url,
                        step=step,
                    )
                    bypass_cache = (
                        attempt > 0
                        or bool(step.get("cache_bypass"))
                        or bool(step.get("override"))
                        or step.get("cache") is False
                    )
                    cached_for_bypass = (
                        _load_step_cache(cache_dir, cache_key)
                        if spec.step_cache_enabled and bypass_cache
                        else None
                    )
                    cached = (
                        _load_step_cache(cache_dir, cache_key)
                        if spec.step_cache_enabled and not bypass_cache
                        else None
                    )
                    cached_url = ""
                    if cached:
                        cached_url = str(
                            cached.get("effective_url")
                            or cached.get("resolved_url")
                            or ""
                        )
                    bypassed_url = ""
                    if cached_for_bypass:
                        bypassed_url = str(
                            cached_for_bypass.get("effective_url")
                            or cached_for_bypass.get("resolved_url")
                            or ""
                        )
                    use_cached_url = bool(cached_url and cached_url != resolved_url)
                    request_url = cached_url if use_cached_url else resolved_url
                    if bypass_cache and spec.step_cache_enabled and bypassed_url:
                        cache_events.append(
                            TesterStepCacheEvent(
                                step_id=str(step.get("id") or idx),
                                cache_key=cache_key,
                                status="bypass",
                                cached_url=bypassed_url,
                                resolved_url=resolved_url,
                                message="Bypassed step cache for retry or explicit override.",
                            )
                        )
                    if use_cached_url:
                        cache_events.append(
                            TesterStepCacheEvent(
                                step_id=str(step.get("id") or idx),
                                cache_key=cache_key,
                                status="hit",
                                cached_url=cached_url,
                                resolved_url=resolved_url,
                                message="Using cached effective URL.",
                            )
                        )
                    try:
                        last_response = client.get(request_url)
                        if use_cached_url and not _cached_step_has_effect(
                            response=last_response,
                            step=step,
                        ):
                            raise httpx.HTTPStatusError(
                                "Cached step URL did not satisfy observable effect.",
                                request=last_response.request,
                                response=last_response,
                            )
                    except Exception as exc:
                        if not use_cached_url:
                            raise
                        cache_events.append(
                            TesterStepCacheEvent(
                                step_id=str(step.get("id") or idx),
                                cache_key=cache_key,
                                status="auto_heal",
                                cached_url=cached_url,
                                resolved_url=resolved_url,
                                message=(
                                    f"{exc}; retried fresh spec URL instead of cached "
                                    "effective URL."
                                ),
                            )
                        )
                        last_response = client.get(resolved_url)
                    if spec.step_cache_enabled and last_response.status_code < 500:
                        effective_url = str(last_response.url)
                        _save_step_cache(
                            cache_dir,
                            cache_key,
                            {
                                "spec_id": spec.spec_id,
                                "step_id": str(step.get("id") or idx),
                                "resolved_url": resolved_url,
                                "effective_url": effective_url,
                                "updated_at": now_iso(),
                            },
                        )
                        if not use_cached_url:
                            cache_events.append(
                                TesterStepCacheEvent(
                                    step_id=str(step.get("id") or idx),
                                    cache_key=cache_key,
                                    status="miss_store",
                                    cached_url="",
                                    resolved_url=effective_url,
                                    message="Stored effective URL for future runs.",
                                )
                            )
                    response_path = artifacts_dir / f"response-{idx}.txt"
                    response_path.write_text(last_response.text, encoding="utf-8")
                    artifacts.append(
                        TesterArtifact(
                            artifact_id=f"response-{idx}",
                            artifact_type="http_response_body",
                            path=str(response_path),
                            label=f"Response body for step {idx + 1}",
                            metadata={
                                "url": str(last_response.url),
                                "status_code": last_response.status_code,
                                "step_id": step.get("id", ""),
                            },
                        )
                    )
                elif action in {"assert_status", "status_code"}:
                    assertion_results.append(
                        _evaluate_native_assertion(
                            {"type": "status_code", **step},
                            response=last_response,
                        )
                    )
                elif action in {"assert_text", "text_contains", "contains"}:
                    assertion_results.append(
                        _evaluate_native_assertion(
                            {"type": "text_contains", **step},
                            response=last_response,
                        )
                    )
                elif action in {"click", "type", "keypress", "wait", "hover", "upload", "drag", "drop"}:
                    artifact_path = artifacts_dir / f"step-{idx}-pending.json"
                    artifact_path.write_text(
                        json.dumps(
                            {
                                "step": step,
                                "status": "pending_browser_runtime",
                                "message": (
                                    "This replay-derived step requires the Playwright "
                                    "browser runtime and a durable locator."
                                ),
                            },
                            indent=2,
                        )
                        + "\n"
                    )
                    artifacts.append(
                        TesterArtifact(
                            artifact_id=f"step-{idx}-pending",
                            artifact_type="pending_browser_step",
                            path=str(artifact_path),
                            label=f"Pending browser step {idx + 1}",
                            metadata={"action": action},
                        )
                    )
                else:
                    assertion_results.append(
                        _assertion_result(
                            assertion=step,
                            ok=False,
                            expected=step.get("expected"),
                            actual={"unsupported_action": action},
                            message=f"Unsupported native step action: {action}.",
                        )
                    )

            for assertion in spec.assertions:
                assertion_results.append(
                    _evaluate_native_assertion(assertion, response=last_response)
                )

            for extraction in spec.data_extraction:
                if last_response is None:
                    continue
                pattern = str(extraction.get("regex") or "")
                if not pattern:
                    continue
                matches = re.findall(pattern, last_response.text)
                extraction_path = artifacts_dir / f"extraction-{len(artifacts)}.json"
                extraction_path.write_text(
                    json.dumps(
                        {
                            "id": extraction.get("id", ""),
                            "regex": pattern,
                            "matches": matches,
                        },
                        indent=2,
                    )
                    + "\n"
                )
                artifacts.append(
                    TesterArtifact(
                        artifact_id=f"extraction-{len(artifacts)}",
                        artifact_type="data_extraction",
                        path=str(extraction_path),
                        label=str(extraction.get("label") or "Data extraction"),
                        metadata={"match_count": len(matches)},
                    )
                )
    except Exception as exc:
        error = str(exc)

    if not assertion_results and last_response is not None:
        assertion_results.append(
            _assertion_result(
                assertion={"id": "default-status", "type": "status_code"},
                ok=last_response.status_code < 500,
                expected="<500",
                actual=last_response.status_code,
                message="Expected default GET response to be below HTTP 500.",
            )
        )

    assertions_path = artifacts_dir / "assertions.json"
    assertions_payload = [asdict(item) for item in assertion_results]
    assertions_path.write_text(json.dumps(assertions_payload, indent=2) + "\n")
    artifacts.append(
        TesterArtifact(
            artifact_id="assertions",
            artifact_type="assertion_results",
            path=str(assertions_path),
            label="Structured assertion results",
            metadata={"count": len(assertion_results)},
        )
    )
    consensus = _consensus_summary(assertion_results) if spec.assertion_consensus_enabled else []
    if consensus:
        consensus_path = artifacts_dir / "assertion-consensus.json"
        consensus_path.write_text(json.dumps(consensus, indent=2) + "\n")
        artifacts.append(
            TesterArtifact(
                artifact_id="assertion-consensus",
                artifact_type="assertion_consensus",
                path=str(consensus_path),
                label="Assertion consensus summary",
                metadata={"group_count": len(consensus)},
            )
        )
    cache_events_payload = [asdict(item) for item in cache_events]
    if cache_events_payload:
        cache_path = artifacts_dir / "step-cache-events.json"
        cache_path.write_text(json.dumps(cache_events_payload, indent=2) + "\n")
        artifacts.append(
            TesterArtifact(
                artifact_id="step-cache-events",
                artifact_type="step_cache_events",
                path=str(cache_path),
                label="Step cache and auto-heal events",
                metadata={"count": len(cache_events_payload)},
            )
        )

    summary = {
        "execution_engine": "native",
        "steps": steps,
        "assertion_count": len(assertion_results),
        "consensus_group_count": len(consensus),
        "step_cache_event_count": len(cache_events_payload),
        "artifact_count": len(artifacts),
        "error": error,
    }
    summary_path = artifacts_dir / "native-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    artifacts.append(
        TesterArtifact(
            artifact_id="native-summary",
            artifact_type="native_run_summary",
            path=str(summary_path),
            label="Native runner summary",
            metadata={},
        )
    )
    with log_path.open("a") as log:
        log.write(json.dumps(summary, indent=2) + "\n")

    ok = not error and all(item.ok for item in assertion_results)
    return (
        0 if ok else 1,
        [asdict(item) for item in artifacts],
        assertions_payload,
        error,
    )


def _should_use_playwright(spec: TesterSpec, steps: list[dict[str, Any]]) -> bool:
    runtime = str(
        spec.browser_settings.get("runtime")
        or spec.browser_settings.get("browser_runtime")
        or ""
    ).strip().lower()
    if runtime == "playwright":
        return True
    browser_actions = {
        "click",
        "type",
        "keypress",
        "wait",
        "wait_for",
        "hover",
        "upload",
        "drag",
        "drop",
        "select",
        "scroll",
    }
    return any(
        str(step.get("action") or step.get("type") or "").lower() in browser_actions
        and bool(step.get("selector") or step.get("text") or step.get("key"))
        for step in steps
    )


def _run_playwright_spec(
    *,
    spec: TesterSpec,
    app_url: str,
    run_dir: Path,
    log_path: Path,
    steps: list[dict[str, Any]],
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]], str]:
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[TesterArtifact] = []
    assertion_results: list[TesterAssertionResult] = []
    error = ""
    last_status: int | None = None
    last_text = ""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        error = f"Playwright runtime unavailable: {exc}"
        return _playwright_result_payload(
            artifacts_dir=artifacts_dir,
            log_path=log_path,
            artifacts=artifacts,
            assertion_results=assertion_results,
            error=error,
            steps=steps,
        )

    try:
        with sync_playwright() as pw:
            browser_name = str(spec.browser_settings.get("browser") or "chromium")
            browser_type = getattr(pw, browser_name)
            browser = browser_type.launch(
                headless=bool(spec.browser_settings.get("headless", True))
            )
            context = browser.new_context(
                viewport=spec.browser_settings.get("viewport")
                if isinstance(spec.browser_settings.get("viewport"), dict)
                else None
            )
            page = context.new_page()
            for idx, step in enumerate(steps):
                action = str(step.get("action") or step.get("type") or "get").lower()
                if action in {"visit", "goto", "get", "navigate"}:
                    response = page.goto(
                        _join_url(app_url, str(step.get("url") or step.get("path") or "")),
                        wait_until="domcontentloaded",
                    )
                    last_status = response.status if response is not None else None
                elif action == "click":
                    selector = _selector_for_browser_step(step)
                    if not selector:
                        raise ValueError(f"click step {step.get('id') or idx} needs selector")
                    page.locator(selector).click()
                elif action == "type":
                    selector = _selector_for_browser_step(step)
                    if not selector:
                        raise ValueError(f"type step {step.get('id') or idx} needs selector")
                    page.locator(selector).fill(str(step.get("text") or ""))
                elif action == "keypress":
                    page.keyboard.press(str(step.get("key") or "Enter"))
                elif action == "wait":
                    page.wait_for_timeout(int(step.get("ms") or step.get("timeout_ms") or 500))
                elif action == "hover":
                    selector = _selector_for_browser_step(step)
                    if not selector:
                        raise ValueError(f"hover step {step.get('id') or idx} needs selector")
                    page.locator(selector).hover()
                elif action == "upload":
                    selector = _selector_for_browser_step(step)
                    if not selector:
                        raise ValueError(f"upload step {step.get('id') or idx} needs selector")
                    file_path = str(step.get("file") or step.get("file_path") or "")
                    if not file_path:
                        raise ValueError(f"upload step {step.get('id') or idx} needs file path")
                    page.locator(selector).set_input_files(file_path)
                elif action in {"drag", "drop", "drag_and_drop"}:
                    source_selector = _selector_for_browser_step(step)
                    target_selector = _drag_target_selector(step)
                    if not source_selector or not target_selector:
                        raise ValueError(
                            f"{action} step {step.get('id') or idx} needs source and target selectors"
                        )
                    page.locator(source_selector).drag_to(page.locator(target_selector))
                elif action == "select":
                    selector = _selector_for_browser_step(step)
                    if not selector:
                        raise ValueError(f"select step {step.get('id') or idx} needs selector")
                    option = step.get("value") or step.get("option") or step.get("text")
                    if option is None:
                        raise ValueError(f"select step {step.get('id') or idx} needs value/option/text")
                    if isinstance(option, list):
                        page.locator(selector).select_option(option)
                    elif isinstance(option, dict):
                        # Playwright select_option accepts only value/label/index;
                        # filter to keep us out of pw's "unexpected kwarg" trap.
                        allowed = {"value", "label", "index"}
                        filtered = {k: v for k, v in option.items() if k in allowed}
                        if not filtered:
                            raise ValueError(
                                f"select step {step.get('id') or idx} dict must include "
                                f"value/label/index"
                            )
                        page.locator(selector).select_option(**filtered)
                    else:
                        page.locator(selector).select_option(str(option))
                elif action == "scroll":
                    selector = _selector_for_browser_step(step)
                    if selector:
                        page.locator(selector).scroll_into_view_if_needed()
                    else:
                        delta_y = int(step.get("y") or step.get("delta_y") or 0)
                        delta_x = int(step.get("x") or step.get("delta_x") or 0)
                        page.mouse.wheel(delta_x, delta_y)
                elif action == "wait_for":
                    selector = _selector_for_browser_step(step)
                    if not selector:
                        raise ValueError(f"wait_for step {step.get('id') or idx} needs selector")
                    state = str(step.get("state") or "visible")
                    timeout_ms = int(step.get("timeout_ms") or step.get("ms") or 5000)
                    page.locator(selector).wait_for(state=state, timeout=timeout_ms)
                elif action in {"assert_status", "status_code"}:
                    expected = int(step.get("expected", step.get("status", 200)))
                    assertion_results.append(
                        _assertion_result(
                            assertion={"type": "status_code", **step},
                            ok=last_status == expected,
                            expected=expected,
                            actual=last_status,
                            message=f"Expected status {expected}, got {last_status}.",
                        )
                    )
                elif action in {"assert_text", "text_contains", "contains"}:
                    last_text = page.locator("body").inner_text(timeout=3000)
                    expected = str(step.get("expected", step.get("text", "")))
                    assertion_results.append(
                        _assertion_result(
                            assertion={"type": "text_contains", **step},
                            ok=expected in last_text,
                            expected=expected,
                            actual={"contains": expected in last_text},
                            message=f"Expected page text to contain {expected!r}.",
                        )
                    )

            # Evaluate top-level spec.assertions
            for assertion in spec.assertions:
                assertion_type = str(
                    assertion.get("type") or assertion.get("assertion_type") or ""
                ).lower()
                if assertion_type in {"status", "status_code", "assert_status"}:
                    expected = int(assertion.get("expected", assertion.get("status", 200)))
                    assertion_results.append(
                        _assertion_result(
                            assertion=assertion,
                            ok=last_status == expected,
                            expected=expected,
                            actual=last_status,
                            message=f"Expected status {expected}, got {last_status}.",
                        )
                    )
                elif assertion_type in {"text_contains", "body_contains", "assert_text", "contains"}:
                    expected = str(assertion.get("expected", assertion.get("text", "")))
                    try:
                        last_text = page.locator("body").inner_text(timeout=3000)
                    except Exception:
                        last_text = ""
                    assertion_results.append(
                        _assertion_result(
                            assertion=assertion,
                            ok=expected in last_text,
                            expected=expected,
                            actual={"contains": expected in last_text},
                            message=f"Expected page text to contain {expected!r}.",
                        )
                    )
                elif assertion_type in {"model_consensus", "consensus", "ai_consensus"}:
                    # For consensus assertions, we don't have an httpx.Response, so pass None
                    assertion_results.append(
                        _evaluate_model_backed_consensus_assertion(assertion, response=None)
                    )
                elif assertion_type in {"url_contains", "url"}:
                    expected = str(assertion.get("expected", assertion.get("value", "")))
                    current_url = str(page.url or "")
                    assertion_results.append(
                        _assertion_result(
                            assertion=assertion,
                            ok=expected in current_url,
                            expected=expected,
                            actual={"url": current_url},
                            message=f"Expected URL to contain {expected!r}.",
                        )
                    )
                elif assertion_type in {"selector_visible", "element_visible", "visible"}:
                    selector = _selector_for_assertion(assertion)
                    timeout_ms = int(assertion.get("timeout_ms") or 3000)
                    visible = False
                    if selector:
                        try:
                            page.locator(selector).first.wait_for(
                                state="visible", timeout=timeout_ms
                            )
                            visible = True
                        except Exception:
                            visible = False
                    assertion_results.append(
                        _assertion_result(
                            assertion=assertion,
                            ok=visible,
                            expected={"selector": selector, "state": "visible"},
                            actual={"visible": visible},
                            message=f"Expected selector {selector!r} to be visible.",
                        )
                    )
                elif assertion_type in {"selector_text", "element_text"}:
                    selector = _selector_for_assertion(assertion)
                    expected = str(assertion.get("expected", assertion.get("text", "")))
                    actual_text = ""
                    ok = False
                    if selector:
                        try:
                            actual_text = page.locator(selector).first.inner_text(
                                timeout=int(assertion.get("timeout_ms") or 3000)
                            )
                            ok = expected in actual_text
                        except Exception as exc:
                            actual_text = f"<error: {exc}>"
                    assertion_results.append(
                        _assertion_result(
                            assertion=assertion,
                            ok=ok,
                            expected=expected,
                            actual={"text": actual_text},
                            message=f"Expected selector {selector!r} text to contain {expected!r}.",
                        )
                    )
                elif assertion_type in {"selector_count", "element_count"}:
                    selector = _selector_for_assertion(assertion)
                    expected = int(assertion.get("expected", assertion.get("count", 0)))
                    actual_count = 0
                    if selector:
                        try:
                            actual_count = page.locator(selector).count()
                        except Exception:
                            actual_count = -1
                    assertion_results.append(
                        _assertion_result(
                            assertion=assertion,
                            ok=actual_count == expected,
                            expected=expected,
                            actual={"count": actual_count},
                            message=f"Expected selector {selector!r} count {expected}, got {actual_count}.",
                        )
                    )
                elif assertion_type in {"text_matches", "regex"}:
                    import re as _re

                    pattern = str(assertion.get("expected", assertion.get("pattern", "")))
                    try:
                        last_text = page.locator("body").inner_text(timeout=3000)
                    except Exception:
                        last_text = ""
                    ok = bool(pattern) and bool(_re.search(pattern, last_text))
                    assertion_results.append(
                        _assertion_result(
                            assertion=assertion,
                            ok=ok,
                            expected=pattern,
                            actual={"matched": ok},
                            message=f"Expected page text to match /{pattern}/.",
                        )
                    )
                else:
                    assertion_results.append(
                        _assertion_result(
                            assertion=assertion,
                            ok=False,
                            expected=assertion.get("expected"),
                            actual={"unsupported_in_playwright": assertion_type},
                            message=f"Assertion type {assertion_type} not yet supported in Playwright runner.",
                        )
                    )

            if not assertion_results:
                assertion_results.append(
                    _assertion_result(
                        assertion={"id": "default-status", "type": "status_code"},
                        ok=(last_status is None or last_status < 500),
                        expected="<500",
                        actual=last_status,
                        message="Expected browser navigation response below HTTP 500.",
                    )
                )
            screenshot_path = artifacts_dir / "playwright-final.png"
            page.screenshot(path=str(screenshot_path), full_page=True)
            artifacts.append(
                TesterArtifact(
                    artifact_id="playwright-final-screenshot",
                    artifact_type="screenshot",
                    path=str(screenshot_path),
                    label="Final Playwright screenshot",
                    metadata={"url": page.url},
                )
            )
            context.close()
            browser.close()
    except Exception as exc:
        error = str(exc)

    return _playwright_result_payload(
        artifacts_dir=artifacts_dir,
        log_path=log_path,
        artifacts=artifacts,
        assertion_results=assertion_results,
        error=error,
        steps=steps,
    )


def _selector_for_browser_step(step: dict[str, Any]) -> str:
    selector = str(step.get("selector") or "").strip()
    if selector:
        return selector
    target = step.get("target")
    if isinstance(target, dict):
        return str(target.get("selector") or "").strip()
    return ""


def _drag_target_selector(step: dict[str, Any]) -> str:
    direct = str(
        step.get("target_selector")
        or step.get("destination_selector")
        or step.get("to")
        or ""
    ).strip()
    if direct:
        return direct
    target = step.get("destination") or step.get("drop_target")
    if isinstance(target, dict):
        return str(target.get("selector") or "").strip()
    return ""


def _selector_for_assertion(assertion: dict[str, Any]) -> str:
    selector = str(assertion.get("selector") or "").strip()
    if selector:
        return selector
    target = assertion.get("target")
    if isinstance(target, dict):
        return str(target.get("selector") or "").strip()
    return ""


def _playwright_result_payload(
    *,
    artifacts_dir: Path,
    log_path: Path,
    artifacts: list[TesterArtifact],
    assertion_results: list[TesterAssertionResult],
    error: str,
    steps: list[dict[str, Any]],
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]], str]:
    assertions_payload = [asdict(item) for item in assertion_results]
    assertions_path = artifacts_dir / "assertions.json"
    assertions_path.write_text(json.dumps(assertions_payload, indent=2) + "\n")
    artifacts.append(
        TesterArtifact(
            artifact_id="assertions",
            artifact_type="assertion_results",
            path=str(assertions_path),
            label="Structured assertion results",
            metadata={"count": len(assertion_results)},
        )
    )
    summary = {
        "execution_engine": "native",
        "runtime": "playwright",
        "steps": steps,
        "assertion_count": len(assertion_results),
        "artifact_count": len(artifacts),
        "error": error,
    }
    summary_path = artifacts_dir / "native-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    artifacts.append(
        TesterArtifact(
            artifact_id="native-summary",
            artifact_type="native_run_summary",
            path=str(summary_path),
            label="Native runner summary",
            metadata={"runtime": "playwright"},
        )
    )
    with log_path.open("a") as log:
        log.write(json.dumps(summary, indent=2) + "\n")
    ok = not error and all(item.ok for item in assertion_results)
    return 0 if ok else 1, [asdict(item) for item in artifacts], assertions_payload, error


def _cached_step_has_effect(*, response: httpx.Response, step: dict[str, Any]) -> bool:
    if response.status_code >= 500:
        return False
    if "expected_status" in step and response.status_code != int(step["expected_status"]):
        return False
    expected_text = str(step.get("expected_text") or step.get("effect_text") or "")
    if expected_text and expected_text not in response.text:
        return False
    return True


def run_spec(
    *,
    spec: TesterSpec,
    runs_dir: Path,
    prompt_override: Optional[str] = None,
    app_url_override: Optional[str] = None,
    start_command_override: Optional[str] = None,
    auth_context_override: Optional[dict[str, str]] = None,
    max_retries: int = 0,
    cwd: Optional[Path] = None,
) -> TesterRunResult:
    validate_spec(spec)
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    run_dir = runs_dir / f"{run_id}-{slugify(spec.name)[:40]}"
    run_dir.mkdir(parents=True, exist_ok=False)

    prompt = (prompt_override or spec.prompt or "").strip()
    app_url = (app_url_override or spec.app_url or DEFAULT_APP_URL).strip()
    start_command = (start_command_override or spec.start_command or "").strip()
    auth_context = auth_context_override or _auth_context_from_env(spec)
    final_prompt = _compose_task_prompt(
        mode=spec.mode,
        prompt=prompt,
        auth_context=auth_context,
    )
    execution_engine = spec.execution_engine
    if execution_engine == "auto":
        if spec.exact_steps or spec.assertions:
            execution_engine = "native"
        elif spec.exploratory_goals:
            execution_engine = "explore"
        else:
            execution_engine = "harness"
    harness_cmd = ""
    if execution_engine == "harness":
        harness_cmd = _format_harness_command(
            spec.harness_command,
            app_url=app_url,
            prompt=final_prompt,
            run_dir=run_dir,
        )

    harness_log_path = run_dir / "harness.log"
    app_log_path = run_dir / "app.log"
    meta_path = run_dir / "run.json"

    app_proc: Optional[subprocess.Popen[Any]] = None
    harness_proc: Optional[subprocess.Popen[Any]] = None

    attempts = 0
    last_exit = 1
    last_error = ""
    artifacts: list[dict[str, Any]] = []
    assertion_results: list[dict[str, Any]] = []
    try:
        with app_log_path.open("w") as app_log:
            if start_command:
                app_proc = _run_shell(
                    start_command,
                    stdout_fh=app_log,
                    stderr_fh=app_log,
                    cwd=cwd,
                    env_overrides=spec.env_overrides,
                )
                if not _wait_for_http(app_url):
                    raise RuntimeError(
                        f"App did not become reachable at {app_url} after startup."
                    )

        if execution_engine == "native":
            for attempt in range(max(0, int(max_retries)) + 1):
                attempts += 1
                last_exit, artifacts, assertion_results, last_error = _run_native_spec(
                    spec=spec,
                    app_url=app_url,
                    run_dir=run_dir,
                    log_path=harness_log_path,
                    attempt=attempt,
                )
                if last_exit == 0:
                    break
        elif execution_engine == "explore":
            attempts += 1
            last_exit, artifacts, assertion_results, last_error = _run_explore_spec(
                spec=spec,
                app_url=app_url,
                run_dir=run_dir,
                runs_dir=runs_dir,
                log_path=harness_log_path,
            )
        else:
            for attempt in range(max(0, int(max_retries)) + 1):
                attempts += 1
                try:
                    with harness_log_path.open("a") as harness_log:
                        if attempt > 0:
                            harness_log.write(f"\n--- retry attempt {attempt} ---\n")
                        harness_proc = _run_shell(
                            harness_cmd,
                            stdout_fh=harness_log,
                            stderr_fh=harness_log,
                            cwd=cwd,
                            auth_context=auth_context,
                            env_overrides=spec.env_overrides,
                        )
                        last_exit = harness_proc.wait(timeout=900)
                    if last_exit == 0:
                        break
                except Exception as exc:
                    last_error = str(exc)
                    last_exit = 1
                    if harness_proc and harness_proc.poll() is None:
                        harness_proc.terminate()
                        try:
                            harness_proc.wait(timeout=5)
                        except Exception:
                            harness_proc.kill()
                            harness_proc.wait(timeout=5)
                    harness_proc = None
                    if attempt >= int(max_retries):
                        break
                    continue

            if harness_log_path.exists():
                artifacts.append(
                    asdict(
                        TesterArtifact(
                            artifact_id="harness-log",
                            artifact_type="log",
                            path=str(harness_log_path),
                            label="Harness log",
                            metadata={},
                        )
                    )
                )

        flake_reason = _classify_flake_reason(harness_log_path, last_error)
        flaky = attempts > 1 and last_exit == 0
        ok = last_exit == 0
        status = (
            "flaky_passed"
            if flaky
            else ("passed" if ok else ("flaky_failed" if flake_reason else "failed"))
        )
        result = TesterRunResult(
            run_id=run_id,
            spec_id=spec.spec_id,
            ok=ok,
            exit_code=last_exit,
            run_dir=str(run_dir),
            harness_log_path=str(harness_log_path),
            app_log_path=str(app_log_path),
            command=harness_cmd,
            final_prompt=final_prompt,
            attempts=attempts,
            flaky=flaky,
            flake_reason=flake_reason,
            status=status,
            error=last_error,
            execution_engine=execution_engine,
            artifacts=artifacts,
            assertion_results=assertion_results,
        )
    except Exception as exc:
        flake_reason = _classify_flake_reason(harness_log_path, str(exc))
        result = TesterRunResult(
            run_id=run_id,
            spec_id=spec.spec_id,
            ok=False,
            exit_code=1,
            run_dir=str(run_dir),
            harness_log_path=str(harness_log_path),
            app_log_path=str(app_log_path),
            command=harness_cmd,
            final_prompt=final_prompt,
            attempts=max(1, attempts),
            flaky=False,
            flake_reason=flake_reason,
            status="flaky_failed" if flake_reason else "failed",
            error=str(exc),
            execution_engine=execution_engine,
            artifacts=artifacts,
            assertion_results=assertion_results,
        )
    finally:
        if harness_proc and harness_proc.poll() is None:
            harness_proc.terminate()
        if app_proc and app_proc.poll() is None:
            app_proc.terminate()

    meta_path.write_text(json.dumps(asdict(result), indent=2) + "\n")
    return result


def _classify_flake_reason(harness_log_path: Path, error: str) -> str:
    text = ""
    try:
        if harness_log_path.exists():
            text = harness_log_path.read_text(encoding="utf-8", errors="ignore").lower()
    except Exception:
        text = ""
    merged = f"{text}\n{(error or '').lower()}"
    if any(k in merged for k in ["timeout", "timed out", "net::err", "connection reset"]):
        return "network_timeout"
    if any(k in merged for k in ["selector", "element not found", "stale element"]):
        return "selector_drift"
    if any(k in merged for k in ["401", "403", "unauthorized", "forbidden", "auth"]):
        return "auth_failure"
    return ""


def load_run_summaries(runs_dir: Path, *, limit: int = 25) -> list[dict[str, Any]]:
    if not runs_dir.exists():
        return []
    items: list[dict[str, Any]] = []
    for run_file in sorted(
        runs_dir.glob("*/run.json"), key=lambda p: p.stat().st_mtime, reverse=True
    ):
        try:
            items.append(json.loads(run_file.read_text()))
        except Exception:
            continue
        if len(items) >= limit:
            break
    return items


def enqueue_spec_run(
    *,
    queue_dir: Path,
    spec_id: str,
    prompt_override: Optional[str] = None,
    app_url_override: Optional[str] = None,
    start_command_override: Optional[str] = None,
    retries: int = 0,
) -> dict[str, Any]:
    queue_dir.mkdir(parents=True, exist_ok=True)
    job_id = (
        datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    payload = {
        "job_id": job_id,
        "spec_id": spec_id,
        "prompt_override": prompt_override or "",
        "app_url_override": app_url_override or "",
        "start_command_override": start_command_override or "",
        "retries": max(0, int(retries)),
        "status": "queued",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    final_path = queue_dir / f"{job_id}.json"
    temp_path = queue_dir / f".{job_id}.json.tmp"
    with temp_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, indent=2) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    temp_path.replace(final_path)
    return payload


def run_queued_spec_once(
    *,
    specs_dir: Path,
    runs_dir: Path,
    queue_dir: Path,
    cwd: Optional[Path] = None,
) -> dict[str, Any] | None:
    queue_dir.mkdir(parents=True, exist_ok=True)
    running_dir = queue_dir / "running"
    done_dir = queue_dir / "done"
    failed_dir = queue_dir / "failed"
    running_dir.mkdir(exist_ok=True)
    done_dir.mkdir(exist_ok=True)
    failed_dir.mkdir(exist_ok=True)
    queued = sorted(queue_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not queued:
        return None
    job_path = queued[0]
    running_path = running_dir / job_path.name
    try:
        job_path.rename(running_path)
    except FileNotFoundError:
        return None

    try:
        job = json.loads(running_path.read_text())
        spec = load_spec(specs_dir, str(job["spec_id"]))
        result = run_spec(
            spec=spec,
            runs_dir=runs_dir,
            prompt_override=str(job.get("prompt_override") or "") or None,
            app_url_override=str(job.get("app_url_override") or "") or None,
            start_command_override=str(job.get("start_command_override") or "")
            or None,
            max_retries=int(job.get("retries") or 0),
            cwd=cwd,
        )
        job.update(
            {
                "status": "succeeded" if result.ok else "failed",
                "updated_at": now_iso(),
                "result": asdict(result),
            }
        )
        target = done_dir / running_path.name if result.ok else failed_dir / running_path.name
        running_path.write_text(json.dumps(job, indent=2) + "\n")
        running_path.rename(target)
        return job
    except Exception as exc:
        try:
            job = json.loads(running_path.read_text())
        except Exception:
            job = {"job_id": running_path.stem, "status": "failed"}
        job.update({"status": "failed", "updated_at": now_iso(), "error": str(exc)})
        running_path.write_text(json.dumps(job, indent=2) + "\n")
        running_path.rename(failed_dir / running_path.name)
        return job
