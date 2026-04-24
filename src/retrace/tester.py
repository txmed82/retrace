from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx


DEFAULT_HARNESS_COMMAND = (
    "browser-harness run --url {app_url} --task {prompt_q} --output {run_dir_q}"
)
DEFAULT_APP_URL = "http://127.0.0.1:3000"


@dataclass
class TesterSpec:
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
    error: str = ""


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
    return specs_dir / f"{spec_id}.json"


def save_spec(specs_dir: Path, spec: TesterSpec) -> Path:
    specs_dir.mkdir(parents=True, exist_ok=True)
    p = _spec_path(specs_dir, spec.spec_id)
    p.write_text(json.dumps(asdict(spec), indent=2) + "\n")
    return p


def load_spec(specs_dir: Path, spec_id: str) -> TesterSpec:
    p = _spec_path(specs_dir, spec_id)
    data = json.loads(p.read_text())
    _apply_spec_defaults(data)
    return TesterSpec(**data)


def list_specs(specs_dir: Path) -> list[TesterSpec]:
    if not specs_dir.exists():
        return []
    out: list[TesterSpec] = []
    for p in sorted(specs_dir.glob("*.json"), key=lambda x: x.stat().st_mtime):
        try:
            data = json.loads(p.read_text())
            _apply_spec_defaults(data)
            out.append(TesterSpec(**data))
        except Exception:
            continue
    return out


def _apply_spec_defaults(data: dict[str, Any]) -> None:
    data.setdefault("mode", "describe")
    data.setdefault("auth_required", False)
    data.setdefault("auth_mode", "none")
    data.setdefault("auth_login_url", "")
    data.setdefault("auth_username", "")
    data.setdefault("auth_password_env", "RETRACE_TESTER_AUTH_PASSWORD")
    data.setdefault("auth_jwt_env", "RETRACE_TESTER_AUTH_JWT")
    data.setdefault("auth_headers_env", "RETRACE_TESTER_AUTH_HEADERS")


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
) -> TesterSpec:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    spec_id = f"{ts}-{slugify(name)[:48]}"
    created_at = now_iso()
    spec = TesterSpec(
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
    )
    save_spec(specs_dir, spec)
    return spec


def _run_shell(
    command: str, *, stdout_fh: Any, stderr_fh: Any, cwd: Optional[Path] = None
) -> subprocess.Popen[Any]:
    return subprocess.Popen(
        ["zsh", "-lc", command],
        stdout=stdout_fh,
        stderr=stderr_fh,
        cwd=str(cwd) if cwd else None,
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


def run_spec(
    *,
    spec: TesterSpec,
    runs_dir: Path,
    prompt_override: Optional[str] = None,
    app_url_override: Optional[str] = None,
    start_command_override: Optional[str] = None,
    auth_context_override: Optional[dict[str, str]] = None,
    cwd: Optional[Path] = None,
) -> TesterRunResult:
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir = runs_dir / f"{run_id}-{slugify(spec.name)[:40]}"
    run_dir.mkdir(parents=True, exist_ok=True)

    prompt = (prompt_override or spec.prompt or "").strip()
    app_url = (app_url_override or spec.app_url or DEFAULT_APP_URL).strip()
    start_command = (start_command_override or spec.start_command or "").strip()
    final_prompt = _compose_task_prompt(
        mode=spec.mode,
        prompt=prompt,
        auth_context=auth_context_override or _auth_context_from_env(spec),
    )
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

    try:
        with app_log_path.open("w") as app_log:
            if start_command:
                app_proc = _run_shell(
                    start_command, stdout_fh=app_log, stderr_fh=app_log, cwd=cwd
                )
                if not _wait_for_http(app_url):
                    raise RuntimeError(
                        f"App did not become reachable at {app_url} after startup."
                    )

        exit_code = 1
        with harness_log_path.open("w") as harness_log:
            harness_proc = _run_shell(
                harness_cmd, stdout_fh=harness_log, stderr_fh=harness_log, cwd=cwd
            )
            exit_code = harness_proc.wait(timeout=900)

        result = TesterRunResult(
            run_id=run_id,
            spec_id=spec.spec_id,
            ok=(exit_code == 0),
            exit_code=exit_code,
            run_dir=str(run_dir),
            harness_log_path=str(harness_log_path),
            app_log_path=str(app_log_path),
            command=harness_cmd,
            final_prompt=final_prompt,
        )
    except Exception as exc:
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
            error=str(exc),
        )
    finally:
        if harness_proc and harness_proc.poll() is None:
            harness_proc.terminate()
        if app_proc and app_proc.poll() is None:
            app_proc.terminate()

    meta_path.write_text(json.dumps(asdict(result), indent=2) + "\n")
    return result


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
