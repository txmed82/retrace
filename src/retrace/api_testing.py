from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from retrace.evidence import EvidenceItem, evidence_dedupe_key
from retrace.failures import canonical_failure_from_api_run
from retrace.matching import CodeCandidate, score_repo_for_finding
from retrace.script_steps import run_script_step
from retrace.storage import Storage


API_SPEC_SCHEMA_VERSION = 1
SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-api-key",
    "x-csrf-token",
    "x-xsrf-token",
}
SENSITIVE_BODY_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "jwt",
    "password",
    "secret",
    "token",
}


@dataclass
class APIAssertionResult:
    assertion_id: str
    assertion_type: str
    ok: bool
    expected: Any
    actual: Any
    message: str


@dataclass
class APITestArtifact:
    artifact_id: str
    artifact_type: str
    path: str
    label: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class APITestSpec:
    schema_version: int
    spec_id: str
    name: str
    method: str
    url: str
    query: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    headers_env: str = ""
    body: Any = None
    auth: dict[str, Any] = field(default_factory=dict)
    auth_profile: str = ""
    env_profile: str = ""
    env_overrides: dict[str, str] = field(default_factory=dict)
    expected_status: int = 200
    json_assertions: list[dict[str, Any]] = field(default_factory=list)
    schema_assertions: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: int = 0
    timeout_seconds: float = 15.0
    setup_steps: list[dict[str, Any]] = field(default_factory=list)
    teardown_steps: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    fixtures: dict[str, Any] = field(default_factory=dict)


@dataclass
class APITestRunResult:
    run_id: str
    spec_id: str
    ok: bool
    status: str
    status_code: int
    elapsed_ms: int
    run_dir: str
    failure_classification: str = ""
    error: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    assertion_results: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class APIFailurePersistenceResult:
    failure_id: str
    repair_task_id: str
    evidence_ids: list[str]
    likely_files: list[str]
    prompt_path: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return s or "api-test"


def api_specs_dir_for_data_dir(data_dir: Path) -> Path:
    return data_dir / "api-tests" / "specs"


def api_runs_dir_for_data_dir(data_dir: Path) -> Path:
    return data_dir / "api-tests" / "runs"


def _spec_path(specs_dir: Path, spec_id: str) -> Path:
    if not spec_id or not re.match(r"^[a-zA-Z0-9_-]+$", spec_id):
        raise ValueError("Invalid API spec_id")
    candidate = (specs_dir / f"{spec_id}.json").resolve()
    candidate.relative_to(specs_dir.resolve())
    return candidate


def _coerce_spec(data: dict[str, Any]) -> APITestSpec:
    data = dict(data)
    data.setdefault("schema_version", API_SPEC_SCHEMA_VERSION)
    data.setdefault("query", {})
    data.setdefault("headers", {})
    data.setdefault("headers_env", "")
    data.setdefault("auth", {})
    data.setdefault("auth_profile", "")
    data.setdefault("env_profile", "")
    data.setdefault("env_overrides", {})
    data.setdefault("expected_status", 200)
    data.setdefault("json_assertions", [])
    data.setdefault("schema_assertions", [])
    data.setdefault("latency_ms", 0)
    data.setdefault("timeout_seconds", 15.0)
    data.setdefault("setup_steps", [])
    data.setdefault("teardown_steps", [])
    data.setdefault("steps", [])
    data.setdefault("fixtures", {})
    return APITestSpec(**data)


def validate_api_spec(spec: APITestSpec) -> None:
    if spec.schema_version != API_SPEC_SCHEMA_VERSION:
        raise ValueError("unsupported API spec schema_version")
    if not spec.spec_id.strip():
        raise ValueError("spec_id is required")
    if not spec.name.strip():
        raise ValueError("name is required")
    if spec.method.upper() not in {
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "HEAD",
        "OPTIONS",
    }:
        raise ValueError("method is not supported")
    if not spec.url.strip():
        raise ValueError("url is required")
    if not isinstance(spec.query, dict):
        raise ValueError("query must be an object")
    if not isinstance(spec.headers, dict):
        raise ValueError("headers must be an object")
    leaked_headers = sorted(
        key for key in spec.headers if str(key).lower() in SENSITIVE_HEADER_NAMES
    )
    if leaked_headers:
        raise ValueError(
            "sensitive static headers must use auth env vars: "
            + ", ".join(leaked_headers)
        )
    if not isinstance(spec.auth, dict):
        raise ValueError("auth must be an object")
    if not isinstance(spec.env_overrides, dict):
        raise ValueError("env_overrides must be an object")
    if float(spec.timeout_seconds) <= 0:
        raise ValueError("timeout_seconds must be greater than 0")
    for field_name in (
        "json_assertions",
        "schema_assertions",
        "setup_steps",
        "teardown_steps",
        "steps",
    ):
        if not isinstance(getattr(spec, field_name), list):
            raise ValueError(f"{field_name} must be a list")
    for idx, step in enumerate(spec.steps):
        if not isinstance(step, dict):
            raise ValueError("steps must be objects")
        method = str(step.get("method") or spec.method).upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            raise ValueError(f"steps[{idx}].method is not supported")
        headers = step.get("headers", {})
        if headers is not None and not isinstance(headers, dict):
            raise ValueError(f"steps[{idx}].headers must be an object")
        leaked_step_headers = sorted(
            key for key in dict(headers or {}) if str(key).lower() in SENSITIVE_HEADER_NAMES
        )
        if leaked_step_headers:
            raise ValueError(
                "sensitive static headers must use auth env vars: "
                + ", ".join(leaked_step_headers)
            )


