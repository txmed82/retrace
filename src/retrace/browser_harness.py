from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class BrowserHarnessArtifact:
    artifact_id: str
    artifact_type: str
    path: str
    label: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BrowserHarnessRun:
    exit_code: int
    error: str
    final_status: str
    artifacts: list[dict[str, Any]]
    assertion_results: list[dict[str, Any]]
    structured_output: dict[str, Any]


class BrowserHarnessAdapter:
    def __init__(
        self,
        *,
        command: str,
        run_dir: Path,
        log_path: Path,
        cwd: Path | None = None,
        auth_context: dict[str, str] | None = None,
        env_overrides: dict[str, str] | None = None,
        timeout_seconds: int = 900,
        shell_runner: Callable[..., subprocess.Popen[Any]] | None = None,
    ) -> None:
        self.command = command
        self.run_dir = run_dir
        self.log_path = log_path
        self.cwd = cwd
        self.auth_context = dict(auth_context or {})
        self.env_overrides = dict(env_overrides or {})
        self.timeout_seconds = int(timeout_seconds)
        self.shell_runner = shell_runner or _run_shell

    def run(self) -> BrowserHarnessRun:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        artifacts_dir = self.run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        exit_code = 1
        error = ""
        proc: subprocess.Popen[Any] | None = None
        try:
            with self.log_path.open("a") as harness_log:
                proc = self.shell_runner(
                    self.command,
                    stdout_fh=harness_log,
                    stderr_fh=harness_log,
                    cwd=self.cwd,
                    auth_context=self.auth_context,
                    env_overrides=self.env_overrides,
                )
                exit_code = proc.wait(timeout=self.timeout_seconds)
        except Exception as exc:
            error = str(exc)
            exit_code = 1
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                    proc.wait(timeout=5)

        structured = normalize_browser_harness_output(
            run_dir=self.run_dir,
            log_path=self.log_path,
            exit_code=exit_code,
            error=error,
        )
        artifacts = _write_structured_artifacts(
            run_dir=self.run_dir,
            log_path=self.log_path,
            structured=structured,
        )
        return BrowserHarnessRun(
            exit_code=exit_code,
            error=error or str(structured.get("error") or ""),
            final_status=str(structured.get("final_status") or ""),
            artifacts=[asdict(item) for item in artifacts],
            assertion_results=list(structured.get("assertion_results") or []),
            structured_output=structured,
        )


def clear_browser_harness_attempt_outputs(run_dir: Path, log_path: Path) -> None:
    if log_path.exists():
        _unlink_quietly(log_path)
    if not run_dir.exists():
        return
    removable_suffixes = {".json", ".png", ".jpg", ".jpeg", ".webp"}
    for path in sorted(run_dir.rglob("*"), reverse=True):
        if path.is_dir():
            continue
        if path.name in {"run.json", "artifact-manifest.json"}:
            continue
        if path == log_path or path.suffix.lower() in removable_suffixes:
            _unlink_quietly(path)


def normalize_browser_harness_output(
    *,
    run_dir: Path,
    log_path: Path,
    exit_code: int,
    error: str = "",
) -> dict[str, Any]:
    payloads = _load_harness_payloads(run_dir=run_dir, log_path=log_path)
    steps: list[dict[str, Any]] = []
    screenshots: list[dict[str, Any]] = []
    console: list[dict[str, Any]] = []
    network: list[dict[str, Any]] = []
    assertions: list[dict[str, Any]] = []
    discovered_artifacts: list[dict[str, Any]] = []
    final_status = ""
    payload_error = ""

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        payload_status = _first_text(
            payload, ["final_status", "status", "outcome", "result"]
        )
        final_status = final_status or payload_status
        payload_error = payload_error or _payload_error(payload, payload_status)
        steps.extend(_normalize_steps(_first_list(payload, ["steps", "actions"])))
        screenshots.extend(
            _normalize_screenshots(_first_list(payload, ["screenshots"]), run_dir)
        )
        console.extend(_normalize_records(_first_list(payload, ["console", "logs"])))
        network.extend(_normalize_records(_first_list(payload, ["network", "requests"])))
        assertions.extend(
            _normalize_assertions(
                _first_list(payload, ["assertion_results", "assertions", "tests"])
            )
        )
        discovered_artifacts.extend(
            _normalize_declared_artifacts(_first_list(payload, ["artifacts"]), run_dir)
        )

    screenshots.extend(_discover_screenshots(run_dir, screenshots))
    if not final_status:
        final_status = "passed" if exit_code == 0 else "failed"
    return {
        "schema_version": "browser_harness_run.v1",
        "final_status": final_status,
        "exit_code": int(exit_code),
        "ok": int(exit_code) == 0 and final_status.lower() not in {"failed", "error"},
        "error": error or payload_error,
        "steps": steps,
        "screenshots": screenshots,
        "console": console,
        "network": network,
        "assertion_results": assertions,
        "artifacts": discovered_artifacts,
    }


