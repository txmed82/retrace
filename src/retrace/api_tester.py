"""First-party API contract test runner.

The third pillar of the unified-Incident pipeline: where `tester.py` runs
browser-driven UI specs, this module runs HTTP-level API specs against a
running service. Failures flow into the same `qa_incidents` table as
replay-derived and UI-test-derived incidents, so the existing
`retrace qa auto` killer-demo loop covers backend regressions for free.

Design intent:
  - Spec format mirrors `tester.py` shape (JSON files under `data/api-tests/specs/*`)
    so users get the same "saved spec, re-runnable, versioned" UX.
  - Assertions are declarative: status code, response time budget,
    response-body substring, response JSON path equality.
  - Runs are deterministic and side-effect-light: each run captures
    request/response (redacted) into a per-run directory matching the
    UI tester layout.
  - On failure, the runner calls `qa_incidents.incident_from_api_test`
    and upserts the result so `retrace qa list` sees it next to UI and
    replay incidents.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from retrace.qa_incidents import (
    incident_from_api_test,
    redact_sensitive_text,
)
from retrace.storage import Storage


API_SPEC_SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 30
ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
ALLOWED_ASSERTION_TYPES = {
    "status_equals",
    "status_in",
    "header_equals",
    "header_contains",
    "body_contains",
    "body_not_contains",
    "body_matches",            # regex match
    "json_path_equals",        # dotted path like a.b.c
    "json_path_in",
    "response_time_ms_under",
}


# ---------------------------------------------------------------------------
# Spec + Run shapes (deliberately mirror the UI tester so tooling can
# generalise later).
# ---------------------------------------------------------------------------


@dataclass
class APIAssertion:
    assertion_type: str
    value: Any = None
    target: str = ""           # e.g. "status", a header name, a JSON path
    description: str = ""


@dataclass
class APITestSpec:
    schema_version: int
    spec_id: str
    name: str
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    body: str = ""             # raw string; use json_body for JSON
    json_body: Optional[Any] = None
    auth_bearer_env: str = ""  # env var holding a bearer token
    auth_header_env: str = ""  # env var holding a full Authorization header value
    assertions: list[dict[str, Any]] = field(default_factory=list)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    project_id: str = "local"
    environment_id: str = "production"
    created_at: str = ""
    updated_at: str = ""


@dataclass
class APIAssertionResult:
    assertion_type: str
    target: str
    expected: Any
    actual: Any
    ok: bool
    message: str = ""


@dataclass
class APITestRunResult:
    run_id: str
    spec_id: str
    status: str                # "pass" | "fail" | "error"
    method: str
    url: str
    request_started_at: str
    duration_ms: int
    response_status: int = 0
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: str = ""    # redacted, truncated
    assertion_results: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    incident_id: str = ""      # populated when a failure created an incident
    run_dir: str = ""


# ---------------------------------------------------------------------------
# Filesystem layout helpers.
# ---------------------------------------------------------------------------


def api_specs_dir_for_data_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "api-tests" / "specs"


def api_runs_dir_for_data_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "api-tests" / "runs"


_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _spec_path(specs_dir: Path, spec_id: str) -> Path:
    if not spec_id or not _SAFE_ID_RE.match(spec_id):
        raise ValueError("Invalid spec_id: must match [a-zA-Z0-9_-]+")
    target = (specs_dir / f"{spec_id}.json").resolve()
    try:
        target.relative_to(specs_dir.resolve())
    except (ValueError, RuntimeError) as exc:
        raise ValueError("spec_id path traversal blocked") from exc
    return target


def _slugify(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return s[:40] or "api-test"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Spec lifecycle.
# ---------------------------------------------------------------------------


def _spec_from_data(data: dict[str, Any]) -> APITestSpec:
    allowed = {f.name for f in fields(APITestSpec)}
    payload = {k: v for k, v in data.items() if k in allowed}
    payload.setdefault("schema_version", API_SPEC_SCHEMA_VERSION)
    payload.setdefault("headers", {})
    payload.setdefault("query", {})
    payload.setdefault("body", "")
    payload.setdefault("json_body", None)
    payload.setdefault("auth_bearer_env", "")
    payload.setdefault("auth_header_env", "")
    payload.setdefault("assertions", [])
    payload.setdefault("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    payload.setdefault("project_id", "local")
    payload.setdefault("environment_id", "production")
    payload.setdefault("created_at", "")
    payload.setdefault("updated_at", "")
    return APITestSpec(**payload)


def validate_spec(spec: APITestSpec) -> None:
    if spec.schema_version != API_SPEC_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported api-test schema_version={spec.schema_version}. "
            f"Expected {API_SPEC_SCHEMA_VERSION}."
        )
    if not spec.spec_id.strip():
        raise ValueError("spec_id is required")
    if not spec.name.strip():
        raise ValueError("name is required")
    method_upper = (spec.method or "").upper().strip()
    if method_upper not in ALLOWED_METHODS:
        raise ValueError(f"method must be one of: {sorted(ALLOWED_METHODS)}")
    if not (spec.url or "").strip():
        raise ValueError("url is required")
    if not isinstance(spec.headers, dict):
        raise ValueError("headers must be an object")
    if not isinstance(spec.query, dict):
        raise ValueError("query must be an object")
    if not isinstance(spec.assertions, list):
        raise ValueError("assertions must be a list")
    if spec.body and spec.json_body is not None:
        raise ValueError("set either body or json_body, not both")
    if spec.timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")
    for i, a in enumerate(spec.assertions):
        if not isinstance(a, dict):
            raise ValueError(f"assertion #{i} must be an object")
        if a.get("assertion_type") not in ALLOWED_ASSERTION_TYPES:
            raise ValueError(
                f"assertion #{i} has unsupported type {a.get('assertion_type')!r}; "
                f"allowed: {sorted(ALLOWED_ASSERTION_TYPES)}"
            )


def save_spec(specs_dir: Path, spec: APITestSpec) -> Path:
    validate_spec(spec)
    specs_dir.mkdir(parents=True, exist_ok=True)
    p = _spec_path(specs_dir, spec.spec_id)
    p.write_text(json.dumps(asdict(spec), indent=2) + "\n")
    return p


def load_spec(specs_dir: Path, spec_id: str) -> APITestSpec:
    p = _spec_path(specs_dir, spec_id)
    return _spec_from_data(json.loads(p.read_text()))


def list_specs(specs_dir: Path) -> list[APITestSpec]:
    if not specs_dir.exists():
        return []
    out: list[APITestSpec] = []
    for p in sorted(specs_dir.glob("*.json"), key=lambda x: x.stat().st_mtime):
        try:
            out.append(_spec_from_data(json.loads(p.read_text())))
        except Exception:
            continue
    return out


def create_spec(
    *,
    specs_dir: Path,
    name: str,
    method: str,
    url: str,
    headers: Optional[dict[str, str]] = None,
    query: Optional[dict[str, str]] = None,
    body: str = "",
    json_body: Any = None,
    auth_bearer_env: str = "",
    auth_header_env: str = "",
    assertions: Optional[list[dict[str, Any]]] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    project_id: str = "local",
    environment_id: str = "production",
) -> APITestSpec:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    spec_id = f"{ts}-{_slugify(name)}-{uuid.uuid4().hex[:8]}"
    created_at = _now_iso()
    spec = APITestSpec(
        schema_version=API_SPEC_SCHEMA_VERSION,
        spec_id=spec_id,
        name=name.strip() or "API test",
        method=method.upper().strip() or "GET",
        url=url.strip(),
        headers=dict(headers or {}),
        query=dict(query or {}),
        body=body or "",
        json_body=json_body,
        auth_bearer_env=(auth_bearer_env or "").strip(),
        auth_header_env=(auth_header_env or "").strip(),
        assertions=list(assertions or []),
        timeout_seconds=float(timeout_seconds),
        project_id=project_id,
        environment_id=environment_id,
        created_at=created_at,
        updated_at=created_at,
    )
    save_spec(specs_dir, spec)
    return spec


# ---------------------------------------------------------------------------
# Assertion engine.
# ---------------------------------------------------------------------------


def _get_json_path(obj: Any, path: str) -> Any:
    """Tiny dotted-path lookup; supports list indices like `users.0.email`."""
    cur = obj
    if not path:
        return cur
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _evaluate_assertion(
    assertion: dict[str, Any],
    *,
    response_status: int,
    response_headers: dict[str, str],
    response_text: str,
    response_json: Any,
    duration_ms: int,
) -> APIAssertionResult:
    a_type = str(assertion.get("assertion_type", ""))
    target = str(assertion.get("target", ""))
    expected = assertion.get("value")
    desc = str(assertion.get("description", "") or "")

    def _result(ok: bool, actual: Any, message: str = "") -> APIAssertionResult:
        return APIAssertionResult(
            assertion_type=a_type,
            target=target,
            expected=expected,
            actual=actual,
            ok=ok,
            message=message or desc,
        )

    try:
        if a_type == "status_equals":
            ok = response_status == int(expected)
            return _result(ok, response_status, "" if ok else f"status {response_status} != {expected}")
        if a_type == "status_in":
            allowed = [int(x) for x in (expected or [])]
            ok = response_status in allowed
            return _result(ok, response_status, "" if ok else f"status {response_status} not in {allowed}")
        if a_type == "header_equals":
            actual_value = response_headers.get(target.lower(), "")
            ok = actual_value == str(expected)
            return _result(ok, actual_value, "" if ok else f"header `{target}` = {actual_value!r}, expected {expected!r}")
        if a_type == "header_contains":
            actual_value = response_headers.get(target.lower(), "")
            ok = str(expected) in actual_value
            return _result(ok, actual_value, "" if ok else f"header `{target}` did not contain {expected!r}")
        if a_type == "body_contains":
            ok = str(expected) in response_text
            return _result(ok, len(response_text), "" if ok else f"body did not contain {expected!r}")
        if a_type == "body_not_contains":
            ok = str(expected) not in response_text
            return _result(ok, len(response_text), "" if ok else f"body unexpectedly contained {expected!r}")
        if a_type == "body_matches":
            pattern = str(expected)
            ok = re.search(pattern, response_text) is not None
            return _result(ok, None, "" if ok else f"body did not match /{pattern}/")
        if a_type == "json_path_equals":
            actual_value = _get_json_path(response_json, target)
            ok = actual_value == expected
            return _result(ok, actual_value, "" if ok else f"`{target}` = {actual_value!r}, expected {expected!r}")
        if a_type == "json_path_in":
            allowed = list(expected or [])
            actual_value = _get_json_path(response_json, target)
            ok = actual_value in allowed
            return _result(ok, actual_value, "" if ok else f"`{target}` = {actual_value!r}, not in {allowed}")
        if a_type == "response_time_ms_under":
            ok = duration_ms < int(expected)
            return _result(ok, duration_ms, "" if ok else f"response took {duration_ms}ms (budget {expected}ms)")
    except Exception as exc:
        return _result(False, None, f"assertion evaluator errored: {exc}")
    return _result(False, None, f"unknown assertion_type {a_type!r}")


# ---------------------------------------------------------------------------
# Run a single spec.
# ---------------------------------------------------------------------------


def _build_request_kwargs(spec: APITestSpec) -> dict[str, Any]:
    headers = dict(spec.headers or {})
    if spec.auth_bearer_env:
        token = os.environ.get(spec.auth_bearer_env, "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    if spec.auth_header_env:
        full = os.environ.get(spec.auth_header_env, "").strip()
        if full:
            headers["Authorization"] = full
    kwargs: dict[str, Any] = {
        "method": spec.method.upper(),
        "url": spec.url,
        "headers": headers,
        "timeout": float(spec.timeout_seconds),
    }
    if spec.query:
        kwargs["params"] = dict(spec.query)
    if spec.json_body is not None:
        kwargs["json"] = spec.json_body
    elif spec.body:
        kwargs["content"] = spec.body
    return kwargs


def run_spec(
    *,
    spec: APITestSpec,
    runs_dir: Path,
    client: Optional[httpx.Client] = None,
) -> APITestRunResult:
    """Execute the spec, evaluate assertions, write run artifacts."""
    validate_spec(spec)
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    run_dir = runs_dir / f"{run_id}-{_slugify(spec.name)}"
    run_dir.mkdir(parents=True, exist_ok=False)

    req_kwargs = _build_request_kwargs(spec)
    started_at_iso = _now_iso()
    t0 = time.monotonic()

    owns_client = client is None
    if client is None:
        client = httpx.Client(follow_redirects=True)
    try:
        try:
            response = client.request(**req_kwargs)
            duration_ms = int((time.monotonic() - t0) * 1000)
            response_status = response.status_code
            response_headers = {k.lower(): v for k, v in response.headers.items()}
            response_text_raw = response.text
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            # Network/transport error — treat as a run error, not a pass/fail.
            result = APITestRunResult(
                run_id=run_id,
                spec_id=spec.spec_id,
                status="error",
                method=spec.method.upper(),
                url=spec.url,
                request_started_at=started_at_iso,
                duration_ms=duration_ms,
                error=str(exc),
                run_dir=str(run_dir),
            )
            _persist_run_artifacts(run_dir, spec=spec, result=result, response_text="", response_headers={})
            return result
    finally:
        if owns_client:
            client.close()

    # Parse JSON best-effort; non-JSON bodies are fine, just leave it None.
    response_json: Any = None
    try:
        response_json = json.loads(response_text_raw)
    except Exception:
        response_json = None

    # Run assertions. Empty assertions list → implicit "2xx" check so the
    # spec is still meaningful.
    assertion_results: list[APIAssertionResult] = []
    if not spec.assertions:
        ok = 200 <= response_status < 300
        assertion_results.append(
            APIAssertionResult(
                assertion_type="status_in",
                target="status",
                expected=[200, 201, 202, 204],
                actual=response_status,
                ok=ok,
                message="" if ok else f"default 2xx check failed (status={response_status})",
            )
        )
    else:
        for a in spec.assertions:
            assertion_results.append(
                _evaluate_assertion(
                    a,
                    response_status=response_status,
                    response_headers=response_headers,
                    response_text=response_text_raw,
                    response_json=response_json,
                    duration_ms=duration_ms,
                )
            )

    failed = [a for a in assertion_results if not a.ok]
    status_str = "pass" if not failed else "fail"

    result = APITestRunResult(
        run_id=run_id,
        spec_id=spec.spec_id,
        status=status_str,
        method=spec.method.upper(),
        url=spec.url,
        request_started_at=started_at_iso,
        duration_ms=duration_ms,
        response_status=response_status,
        response_headers=response_headers,
        response_body=redact_sensitive_text(response_text_raw, max_len=4096),
        assertion_results=[asdict(a) for a in assertion_results],
        run_dir=str(run_dir),
    )
    _persist_run_artifacts(
        run_dir,
        spec=spec,
        result=result,
        response_text=response_text_raw,
        response_headers=response_headers,
    )
    return result


def _persist_run_artifacts(
    run_dir: Path,
    *,
    spec: APITestSpec,
    result: APITestRunResult,
    response_text: str,
    response_headers: dict[str, str],
) -> None:
    """Mirror the UI tester layout: one run dir per execution."""
    try:
        (run_dir / "spec.json").write_text(json.dumps(asdict(spec), indent=2))
        (run_dir / "run.json").write_text(json.dumps(asdict(result), indent=2))
        # Response body lands as its own file so it's grep-friendly.
        body_redacted = redact_sensitive_text(response_text or "", max_len=64 * 1024)
        (run_dir / "response.txt").write_text(body_redacted)
        (run_dir / "headers.json").write_text(
            json.dumps(response_headers, indent=2, sort_keys=True)
        )
    except Exception:
        # Artifact persistence must never break a run.
        pass


# ---------------------------------------------------------------------------
# Failure -> incident bridge.
# ---------------------------------------------------------------------------


def run_spec_and_record(
    *,
    spec: APITestSpec,
    runs_dir: Path,
    store: Storage,
    suspected_cause: str = "",
) -> APITestRunResult:
    """Run a spec and, on failure, upsert a qa_incident for it.

    This is what `retrace api-test run` (and any future scheduler) calls.
    The returned `APITestRunResult` carries `incident_id` populated when a
    failure produced an incident, so callers can chain into `retrace qa
    reproduce` or `qa auto` without re-querying.
    """
    result = run_spec(spec=spec, runs_dir=runs_dir)
    if result.status == "pass":
        return result

    failed = [
        a for a in result.assertion_results
        if isinstance(a, dict) and not a.get("ok", True)
    ]
    if failed:
        first = failed[0]
        title = f"API test {spec.method} {spec.url} failed: {first.get('assertion_type', '')}"
        summary = str(first.get("message", "") or "")
    else:
        title = f"API test {spec.method} {spec.url} errored"
        summary = result.error or "request failed before assertions ran"

    # Pull the most informative numbers for the incident shape.
    expected_status = 200
    actual_status = result.response_status
    for a in spec.assertions:
        if a.get("assertion_type") == "status_equals":
            try:
                expected_status = int(a.get("value"))
                break
            except (TypeError, ValueError):
                continue

    incident = incident_from_api_test(
        project_id=spec.project_id,
        environment_id=spec.environment_id,
        title=title[:240],
        summary=summary[:1024],
        method=spec.method,
        url=spec.url,
        expected_status=expected_status,
        actual_status=actual_status,
        request_body=spec.body or (json.dumps(spec.json_body) if spec.json_body is not None else ""),
        response_body=result.response_body,
        suspected_cause=suspected_cause,
        run_id=result.run_id,
    )
    incident_id, _inserted = store.upsert_qa_incident(incident.to_row())
    result.incident_id = incident.public_id
    # Patch the on-disk run.json with the incident id so the artifact tells
    # the full story.
    try:
        (Path(result.run_dir) / "run.json").write_text(json.dumps(asdict(result), indent=2))
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Listing runs (used by the CLI).
# ---------------------------------------------------------------------------


def load_run_summaries(runs_dir: Path, *, limit: int = 50) -> list[dict[str, Any]]:
    if not runs_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(runs_dir.glob("*/run.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        out.append({
            "run_id": str(data.get("run_id", "")),
            "spec_id": str(data.get("spec_id", "")),
            "status": str(data.get("status", "")),
            "method": str(data.get("method", "")),
            "url": str(data.get("url", "")),
            "duration_ms": int(data.get("duration_ms", 0) or 0),
            "response_status": int(data.get("response_status", 0) or 0),
            "incident_id": str(data.get("incident_id", "") or ""),
            "run_dir": str(p.parent),
        })
        if len(out) >= limit:
            break
    return out