def create_api_spec(
    *,
    specs_dir: Path,
    name: str,
    method: str,
    url: str,
    query: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    headers_env: str = "",
    body: Any = None,
    auth: Optional[dict[str, Any]] = None,
    auth_profile: str = "",
    env_profile: str = "",
    env_overrides: Optional[dict[str, str]] = None,
    expected_status: int = 200,
    json_assertions: Optional[list[dict[str, Any]]] = None,
    schema_assertions: Optional[list[dict[str, Any]]] = None,
    latency_ms: int = 0,
    timeout_seconds: float = 15.0,
    setup_steps: Optional[list[dict[str, Any]]] = None,
    teardown_steps: Optional[list[dict[str, Any]]] = None,
    steps: Optional[list[dict[str, Any]]] = None,
    fixtures: Optional[dict[str, Any]] = None,
) -> APITestSpec:
    created_at = now_iso()
    spec = APITestSpec(
        schema_version=API_SPEC_SCHEMA_VERSION,
        spec_id=f"api_{slugify(name)}_{uuid.uuid4().hex[:8]}",
        name=name.strip(),
        method=method.upper().strip(),
        url=url.strip(),
        query=dict(query or {}),
        headers={str(k): str(v) for k, v in dict(headers or {}).items()},
        headers_env=headers_env.strip(),
        body=body,
        auth=dict(auth or {}),
        auth_profile=auth_profile.strip(),
        env_profile=env_profile.strip(),
        env_overrides={str(k): str(v) for k, v in dict(env_overrides or {}).items()},
        expected_status=int(expected_status),
        json_assertions=list(json_assertions or []),
        schema_assertions=list(schema_assertions or []),
        latency_ms=max(0, int(latency_ms or 0)),
        timeout_seconds=float(timeout_seconds),
        setup_steps=list(setup_steps or []),
        teardown_steps=list(teardown_steps or []),
        steps=list(steps or []),
        created_at=created_at,
        updated_at=created_at,
        fixtures=dict(fixtures or {}),
    )
    validate_api_spec(spec)
    save_api_spec(specs_dir, spec)
    return spec


def save_api_spec(specs_dir: Path, spec: APITestSpec) -> None:
    validate_api_spec(spec)
    specs_dir.mkdir(parents=True, exist_ok=True)
    _spec_path(specs_dir, spec.spec_id).write_text(
        json.dumps(asdict(spec), indent=2) + "\n"
    )


def load_api_spec(specs_dir: Path, spec_id: str) -> APITestSpec:
    path = _spec_path(specs_dir, spec_id)
    if not path.exists():
        raise FileNotFoundError(spec_id)
    return _coerce_spec(json.loads(path.read_text()))


def list_api_specs(specs_dir: Path) -> list[APITestSpec]:
    if not specs_dir.exists():
        return []
    specs: list[APITestSpec] = []
    for path in sorted(specs_dir.glob("*.json")):
        try:
            specs.append(load_api_spec(specs_dir, path.stem))
        except Exception:
            continue
    return specs


