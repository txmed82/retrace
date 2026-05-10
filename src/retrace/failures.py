from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse


FailureStatus = Literal[
    "new",
    "triaged",
    "in_progress",
    "resolved",
    "regressed",
    "ignored",
]

FailureSourceType = Literal[
    "replay_issue",
    "test_run",
    "monitor_incident",
    "ci_job",
    "github_pr_review",
    "manual",
]


_STATUS_MAP = {
    "new": "new",
    "triaged": "triaged",
    "ongoing": "triaged",
    "unresolved": "triaged",
    "in_progress": "in_progress",
    "ticket_created": "in_progress",
    "resolved": "resolved",
    "verified": "resolved",
    "regressed": "regressed",
    "ignored": "ignored",
}


@dataclass(frozen=True)
class CanonicalFailure:
    """Shared failure shape used by replay, testing, monitoring, and review loops."""

    public_id: str
    project_id: str
    environment_id: str
    source_type: str
    source_external_id: str
    fingerprint: str
    title: str
    summary: str
    severity: str
    confidence: str
    status: FailureStatus
    affected_users: int = 0
    affected_sessions: int = 0
    first_seen_ms: int = 0
    last_seen_ms: int = 0
    related_deploy_sha: str = ""
    related_pr_number: int | None = None
    linked_tests: list[str] = field(default_factory=list)
    linked_repair_task_id: str = ""
    linked_external_thread_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_storage_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_failure_status(status: object) -> FailureStatus:
    raw = str(status or "new").strip().lower()
    return _STATUS_MAP.get(raw, "new")  # type: ignore[return-value]


def stable_failure_public_id(
    project_id: str,
    environment_id: str,
    source_type: str,
    source_external_id: str,
) -> str:
    raw = "\x1f".join(
        [project_id, environment_id, source_type, source_external_id]
    )
    return f"flr_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def canonical_failure_from_replay_issue(issue: Mapping[str, Any]) -> CanonicalFailure:
    public_id = str(issue.get("public_id") or "")
    project_id = str(issue.get("project_id") or "")
    environment_id = str(issue.get("environment_id") or "")
    source_external_id = public_id or str(issue.get("id") or "")
    signal_summary = _json_obj(issue.get("signal_summary_json"))
    evidence = _json_obj(issue.get("evidence_json"))
    error_issue_ids = _json_list(issue.get("error_issue_ids_json"))
    trace_ids = _json_list(issue.get("trace_ids_json"))
    return CanonicalFailure(
        public_id=stable_failure_public_id(
            project_id, environment_id, "replay_issue", source_external_id
        ),
        project_id=project_id,
        environment_id=environment_id,
        source_type="replay_issue",
        source_external_id=source_external_id,
        fingerprint=str(issue.get("fingerprint") or source_external_id),
        title=str(issue.get("title") or "Replay issue"),
        summary=str(issue.get("summary") or ""),
        severity=str(issue.get("severity") or "medium"),
        confidence=str(issue.get("confidence") or "medium"),
        status=normalize_failure_status(issue.get("status")),
        affected_users=_safe_int(issue.get("affected_users")),
        affected_sessions=_safe_int(issue.get("affected_count")),
        first_seen_ms=_safe_int(issue.get("first_seen_ms")),
        last_seen_ms=_safe_int(issue.get("last_seen_ms")),
        linked_external_thread_id=str(issue.get("external_ticket_id") or ""),
        metadata={
            "replay_issue_id": str(issue.get("id") or ""),
            "replay_issue_public_id": public_id,
            "representative_session_id": str(
                issue.get("representative_session_id") or ""
            ),
            "signal_summary": signal_summary,
            "evidence": evidence,
            "distinct_id": str(issue.get("distinct_id") or ""),
            "error_issue_ids": error_issue_ids,
            "trace_ids": trace_ids,
            "top_stack_frame": str(issue.get("top_stack_frame") or ""),
            "error_tracking_url": str(issue.get("error_tracking_url") or ""),
            "logs_url": str(issue.get("logs_url") or ""),
        },
    )


