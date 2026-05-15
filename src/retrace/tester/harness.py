from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from retrace.artifacts import tester_artifact_manifest_items, write_artifact_manifest
from retrace.browser_harness import (
    BrowserHarnessAdapter,
    clear_browser_harness_attempt_outputs,
)
from retrace.script_steps import (
    render_template,
    run_script_step,
)

from .models import (
    DEFAULT_HARNESS_COMMAND,
    FAILURE_CLASSIFICATIONS,
    SUITE_PROPOSAL_SCHEMA_VERSION,
    EngineSelection,
    TesterArtifact,
    TesterAssertionResult,
    TesterRunResult,
    TesterSpec,
    TesterStepCacheEvent,
    now_iso,
    slugify,
)
from .assertions import (
    _assertion_result,
    _assertion_text_for_classification,
    _classify_failure,
    _evaluate_consensus_assertion,
    _evaluate_model_backed_consensus_assertion,
    _evaluate_native_assertion,
    _failed_selector_assertion,
    _flake_reason_from_classification,
    _redacted_response_headers,
    _response_assertion_evidence,
)
from .specs import load_spec, select_execution_engine, validate_spec, create_spec

logger = logging.getLogger(__name__)

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
        "profile": spec.auth_profile,
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
        if auth_context.get("profile"):
            lines.append(f"- profile: {auth_context.get('profile', '')}")
        lines.append(f"- login_url: {auth_context.get('login_url', '')}")
        lines.append(f"- username: {auth_context.get('username', '')}")
        lines.append("- password: use provided runtime secret")
    elif mode_name == "jwt":
        if auth_context.get("profile"):
            lines.append(f"- profile: {auth_context.get('profile', '')}")
        lines.append("- auth type: bearer token")
        lines.append("- jwt: use provided runtime token")
    elif mode_name == "headers":
        if auth_context.get("profile"):
            lines.append(f"- profile: {auth_context.get('profile', '')}")
        lines.append("- auth type: custom headers")
        lines.append("- headers_json: use provided runtime JSON headers")
    else:
        lines.append("- auth required but mode unspecified; discover login path first")
    return "\n".join(lines)


def _native_auth_headers(auth_context: dict[str, str]) -> tuple[dict[str, str], str]:
    if str(auth_context.get("required", "")).lower() not in {"1", "true", "yes", "on"}:
        return {}, ""
    mode = str(auth_context.get("mode") or "none").strip().lower()
    if mode == "jwt":
        token = str(auth_context.get("jwt") or "").strip()
        if not token:
            return {}, "auth failure: missing JWT token env var"
        return {"Authorization": f"Bearer {token}"}, ""
    if mode == "headers":
        raw = str(auth_context.get("headers_json") or "").strip()
        if not raw:
            return {}, "auth failure: missing auth headers env var"
        try:
            parsed = json.loads(raw)
        except Exception as exc:
            return {}, f"auth failure: invalid auth headers JSON: {exc}"
        if not isinstance(parsed, dict):
            return {}, "auth failure: auth headers must be a JSON object"
        return {str(k): str(v) for k, v in parsed.items()}, ""
    if mode == "form":
        return {}, "auth failure: native execution does not support form auth"
    return {}, "auth failure: auth mode is required"