def run_api_spec(*, spec: APITestSpec, runs_dir: Path) -> APITestRunResult:
    validate_api_spec(spec)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    run_dir = runs_dir / f"{run_id}-{slugify(spec.name)[:40]}"
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=False)
    artifacts: list[APITestArtifact] = []
    assertion_results: list[APIAssertionResult] = []
    status_code = 0
    elapsed_ms = 0
    error = ""
    effective_env = dict(os.environ)
    effective_env.update({str(k): str(v) for k, v in spec.env_overrides.items()})
    scope: dict[str, Any] = {"vars": {}, "env": effective_env, "steps": []}
    executed_requests: list[dict[str, Any]] = []

    try:
        for idx, step in enumerate(spec.setup_steps):
            _record_script_step(
                artifacts=artifacts,
                assertion_results=assertion_results,
                artifacts_dir=artifacts_dir,
                scope=scope,
                step=step,
                phase="setup",
                idx=idx,
            )
        for idx, step in enumerate(_request_steps(spec)):
            step_id = str(step.get("id") or f"request-{idx + 1}")
            method = str(step.get("method") or spec.method).upper()
            url = _render_text(str(step.get("url") or spec.url), scope)
            status_code = 0
            elapsed_ms = 0
            query = _render_mapping(step.get("query", spec.query), scope)
            headers = _resolve_headers(
                spec,
                step_headers_env=step.get("headers_env"),
                step_auth=step.get("auth"),
                env=effective_env,
            )
            headers.update(
                {
                    str(k): _render_text(str(v), scope)
                    for k, v in dict(step.get("headers") or {}).items()
                }
            )
            request_body = _render_body(step.get("body", spec.body), scope)
            request_payload = {
                "step_id": step_id,
                "method": method,
                "url": url,
                "query": query,
                "headers": _redact_headers(headers),
                "body": _redact_json(request_body),
            }
            request_trace_ids = _trace_ids_from_blob({"headers": headers})
            request_path = artifacts_dir / f"{step_id}-request.json"
            request_path.write_text(json.dumps(request_payload, indent=2) + "\n")
            artifacts.append(
                APITestArtifact(
                    artifact_id=f"{step_id}-request",
                    artifact_type="api_request",
                    path=str(request_path),
                    label=f"API request: {step_id}",
                    metadata={"step_id": step_id, "method": method, "url": url},
                )
            )
            started = time.perf_counter()
            with httpx.Client(timeout=float(spec.timeout_seconds), follow_redirects=True) as client:
                response = client.request(
                    method,
                    url,
                    params=query,
                    headers=headers,
                    json=request_body if isinstance(request_body, (dict, list)) else None,
                    content=request_body if isinstance(request_body, (str, bytes)) else None,
                )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            status_code = response.status_code
            response_json, response_body = _response_body(response)
            scope["response"] = {
                "status_code": status_code,
                "headers": dict(response.headers),
                "json": response_json,
                "body": response_body,
                "elapsed_ms": elapsed_ms,
            }
            step_summary = {
                "id": step_id,
                "status_code": status_code,
                "json": response_json,
                "body": response_body,
                "elapsed_ms": elapsed_ms,
            }
            scope["steps"].append(step_summary)
            _apply_extractors(step.get("extract", []), scope)
            response_payload = {
                "step_id": step_id,
                "status_code": status_code,
                "elapsed_ms": elapsed_ms,
                "headers": _redact_headers(dict(response.headers)),
                "body": _redact_json(response_json if response_json is not None else response_body),
            }
            trace_ids = _unique_strings(
                [*request_trace_ids, *_trace_ids_from_blob({"headers": dict(response.headers)})]
            )
            executed_requests.append(
                {
                    "step_id": step_id,
                    "method": method,
                    "url": url,
                    "route_path": _url_path(url),
                    "status_code": status_code,
                    "elapsed_ms": elapsed_ms,
                    "trace_ids": trace_ids,
                }
            )
            response_path = artifacts_dir / f"{step_id}-response.json"
            response_path.write_text(json.dumps(response_payload, indent=2) + "\n")
            artifacts.append(
                APITestArtifact(
                    artifact_id=f"{step_id}-response",
                    artifact_type="api_response",
                    path=str(response_path),
                    label=f"API response: {step_id}",
                    metadata={"step_id": step_id, "status_code": status_code, "elapsed_ms": elapsed_ms},
                )
            )
            assertion_results.extend(
                _evaluate_api_assertions(
                    _step_spec(spec, step, method=method, url=url, query=query),
                    response_json,
                    elapsed_ms,
                    status_code,
                    assertion_prefix=step_id if spec.steps else "",
                )
            )
    except Exception as exc:
        error = str(exc)
    finally:
        for idx, step in enumerate(spec.teardown_steps):
            _record_script_step(
                artifacts=artifacts,
                assertion_results=assertion_results,
                artifacts_dir=artifacts_dir,
                scope=scope,
                step=step,
                phase="teardown",
                idx=idx,
            )

    assertions_payload = [asdict(item) for item in assertion_results]
    assertions_path = artifacts_dir / "assertions.json"
    assertions_path.write_text(json.dumps(assertions_payload, indent=2) + "\n")
    artifacts.append(
        APITestArtifact(
            artifact_id="assertions",
            artifact_type="api_assertion_results",
            path=str(assertions_path),
            label="API assertion results",
            metadata={"count": len(assertion_results)},
        )
    )
    ok = not error and all(item.ok for item in assertion_results)
    classification = _classify_api_run(
        ok=ok,
        status_code=status_code,
        error=error,
        assertion_results=assertions_payload,
    )
    run_summary = _api_run_summary(
        spec=spec,
        run_id=run_id,
        ok=ok,
        status_code=status_code,
        elapsed_ms=elapsed_ms,
        error=error,
        failure_classification=classification,
        assertion_results=assertions_payload,
        executed_requests=executed_requests,
    )
    summary_path = artifacts_dir / "run-summary.json"
    summary_path.write_text(json.dumps(run_summary, indent=2) + "\n")
    artifacts.append(
        APITestArtifact(
            artifact_id="run-summary",
            artifact_type="api_run_summary",
            path=str(summary_path),
            label="API run summary",
            metadata={
                "failure_classification": classification,
                "request_count": len(executed_requests),
                "route_path": _url_path(spec.url),
            },
        )
    )
    result = APITestRunResult(
        run_id=run_id,
        spec_id=spec.spec_id,
        ok=ok,
        status="passed" if ok else "failed",
        status_code=status_code,
        elapsed_ms=elapsed_ms,
        run_dir=str(run_dir),
        failure_classification=classification,
        error=error,
        artifacts=[asdict(item) for item in artifacts],
        assertion_results=assertions_payload,
    )
    (run_dir / "run.json").write_text(json.dumps(asdict(result), indent=2) + "\n")
    return result