def canonical_failure_from_test_run(
    *,
    project_id: str,
    environment_id: str,
    run_result: Any,
    spec_name: str = "",
) -> CanonicalFailure:
    run_id = str(getattr(run_result, "run_id", "") or "")
    spec_id = str(getattr(run_result, "spec_id", "") or "")
    exit_code = _safe_int(getattr(run_result, "exit_code", 0))
    status = str(getattr(run_result, "status", "") or "")
    failure_classification = str(
        getattr(run_result, "failure_classification", "") or "unknown"
    )
    fingerprint_payload = {
        "spec_id": spec_id,
        "status": status,
        "exit_code": exit_code,
        "failure_classification": failure_classification,
        "error": str(getattr(run_result, "error", "") or ""),
        "execution_engine": str(getattr(run_result, "execution_engine", "") or ""),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    source_external_id = run_id or fingerprint[:16]
    ok = bool(getattr(run_result, "ok", False))
    return CanonicalFailure(
        public_id=stable_failure_public_id(
            project_id, environment_id, "test_run", source_external_id
        ),
        project_id=project_id,
        environment_id=environment_id,
        source_type="test_run",
        source_external_id=source_external_id,
        fingerprint=fingerprint,
        title=(
            f"Tester run failed ({failure_classification}): "
            f"{spec_name or spec_id or source_external_id}"
        ),
        summary=str(getattr(run_result, "error", "") or f"exit code {exit_code}"),
        severity="medium",
        confidence="high" if not ok else "low",
        status="resolved" if ok else "new",
        linked_tests=[spec_id] if spec_id else [],
        metadata={
            "run_id": run_id,
            "spec_id": spec_id,
            "exit_code": exit_code,
            "status": status,
            "flaky": bool(getattr(run_result, "flaky", False)),
            "flake_reason": str(getattr(run_result, "flake_reason", "") or ""),
            "failure_classification": failure_classification,
            "execution_engine": str(getattr(run_result, "execution_engine", "") or ""),
            "artifacts": list(getattr(run_result, "artifacts", []) or []),
            "trace_ids": _trace_ids_from_run_artifacts(run_result),
            "assertion_results": list(
                getattr(run_result, "assertion_results", []) or []
            ),
        },
    )


def canonical_failure_from_harness_run(
    *,
    project_id: str,
    environment_id: str,
    run_result: Any,
    spec_name: str = "",
) -> CanonicalFailure:
    failure = canonical_failure_from_test_run(
        project_id=project_id,
        environment_id=environment_id,
        run_result=run_result,
        spec_name=spec_name,
    )
    source_external_id = f"harness:{failure.fingerprint[:16]}"
    return CanonicalFailure(
        **{
            **failure.to_storage_dict(),
            "public_id": stable_failure_public_id(
                project_id,
                environment_id,
                "test_run",
                source_external_id,
            ),
            "source_external_id": source_external_id,
        }
    )


def canonical_failure_from_api_run(
    *,
    project_id: str,
    environment_id: str,
    spec: Any,
    run_result: Any,
) -> CanonicalFailure:
    spec_id = str(getattr(spec, "spec_id", "") or getattr(run_result, "spec_id", "") or "")
    method = str(getattr(spec, "method", "") or "").upper()
    url = str(getattr(spec, "url", "") or "")
    route_path = _url_path(url)
    expected_status = _safe_int(getattr(spec, "expected_status", 0))
    status_code = _safe_int(getattr(run_result, "status_code", 0))
    error = str(getattr(run_result, "error", "") or "")
    assertion_results = list(getattr(run_result, "assertion_results", []) or [])
    fingerprint_payload = {
        "spec_id": spec_id,
        "method": method,
        "url": url,
        "expected_status": expected_status,
        "status_code": status_code,
        "assertion_results": assertion_results,
        "error": error,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    run_id = str(getattr(run_result, "run_id", "") or "")
    source_external_id = f"api:{run_id or fingerprint[:16]}"
    ok = bool(getattr(run_result, "ok", False))
    failed_assertions = [
        str(item.get("message") or item.get("assertion_id") or "")
        for item in assertion_results
        if isinstance(item, dict) and not bool(item.get("ok"))
    ]
    if status_code and status_code != expected_status:
        summary = f"{method} {url} expected status {expected_status}, got {status_code}."
        if error:
            summary = f"{summary} Error: {error}"
    elif status_code:
        assertion_summary = error or "; ".join(item for item in failed_assertions if item)
        summary = (
            f"{method} {url} assertion failed: {assertion_summary}"
            if assertion_summary
            else f"{method} {url} failed after receiving status {status_code}."
        )
    else:
        summary = f"{method} {url} failed before receiving a response."
        if error:
            summary = f"{summary} Error: {error}"
    return CanonicalFailure(
        public_id=stable_failure_public_id(
            project_id, environment_id, "test_run", source_external_id
        ),
        project_id=project_id,
        environment_id=environment_id,
        source_type="test_run",
        source_external_id=source_external_id,
        fingerprint=fingerprint,
        title=f"API test failed: {getattr(spec, 'name', '') or spec_id}",
        summary=summary,
        severity="medium",
        confidence="high" if not ok else "low",
        status="resolved" if ok else "new",
        linked_tests=[spec_id] if spec_id else [],
        metadata={
            "run_id": run_id,
            "spec_id": spec_id,
            "method": method,
            "url": url,
            "route_path": route_path,
            "query": dict(getattr(spec, "query", {}) or {}),
            "expected_status": expected_status,
            "status_code": status_code,
            "status": str(getattr(run_result, "status", "") or ""),
            "error": error,
            "artifacts": list(getattr(run_result, "artifacts", []) or []),
            "assertion_results": assertion_results,
            "trace_ids": _api_trace_ids_from_spec_and_run(spec, run_result),
        },
    )


def canonical_failure_from_monitor_incident(
    *,
    project_id: str,
    environment_id: str,
    provider: str,
    external_id: str,
    title: str,
    summary: str = "",
    severity: str = "medium",
    fingerprint: str = "",
    occurred_at_ms: int = 0,
    metadata: dict[str, Any] | None = None,
) -> CanonicalFailure:
    source_external_id = f"{provider}:{external_id}"
    clean_fingerprint = fingerprint or hashlib.sha256(
        json.dumps(
            {
                "provider": provider,
                "external_id": external_id,
                "title": title,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return CanonicalFailure(
        public_id=stable_failure_public_id(
            project_id, environment_id, "monitor_incident", source_external_id
        ),
        project_id=project_id,
        environment_id=environment_id,
        source_type="monitor_incident",
        source_external_id=source_external_id,
        fingerprint=clean_fingerprint,
        title=title,
        summary=summary,
        severity=severity,
        confidence="high",
        status="new",
        first_seen_ms=_safe_int(occurred_at_ms),
        last_seen_ms=_safe_int(occurred_at_ms),
        metadata={"provider": provider, **(metadata or {})},
    )


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _json_obj(raw: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(raw: object) -> list[Any]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _url_path(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path or url


def _api_trace_ids_from_spec(spec: Any) -> list[str]:
    fixtures = getattr(spec, "fixtures", {}) or {}
    if not isinstance(fixtures, dict):
        return []
    api_regression = fixtures.get("api_regression") or {}
    if not isinstance(api_regression, dict):
        return []
    values = api_regression.get("trace_ids") or []
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in out:
            out.append(item)
    return out


def _api_trace_ids_from_spec_and_run(spec: Any, run_result: Any) -> list[str]:
    out = _api_trace_ids_from_spec(spec)
    for artifact in list(getattr(run_result, "artifacts", []) or []):
        if not isinstance(artifact, dict):
            continue
        artifact_type = str(artifact.get("artifact_type") or "")
        if artifact_type not in {"api_request", "api_response", "api_run_summary"}:
            continue
        payload = _artifact_payload(str(artifact.get("path") or ""))
        _collect_trace_ids(payload, out)
        if len(out) >= 10:
            break
    return out[:10]


def _trace_ids_from_run_artifacts(run_result: Any) -> list[str]:
    out: list[str] = []
    for artifact in list(getattr(run_result, "artifacts", []) or []):
        if not isinstance(artifact, dict):
            continue
        artifact_type = str(artifact.get("artifact_type") or "")
        if artifact_type not in {"network_output", "browser_harness_output"}:
            continue
        payload = _artifact_payload(str(artifact.get("path") or ""))
        _collect_trace_ids(payload, out)
        if len(out) >= 10:
            break
    return out[:10]


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
