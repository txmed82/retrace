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
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx


DEFAULT_HARNESS_COMMAND = (
    "browser-harness run --url {app_url} --task {prompt_q} --output {run_dir_q}"
)
DEFAULT_APP_URL = "http://127.0.0.1:3000"
SPEC_SCHEMA_VERSION = 1
ALLOWED_MODES = {"describe", "explore_suite"}
ALLOWED_AUTH_MODES = {"none", "form", "jwt", "headers"}
ALLOWED_EXECUTION_ENGINES = {"harness", "native", "auto"}


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
    data.setdefault("step_cache_enabled", True)
    data.setdefault("assertion_consensus_enabled", True)
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
    needs_harness_command = spec.execution_engine == "harness" or (
        spec.execution_engine == "auto" and not (spec.exact_steps or spec.assertions)
    )
    native_selected = spec.execution_engine == "native" or (
        spec.execution_engine == "auto" and bool(spec.exact_steps or spec.assertions)
    )
    if native_selected and spec.auth_required:
        raise ValueError("native execution does not yet support auth_required specs")
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
) -> TesterAssertionResult:
    assertion_type = str(
        assertion.get("type") or assertion.get("assertion_type") or "unknown"
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
        confidence=float(assertion.get("confidence") or 1.0),
        consensus_group=str(assertion.get("consensus_group") or ""),
        model_votes=list(assertion.get("model_votes") or []),
    )


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
    votes = [
        vote
        for vote in list(assertion.get("model_votes") or assertion.get("votes") or [])
        if isinstance(vote, dict)
    ]
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
    if pass_count == fail_count and arbiter is not None:
        ok = arbiter
        decision = "arbiter"
    else:
        ok = pass_count > fail_count
        decision = "majority"
    return _assertion_result(
        assertion=assertion,
        ok=ok,
        expected=assertion.get("expected", "model vote majority"),
        actual={
            "decision": decision,
            "pass_votes": pass_count,
            "fail_votes": fail_count,
            "arbiter_vote": arbiter,
        },
        message=(
            f"Consensus {decision}: {pass_count} pass vote(s), "
            f"{fail_count} fail vote(s)."
        ),
    )


def _evaluate_native_assertion(
    assertion: dict[str, Any],
    *,
    response: Optional[httpx.Response],
) -> TesterAssertionResult:
    kind = str(assertion.get("type") or assertion.get("assertion_type") or "").lower()
    if kind in {"model_consensus", "consensus", "ai_consensus"}:
        return _evaluate_consensus_assertion(assertion)
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


def _run_native_spec(
    *,
    spec: TesterSpec,
    app_url: str,
    run_dir: Path,
    log_path: Path,
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
                    cached = (
                        _load_step_cache(cache_dir, cache_key)
                        if spec.step_cache_enabled
                        else None
                    )
                    cached_url = ""
                    if cached:
                        cached_url = str(
                            cached.get("effective_url")
                            or cached.get("resolved_url")
                            or ""
                        )
                    use_cached_url = bool(cached_url and cached_url != resolved_url)
                    request_url = cached_url if use_cached_url else resolved_url
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
                        if use_cached_url and last_response.status_code >= 500:
                            raise httpx.HTTPStatusError(
                                "Cached step URL returned server error.",
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
        execution_engine = (
            "native" if spec.exact_steps or spec.assertions else "harness"
        )
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
            attempts += 1
            last_exit, artifacts, assertion_results, last_error = _run_native_spec(
                spec=spec,
                app_url=app_url,
                run_dir=run_dir,
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