def _classify_api_run(
    *,
    ok: bool,
    status_code: int,
    error: str,
    assertion_results: list[dict[str, Any]],
) -> str:
    if ok:
        return "passed"
    failed_assertions = [
        item for item in assertion_results if not bool(item.get("ok"))
    ]
    status_assertion_failed = any(
        str(item.get("assertion_type") or "") == "status_code"
        for item in failed_assertions
    )
    error_l = error.lower()
    if "timeout" in error_l or "timed out" in error_l:
        return "timeout"
    if error and status_code == 0:
        return "network_error"
    if status_assertion_failed:
        if "auth failure" in error_l or status_code in {401, 403}:
            return "auth_failure"
        if status_code >= 500:
            return "server_error"
        if status_code >= 400:
            return "client_error"
    if failed_assertions:
        return "assertion_failure"
    if "auth failure" in error_l or status_code in {401, 403}:
        return "auth_failure"
    if status_code >= 500:
        return "server_error"
    if status_code >= 400:
        return "client_error"
    return "unknown"


def _api_run_summary(
    *,
    spec: APITestSpec,
    run_id: str,
    ok: bool,
    status_code: int,
    elapsed_ms: int,
    error: str,
    failure_classification: str,
    assertion_results: list[dict[str, Any]],
    executed_requests: list[dict[str, Any]],
) -> dict[str, Any]:
    api_regression = (
        spec.fixtures.get("api_regression")
        if isinstance(spec.fixtures.get("api_regression"), dict)
        else {}
    )
    failed_assertions = [
        item for item in assertion_results if not bool(item.get("ok"))
    ]
    return scrub_pii_from_blob(
        {
            "spec_id": spec.spec_id,
            "run_id": run_id,
            "ok": ok,
            "status": "passed" if ok else "failed",
            "failure_classification": failure_classification,
            "method": spec.method,
            "url": spec.url,
            "route_path": _url_path(spec.url),
            "auth_profile": spec.auth_profile,
            "env_profile": spec.env_profile,
            "status_code": status_code,
            "elapsed_ms": elapsed_ms,
            "error": error,
            "request_count": len(executed_requests),
            "requests": executed_requests,
            "assertion_count": len(assertion_results),
            "failed_assertions": failed_assertions,
            "trace_ids": _unique_strings(
                [
                    *list(api_regression.get("trace_ids", []) or []),
                    *[
                        trace_id
                        for request in executed_requests
                        for trace_id in list(request.get("trace_ids", []) or [])
                    ],
                ]
            ),
            "source_issue_public_id": spec.fixtures.get("issue_public_id", ""),
        }
    )


def persist_api_failure(
    *,
    store: Storage,
    spec: APITestSpec,
    result: APITestRunResult,
    project_id: str,
    environment_id: str,
    repo_path: Optional[Path] = None,
) -> APIFailurePersistenceResult:
    if result.ok:
        raise ValueError("only failed API runs can be persisted as failures")
    evidence_text = _api_failure_evidence_text(spec, result)
    candidates: list[CodeCandidate] = []
    if repo_path is not None and repo_path.exists():
        candidates = score_repo_for_finding(
            repo_path=repo_path,
            title=f"API failure {spec.method} {_url_path(spec.url)}",
            category="api_failure",
            evidence_text=evidence_text,
            top_n=8,
        )
    prompt_path = _write_api_repair_prompt(
        spec=spec,
        result=result,
        candidates=candidates,
        evidence_text=evidence_text,
    )
    failure = canonical_failure_from_api_run(
        project_id=project_id,
        environment_id=environment_id,
        spec=spec,
        run_result=result,
    )
    persisted_failure_id, evidence_ids, repair_task_id = (
        store.upsert_failure_with_evidence_and_repair_task(
            failure=failure,
            evidence_items=_api_failure_evidence_items(result=result),
            repair_task={
                "title": f"Repair API failure: {spec.name or spec.spec_id}",
                "source_type": "test_run",
                "source_external_id": failure.source_external_id,
                "status": "open",
                "likely_files": [candidate.file_path for candidate in candidates],
                "prompt_artifacts": [
                    {
                        "artifact_type": "repair_prompt",
                        "path": prompt_path,
                        "label": "API repair prompt",
                        "metadata": {
                            "agent_target": "codex",
                            "spec_id": spec.spec_id,
                            "run_id": result.run_id,
                        },
                    }
                ],
                "validation_commands": [f"retrace tester api-run {spec.spec_id}"],
                "risk_notes": (
                    "Reproduce the failing API request locally before applying fixes."
                ),
                "metadata": {
                    "run_id": result.run_id,
                    "spec_id": spec.spec_id,
                    "method": spec.method,
                    "url": spec.url,
                    "route_path": _url_path(spec.url),
                    "expected_status": spec.expected_status,
                    "status_code": result.status_code,
                    "failure_classification": result.failure_classification,
                    "repo_path": str(repo_path) if repo_path else "",
                    "candidate_rationale": [
                        {
                            "file_path": candidate.file_path,
                            "score": candidate.score,
                            "rationale": candidate.rationale,
                        }
                        for candidate in candidates
                    ],
                },
            },
        )
    )
    store.upsert_failure_test_link(
        failure_id=persisted_failure_id,
        spec_id=spec.spec_id,
        spec_name=spec.name,
        source="api_test_run",
    )
    links = store.list_failure_test_links(
        failure_id=persisted_failure_id,
        spec_id=spec.spec_id,
        limit=1,
    )
    if links:
        store.update_failure_test_link_run(
            spec_id=spec.spec_id,
            run_result=result,
            link_id=links[0].id,
        )
    return APIFailurePersistenceResult(
        failure_id=persisted_failure_id,
        repair_task_id=repair_task_id,
        evidence_ids=evidence_ids,
        likely_files=[candidate.file_path for candidate in candidates],
        prompt_path=prompt_path,
    )