def _write_structured_artifacts(
    *,
    run_dir: Path,
    log_path: Path,
    structured: dict[str, Any],
) -> list[BrowserHarnessArtifact]:
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[BrowserHarnessArtifact] = []
    if log_path.exists():
        artifacts.append(
            BrowserHarnessArtifact(
                artifact_id="harness-log",
                artifact_type="log",
                path=str(log_path),
                label="Harness log",
            )
        )
    output_path = artifacts_dir / "browser-harness-output.json"
    output_path.write_text(json.dumps(structured, indent=2, sort_keys=True) + "\n")
    artifacts.append(
        BrowserHarnessArtifact(
            artifact_id="browser-harness-output",
            artifact_type="browser_harness_output",
            path=str(output_path),
            label="Structured Browser Harness output",
            metadata={"schema_version": structured.get("schema_version")},
        )
    )
    for key, artifact_type, label in [
        ("steps", "browser_harness_steps", "Browser Harness steps"),
        ("console", "console_output", "Browser console output"),
        ("network", "network_output", "Browser network output"),
        ("assertion_results", "assertion_results", "Harness assertion results"),
    ]:
        records = list(structured.get(key) or [])
        if not records:
            continue
        path = artifacts_dir / f"{key}.json"
        path.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n")
        artifacts.append(
            BrowserHarnessArtifact(
                artifact_id=f"browser-harness-{key}",
                artifact_type=artifact_type,
                path=str(path),
                label=label,
                metadata={"count": len(records)},
            )
        )
    for idx, screenshot in enumerate(structured.get("screenshots") or [], start=1):
        path = str(screenshot.get("path") or "")
        if not path:
            continue
        artifacts.append(
            BrowserHarnessArtifact(
                artifact_id=str(screenshot.get("artifact_id") or f"screenshot-{idx}"),
                artifact_type="screenshot",
                path=path,
                label=str(screenshot.get("label") or f"Screenshot {idx}"),
                metadata={k: v for k, v in screenshot.items() if k != "path"},
            )
        )
    for idx, item in enumerate(structured.get("artifacts") or [], start=1):
        path = str(item.get("path") or "")
        if not path:
            continue
        artifacts.append(
            BrowserHarnessArtifact(
                artifact_id=str(item.get("artifact_id") or f"harness-artifact-{idx}"),
                artifact_type=str(item.get("artifact_type") or "harness_artifact"),
                path=path,
                label=str(item.get("label") or f"Harness artifact {idx}"),
                metadata=dict(item.get("metadata") or {}),
            )
        )
    return artifacts