def _join_url(base_url: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path:
        return base_url
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


from .assertions import (
    _assertion_result,
    _assertion_text_for_classification,
    _classify_failure,
    _evaluate_consensus_assertion,
    _evaluate_model_backed_consensus_assertion,
    _evaluate_native_assertion,
    _failed_selector_assertion,
    _flake_reason_from_classification,
    _redacted_response_headers,
    _response_assertion_evidence,
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


def _record_script_step(
    *,
    step: dict[str, Any],
    idx: int,
    scope: dict[str, Any],
    artifacts_dir: Path,
    artifacts: list[TesterArtifact],
    assertion_results: list[TesterAssertionResult],
) -> None:
    """Run a `script` step under the safe evaluator and record its outputs.

    Writes one JSON artifact per script step (containing the variables that
    were set and the per-assertion results) and appends each script
    assertion to `assertion_results` so the run summary reflects them.
    """
    step_id = str(step.get("id") or f"script-{idx}")
    outcome = run_script_step(step, scope=scope)
    payload = {
        "step_id": step_id,
        "set": outcome.set_vars,
        "assertions": outcome.assertions,
        "error": outcome.error,
    }
    out_path = artifacts_dir / f"script-{idx}.json"
    out_path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    artifacts.append(
        TesterArtifact(
            artifact_id=f"script-{idx}",
            artifact_type="script_step",
            path=str(out_path),
            label=f"Script step {step_id}",
            metadata={
                "set_count": len(outcome.set_vars),
                "assert_count": len(outcome.assertions),
                "ok": outcome.ok,
            },
        )
    )
    if outcome.error:
        assertion_results.append(
            _assertion_result(
                assertion={"id": step_id, "type": "script"},
                ok=False,
                expected="script_step_ok",
                actual=outcome.error,
                message=outcome.error,
            )
        )
        return
    for record in outcome.assertions:
        assertion_results.append(
            _assertion_result(
                assertion={
                    "id": str(record.get("id") or step_id),
                    "type": "script",
                    "expression": record.get("expression"),
                },
                ok=bool(record.get("ok")),
                expected=True,
                actual=record.get("ok"),
                message=str(record.get("message") or ""),
            )
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


_VISUAL_DRIVER_FACTORY: Optional[Callable[..., Any]] = None
_VISUAL_LLM_FACTORY: Optional[Callable[..., Any]] = None


def set_visual_factories(
    *,
    driver_factory: Optional[Callable[..., Any]] = None,
    llm_factory: Optional[Callable[..., Any]] = None,
) -> None:
    """Override visual-mode driver/LLM factories for tests."""
    global _VISUAL_DRIVER_FACTORY, _VISUAL_LLM_FACTORY
    _VISUAL_DRIVER_FACTORY = driver_factory
    _VISUAL_LLM_FACTORY = llm_factory


def _run_visual_spec(
    *,
    spec: TesterSpec,
    app_url: str,
    run_dir: Path,
    log_path: Path,
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]], str]:
    from retrace.visual_explorer import run_visual_explorer

    artifacts: list[dict[str, Any]] = []
    assertion_results: list[dict[str, Any]] = []
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as log_fh:
        log_fh.write(f"--- visual engine starting for {spec.spec_id} ---\n")

    try:
        if _VISUAL_DRIVER_FACTORY is not None:
            driver = _VISUAL_DRIVER_FACTORY(browser_settings=spec.browser_settings)
        else:
            from retrace.visual_explorer import build_playwright_visual_driver

            driver = build_playwright_visual_driver(
                browser_settings=spec.browser_settings
            )
    except Exception as exc:
        return 1, artifacts, assertion_results, f"visual driver setup failed: {exc}"

    try:
        if _VISUAL_LLM_FACTORY is not None:
            llm = _VISUAL_LLM_FACTORY()
        else:
            from retrace.config import LLMConfig
            from retrace.llm.client import LLMClient

            base_cfg = spec.fixtures.get("__llm__")
            cfg = base_cfg if isinstance(base_cfg, LLMConfig) else None
            if cfg is None:
                raise RuntimeError(
                    "visual engine requires a multimodal LLM; provide one via "
                    "set_visual_factories or via the calling integration"
                )
            llm = LLMClient(cfg)
    except Exception as exc:
        try:
            driver.close()
        except Exception:
            pass
        return 1, artifacts, assertion_results, f"visual llm setup failed: {exc}"

    visual_max_steps_raw = spec.browser_settings.get("visual_max_steps")
    try:
        run_kwargs: dict[str, Any] = {
            "spec_id": spec.spec_id,
            "spec_name": spec.name,
            "app_url": app_url,
            "exploratory_goals": list(spec.exploratory_goals),
            "run_dir": run_dir,
            "driver": driver,
            "llm": llm,
        }
        if isinstance(visual_max_steps_raw, int) and visual_max_steps_raw > 0:
            run_kwargs["max_steps"] = visual_max_steps_raw
        result = run_visual_explorer(**run_kwargs)
    except Exception as exc:
        return 1, artifacts, assertion_results, f"visual run failed: {exc}"
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


def _run_explore_spec(
    *,
    spec: TesterSpec,
    app_url: str,
    run_dir: Path,
    runs_dir: Path,
    log_path: Path,
    run_id: str,
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
    if spec.mode == "explore_suite" and result.ok:
        artifacts.extend(
            _write_suite_proposal_and_drafts(
                spec=spec,
                app_url=app_url,
                run_dir=run_dir,
                specs_dir=runs_dir.parent / "specs",
                source_run=run_id,
                explore_result=result,
            )
        )
    error = result.error
    exit_code = 0 if result.ok else 1
    return exit_code, artifacts, assertion_results, error


def _write_suite_proposal_and_drafts(
    *,
    spec: TesterSpec,
    app_url: str,
    run_dir: Path,
    specs_dir: Path,
    source_run: str,
    explore_result: Any,
) -> list[dict[str, Any]]:
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    goals = [str(goal).strip() for goal in spec.exploratory_goals if str(goal).strip()]
    if not goals:
        goals = [spec.name or "Primary user journey"]
    exact_steps = _exact_steps_from_exploration(app_url, explore_result.steps)
    proposal_id = f"suite_{uuid.uuid4().hex[:12]}"
    proposals: list[dict[str, Any]] = []
    for rank, goal in enumerate(goals, start=1):
        criticality = _criticality_for_goal(goal)
        reason = _proposal_reason(
            goal=goal,
            criticality=criticality,
            finish_summary=str(explore_result.finish_summary or ""),
        )
        draft = create_spec(
            specs_dir=specs_dir,
            name=f"{goal[:72]} regression",
            prompt=goal,
            app_url=app_url,
            start_command=spec.start_command or "",
            harness_command=spec.harness_command or DEFAULT_HARNESS_COMMAND,
            mode="describe",
            execution_engine="native",
            auth_required=spec.auth_required,
            auth_mode=spec.auth_mode,
            auth_login_url=spec.auth_login_url,
            auth_username=spec.auth_username,
            auth_password_env=spec.auth_password_env,
            auth_jwt_env=spec.auth_jwt_env,
            auth_headers_env=spec.auth_headers_env,
            auth_profile=spec.auth_profile,
            auth_setup_steps=list(spec.auth_setup_steps or []),
            exact_steps=exact_steps,
            assertions=[
                {
                    "id": "page-loads",
                    "type": "status_code",
                    "expected_status": 200,
                    "source": "suite_proposal",
                }
            ],
            env_overrides=dict(spec.env_overrides or {}),
            browser_settings={**dict(spec.browser_settings or {}), "runtime": "http"},
            fixtures={
                "draft_status": "draft",
                "draft_reason": reason,
                "source_exploration_run": source_run,
                "suite_proposal_id": proposal_id,
                "source_explore_spec_id": spec.spec_id,
                "criticality": criticality,
                "rank": rank,
                "source_auth": {
                    "auth_required": spec.auth_required,
                    "auth_mode": spec.auth_mode,
                    "auth_login_url": spec.auth_login_url,
                    "auth_username": spec.auth_username,
                    "auth_password_env": spec.auth_password_env,
                    "auth_jwt_env": spec.auth_jwt_env,
                    "auth_headers_env": spec.auth_headers_env,
                },
            },
        )
        proposals.append(
            {
                "rank": rank,
                "criticality": criticality,
                "name": draft.name,
                "draft_spec_id": draft.spec_id,
                "reason": reason,
                "source_goal": goal,
                "source_exploration_run": source_run,
                "exact_steps": exact_steps,
            }
        )
    payload = {
        "schema_version": SUITE_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "source_spec_id": spec.spec_id,
        "source_exploration_run": source_run,
        "generated_at": now_iso(),
        "finish_summary": str(explore_result.finish_summary or ""),
        "proposals": proposals,
    }
    proposal_path = artifacts_dir / "suite-proposal.json"
    proposal_path.write_text(json.dumps(payload, indent=2) + "\n")
    return [
        {
            "artifact_id": "suite-proposal",
            "artifact_type": "suite_proposal",
            "path": str(proposal_path),
            "label": "AI exploration suite proposal",
            "metadata": {
                "proposal_id": proposal_id,
                "draft_count": len(proposals),
                "source_exploration_run": source_run,
            },
        }
    ]


def _exact_steps_from_exploration(
    app_url: str,
    steps: list[Any],
) -> list[dict[str, Any]]:
    exact_steps: list[dict[str, Any]] = []
    for step in steps:
        if not getattr(step, "ok", False):
            continue
        call = getattr(step, "call", None)
        tool = str(getattr(call, "tool", "") or "")
        args = dict(getattr(call, "args", {}) or {})
        step_id = f"explore-{len(exact_steps) + 1}"
        if tool == "navigate":
            exact_steps.append(
                {"id": step_id, "action": "navigate", "url": str(args.get("url") or app_url)}
            )
        elif tool == "click" and args.get("selector"):
            exact_steps.append(
                {"id": step_id, "action": "click", "selector": str(args["selector"])}
            )
        elif tool == "type" and args.get("selector"):
            exact_steps.append(
                {
                    "id": step_id,
                    "action": "type",
                    "selector": str(args["selector"]),
                    "text": str(args.get("text") or ""),
                }
            )
        elif tool == "press":
            exact_steps.append(
                {
                    "id": step_id,
                    "action": "keypress",
                    "selector": str(args.get("selector") or ""),
                    "key": str(args.get("key") or ""),
                }
            )
        elif tool == "wait_for" and args.get("selector"):
            wait_step = {
                "id": step_id,
                "action": "wait_for",
                "selector": str(args["selector"]),
                "timeout_ms": int(args.get("timeout_ms") or 5000),
            }
            if args.get("state"):
                wait_step["state"] = str(args["state"])
            exact_steps.append(wait_step)
    if not exact_steps:
        exact_steps.append({"id": "page-load", "action": "navigate", "url": app_url})
    return exact_steps


def _criticality_for_goal(goal: str) -> str:
    normalized = goal.lower()
    if any(
        term in normalized
        for term in ["checkout", "payment", "billing", "signup", "login", "purchase"]
    ):
        return "high"
    if any(term in normalized for term in ["settings", "profile", "search", "invite"]):
        return "medium"
    return "low"


def _proposal_reason(
    *,
    goal: str,
    criticality: str,
    finish_summary: str,
) -> str:
    summary = finish_summary.strip()
    suffix = f" Exploration summary: {summary}" if summary else ""
    return f"{criticality.title()} criticality flow discovered for goal: {goal}.{suffix}"


def _run_native_spec(
    *,
    spec: TesterSpec,
    app_url: str,
    run_dir: Path,
    log_path: Path,
    auth_context: Optional[dict[str, str]] = None,
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
    script_scope: dict[str, Any] = {"vars": {}, "env": dict(spec.env_overrides or {})}
    request_headers, auth_error = _native_auth_headers(auth_context or {})
    if auth_error:
        return 1, artifacts, assertion_results, auth_error

    steps = [*spec.auth_setup_steps, *(spec.exact_steps or [])] or [
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
                if action == "script":
                    _record_script_step(
                        step=step,
                        idx=idx,
                        scope=script_scope,
                        artifacts_dir=artifacts_dir,
                        artifacts=artifacts,
                        assertion_results=assertion_results,
                    )
                    continue
                if action in {"visit", "goto", "get", "navigate"}:
                    resolved_url = _join_url(
                        app_url,
                        render_template(
                            str(step.get("url") or step.get("path") or ""),
                            script_scope,
                        ),
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
                        last_response = client.get(request_url, headers=request_headers)
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
                        last_response = client.get(resolved_url, headers=request_headers)
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
        "drag_and_drop",
        "select",
        "scroll",
    }
    return any(
        str(step.get("action") or step.get("type") or "").lower() in browser_actions
        and _browser_step_has_runtime_signal(step)
        for step in steps
    )


def _browser_step_has_runtime_signal(step: dict[str, Any]) -> bool:
    action = str(step.get("action") or step.get("type") or "").strip().lower()
    if action in {"wait", "keypress", "scroll", "drag_and_drop"}:
        return True
    if step.get("selector") or step.get("text") or step.get("key"):
        return True
    target = step.get("target")
    if isinstance(target, dict) and target.get("selector"):
        return True
    if step.get("to") or step.get("target_selector") or step.get("destination_selector"):
        return True
    return False


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
    script_scope: dict[str, Any] = {"vars": {}, "env": dict(spec.env_overrides or {})}
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
                if action == "script":
                    _record_script_step(
                        step=step,
                        idx=idx,
                        scope=script_scope,
                        artifacts_dir=artifacts_dir,
                        artifacts=artifacts,
                        assertion_results=assertion_results,
                    )
                    continue
                if action in {"visit", "goto", "get", "navigate"}:
                    response = page.goto(
                        _join_url(
                            app_url,
                            render_template(
                                str(step.get("url") or step.get("path") or ""),
                                script_scope,
                            ),
                        ),
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
                    page.locator(selector).fill(
                        render_template(str(step.get("text") or ""), script_scope)
                    )
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
                    # Preserve falsy-but-valid option values (0, "") by
                    # checking key presence rather than truthiness.
                    if "value" in step:
                        option = step["value"]
                    elif "option" in step:
                        option = step["option"]
                    elif "text" in step:
                        option = step["text"]
                    else:
                        option = None
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
                    if not pattern:
                        ok = False
                        actual_payload: dict[str, Any] = {
                            "matched": False,
                            "error": "empty_pattern",
                        }
                    else:
                        try:
                            ok = bool(_re.search(pattern, last_text))
                            actual_payload = {"matched": ok}
                        except _re.error as exc:
                            ok = False
                            actual_payload = {
                                "matched": False,
                                "error": f"invalid_regex: {exc}",
                            }
                    assertion_results.append(
                        _assertion_result(
                            assertion=assertion,
                            ok=ok,
                            expected=pattern,
                            actual=actual_payload,
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
            try:
                current_url = str(page.url or "")
            except Exception:
                current_url = ""
            try:
                page.screenshot(path=str(screenshot_path), full_page=True)
                artifacts.append(
                    TesterArtifact(
                        artifact_id="playwright-final-screenshot",
                        artifact_type="screenshot",
                        path=str(screenshot_path),
                        label="Final Playwright screenshot",
                        metadata={"url": current_url},
                    )
                )
            except Exception as exc:
                artifacts.append(
                    TesterArtifact(
                        artifact_id="playwright-final-screenshot-error",
                        artifact_type="screenshot_error",
                        path="",
                        label="Final Playwright screenshot error",
                        metadata={"url": current_url, "error": str(exc)},
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
    # `to` may be a string selector or a dict like {"selector": "..."};
    # do NOT str() a dict here — that produces a literal "{'selector': '#x'}"
    # which Playwright then tries to parse as a CSS selector.
    to_value = step.get("to")
    if isinstance(to_value, dict):
        from_to_dict = str(to_value.get("selector") or "").strip()
        if from_to_dict:
            return from_to_dict
        to_value = None
    direct = str(
        step.get("target_selector")
        or step.get("destination_selector")
        or to_value
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
    engine_selection = select_execution_engine(spec)
    execution_engine = engine_selection.execution_engine
    engine_reason = engine_selection.reason
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
    last_failed_classification = ""
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
                    auth_context=auth_context,
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
                run_id=run_id,
            )
        elif execution_engine == "visual":
            attempts += 1
            last_exit, artifacts, assertion_results, last_error = _run_visual_spec(
                spec=spec,
                app_url=app_url,
                run_dir=run_dir,
                log_path=harness_log_path,
            )
        else:
            retry_count = max(0, int(max_retries))
            for attempt in range(retry_count + 1):
                attempts += 1
                clear_browser_harness_attempt_outputs(run_dir, harness_log_path)
                harness_run = BrowserHarnessAdapter(
                    command=harness_cmd,
                    run_dir=run_dir,
                    log_path=harness_log_path,
                    cwd=cwd,
                    auth_context=auth_context,
                    env_overrides=spec.env_overrides,
                    shell_runner=_run_shell,
                ).run()
                last_exit = harness_run.exit_code
                last_error = harness_run.error
                artifacts = harness_run.artifacts
                assertion_results = harness_run.assertion_results
                if last_exit == 0:
                    break
                last_failed_classification = _classify_failure(
                    harness_log_path=harness_log_path,
                    error=last_error,
                    assertion_results=assertion_results,
                    exit_code=last_exit,
                )

        failure_classification = _classify_failure(
            harness_log_path=harness_log_path,
            error=last_error,
            assertion_results=assertion_results,
            exit_code=last_exit,
        )
        if last_exit == 0 and last_failed_classification:
            failure_classification = last_failed_classification
        flake_reason = _flake_reason_from_classification(failure_classification)
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
            failure_classification=failure_classification,
            error=last_error,
            execution_engine=execution_engine,
            engine_reason=engine_reason,
            artifacts=artifacts,
            assertion_results=assertion_results,
        )
    except Exception as exc:
        failure_classification = _classify_failure(
            harness_log_path=harness_log_path,
            error=str(exc),
            assertion_results=assertion_results,
            exit_code=1,
        )
        flake_reason = _flake_reason_from_classification(failure_classification)
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
            failure_classification=failure_classification,
            error=str(exc),
            execution_engine=execution_engine,
            engine_reason=engine_reason,
            artifacts=artifacts,
            assertion_results=assertion_results,
        )
    finally:
        if harness_proc and harness_proc.poll() is None:
            harness_proc.terminate()
        if app_proc and app_proc.poll() is None:
            app_proc.terminate()

    try:
        manifest_items = tester_artifact_manifest_items(
            result.artifacts,
            source_run=result.run_id,
        )
        manifest_artifact = write_artifact_manifest(
            manifest_path=run_dir / "artifacts" / "artifact-manifest.json",
            artifacts=manifest_items,
            source_run=result.run_id,
            metadata={"spec_id": spec.spec_id, "execution_engine": execution_engine},
        )
        result.artifacts = [*result.artifacts, manifest_artifact]
    except Exception as exc:
        logger.warning(
            "failed to write tester artifact manifest for run %s: %s",
            result.run_id,
            exc,
        )
    meta_path.write_text(json.dumps(asdict(result), indent=2) + "\n")
    return result


def _classify_failure(
    *,
    harness_log_path: Path,
    error: str,
    assertion_results: list[dict[str, Any]],
    exit_code: int,
) -> str:
    text = ""
    try:
        if harness_log_path.exists():
            text = harness_log_path.read_text(encoding="utf-8", errors="ignore").lower()
    except Exception:
        text = ""
    failed_assertions = [
        item for item in assertion_results if not bool(item.get("ok", False))
    ]
    merged = "\n".join(
        [
            text,
            str(error or ""),
            _assertion_text_for_classification(failed_assertions),
        ]
    ).lower()
    if any(
        k in merged
        for k in [
            "app did not become reachable",
            "connection refused",
            "econnrefused",
            "net::err_connection_refused",
            "failed to connect",
            "could not connect",
            "server unavailable",
        ]
    ):
        return "environment_failure"
    if any(k in merged for k in ["timeout", "timed out", "deadline exceeded"]):
        return "timeout"
    if any(
        k in merged
        for k in [
            "needs selector",
            "unsupported_in_playwright",
            "unsupported native assertion type",
            "unsupported native step action",
            "invalid_regex",
            "unknown action",
            "malformed",
        ]
    ):
        return "test_bug"
    if any(k in merged for k in ["401", "403", "unauthorized", "forbidden"]):
        return "auth_failure"
    if any(k in merged for k in ["authentication failed", "auth failure", "login failed"]):
        return "auth_failure"
    if _failed_selector_assertion(failed_assertions) or any(
        k in merged
        for k in [
            "element not found",
            "stale element",
            "waiting for selector",
            "waiting for locator",
            "strict mode violation",
        ]
    ):
        return "selector_drift"
    if failed_assertions:
        return "app_bug"
    if int(exit_code) != 0 or error:
        return "unknown"
    return "unknown"


def _assertion_text_for_classification(items: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for item in items:
        for key in (
            "assertion_type",
            "message",
            "actual",
            "expected",
            "step",
            "assertion",
        ):
            value = item.get(key)
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                chunks.append(json.dumps(value, sort_keys=True, default=str))
            else:
                chunks.append(str(value))
    return "\n".join(chunks)


def _failed_selector_assertion(items: list[dict[str, Any]]) -> bool:
    for item in items:
        assertion_type = str(item.get("assertion_type") or "").lower()
        if assertion_type in {
            "selector_visible",
            "element_visible",
            "visible",
        }:
            return True
    return False


def _flake_reason_from_classification(failure_classification: str) -> str:
    if failure_classification in {
        "auth_failure",
        "environment_failure",
        "selector_drift",
        "timeout",
    }:
        return failure_classification
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