def _api_failure_evidence_items(*, result: APITestRunResult) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    source = f"api_run:{result.run_id}"
    for artifact in result.artifacts:
        artifact_type = str(artifact.get("artifact_type") or "")
        evidence_type = {
            "api_request": "api_request",
            "api_response": "api_response",
            "api_assertion_results": "test_transcript",
            "api_run_summary": "test_transcript",
        }.get(artifact_type)
        if not evidence_type:
            continue
        artifact_path = str(artifact.get("path") or "")
        payload = {
            "run_id": result.run_id,
            "spec_id": result.spec_id,
            "artifact_id": str(artifact.get("artifact_id") or ""),
            "artifact_type": artifact_type,
            "label": str(artifact.get("label") or ""),
            "metadata": scrub_pii_from_blob(dict(artifact.get("metadata") or {})),
            "artifact": scrub_pii_from_blob(_artifact_payload(artifact_path)),
        }
        items.append(
            EvidenceItem(
                failure_id="",
                evidence_type=evidence_type,
                occurred_at_ms=0,
                source=source,
                redaction_state="redacted",
                payload=payload,
                artifact_path=artifact_path,
                dedupe_key=evidence_dedupe_key(
                    failure_id="",
                    evidence_type=evidence_type,
                    source=source,
                    occurred_at_ms=0,
                    payload=payload,
                ),
            )
        )
    return items


def _artifact_payload(path_value: str) -> Any:
    if not path_value:
        return {}
    try:
        path = Path(path_value)
        if not path.exists() or path.stat().st_size > 256_000:
            return {}
        return json.loads(path.read_text())
    except Exception:
        return {}


def _api_failure_evidence_text(spec: APITestSpec, result: APITestRunResult) -> str:
    request = _artifact_by_type(result, "api_request")
    response = _artifact_by_type(result, "api_response")
    summary = _artifact_by_type(result, "api_run_summary")
    payload = {
        "api_reproduction": {
            "spec_id": spec.spec_id,
            "run_id": result.run_id,
            "method": spec.method,
            "url": spec.url,
            "route_path": _url_path(spec.url),
            "failure_classification": result.failure_classification,
            "query": spec.query,
            "expected_status": spec.expected_status,
            "actual_status": result.status_code,
            "body": _redact_json(spec.body),
            "command": f"retrace tester api-run {spec.spec_id}",
        },
        "run_summary": scrub_pii_from_blob(summary),
        "request": scrub_pii_from_blob(request),
        "response": scrub_pii_from_blob(response),
        "assertion_results": scrub_pii_from_blob(result.assertion_results),
        "error": result.error,
    }
    return json.dumps(scrub_pii_from_blob(payload), indent=2, sort_keys=True)


def scrub_pii_from_blob(blob: Any) -> Any:
    pii_keys = {
        "address",
        "email",
        "first_name",
        "fullname",
        "last_name",
        "name",
        "phone",
        "postal_code",
        "ssn",
        "street",
        "zip",
    }
    if isinstance(blob, dict):
        return {
            str(key): "[redacted]"
            if str(key).lower() in pii_keys
            else scrub_pii_from_blob(value)
            for key, value in blob.items()
        }
    if isinstance(blob, list):
        return [scrub_pii_from_blob(item) for item in blob]
    if isinstance(blob, str):
        return _scrub_pii_text(blob)
    return blob