def _run_shell(
    command: str,
    *,
    stdout_fh: Any,
    stderr_fh: Any,
    cwd: Path | None = None,
    auth_context: dict[str, str] | None = None,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.Popen[Any]:
    shell = os.environ.get("SHELL", "").strip()
    shell_cmd = shell if shell and shutil.which(shell) else ""
    if not shell_cmd:
        shell_cmd = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
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


def _load_harness_payloads(*, run_dir: Path, log_path: Path) -> list[Any]:
    payloads: list[Any] = []
    for path in sorted(run_dir.rglob("*.json")):
        if path.name in {"run.json", "artifact-manifest.json"}:
            continue
        try:
            payloads.append(json.loads(path.read_text()))
        except Exception:
            continue
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not (line.startswith("{") and line.endswith("}")):
                continue
            try:
                payloads.append(json.loads(line))
            except Exception:
                continue
    return payloads


def _first_text(payload: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_list(payload: dict[str, Any], keys: list[str]) -> list[Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _normalize_steps(records: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, record in enumerate(records, start=1):
        if isinstance(record, str):
            out.append({"index": idx, "action": record, "ok": True, "raw": record})
        elif isinstance(record, dict):
            out.append(
                {
                    "index": _safe_int(_first_present(record, ["index", "step"], idx), idx),
                    "action": str(
                        record.get("action")
                        or record.get("type")
                        or record.get("name")
                        or ""
                    ),
                    "target": record.get("target") or record.get("selector") or "",
                    "ok": _step_ok(record),
                    "message": str(record.get("message") or ""),
                    "raw": record,
                }
            )
    return out


def _normalize_screenshots(records: list[Any], run_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, record in enumerate(records, start=1):
        if isinstance(record, str):
            path = _resolve_artifact_path(record, run_dir)
            out.append({"artifact_id": f"screenshot-{idx}", "path": path})
        elif isinstance(record, dict):
            path = _resolve_artifact_path(str(record.get("path") or ""), run_dir)
            if path:
                item = dict(record)
                item["path"] = path
                item.setdefault("artifact_id", f"screenshot-{idx}")
                out.append(item)
    return out


def _normalize_records(records: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, record in enumerate(records, start=1):
        if isinstance(record, dict):
            item = dict(record)
            item.setdefault("index", idx)
            out.append(item)
        else:
            out.append({"index": idx, "message": str(record)})
    return out


def _normalize_assertions(records: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            out.append(
                {
                    "assertion_id": f"harness-assertion-{idx}",
                    "assertion_type": "harness",
                    "ok": bool(record),
                    "expected": True,
                    "actual": record,
                    "message": "",
                    "source": "harness",
                    "confidence": 1.0,
                }
            )
            continue
        out.append(
            {
                "assertion_id": str(
                    record.get("assertion_id")
                    or record.get("id")
                    or f"harness-assertion-{idx}"
                ),
                "assertion_type": str(record.get("assertion_type") or "harness"),
                "ok": _assertion_ok(record),
                "expected": record.get("expected"),
                "actual": record.get("actual"),
                "message": str(record.get("message") or record.get("name") or ""),
                "source": "harness",
                "confidence": _safe_float(
                    record.get("confidence") if "confidence" in record else None,
                    1.0,
                ),
            }
        )
    return out


def _normalize_declared_artifacts(
    records: list[Any],
    run_dir: Path,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            continue
        path = _resolve_artifact_path(str(record.get("path") or ""), run_dir)
        if not path:
            continue
        out.append(
            {
                "artifact_id": str(
                    record.get("artifact_id") or f"harness-artifact-{idx}"
                ),
                "artifact_type": str(
                    record.get("artifact_type")
                    or record.get("type")
                    or "harness_artifact"
                ),
                "path": path,
                "label": str(record.get("label") or f"Harness artifact {idx}"),
                "metadata": dict(record.get("metadata") or {}),
            }
        )
    return out


def _discover_screenshots(
    run_dir: Path,
    existing: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    existing_paths = {str(item.get("path") or "") for item in existing}
    out: list[dict[str, Any]] = []
    for path in sorted(run_dir.rglob("*")):
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        path_text = str(path)
        if path_text in existing_paths:
            continue
        out.append(
            {
                "artifact_id": f"screenshot-{len(existing) + len(out) + 1}",
                "path": path_text,
                "label": path.name,
            }
        )
    return out


def _resolve_artifact_path(value: str, run_dir: Path) -> str:
    if not value:
        return ""
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str(run_dir / path)


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _payload_error(payload: dict[str, Any], status: str) -> str:
    explicit = _first_text(payload, ["error", "exception"])
    if explicit:
        return explicit
    if _failed_status(status):
        return _first_text(payload, ["message"])
    return ""


def _failed_status(status: object) -> bool:
    return str(status or "").strip().lower() in {
        "error",
        "errored",
        "fail",
        "failed",
        "failure",
    }


def _first_present(
    record: dict[str, Any],
    keys: list[str],
    default: Any,
) -> Any:
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return default


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "passed", "pass"}:
            return True
        if normalized in {"0", "false", "no", "failed", "fail", "error"}:
            return False
        return default
    if value is None:
        return default
    return bool(value)


def _step_ok(record: dict[str, Any]) -> bool:
    if "ok" in record:
        return _safe_bool(record.get("ok"), default=False)
    if "passed" in record:
        return _safe_bool(record.get("passed"), default=False)
    return not _failed_status(record.get("status"))


def _assertion_ok(record: dict[str, Any]) -> bool:
    if "ok" in record:
        return _safe_bool(record.get("ok"), default=False)
    if "passed" in record:
        return _safe_bool(record.get("passed"), default=False)
    return not _failed_status(record.get("status"))