def _scrub_pii_text(value: str) -> str:
    value = re.sub(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "[redacted-email]",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b",
        "[redacted-phone]",
        value,
    )
    value = re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "[redacted-ssn]", value)
    value = re.sub(
        r"\b\d{1,6}\s+[A-Za-z0-9.'-]+(?:\s+[A-Za-z0-9.'-]+){0,4}\s+"
        r"(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane)\b",
        "[redacted-address]",
        value,
        flags=re.IGNORECASE,
    )
    return value


def _write_api_repair_prompt(
    *,
    spec: APITestSpec,
    result: APITestRunResult,
    candidates: list[CodeCandidate],
    evidence_text: str,
) -> str:
    artifacts_dir = Path(result.run_dir) / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = artifacts_dir / "api-repair-prompt.md"
    candidate_lines = "\n".join(
        f"- `{candidate.file_path}` score={candidate.score}: {candidate.rationale}"
        for candidate in candidates
    ) or "- No local repo candidates were available."
    prompt = f"""# Repair API failure: {spec.name}

## API Reproduction

- Spec ID: `{spec.spec_id}`
- Run ID: `{result.run_id}`
- Method: `{spec.method}`
- URL: `{spec.url}`
- Query: `{json.dumps(spec.query, sort_keys=True)}`
- Expected status: `{spec.expected_status}`
- Actual status: `{result.status_code}`
- Failure classification: `{result.failure_classification}`
- Validation command: `retrace tester api-run {spec.spec_id}`

## Likely Source Files

{candidate_lines}

## Evidence

```json
{evidence_text}
```
"""
    prompt_path.write_text(prompt, encoding="utf-8")
    return str(prompt_path)


def _artifact_by_type(result: APITestRunResult, artifact_type: str) -> Any:
    for artifact in result.artifacts:
        if str(artifact.get("artifact_type") or "") == artifact_type:
            return _artifact_payload(str(artifact.get("path") or ""))
    return {}


def _url_path(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path or url


def _trace_ids_from_blob(value: Any) -> list[str]:
    out: list[str] = []
    _collect_trace_ids(value, out)
    return out[:10]


def _collect_trace_ids(value: Any, out: list[str]) -> None:
    if len(out) >= 10:
        return
    if isinstance(value, dict):
        for key, nested in value.items():
            key_s = str(key).casefold()
            if key_s in {"trace_id", "traceid", "requesttraceid", "responsetraceid"}:
                _append_trace_id(str(nested or ""), out)
            elif key_s in {"traceparent", "requesttraceparent", "responsetraceparent"}:
                _append_trace_id(_trace_id_from_traceparent(str(nested or "")), out)
            _collect_trace_ids(nested, out)
    elif isinstance(value, list):
        for item in value:
            _collect_trace_ids(item, out)


def _append_trace_id(value: str, out: list[str]) -> None:
    trace_id = value.strip().lower()
    if trace_id and trace_id not in out:
        out.append(trace_id)


def _trace_id_from_traceparent(value: str) -> str:
    parts = value.strip().split("-")
    if len(parts) < 4:
        return ""
    trace_id = parts[1].lower()
    if len(trace_id) != 32 or any(c not in "0123456789abcdef" for c in trace_id):
        return ""
    return trace_id


def _unique_strings(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _request_steps(spec: APITestSpec) -> list[dict[str, Any]]:
    if spec.steps:
        return [dict(step) for step in spec.steps]
    return [
        {
            "id": "request",
            "method": spec.method,
            "url": spec.url,
            "query": spec.query,
            "headers": {},
            "body": spec.body,
            "auth": spec.auth,
            "expected_status": spec.expected_status,
            "json_assertions": spec.json_assertions,
            "schema_assertions": spec.schema_assertions,
            "latency_ms": spec.latency_ms,
        }
    ]


def _step_spec(
    spec: APITestSpec,
    step: dict[str, Any],
    *,
    method: str,
    url: str,
    query: dict[str, Any],
) -> APITestSpec:
    return APITestSpec(
        schema_version=spec.schema_version,
        spec_id=spec.spec_id,
        name=str(step.get("name") or step.get("id") or spec.name),
        method=method,
        url=url,
        query=query,
        headers=dict(step.get("headers") or {}),
        headers_env=str(step.get("headers_env") or spec.headers_env or ""),
        body=step.get("body", spec.body),
        auth=dict(step.get("auth") or spec.auth),
        auth_profile=spec.auth_profile,
        env_profile=spec.env_profile,
        env_overrides=dict(spec.env_overrides),
        expected_status=int(step.get("expected_status", spec.expected_status)),
        json_assertions=list(step.get("json_assertions", spec.json_assertions)),
        schema_assertions=list(step.get("schema_assertions", spec.schema_assertions)),
        latency_ms=int(step.get("latency_ms", spec.latency_ms) or 0),
        timeout_seconds=spec.timeout_seconds,
        setup_steps=[],
        teardown_steps=[],
        steps=[],
        created_at=spec.created_at,
        updated_at=spec.updated_at,
        fixtures=dict(spec.fixtures),
    )


def _render_text(value: str, scope: dict[str, Any]) -> str:
    from retrace.script_steps import render_template

    return render_template(value, scope)


def _render_mapping(value: Any, scope: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key, item in value.items():
        out[str(key)] = _render_body(item, scope)
    return out


def _apply_extractors(raw_extractors: Any, scope: dict[str, Any]) -> None:
    if not isinstance(raw_extractors, list):
        return
    for extractor in raw_extractors:
        if not isinstance(extractor, dict):
            continue
        name = str(extractor.get("name") or "").strip()
        if not name:
            continue
        source = str(extractor.get("from") or "json").strip().lower()
        if source == "header":
            header_name = str(extractor.get("header") or extractor.get("path") or "")
            value = (scope.get("response") or {}).get("headers", {}).get(
                header_name
            )
        else:
            _, value = _json_path_lookup(
                (scope.get("response") or {}).get("json"),
                str(extractor.get("path") or "$"),
            )
        scope.setdefault("vars", {})[name] = value


def _resolve_headers(
    spec: APITestSpec,
    *,
    step_headers_env: Any = None,
    step_auth: Any = None,
    env: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    headers = {str(k): str(v) for k, v in spec.headers.items()}
    env_values = env if env is not None else dict(os.environ)
    headers_env = str(step_headers_env or spec.headers_env or "").strip()
    if headers_env:
        raw_headers = env_values.get(headers_env, "").strip()
        if not raw_headers:
            raise RuntimeError(f"auth failure: missing headers env var {headers_env}")
        try:
            parsed_headers = json.loads(raw_headers)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"auth failure: headers env var {headers_env} must be valid JSON"
            ) from exc
        if not isinstance(parsed_headers, dict):
            raise RuntimeError("auth failure: headers env var must be a JSON object")
        headers.update({str(k): str(v) for k, v in parsed_headers.items()})
    auth = step_auth if isinstance(step_auth, dict) else spec.auth
    auth_type = str(auth.get("type") or "none").strip().lower()
    if auth_type in {"", "none"}:
        return headers
    if auth_type == "bearer":
        token_env = str(auth.get("token_env") or "").strip()
        token = env_values.get(token_env, "").strip()
        if not token:
            raise RuntimeError(f"auth failure: missing bearer token env var {token_env}")
        headers["Authorization"] = f"Bearer {token}"
        return headers
    if auth_type == "headers":
        headers_env = str(auth.get("headers_env") or "").strip()
        raw = env_values.get(headers_env, "").strip()
        if not raw:
            raise RuntimeError(f"auth failure: missing headers env var {headers_env}")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise RuntimeError("auth failure: headers auth env var must be a JSON object")
        headers.update({str(k): str(v) for k, v in parsed.items()})
        return headers
    raise RuntimeError(f"unsupported auth type: {auth_type}")


def _render_body(body: Any, scope: dict[str, Any]) -> Any:
    if isinstance(body, str):
        from retrace.script_steps import render_template

        return render_template(body, scope)
    if isinstance(body, dict):
        return {str(k): _render_body(v, scope) for k, v in body.items()}
    if isinstance(body, list):
        return [_render_body(item, scope) for item in body]
    return body


def _response_body(response: httpx.Response) -> tuple[Any, Any]:
    try:
        return response.json(), None
    except Exception:
        return None, response.text[:5000]


def _record_script_step(
    *,
    artifacts: list[APITestArtifact],
    assertion_results: list[APIAssertionResult],
    artifacts_dir: Path,
    scope: dict[str, Any],
    step: dict[str, Any],
    phase: str,
    idx: int,
) -> None:
    outcome = run_script_step(step, scope=scope)
    path = artifacts_dir / f"{phase}-script-{idx}.json"
    path.write_text(json.dumps(asdict(outcome), indent=2) + "\n")
    artifacts.append(
        APITestArtifact(
            artifact_id=f"{phase}-script-{idx}",
            artifact_type="api_script_step",
            path=str(path),
            label=f"API {phase} script step",
            metadata={"phase": phase},
        )
    )
    if outcome.error:
        assertion_results.append(
            APIAssertionResult(
                assertion_id=f"{phase}-script-{idx}",
                assertion_type="script",
                ok=False,
                expected="script_step_ok",
                actual=outcome.error,
                message=outcome.error,
            )
        )
    for raw in outcome.assertions:
        assertion_results.append(
            APIAssertionResult(
                assertion_id=str(raw.get("id") or f"{phase}-script-{idx}"),
                assertion_type="script",
                ok=bool(raw.get("ok")),
                expected=True,
                actual=raw.get("ok"),
                message=str(raw.get("message") or ""),
            )
        )


def _evaluate_api_assertions(
    spec: APITestSpec,
    response_json: Any,
    elapsed_ms: int,
    status_code: int,
    assertion_prefix: str = "",
) -> list[APIAssertionResult]:
    api_regression = (
        spec.fixtures.get("api_regression")
        if isinstance(spec.fixtures.get("api_regression"), dict)
        else {}
    )
    if api_regression.get("status_assertion") == "not_equal":
        forbidden_status = int(api_regression.get("forbidden_status") or spec.expected_status)
        message = (
            f"Expected status to differ from captured failure {forbidden_status}, "
            f"got {status_code}."
        )
        results = [
            APIAssertionResult(
                assertion_id=_assertion_id("forbidden-status", assertion_prefix),
                assertion_type="status_code",
                ok=status_code != forbidden_status,
                expected=f"!={forbidden_status}",
                actual=status_code,
                message=message,
            )
        ]
    else:
        results = [
            APIAssertionResult(
                assertion_id=_assertion_id("expected-status", assertion_prefix),
                assertion_type="status_code",
                ok=status_code == spec.expected_status,
                expected=spec.expected_status,
                actual=status_code,
                message=f"Expected status {spec.expected_status}, got {status_code}.",
            )
        ]
    if spec.latency_ms > 0:
        results.append(
            APIAssertionResult(
                assertion_id=_assertion_id("latency-ms", assertion_prefix),
                assertion_type="latency_ms",
                ok=elapsed_ms <= spec.latency_ms,
                expected=f"<={spec.latency_ms}",
                actual=elapsed_ms,
                message=f"Expected latency <= {spec.latency_ms}ms, got {elapsed_ms}ms.",
            )
        )
    for idx, assertion in enumerate(spec.json_assertions):
        results.append(_evaluate_json_assertion(assertion, response_json, idx, assertion_prefix))
    for idx, assertion in enumerate(spec.schema_assertions):
        schema = assertion.get("schema") if "schema" in assertion else assertion
        results.append(_evaluate_schema_assertion(schema, response_json, idx, assertion_prefix))
    return results


def _assertion_id(value: str, prefix: str) -> str:
    return f"{prefix}:{value}" if prefix else value


def _evaluate_json_assertion(
    assertion: dict[str, Any],
    response_json: Any,
    idx: int,
    prefix: str = "",
) -> APIAssertionResult:
    path = str(assertion.get("path") or "")
    found, actual = _json_path_lookup(response_json, path)
    assertion_id = _assertion_id(str(assertion.get("id") or f"json-{idx}"), prefix)
    if assertion.get("exists") is True:
        ok = found
        return APIAssertionResult(assertion_id, "json_exists", ok, True, actual, path)
    if "equals" in assertion:
        expected = assertion.get("equals")
        ok = actual == expected
        return APIAssertionResult(assertion_id, "json_equals", ok, expected, actual, path)
    if "contains" in assertion:
        expected = str(assertion.get("contains"))
        ok = expected in str(actual)
        return APIAssertionResult(assertion_id, "json_contains", ok, expected, actual, path)
    return APIAssertionResult(assertion_id, "json_assertion", False, "known assertion", actual, path)


def _evaluate_schema_assertion(
    schema: Any,
    response_json: Any,
    idx: int,
    prefix: str = "",
) -> APIAssertionResult:
    errors = _schema_errors(schema, response_json, "$")
    return APIAssertionResult(
        assertion_id=_assertion_id(f"schema-{idx}", prefix),
        assertion_type="json_schema",
        ok=not errors,
        expected=schema,
        actual={"errors": errors},
        message="schema matched" if not errors else "; ".join(errors[:5]),
    )


def _schema_errors(schema: Any, value: Any, path: str) -> list[str]:
    if not isinstance(schema, dict):
        return [f"{path}: schema must be an object"]
    errors: list[str] = []
    expected_type = schema.get("type")
    if expected_type and not _matches_type(value, str(expected_type)):
        errors.append(f"{path}: expected {expected_type}, got {type(value).__name__}")
        return errors
    required = schema.get("required") or []
    if isinstance(required, list) and isinstance(value, dict):
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key}: required property missing")
    properties = schema.get("properties") or {}
    if isinstance(properties, dict) and isinstance(value, dict):
        for key, child_schema in properties.items():
            if key in value:
                errors.extend(_schema_errors(child_schema, value[key], f"{path}.{key}"))
    return errors


def _matches_type(value: Any, expected_type: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected_type, True)


def _json_path(value: Any, path: str) -> Any:
    return _json_path_lookup(value, path)[1]


def _json_path_lookup(value: Any, path: str) -> tuple[bool, Any]:
    if path in {"", "$"}:
        return True, value
    cursor = value
    for raw_part in path.removeprefix("$.").split("."):
        if raw_part == "":
            continue
        if isinstance(cursor, dict):
            if raw_part not in cursor:
                return False, None
            cursor = cursor.get(raw_part)
        elif isinstance(cursor, list) and raw_part.isdigit():
            idx = int(raw_part)
            if idx >= len(cursor):
                return False, None
            cursor = cursor[idx]
        else:
            return False, None
    return True, cursor


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        out[key] = "[redacted]" if key.lower() in SENSITIVE_HEADER_NAMES else value
    return out


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): "[redacted]" if str(k).lower() in SENSITIVE_BODY_KEYS else _redact_json(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    return value
