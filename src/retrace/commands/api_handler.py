from __future__ import annotations

import json
import logging
from threading import Lock
import time
import uuid
from http.server import BaseHTTPRequestHandler
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

import click

from retrace.github_app import GitHubWebhookError, handle_github_webhook
from retrace.incidents import get_incident_detail
from retrace.issue_sinks import build_issue_card
from retrace.llm.client import LLMClient
from retrace.notification_sinks import (
    NotificationEvent,
    NotificationPayload,
    dispatch_notification,
)
from retrace.observability import collect_local_observability, record_api_request
from retrace.enrichment import CorrelationEnricher
from retrace.deploys import correlate_recent_failures_to_deploys, record_deploy
from retrace.monitoring_ingest import ingest_monitoring_webhook
from retrace.otel_ingest import ingest_otel_logs, ingest_otel_traces
from retrace.replay_api import (
    MAX_REPLAY_BODY_BYTES,
    ReplayIngestError,
    ingest_replay_request,
)
from retrace.replay_core import process_queued_replay_jobs
from retrace.sdk_keys import (
    authenticate_sdk_key,
    authenticate_service_token,
    create_sdk_key,
    create_service_token,
)
from retrace.sentry_compat import (
    MAX_SENTRY_BODY_BYTES,
    SentryCompatIngestError,
    build_sentry_dsn,
    extract_sentry_ingest_key,
    ingest_sentry_compat_request,
)
from retrace.source_maps import upload_source_map
from retrace.storage import RateLimitDecision, Storage


logger = logging.getLogger(__name__)

INGEST_RATE_LIMITS: dict[str, tuple[int, int]] = {
    "replay": (600, 60),
    "sentry": (600, 60),
    "monitoring": (300, 60),
    "source_maps": (30, 60),
    # OTel logs/traces — high-volume by design (every span is a row),
    # so we run the same ceiling as replay rather than the tighter
    # monitoring webhook quota. Operators with bursty OTel traffic
    # should tune via the existing `consume_ingest_rate_limit` knobs
    # if they hit the limit on legitimate load.
    "otel": (600, 60),
    # P3.6 (scaffold) — server-side replay sessions are captured at
    # the failure moment (one per SSR exception, not a stream), so
    # expected volume is much lower than browser replay. Tight
    # default; operators with high failure rates can tune.
    "server_replay": (120, 60),
}
HOSTED_ONBOARDING_SCOPES = (
    "ingest",
    "source_maps:write",
    "app_errors:read",
    "app_errors:write",
    "issues:read",
    "replay:read",
)


def _maybe_llm_client(cfg: Any, *, enabled: bool) -> LLMClient | None:
    if not enabled:
        return None
    return LLMClient(cfg.llm)


def _json_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> None:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    setattr(handler, "_retrace_response_status", status)
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    trace_id = str(getattr(handler, "_retrace_trace_id", "") or "")
    if trace_id:
        handler.send_header("X-Retrace-Trace-Id", trace_id)
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    _cors_headers(handler)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _cors_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header(
        "Access-Control-Expose-Headers",
        (
            "X-Retrace-Trace-Id, Retry-After, X-RateLimit-Limit, "
            "X-RateLimit-Remaining, X-RateLimit-Reset, X-RateLimit-Window"
        ),
    )
    handler.send_header(
        "Access-Control-Allow-Headers",
        (
            "authorization, content-encoding, content-type, x-github-delivery, "
            "x-github-event, x-hub-signature-256, x-retrace-key, x-sentry-auth"
        ),
    )
    handler.send_header("Access-Control-Max-Age", "86400")


def _query_dict(query: str) -> dict[str, str]:
    return {k: v[-1] for k, v in parse_qs(query, keep_blank_values=True).items()}


def _sentry_ingest_path_parts(path: str) -> tuple[str, str] | None:
    if path.startswith("/api/sentry/"):
        suffix = path.removeprefix("/api/sentry/").strip("/")
        parts = [part for part in suffix.split("/") if part]
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
        return None
    if not path.startswith("/api/"):
        return None
    suffix = path.removeprefix("/api/").strip("/")
    parts = [part for part in suffix.split("/") if part]
    if len(parts) == 2 and parts[1].strip().lower() in {"store", "envelope"}:
        return parts[0].strip(), parts[1].strip()
    return None


def _bearer_token(headers: Any) -> str:
    auth = str(headers.get("Authorization") or headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _header_value(headers: Any, name: str) -> str:
    lname = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lname:
            return str(value)
    return ""


def _extract_replay_sdk_key(headers: Any, query: dict[str, str]) -> str:
    direct = _header_value(headers, "x-retrace-key").strip()
    if direct:
        return direct
    auth = _header_value(headers, "authorization").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return str(
        query.get("key") or query.get("api_key") or query.get("apiKey") or ""
    ).strip()


def _rate_limit_headers(decision: RateLimitDecision) -> dict[str, str]:
    return {
        "Retry-After": str(decision.reset_after_seconds),
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str(decision.reset_after_seconds),
        "X-RateLimit-Window": str(decision.window_seconds),
    }


def _rate_limited_response(
    handler: BaseHTTPRequestHandler,
    *,
    bucket: str,
    decision: RateLimitDecision,
) -> None:
    _json_response(
        handler,
        429,
        {
            "error": "rate_limited",
            "message": f"{bucket} ingest rate limit exceeded.",
            "limit": decision.limit,
            "remaining": decision.remaining,
            "retry_after_seconds": decision.reset_after_seconds,
            "window_seconds": decision.window_seconds,
        },
        headers=_rate_limit_headers(decision),
    )


def _consume_rate_limit(
    store: Storage,
    *,
    project_id: str,
    environment_id: str,
    bucket: str,
    identity: str,
) -> RateLimitDecision:
    limit, window_seconds = INGEST_RATE_LIMITS[bucket]
    return store.consume_ingest_rate_limit(
        project_id=project_id,
        environment_id=environment_id,
        bucket=bucket,
        identity=identity,
        limit=limit,
        window_seconds=window_seconds,
    )


def _require_service_token(
    handler: BaseHTTPRequestHandler,
    store: Storage,
    *,
    scopes: set[str],
):
    token = authenticate_service_token(store, _bearer_token(handler.headers))
    if token is None:
        _json_response(
            handler,
            401,
            {"error": "unauthorized", "message": "Missing or invalid service token."},
        )
        return None
    if scopes and not scopes.intersection(set(token.scopes)):
        _json_response(
            handler,
            403,
            {"error": "forbidden", "message": "Service token lacks the required scope."},
        )
        return None
    return token


def _row_dict(row: Any, *, include_payload: bool = False) -> dict[str, Any]:
    out = {k: row[k] for k in row.keys()}
    for key in (
        "metadata_json",
        "preview_json",
        "signal_summary_json",
        "reproduction_steps_json",
    ):
        if key in out:
            try:
                out[key.removesuffix("_json")] = json.loads(out[key] or "{}")
            except json.JSONDecodeError:
                out[key.removesuffix("_json")] = {} if key != "reproduction_steps_json" else []
            del out[key]
    if not include_payload and "payload_json" in out:
        del out["payload_json"]
    return out


def _dt_api(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else ""


def _incident_api_dict(
    *,
    store: Storage,
    incident: Any,
    failures: list[Any] | None = None,
) -> dict[str, Any]:
    linked_failures = failures
    if linked_failures is None:
        linked_failures = store.list_incident_failures(incident_id=incident.id)
    representative = _latest_failure(linked_failures)
    trace_ids: list[str] = []
    top_stack_frame = ""
    transaction = ""
    release = ""
    provider = ""
    alert_state = "active"
    alert_rule_name = ""
    if representative is not None:
        metadata = dict(getattr(representative, "metadata", {}) or {})
        trace_value = metadata.get("trace_ids")
        if isinstance(trace_value, list):
            trace_ids = [str(item) for item in trace_value if str(item).strip()]
        top_stack_frame = str(metadata.get("top_stack_frame") or "")
        transaction = str(metadata.get("transaction") or metadata.get("route") or "")
        release = str(metadata.get("release") or metadata.get("deploy_sha") or "")
        provider = str(metadata.get("provider") or "")
        alert_state = str(metadata.get("alert_state") or "active")
        alert_rule_name = str(metadata.get("alert_rule_name") or "")
    return {
        "id": incident.id,
        "public_id": incident.public_id,
        "title": incident.title,
        "summary": incident.summary,
        "severity": incident.severity,
        "status": incident.status,
        "failure_count": incident.failure_count,
        "evidence_count": incident.evidence_count,
        "repair_task_id": incident.repair_task_id,
        "group_key": incident.group_key,
        "metadata": dict(incident.metadata or {}),
        "first_seen_at": _dt_api(incident.created_at),
        "last_seen_at": _dt_api(incident.updated_at),
        "latest_failure": (
            _failure_api_dict(representative, include_metadata=False)
            if representative is not None
            else None
        ),
        "trace_ids": trace_ids,
        "top_stack_frame": top_stack_frame,
        "transaction": transaction,
        "release": release,
        "provider": provider,
        "alert_state": alert_state,
        "alert_rule_name": alert_rule_name,
    }


def _latest_failure(failures: list[Any]) -> Any | None:
    if not failures:
        return None
    return max(
        failures,
        key=lambda failure: (
            int(getattr(failure, "last_seen_ms", 0) or 0),
            _dt_api(getattr(failure, "updated_at", "")),
            str(getattr(failure, "id", "")),
        ),
    )


def _failure_api_dict(failure: Any, *, include_metadata: bool = True) -> dict[str, Any]:
    payload = {
        "id": failure.id,
        "public_id": failure.public_id,
        "source_type": failure.source_type,
        "source_external_id": failure.source_external_id,
        "fingerprint": failure.fingerprint,
        "title": failure.title,
        "summary": failure.summary,
        "severity": failure.severity,
        "confidence": failure.confidence,
        "status": failure.status,
        "affected_users": failure.affected_users,
        "affected_sessions": failure.affected_sessions,
        "first_seen_ms": failure.first_seen_ms,
        "last_seen_ms": failure.last_seen_ms,
        "related_deploy_sha": failure.related_deploy_sha,
        "linked_tests": list(failure.linked_tests or []),
        "linked_repair_task_id": failure.linked_repair_task_id,
        "created_at": _dt_api(failure.created_at),
        "updated_at": _dt_api(failure.updated_at),
    }
    if include_metadata:
        payload["metadata"] = dict(failure.metadata or {})
    return payload


def _evidence_api_dict(evidence: Any) -> dict[str, Any]:
    return {
        "id": evidence.id,
        "failure_id": evidence.failure_id,
        "evidence_type": evidence.evidence_type,
        "occurred_at_ms": evidence.occurred_at_ms,
        "source": evidence.source,
        "redaction_state": evidence.redaction_state,
        "payload": dict(evidence.payload or {}),
        "artifact_path": evidence.artifact_path,
        "created_at": _dt_api(evidence.created_at),
    }


def _incident_lifecycle_event_api_dict(event: Any) -> dict[str, Any]:
    return {
        "id": event.id,
        "incident_id": event.incident_id,
        "from_status": event.from_status,
        "to_status": event.to_status,
        "actor_type": event.actor_type,
        "actor_id": event.actor_id,
        "reason": event.reason,
        "metadata": dict(event.metadata or {}),
        "created_at": _dt_api(event.created_at),
    }


def _repair_task_api_dict(task: Any) -> dict[str, Any]:
    return {
        "id": task.id,
        "public_id": task.public_id,
        "failure_id": task.failure_id,
        "source_type": task.source_type,
        "source_external_id": task.source_external_id,
        "title": task.title,
        "status": task.status,
        "likely_files": list(task.likely_files or []),
        "validation_commands": list(task.validation_commands or []),
        "branch": task.branch,
        "pr_url": task.pr_url,
        "risk_notes": task.risk_notes,
        "metadata": dict(task.metadata or {}),
        "evidence_ids": list(task.evidence_ids or []),
        "created_at": _dt_api(task.created_at),
        "updated_at": _dt_api(task.updated_at),
    }


def _hosted_onboarding_manifest(
    *,
    base_url: str,
    project_id: str,
    environment_id: str,
    sdk_key: str,
    service_token: str,
    service_token_scopes: Iterable[str],
    release: str = "$GITHUB_SHA",
    artifact_url: str = "https://cdn.example.com/assets/app.min.js",
) -> dict[str, Any]:
    clean_base_url = base_url.rstrip("/") or "http://127.0.0.1:8788"
    clean_release = release.strip() or "$GITHUB_SHA"
    clean_artifact_url = artifact_url.strip() or "https://cdn.example.com/assets/app.min.js"
    sentry_dsn = build_sentry_dsn(
        public_key=sdk_key,
        base_url=clean_base_url,
        project_id=project_id,
    )
    monitoring_webhook = (
        f"{clean_base_url}/api/monitoring/webhook/sentry?environment_id={environment_id}"
    )
    source_map_endpoint = f"{clean_base_url}/api/source-maps?environment_id={environment_id}"
    app_errors_endpoint = f"{clean_base_url}/api/app-errors?environment_id={environment_id}"
    alert_rules_endpoint = (
        f"{clean_base_url}/api/app-error-alert-rules?environment_id={environment_id}"
    )
    prune_endpoint = f"{clean_base_url}/api/app-errors/prune?environment_id={environment_id}"
    health_endpoint = f"{clean_base_url}/healthz"
    smoke_event_id = "retrace-onboarding-smoke-1"
    source_map_upload = (
        "curl -X POST "
        f"'{source_map_endpoint}' "
        f"-H 'Authorization: Bearer {service_token}' "
        "-H 'Content-Type: application/json' "
        f"-d '{{\"release\":\"{clean_release}\",\"artifact_url\":\"{clean_artifact_url}\",\"source_map\":{{\"version\":3,\"sources\":[\"src/app.ts\"],\"names\":[],\"mappings\":\"AAAA\"}}}}'"
    )
    monitoring_smoke = (
        "curl -X POST "
        f"'{monitoring_webhook}' "
        f"-H 'Authorization: Bearer {service_token}' "
        "-H 'Content-Type: application/json' "
        f"-d '{{\"event\":{{\"event_id\":\"{smoke_event_id}\",\"title\":\"Retrace onboarding smoke error\",\"level\":\"error\",\"release\":\"{clean_release}\"}}}}'"
    )
    alert_rule_create = (
        "curl -X POST "
        f"'{alert_rules_endpoint}' "
        f"-H 'Authorization: Bearer {service_token}' "
        "-H 'Content-Type: application/json' "
        "-d '{\"name\":\"Critical production errors\",\"action\":\"alert\",\"min_severity\":\"high\"}'"
    )
    retention_prune = (
        "curl -X POST "
        f"'{prune_endpoint}' "
        f"-H 'Authorization: Bearer {service_token}' "
        "-H 'Content-Type: application/json' "
        "-d '{\"failure_retention_days\":90,\"evidence_retention_days\":90,\"source_map_retention_days\":30,\"rate_limit_retention_hours\":48}'"
    )
    return {
        "workspace": {
            "project_id": project_id,
            "environment_id": environment_id,
            "api_base_url": clean_base_url,
        },
        "credentials": {
            "browser_sdk_key": sdk_key,
            "service_token": service_token,
            "service_token_scopes": [str(scope) for scope in service_token_scopes],
            "sentry_dsn": sentry_dsn,
        },
        "endpoints": {
            "replay_ingest": f"{clean_base_url}/api/sdk/replay",
            "sentry_store": f"{clean_base_url}/api/sentry/{project_id}/store/",
            "sentry_envelope": f"{clean_base_url}/api/sentry/{project_id}/envelope/",
            "monitoring_webhook": monitoring_webhook,
            "source_maps": source_map_endpoint,
            "app_errors": app_errors_endpoint,
            "app_error_alert_rules": alert_rules_endpoint,
            "app_error_retention_prune": prune_endpoint,
        },
        "snippets": {
            "browser_sdk_install": "npm install @retrace/browser",
            "browser_sdk_init": (
                "import { initRetrace } from '@retrace/browser';\n\n"
                "initRetrace({\n"
                f"  apiBaseUrl: '{clean_base_url}',\n"
                f"  key: '{sdk_key}',\n"
                "  captureConsole: true,\n"
                "  captureNetwork: true,\n"
                "  captureClicks: true,\n"
                "});"
            ),
            "sentry_js_init": (
                "import * as Sentry from '@sentry/browser';\n\n"
                "Sentry.init({\n"
                f"  dsn: '{sentry_dsn}',\n"
                f"  release: '{clean_release}',\n"
                "});"
            ),
            "monitoring_webhook_curl": (
                monitoring_smoke
            ),
            "source_map_upload_curl": (
                source_map_upload
            ),
            "alert_rule_curl": (
                alert_rule_create
            ),
            "resolve_incident_curl": (
                "curl -X POST "
                f"'{clean_base_url}/api/app-errors/<incident_public_id>/lifecycle?environment_id={environment_id}' "
                f"-H 'Authorization: Bearer {service_token}' "
                "-H 'Content-Type: application/json' "
                "-d '{\"action\":\"resolve\",\"reason\":\"fixed and verified\"}'"
            ),
            "retention_cron": (
                retention_prune
            ),
            "github_actions_source_maps": (
                "name: Upload Retrace source maps\n"
                "on: [push]\n"
                "jobs:\n"
                "  upload-source-maps:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - uses: actions/checkout@v4\n"
                "      - run: npm ci && npm run build\n"
                "      - run: |\n"
                "          curl -X POST "
                f"'{source_map_endpoint}' \\\n"
                "            -H 'Authorization: Bearer ${{ secrets.RETRACE_SERVICE_TOKEN }}' \\\n"
                "            -H 'Content-Type: application/json' \\\n"
                "            --data-binary @retrace-source-map-upload.json"
            ),
        },
        "verification": {
            "ordered_steps": [
                {
                    "id": "health",
                    "label": "API health",
                    "command": f"curl -fsS '{health_endpoint}'",
                    "expect": {"status": 200, "json": {"ok": True}},
                },
                {
                    "id": "source_maps",
                    "label": "Upload source map for release",
                    "command": source_map_upload,
                    "expect": {
                        "status": 202,
                        "json_contains": {
                            "source_map": {
                                "release": clean_release,
                                "artifact_url": clean_artifact_url,
                            }
                        },
                    },
                },
                {
                    "id": "alert_rule",
                    "label": "Create high-severity alert rule",
                    "command": alert_rule_create,
                    "expect": {
                        "status": 202,
                        "json_contains": {"rule": {"action": "alert"}},
                    },
                },
                {
                    "id": "monitoring_smoke",
                    "label": "Send monitoring smoke error",
                    "command": monitoring_smoke,
                    "expect": {
                        "status": 202,
                        "json_contains": {"results": [{"external_id": smoke_event_id}]},
                    },
                },
                {
                    "id": "app_errors",
                    "label": "Confirm smoke incident is visible",
                    "command": (
                        f"curl -fsS '{app_errors_endpoint}' "
                        f"-H 'Authorization: Bearer {service_token}'"
                    ),
                    "expect": {
                        "status": 200,
                        "json_path_hint": "$.incidents[?(@.latest_failure.source_external_id contains retrace-onboarding-smoke-1)]",
                    },
                },
                {
                    "id": "retention",
                    "label": "Verify hosted retention prune endpoint",
                    "command": retention_prune,
                    "expect": {
                        "status": 202,
                        "json_contains": {"retention": {"environment_id": environment_id}},
                    },
                },
            ],
            "required_scopes": [str(scope) for scope in service_token_scopes],
            "release": clean_release,
            "artifact_url": clean_artifact_url,
        },
        "checklist": [
            "Start the API with `retrace api serve --host 0.0.0.0 --port 8788` behind TLS.",
            "Install the browser SDK or point Sentry-compatible clients at the DSN.",
            "Upload source maps from CI for each release before or during deploy.",
            "Create at least one alert rule for high-severity production errors.",
            "Send the monitoring smoke webhook and confirm it appears in `GET /api/app-errors`.",
            "Schedule the retention prune request daily for hosted cleanup.",
        ],
    }


def _alert_rule_api_dict(rule: Any) -> dict[str, Any]:
    return {
        "id": rule.id,
        "public_id": rule.public_id,
        "name": rule.name,
        "enabled": rule.enabled,
        "precedence": rule.precedence,
        "action": rule.action,
        "min_severity": rule.min_severity,
        "provider": rule.provider,
        "title_contains": rule.title_contains,
        "fingerprint_contains": rule.fingerprint_contains,
        "route_contains": rule.route_contains,
        "metadata": dict(rule.metadata or {}),
        "created_at": _dt_api(rule.created_at),
        "updated_at": _dt_api(rule.updated_at),
    }


def _retention_result_api_dict(result: Any) -> dict[str, Any]:
    return {
        "dry_run": bool(result.dry_run),
        "failure_retention_days": int(result.failure_retention_days),
        "evidence_retention_days": int(result.evidence_retention_days),
        "source_map_retention_days": int(result.source_map_retention_days),
        "rate_limit_retention_hours": int(result.rate_limit_retention_hours),
        "deleted": {
            "failures": int(result.failures),
            "evidence": int(result.evidence),
            "incident_links": int(result.incident_links),
            "incidents": int(result.incidents),
            "source_maps": int(result.source_maps),
            "rate_limit_rows": int(result.rate_limit_rows),
        },
    }


def _optional_int(payload: dict[str, Any], key: str, default: int) -> int:
    value = payload.get(key, None)
    if value is None:
        return default
    return int(value)


def _optional_bool(payload: dict[str, Any], key: str, default: bool = False) -> bool:
    value = payload.get(key, None)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    raise ValueError(f"{key} must be a boolean")


def _app_error_notification_payload(
    *,
    store: Storage,
    result: Any,
) -> NotificationPayload | None:
    failure = store.get_failure_by_id(str(result.failure_id))
    if failure is None:
        return None
    incident_public_id = str(getattr(result, "incident_public_id", "") or "")
    public_id = incident_public_id or str(getattr(result, "failure_public_id", "") or "")
    provider = str(getattr(result, "provider", "") or "")
    return NotificationPayload(
        event=NotificationEvent.APP_ERROR_CREATED.value,
        title=f"{provider.title() or 'App'} error: {failure.title}",
        summary=failure.summary,
        severity=failure.severity,
        public_id=public_id,
        extra={
            "provider": provider,
            "incident_id": str(getattr(result, "incident_id", "") or ""),
            "incident_public_id": incident_public_id,
            "failure_id": failure.id,
            "failure_public_id": failure.public_id,
            "evidence_id": str(getattr(result, "evidence_id", "") or ""),
            "source_external_id": failure.source_external_id,
            "trace_ids": list((failure.metadata or {}).get("trace_ids") or []),
            "top_stack_frame": str((failure.metadata or {}).get("top_stack_frame") or ""),
        },
    )


def _dispatch_app_error_notifications(
    *,
    sinks: Iterable[Any],
    store: Storage,
    results: Iterable[Any],
    lock: Lock | None = None,
) -> None:
    sink_list = list(sinks)
    if not sink_list:
        return
    for result in results:
        if not bool(getattr(result, "created", False)):
            continue
        payload = _app_error_notification_payload(store=store, result=result)
        if payload is not None:
            try:
                if lock is None:
                    dispatch_notification(sink_list, payload)
                else:
                    with lock:
                        dispatch_notification(sink_list, payload)
            except Exception:
                logger.warning(
                    "failed to dispatch app_error notification",
                    extra={
                        "failure_id": str(getattr(result, "failure_id", "") or ""),
                        "incident_id": str(getattr(result, "incident_id", "") or ""),
                    },
                    exc_info=True,
                )


def _build_enricher(cfg: Any, store: Storage) -> CorrelationEnricher | None:
    """Construct a best-effort correlation enricher when PostHog is configured.

    Returns None if PostHog credentials are missing OR if construction fails
    (e.g. malformed host URL).  Replay processing must never block on an
    optional enrichment feature, so any failure here downgrades to "no
    enricher" rather than propagating.
    """
    try:
        if not getattr(cfg.posthog, "api_key", "").strip():
            return None
    except AttributeError:
        return None
    try:
        return CorrelationEnricher(cfg, store)
    except Exception:
        logger.warning(
            "correlation enricher disabled due to invalid PostHog config",
            exc_info=True,
        )
        return None


def _issue_cards_for_items(
    store: Storage,
    items: list[dict[str, str]],
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for item in items[:10]:
        public_id = str(item.get("public_id") or "")
        project_id = str(item.get("project_id") or "")
        environment_id = str(item.get("environment_id") or "")
        if not public_id or not project_id or not environment_id:
            continue
        issue = store.get_replay_issue(
            project_id=project_id,
            environment_id=environment_id,
            issue_id=public_id,
        )
        if issue is None:
            continue
        sessions = store.list_replay_issue_sessions(str(issue["id"]))
        cards.append(build_issue_card(store=store, issue=issue, sessions=sessions))
    return cards


def _handler(
    store: Storage,
    *,
    enricher: CorrelationEnricher | None = None,
    github_webhook_secret: str = "",
    notification_sinks: Iterable[Any] = (),
) -> type[BaseHTTPRequestHandler]:
    notification_lock = Lock()

    class RetraceAPIHandler(BaseHTTPRequestHandler):
        server_version = "retrace-api/0.1"

        def handle_one_request(self) -> None:
            self._retrace_trace_id = uuid.uuid4().hex
            self._retrace_response_status = 500
            started = time.perf_counter()
            try:
                super().handle_one_request()
            finally:
                latency_ms = (time.perf_counter() - started) * 1000
                method = str(getattr(self, "command", "") or "")
                path = urlsplit(str(getattr(self, "path", "") or "")).path
                status = int(getattr(self, "_retrace_response_status", 500))
                if method and path:
                    record_api_request(
                        method=method,
                        path=path,
                        status=status,
                        latency_ms=latency_ms,
                        trace_id=self._retrace_trace_id,
                    )
                    logger.info(
                        json.dumps(
                            {
                                "event": "api_request",
                                "trace_id": self._retrace_trace_id,
                                "method": method,
                                "path": path,
                                "status": status,
                                "latency_ms": round(latency_ms, 3),
                            },
                            separators=(",", ":"),
                        )
                    )

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path == "/healthz":
                _json_response(self, 200, {"ok": True})
                return
            if parsed.path == "/api/metrics":
                self._handle_metrics()
                return
            if parsed.path == "/api/replays":
                self._handle_list_replays(parsed.query)
                return
            if parsed.path.startswith("/api/replays/"):
                replay_id = parsed.path.removeprefix("/api/replays/").strip("/")
                self._handle_get_replay(replay_id, parsed.query)
                return
            if parsed.path == "/api/app-errors":
                self._handle_list_app_error_incidents(parsed.query)
                return
            if parsed.path == "/api/app-error-alert-rules":
                self._handle_list_app_error_alert_rules(parsed.query)
                return
            if parsed.path.startswith("/api/app-errors/"):
                incident_id = parsed.path.removeprefix("/api/app-errors/").strip("/")
                self._handle_get_app_error_incident(incident_id, parsed.query)
                return
            if parsed.path == "/api/issues":
                self._handle_list_issues(parsed.query)
                return
            _json_response(self, 404, {"error": "not_found"})

        def do_OPTIONS(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if (
                parsed.path != "/api/sdk/replay"
                and parsed.path != "/api/deploys"
                and parsed.path != "/api/source-maps"
                and parsed.path != "/api/onboarding/hosted"
                and parsed.path != "/api/app-error-alert-rules"
                and parsed.path != "/api/app-errors/prune"
                and not (
                    parsed.path.startswith("/api/app-errors/")
                    and parsed.path.endswith("/lifecycle")
                )
                and not parsed.path.startswith("/api/otel/")
                and not parsed.path.startswith("/api/sentry/")
                and _sentry_ingest_path_parts(parsed.path) is None
                and not parsed.path.startswith("/api/monitoring/webhook")
                and parsed.path != "/api/github/webhook"
            ):
                _json_response(self, 404, {"error": "not_found"})
                return
            self._retrace_response_status = 204
            self.send_response(204)
            trace_id = str(getattr(self, "_retrace_trace_id", "") or "")
            if trace_id:
                self.send_header("X-Retrace-Trace-Id", trace_id)
            _cors_headers(self)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path == "/api/replays/process":
                self._handle_process_replays()
                return
            if parsed.path == "/api/deploys":
                self._handle_record_deploy(parsed.query)
                return
            if parsed.path == "/api/source-maps":
                self._handle_upload_source_map(parsed.query)
                return
            if parsed.path == "/api/app-error-alert-rules":
                self._handle_upsert_app_error_alert_rule(parsed.query)
                return
            if parsed.path == "/api/app-errors/prune":
                self._handle_prune_app_errors(parsed.query)
                return
            if parsed.path == "/api/onboarding/hosted":
                self._handle_hosted_onboarding(parsed.query)
                return
            if parsed.path.startswith("/api/app-errors/") and parsed.path.endswith(
                "/lifecycle"
            ):
                incident_id = parsed.path.removeprefix("/api/app-errors/")[
                    : -len("/lifecycle")
                ].strip("/")
                self._handle_app_error_incident_lifecycle(incident_id, parsed.query)
                return
            if parsed.path in {"/api/otel/v1/logs", "/api/otel/v1/traces"}:
                self._handle_otel_ingest(parsed.path, parsed.query)
                return
            if parsed.path == "/api/monitoring/webhook" or parsed.path.startswith(
                "/api/monitoring/webhook/"
            ):
                self._handle_monitoring_webhook(parsed.path, parsed.query)
                return
            if parsed.path.startswith("/api/sentry/"):
                self._handle_sentry_compat_ingest(parsed.path, parsed.query)
                return
            if _sentry_ingest_path_parts(parsed.path) is not None:
                self._handle_sentry_compat_ingest(parsed.path, parsed.query)
                return
            if parsed.path == "/api/github/webhook":
                self._handle_github_webhook()
                return
            if parsed.path == "/api/sdk/server-replay":
                self._handle_server_replay_ingest(parsed.query)
                return
            if parsed.path != "/api/sdk/replay":
                _json_response(self, 404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            if length < 0:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            if length > MAX_REPLAY_BODY_BYTES:
                _json_response(
                    self,
                    413,
                    {
                        "error": "body_too_large",
                        "message": "Replay batch is too large.",
                    },
                )
                return
            replay_query = _query_dict(parsed.query)
            sdk_key = authenticate_sdk_key(
                store, _extract_replay_sdk_key(self.headers, replay_query)
            )
            if sdk_key is None:
                _json_response(
                    self,
                    401,
                    {
                        "error": "unauthorized",
                        "message": "Missing or invalid SDK key.",
                    },
                )
                return
            decision = _consume_rate_limit(
                store,
                project_id=sdk_key.project_id,
                environment_id=sdk_key.environment_id,
                bucket="replay",
                identity=sdk_key.id,
            )
            if not decision.allowed:
                _rate_limited_response(self, bucket="replay", decision=decision)
                return
            try:
                body = self.rfile.read(length)
                result = ingest_replay_request(
                    store=store,
                    headers={k: v for k, v in self.headers.items()},
                    body=body,
                    query=replay_query,
                )
                _json_response(self, 202, result)
            except ReplayIngestError as exc:
                _json_response(
                    self,
                    exc.status,
                    {"error": exc.code, "message": exc.message},
                )
            except Exception:
                logger.exception("Unhandled replay ingest error")
                _json_response(
                    self,
                    500,
                    {
                        "error": "internal_error",
                        "message": "An internal server error occurred.",
                    },
                )

        def _handle_sentry_compat_ingest(self, path: str, query: str) -> None:
            parts = _sentry_ingest_path_parts(path)
            if parts is None:
                _json_response(self, 404, {"error": "not_found"})
                return
            project_id, endpoint = parts
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            if length < 0:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            if length > MAX_SENTRY_BODY_BYTES:
                _json_response(
                    self,
                    413,
                    {
                        "error": "body_too_large",
                        "message": "Sentry payload is too large.",
                    },
                )
                return
            body = self.rfile.read(length)
            sentry_headers = {k: v for k, v in self.headers.items()}
            sentry_query = _query_dict(query)
            raw_sentry_key = extract_sentry_ingest_key(
                headers=sentry_headers,
                query=sentry_query,
                body=body,
                content_encoding=_header_value(self.headers, "content-encoding"),
            )
            if raw_sentry_key:
                sdk_key = authenticate_sdk_key(store, raw_sentry_key)
                if sdk_key is None:
                    _json_response(
                        self,
                        401,
                        {
                            "error": "unauthorized",
                            "message": "Missing or invalid Sentry SDK key.",
                        },
                    )
                    return
                if project_id and sdk_key.project_id != project_id:
                    _json_response(
                        self,
                        403,
                        {
                            "error": "forbidden",
                            "message": "SDK key does not belong to this project.",
                        },
                    )
                    return
                decision = _consume_rate_limit(
                    store,
                    project_id=sdk_key.project_id,
                    environment_id=sdk_key.environment_id,
                    bucket="sentry",
                    identity=sdk_key.id,
                )
                if not decision.allowed:
                    _rate_limited_response(self, bucket="sentry", decision=decision)
                    return
            try:
                result = ingest_sentry_compat_request(
                    store=store,
                    project_id=project_id,
                    endpoint=endpoint,
                    headers=sentry_headers,
                    body=body,
                    query=sentry_query,
                )
                _dispatch_app_error_notifications(
                    sinks=notification_sinks,
                    store=store,
                    results=result.results,
                    lock=notification_lock,
                )
            except SentryCompatIngestError as exc:
                _json_response(
                    self,
                    exc.status,
                    {"error": exc.code, "message": exc.message},
                )
                return
            except Exception:
                logger.exception("Unhandled Sentry compatibility ingest error")
                _json_response(
                    self,
                    500,
                    {
                        "error": "internal_error",
                        "message": "An internal server error occurred.",
                    },
                )
                return
            _json_response(self, 202, result.to_dict())

        def _handle_monitoring_webhook(self, path: str, query: str) -> None:
            token = _require_service_token(
                self, store, scopes={"monitoring:write", "ingest", "admin"}
            )
            if token is None:
                return
            params = _query_dict(query)
            provider = str(params.get("provider") or "").strip().lower()
            suffix = path.removeprefix("/api/monitoring/webhook").strip("/")
            if suffix:
                provider = suffix.split("/", 1)[0].strip().lower()
            if not provider:
                _json_response(
                    self,
                    400,
                    {
                        "error": "missing_provider",
                        "message": "provider is required in the path or query string.",
                    },
                )
                return
            environment_id = str(params.get("environment_id") or "").strip()
            if not environment_id:
                _json_response(
                    self,
                    400,
                    {
                        "error": "missing_environment_id",
                        "message": "environment_id is required.",
                    },
                )
                return
            decision = _consume_rate_limit(
                store,
                project_id=token.project_id,
                environment_id=environment_id,
                bucket="monitoring",
                identity=f"{token.id}:{provider}",
            )
            if not decision.allowed:
                _rate_limited_response(self, bucket="monitoring", decision=decision)
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            if length < 0:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            if length > MAX_REPLAY_BODY_BYTES:
                _json_response(
                    self,
                    413,
                    {
                        "error": "body_too_large",
                        "message": "Webhook payload is too large.",
                    },
                )
                return
            if length == 0:
                _json_response(self, 400, {"error": "invalid_payload"})
                return
            body = self.rfile.read(length)
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "invalid_json"})
                return
            if not isinstance(payload, dict) or not payload:
                _json_response(self, 400, {"error": "invalid_payload"})
                return
            try:
                result = ingest_monitoring_webhook(
                    store=store,
                    project_id=token.project_id,
                    environment_id=environment_id,
                    provider=provider,
                    payload=payload,
                )
                _dispatch_app_error_notifications(
                    sinks=notification_sinks,
                    store=store,
                    results=[result],
                    lock=notification_lock,
                )
            except Exception:
                logger.exception("Unhandled monitoring webhook ingest error")
                _json_response(
                    self,
                    500,
                    {
                        "error": "internal_error",
                        "message": "An internal server error occurred.",
                    },
                )
                return
            _json_response(self, 202, result.to_dict())

        def _handle_github_webhook(self) -> None:
            if not github_webhook_secret.strip():
                _json_response(
                    self,
                    503,
                    {
                        "error": "github_app_not_configured",
                        "message": "GitHub App webhook secret is not configured.",
                    },
                )
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            if length <= 0:
                _json_response(self, 400, {"error": "invalid_payload"})
                return
            if length > MAX_REPLAY_BODY_BYTES:
                _json_response(self, 413, {"error": "payload_too_large"})
                return
            body = self.rfile.read(length)
            try:
                result = handle_github_webhook(
                    store=store,
                    body=body,
                    headers={k: v for k, v in self.headers.items()},
                    webhook_secret=github_webhook_secret,
                )
            except GitHubWebhookError as exc:
                _json_response(
                    self,
                    exc.status,
                    {"error": exc.code, "message": exc.message},
                )
                return
            except Exception:
                logger.exception("Unhandled GitHub webhook error")
                _json_response(
                    self,
                    500,
                    {
                        "error": "internal_error",
                        "message": "An internal server error occurred.",
                    },
                )
                return
            _json_response(self, 202 if result.accepted else 200, result.to_dict())

        def _handle_record_deploy(self, query: str) -> None:
            token = _require_service_token(
                self, store, scopes={"deploy:write", "ingest", "admin"}
            )
            if token is None:
                return
            params = _query_dict(query)
            environment_id = str(params.get("environment_id") or "").strip()
            if not environment_id:
                _json_response(self, 400, {"error": "missing_environment_id"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            if length <= 0:
                _json_response(self, 400, {"error": "invalid_payload"})
                return
            if length > MAX_REPLAY_BODY_BYTES:
                _json_response(self, 413, {"error": "payload_too_large"})
                return
            body = self.rfile.read(length)
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "invalid_json"})
                return
            if not isinstance(payload, dict) or not payload:
                _json_response(self, 400, {"error": "invalid_payload"})
                return
            sha = str(payload.get("sha") or payload.get("commit_sha") or "").strip()
            if not sha:
                _json_response(self, 400, {"error": "missing_sha"})
                return
            changed_files = payload.get("changed_files", payload.get("changedFiles"))
            clean_changed_files: list[str] | None = None
            if changed_files is not None:
                if not isinstance(changed_files, list):
                    _json_response(self, 400, {"error": "invalid_changed_files"})
                    return
                clean_changed_files = [str(item) for item in changed_files]
            try:
                deployed_at_ms = int(payload.get("deployed_at_ms") or 0)
            except (TypeError, ValueError):
                _json_response(self, 400, {"error": "invalid_deployed_at_ms"})
                return
            try:
                deploy = record_deploy(
                    store=store,
                    project_id=token.project_id,
                    environment_id=environment_id,
                    sha=sha,
                    branch=str(payload.get("branch") or ""),
                    author=str(payload.get("author") or ""),
                    deployed_at_ms=deployed_at_ms,
                    changed_files=clean_changed_files,
                    metadata=(
                        dict(payload.get("metadata"))
                        if isinstance(payload.get("metadata"), dict)
                        else None
                    ),
                )
                correlations = correlate_recent_failures_to_deploys(
                    store=store,
                    project_id=token.project_id,
                    environment_id=environment_id,
                )
                correlations = [
                    item for item in correlations if item.deploy_sha == deploy.sha
                ]
            except Exception:
                logger.exception("Unhandled deploy ingest error")
                _json_response(
                    self,
                    500,
                    {
                        "error": "internal_error",
                        "message": "An internal server error occurred.",
                    },
                )
                return
            _json_response(
                self,
                202,
                {
                    "deploy": {
                        "id": deploy.id,
                        "public_id": deploy.public_id,
                        "sha": deploy.sha,
                        "branch": deploy.branch,
                        "author": deploy.author,
                        "deployed_at_ms": deploy.deployed_at_ms,
                        "changed_files": deploy.changed_files,
                    },
                    "correlated_failures": [
                        {"failure_id": item.failure_id, "deploy_sha": item.deploy_sha}
                        for item in correlations
                    ],
                },
            )

        def _handle_upload_source_map(self, query: str) -> None:
            token = _require_service_token(
                self, store, scopes={"source_maps:write", "ingest", "admin"}
            )
            if token is None:
                return
            params = _query_dict(query)
            environment_id = str(params.get("environment_id") or "").strip()
            if not environment_id:
                _json_response(self, 400, {"error": "missing_environment_id"})
                return
            decision = _consume_rate_limit(
                store,
                project_id=token.project_id,
                environment_id=environment_id,
                bucket="source_maps",
                identity=token.id,
            )
            if not decision.allowed:
                _rate_limited_response(self, bucket="source_maps", decision=decision)
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            if length <= 0:
                _json_response(self, 400, {"error": "invalid_payload"})
                return
            if length > MAX_REPLAY_BODY_BYTES:
                _json_response(self, 413, {"error": "payload_too_large"})
                return
            body = self.rfile.read(length)
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "invalid_json"})
                return
            if not isinstance(payload, dict) or not payload:
                _json_response(self, 400, {"error": "invalid_payload"})
                return
            release = str(payload.get("release") or "").strip()
            artifact_url = str(
                payload.get("artifact_url")
                or payload.get("artifactUrl")
                or payload.get("url")
                or ""
            ).strip()
            source_map = payload.get("source_map") or payload.get("sourceMap")
            if not release:
                _json_response(self, 400, {"error": "missing_release"})
                return
            if not artifact_url:
                _json_response(self, 400, {"error": "missing_artifact_url"})
                return
            if not isinstance(source_map, dict) or not source_map:
                _json_response(self, 400, {"error": "invalid_source_map"})
                return
            try:
                row = upload_source_map(
                    store=store,
                    project_id=token.project_id,
                    environment_id=environment_id,
                    release=release,
                    dist=str(payload.get("dist") or ""),
                    artifact_url=artifact_url,
                    source_map=source_map,
                )
            except ValueError as exc:
                _json_response(self, 400, {"error": "invalid_source_map", "message": str(exc)})
                return
            except Exception:
                logger.exception("Unhandled source map upload error")
                _json_response(
                    self,
                    500,
                    {
                        "error": "internal_error",
                        "message": "An internal server error occurred.",
                    },
                )
                return
            _json_response(
                self,
                202,
                {
                    "source_map": {
                        "id": row.id,
                        "public_id": row.public_id,
                        "release": row.release,
                        "dist": row.dist,
                        "artifact_url": row.artifact_url,
                    }
                },
            )

        def _handle_otel_ingest(self, path: str, query: str) -> None:
            token = _require_service_token(
                self, store, scopes={"otel:write", "ingest", "admin"}
            )
            if token is None:
                return
            params = _query_dict(query)
            environment_id = str(params.get("environment_id") or "").strip()
            if not environment_id:
                _json_response(self, 400, {"error": "missing_environment_id"})
                return
            # Rate limit *before* reading the body — a flooded client
            # shouldn't get to spend our bandwidth uploading a payload
            # we're about to reject. Identity is the service-token id
            # so different tokens for the same project don't share a
            # bucket (operators sometimes split tokens per host).
            decision = _consume_rate_limit(
                store,
                project_id=token.project_id,
                environment_id=environment_id,
                bucket="otel",
                identity=token.id,
            )
            if not decision.allowed:
                _rate_limited_response(self, bucket="otel", decision=decision)
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            if length <= 0:
                _json_response(self, 400, {"error": "invalid_payload"})
                return
            if length > MAX_REPLAY_BODY_BYTES:
                _json_response(self, 413, {"error": "payload_too_large"})
                return
            body = self.rfile.read(length)
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "invalid_json"})
                return
            if not isinstance(payload, dict) or not payload:
                _json_response(self, 400, {"error": "invalid_payload"})
                return
            try:
                result = (
                    ingest_otel_logs(
                        store=store,
                        project_id=token.project_id,
                        environment_id=environment_id,
                        payload=payload,
                    )
                    if path.endswith("/logs")
                    else ingest_otel_traces(
                        store=store,
                        project_id=token.project_id,
                        environment_id=environment_id,
                        payload=payload,
                    )
                )
            except Exception:
                logger.exception("Unhandled OpenTelemetry ingest error")
                _json_response(
                    self,
                    500,
                    {
                        "error": "internal_error",
                        "message": "An internal server error occurred.",
                    },
                )
                return
            _json_response(self, 202, result.to_dict())

        def _handle_server_replay_ingest(self, query: str) -> None:
            """P3.6 (scaffold) — accept a single server-side replay
            session record.

            Payload shape (JSON):
                {
                    "session_id": "...",
                    "request": {
                        "method": "GET",
                        "path": "/api/checkout",
                        "headers": {...},
                        "body": "..."
                    },
                    "response": {
                        "status": 500,
                        "headers": {...}
                    },
                    "rendered_html": "...",   // optional snippet
                    "runtime": "node-20",     // optional
                    "occurred_at_ms": 0,      // optional; defaults to now
                    "error_summary": "...",   // optional
                    "metadata": {...}         // optional
                }

            The capture middleware that produces this payload is
            deferred — see `docs/roadmap.md` P3.6. This endpoint
            exists today so that middleware has a defined seam to
            ship against, and so the ingest path can be hardened /
            rate-limited / tested before SDK work begins.
            """
            replay_query = _query_dict(query)
            sdk_key = authenticate_sdk_key(
                store, _extract_replay_sdk_key(self.headers, replay_query)
            )
            if sdk_key is None:
                _json_response(
                    self,
                    401,
                    {
                        "error": "unauthorized",
                        "message": "Missing or invalid SDK key.",
                    },
                )
                return
            decision = _consume_rate_limit(
                store,
                project_id=sdk_key.project_id,
                environment_id=sdk_key.environment_id,
                bucket="server_replay",
                identity=sdk_key.id,
            )
            if not decision.allowed:
                _rate_limited_response(self, bucket="server_replay", decision=decision)
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            if length <= 0:
                _json_response(self, 400, {"error": "invalid_payload"})
                return
            if length > MAX_REPLAY_BODY_BYTES:
                _json_response(self, 413, {"error": "payload_too_large"})
                return
            body = self.rfile.read(length)
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                _json_response(self, 400, {"error": "invalid_json"})
                return
            if not isinstance(payload, dict):
                _json_response(self, 400, {"error": "invalid_payload"})
                return
            request_block = payload.get("request") or {}
            response_block = payload.get("response") or {}
            if not isinstance(request_block, dict) or not isinstance(
                response_block, dict
            ):
                _json_response(
                    self,
                    400,
                    {
                        "error": "invalid_payload",
                        "message": "`request` and `response` must be objects.",
                    },
                )
                return
            try:
                row_id = store.insert_server_replay_session(
                    project_id=sdk_key.project_id,
                    environment_id=sdk_key.environment_id,
                    session_id=str(payload.get("session_id") or ""),
                    request_method=str(request_block.get("method") or ""),
                    request_path=str(request_block.get("path") or ""),
                    request_headers=request_block.get("headers") or {},
                    request_body_text=str(request_block.get("body") or ""),
                    response_status=int(response_block.get("status") or 0),
                    response_headers=response_block.get("headers") or {},
                    rendered_html_snippet=str(payload.get("rendered_html") or ""),
                    runtime=str(payload.get("runtime") or ""),
                    occurred_at_ms=int(payload.get("occurred_at_ms") or 0),
                    error_summary=str(payload.get("error_summary") or ""),
                    metadata=payload.get("metadata") or {},
                )
            except (TypeError, ValueError) as exc:
                _json_response(
                    self,
                    400,
                    {"error": "invalid_payload", "message": str(exc)},
                )
                return
            except Exception:
                logger.exception("Unhandled server-replay ingest error")
                _json_response(
                    self,
                    500,
                    {
                        "error": "internal_error",
                        "message": "An internal server error occurred.",
                    },
                )
                return
            _json_response(self, 202, {"id": row_id, "accepted": 1})

        def _handle_list_replays(self, query: str) -> None:
            token = _require_service_token(
                self, store, scopes={"replay:read", "mcp:read", "admin"}
            )
            if token is None:
                return
            params = _query_dict(query)
            environment_id = str(params.get("environment_id") or "").strip()
            if not environment_id:
                _json_response(
                    self,
                    400,
                    {
                        "error": "missing_environment_id",
                        "message": "environment_id is required.",
                    },
                )
                return
            status = str(params.get("status") or "").strip() or None
            try:
                limit = int(params.get("limit") or "100")
            except ValueError:
                _json_response(self, 400, {"error": "invalid_limit"})
                return
            rows = store.list_replay_sessions(
                project_id=token.project_id,
                environment_id=environment_id,
                status=status,
                limit=limit,
            )
            _json_response(
                self,
                200,
                {
                    "project_id": token.project_id,
                    "environment_id": environment_id,
                    "sessions": [_row_dict(r) for r in rows],
                },
            )

        def _handle_metrics(self) -> None:
            token = _require_service_token(
                self, store, scopes={"admin", "mcp:read"}
            )
            if token is None:
                return
            _json_response(self, 200, collect_local_observability(store).to_dict())

        def _handle_process_replays(self) -> None:
            token = _require_service_token(
                self, store, scopes={"replay:write", "admin"}
            )
            if token is None:
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            body = self.rfile.read(max(0, length)) if length else b"{}"
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "invalid_json"})
                return
            try:
                limit = int(payload.get("limit") or 25)
            except (TypeError, ValueError):
                _json_response(self, 400, {"error": "invalid_limit"})
                return
            result = process_queued_replay_jobs(
                store=store,
                limit=limit,
                project_id=token.project_id,
                enricher=enricher,
            )
            _json_response(
                self,
                200,
                {
                    "jobs_seen": result.jobs_seen,
                    "jobs_processed": result.jobs_processed,
                    "jobs_failed": result.jobs_failed,
                    "sessions_processed": result.sessions_processed,
                    "issues_created_or_updated": result.issues_created_or_updated,
                    "project_id": token.project_id,
                },
            )

        def _handle_get_replay(self, replay_id: str, query: str) -> None:
            token = _require_service_token(
                self, store, scopes={"replay:read", "mcp:read", "admin"}
            )
            if token is None:
                return
            params = _query_dict(query)
            environment_id = str(params.get("environment_id") or "").strip()
            if not environment_id:
                _json_response(self, 400, {"error": "missing_environment_id"})
                return
            replay_id = replay_id.strip()
            if not replay_id:
                _json_response(self, 404, {"error": "not_found"})
                return
            playback = store.get_replay_playback(
                project_id=token.project_id,
                environment_id=environment_id,
                replay_id=replay_id,
            )
            if playback is None:
                _json_response(self, 404, {"error": "not_found"})
                return
            _json_response(
                self,
                200,
                {
                    "session": _row_dict(playback.session),
                    "batches": [_row_dict(b) for b in playback.batches],
                    "events": playback.events,
                },
            )

        def _handle_list_issues(self, query: str) -> None:
            token = _require_service_token(
                self, store, scopes={"issues:read", "mcp:read", "admin"}
            )
            if token is None:
                return
            params = _query_dict(query)
            environment_id = str(params.get("environment_id") or "").strip()
            if not environment_id:
                _json_response(self, 400, {"error": "missing_environment_id"})
                return
            status = str(params.get("status") or "").strip() or None
            rows = store.list_replay_issues(
                project_id=token.project_id,
                environment_id=environment_id,
                status=status,
            )
            _json_response(
                self,
                200,
                {
                    "project_id": token.project_id,
                    "environment_id": environment_id,
                    "issues": [_row_dict(r) for r in rows],
                },
            )

        def _handle_list_app_error_incidents(self, query: str) -> None:
            token = _require_service_token(
                self,
                store,
                scopes={"app_errors:read", "issues:read", "mcp:read", "admin"},
            )
            if token is None:
                return
            params = _query_dict(query)
            environment_id = str(params.get("environment_id") or "").strip()
            if not environment_id:
                _json_response(self, 400, {"error": "missing_environment_id"})
                return
            status = str(params.get("status") or "").strip() or None
            try:
                limit = int(params.get("limit") or "100")
            except ValueError:
                _json_response(self, 400, {"error": "invalid_limit"})
                return
            incidents = store.list_incidents(
                project_id=token.project_id,
                environment_id=environment_id,
                status=status,
                limit=limit,
            )
            failures_by_incident = store.list_incident_failures_for_incidents(
                incident_ids=[incident.id for incident in incidents]
            )
            _json_response(
                self,
                200,
                {
                    "project_id": token.project_id,
                    "environment_id": environment_id,
                    "incidents": [
                        _incident_api_dict(
                            store=store,
                            incident=incident,
                            failures=failures_by_incident.get(incident.id, []),
                        )
                        for incident in incidents
                    ],
                },
            )

        def _handle_list_app_error_alert_rules(self, query: str) -> None:
            token = _require_service_token(
                self,
                store,
                scopes={"app_errors:read", "admin"},
            )
            if token is None:
                return
            params = _query_dict(query)
            environment_id = str(params.get("environment_id") or "").strip()
            if not environment_id:
                _json_response(self, 400, {"error": "missing_environment_id"})
                return
            try:
                limit = int(params.get("limit") or "100")
                offset = int(params.get("offset") or "0")
            except ValueError:
                _json_response(self, 400, {"error": "invalid_pagination"})
                return
            rules = store.list_app_error_alert_rules(
                project_id=token.project_id,
                environment_id=environment_id,
                limit=limit,
                offset=offset,
            )
            _json_response(
                self,
                200,
                {
                    "project_id": token.project_id,
                    "environment_id": environment_id,
                    "limit": max(1, min(limit, 500)),
                    "offset": max(0, offset),
                    "rules": [_alert_rule_api_dict(rule) for rule in rules],
                },
            )

        def _handle_upsert_app_error_alert_rule(self, query: str) -> None:
            token = _require_service_token(
                self,
                store,
                scopes={"app_errors:write", "admin"},
            )
            if token is None:
                return
            params = _query_dict(query)
            environment_id = str(params.get("environment_id") or "").strip()
            if not environment_id:
                _json_response(self, 400, {"error": "missing_environment_id"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            if length <= 0:
                _json_response(self, 400, {"error": "invalid_payload"})
                return
            if length > MAX_REPLAY_BODY_BYTES:
                _json_response(self, 413, {"error": "payload_too_large"})
                return
            body = self.rfile.read(length)
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "invalid_json"})
                return
            if not isinstance(payload, dict) or not payload:
                _json_response(self, 400, {"error": "invalid_payload"})
                return
            try:
                rule_id = store.upsert_app_error_alert_rule(
                    project_id=token.project_id,
                    environment_id=environment_id,
                    name=str(payload.get("name") or ""),
                    enabled=bool(payload.get("enabled", True)),
                    precedence=int(payload.get("precedence") or 0),
                    action=str(payload.get("action") or "alert"),
                    min_severity=str(payload.get("min_severity") or ""),
                    provider=str(payload.get("provider") or ""),
                    title_contains=str(payload.get("title_contains") or ""),
                    fingerprint_contains=str(payload.get("fingerprint_contains") or ""),
                    route_contains=str(payload.get("route_contains") or ""),
                    metadata=(
                        dict(payload.get("metadata"))
                        if isinstance(payload.get("metadata"), dict)
                        else None
                    ),
                )
            except ValueError as exc:
                _json_response(self, 400, {"error": "invalid_alert_rule", "message": str(exc)})
                return
            rule = next(
                (
                    item
                    for item in store.list_app_error_alert_rules(
                        project_id=token.project_id,
                        environment_id=environment_id,
                        enabled=None,
                    )
                    if item.id == rule_id
                ),
                None,
            )
            _json_response(
                self,
                202,
                {"rule": _alert_rule_api_dict(rule) if rule is not None else {"id": rule_id}},
            )

        def _handle_hosted_onboarding(self, query: str) -> None:
            token = _require_service_token(
                self,
                store,
                scopes={"admin"},
            )
            if token is None:
                return
            params = _query_dict(query)
            environment_id = str(params.get("environment_id") or "").strip()
            if not environment_id:
                _json_response(self, 400, {"error": "missing_environment_id"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            if length < 0:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            if length > 64 * 1024:
                _json_response(self, 413, {"error": "payload_too_large"})
                return
            payload: dict[str, Any] = {}
            if length:
                try:
                    decoded = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    _json_response(self, 400, {"error": "invalid_json"})
                    return
                if not isinstance(decoded, dict):
                    _json_response(self, 400, {"error": "invalid_payload"})
                    return
                payload = decoded
            raw_scopes = payload.get("service_token_scopes")
            if raw_scopes is None:
                service_token_scopes = list(HOSTED_ONBOARDING_SCOPES)
            elif isinstance(raw_scopes, list) and all(
                isinstance(item, str) and item.strip() for item in raw_scopes
            ):
                service_token_scopes = [str(item).strip() for item in raw_scopes]
            else:
                _json_response(self, 400, {"error": "invalid_service_token_scopes"})
                return
            sdk = create_sdk_key(
                store,
                project_id=token.project_id,
                environment_id=environment_id,
                name=str(payload.get("sdk_key_name") or "Hosted browser SDK"),
            )
            service = create_service_token(
                store,
                project_id=token.project_id,
                name=str(payload.get("service_token_name") or "Hosted onboarding"),
                scopes=service_token_scopes,
            )
            manifest = _hosted_onboarding_manifest(
                base_url=str(payload.get("api_base_url") or "http://127.0.0.1:8788"),
                project_id=token.project_id,
                environment_id=environment_id,
                sdk_key=sdk.key,
                service_token=service.token,
                service_token_scopes=service.scopes,
                release=str(payload.get("release") or "$GITHUB_SHA"),
                artifact_url=str(
                    payload.get("artifact_url")
                    or "https://cdn.example.com/assets/app.min.js"
                ),
            )
            _json_response(self, 201, {"onboarding": manifest})

        def _handle_prune_app_errors(self, query: str) -> None:
            token = _require_service_token(
                self,
                store,
                scopes={"app_errors:write", "admin"},
            )
            if token is None:
                return
            params = _query_dict(query)
            environment_id = str(params.get("environment_id") or "").strip()
            if not environment_id:
                _json_response(self, 400, {"error": "missing_environment_id"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            if length < 0:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            if length > 64 * 1024:
                _json_response(self, 413, {"error": "payload_too_large"})
                return
            payload: dict[str, Any] = {}
            if length:
                try:
                    decoded = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    _json_response(self, 400, {"error": "invalid_json"})
                    return
                if not isinstance(decoded, dict):
                    _json_response(self, 400, {"error": "invalid_payload"})
                    return
                payload = decoded
            try:
                result = store.prune_app_error_retention(
                    project_id=token.project_id,
                    environment_id=environment_id,
                    failure_retention_days=_optional_int(
                        payload, "failure_retention_days", 90
                    ),
                    evidence_retention_days=_optional_int(
                        payload, "evidence_retention_days", 90
                    ),
                    source_map_retention_days=_optional_int(
                        payload, "source_map_retention_days", 30
                    ),
                    rate_limit_retention_hours=_optional_int(
                        payload, "rate_limit_retention_hours", 48
                    ),
                    dry_run=_optional_bool(payload, "dry_run"),
                )
            except (TypeError, ValueError) as exc:
                _json_response(self, 400, {"error": "invalid_retention", "message": str(exc)})
                return
            except Exception:
                logger.exception("Unhandled app-error retention prune error")
                _json_response(
                    self,
                    500,
                    {
                        "error": "internal_error",
                        "message": "An internal server error occurred.",
                    },
                )
                return
            _json_response(self, 202, {"retention": _retention_result_api_dict(result)})

        def _handle_app_error_incident_lifecycle(
            self, incident_id: str, query: str
        ) -> None:
            token = _require_service_token(
                self,
                store,
                scopes={"app_errors:write", "admin"},
            )
            if token is None:
                return
            params = _query_dict(query)
            environment_id = str(params.get("environment_id") or "").strip()
            if not environment_id:
                _json_response(self, 400, {"error": "missing_environment_id"})
                return
            incident_id = incident_id.strip()
            if not incident_id:
                _json_response(self, 404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                _json_response(self, 400, {"error": "invalid_content_length"})
                return
            if length <= 0:
                _json_response(self, 400, {"error": "invalid_payload"})
                return
            if length > 64 * 1024:
                _json_response(self, 413, {"error": "payload_too_large"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "invalid_json"})
                return
            if not isinstance(payload, dict):
                _json_response(self, 400, {"error": "invalid_payload"})
                return
            action = str(payload.get("action") or "").strip().lower()
            status = str(payload.get("status") or "").strip().lower()
            action_statuses = {
                "resolve": "resolved",
                "resolved": "resolved",
                "ignore": "ignored",
                "ignored": "ignored",
                "reopen": "open",
                "open": "open",
                "triage": "triaged",
                "triaged": "triaged",
                "investigate": "investigating",
                "investigating": "investigating",
            }
            if action and not status:
                status = action_statuses.get(action, "")
            if not status:
                _json_response(self, 400, {"error": "missing_status"})
                return
            metadata = payload.get("metadata")
            if metadata is not None and not isinstance(metadata, dict):
                _json_response(self, 400, {"error": "invalid_metadata"})
                return
            try:
                incident = store.transition_app_error_incident(
                    project_id=token.project_id,
                    environment_id=environment_id,
                    incident_id=incident_id,
                    status=status,
                    actor_type=str(payload.get("actor_type") or "service_token"),
                    actor_id=str(payload.get("actor_id") or token.name or token.id),
                    reason=str(payload.get("reason") or ""),
                    metadata=dict(metadata or {}),
                )
            except ValueError as exc:
                message = str(exc)
                if "unknown incident_id" in message:
                    _json_response(self, 404, {"error": "not_found"})
                    return
                _json_response(
                    self,
                    400,
                    {"error": "invalid_lifecycle_transition", "message": message},
                )
                return
            failures = store.list_incident_failures(incident_id=incident.id)
            events = store.list_incident_lifecycle_events(incident_id=incident.id)
            _json_response(
                self,
                202,
                {
                    "project_id": token.project_id,
                    "environment_id": environment_id,
                    "incident": _incident_api_dict(
                        store=store,
                        incident=incident,
                        failures=failures,
                    ),
                    "failures": [
                        _failure_api_dict(failure, include_metadata=False)
                        for failure in failures
                    ],
                    "lifecycle_events": [
                        _incident_lifecycle_event_api_dict(event) for event in events
                    ],
                },
            )

        def _handle_get_app_error_incident(self, incident_id: str, query: str) -> None:
            token = _require_service_token(
                self,
                store,
                scopes={"app_errors:read", "issues:read", "mcp:read", "admin"},
            )
            if token is None:
                return
            params = _query_dict(query)
            environment_id = str(params.get("environment_id") or "").strip()
            if not environment_id:
                _json_response(self, 400, {"error": "missing_environment_id"})
                return
            incident_id = incident_id.strip()
            if not incident_id:
                _json_response(self, 404, {"error": "not_found"})
                return
            include_sensitive = str(
                params.get("include_sensitive") or "false"
            ).strip().lower() in {"1", "true", "yes"}
            if include_sensitive and not {"app_errors:read", "admin"}.intersection(
                set(token.scopes)
            ):
                _json_response(
                    self,
                    403,
                    {
                        "error": "forbidden",
                        "message": "include_sensitive=true requires app_errors:read scope.",
                    },
                )
                return
            try:
                detail = get_incident_detail(
                    store=store,
                    incident_id=incident_id,
                    include_sensitive_evidence=include_sensitive,
                )
            except ValueError:
                _json_response(self, 404, {"error": "not_found"})
                return
            if (
                detail.incident.project_id != token.project_id
                or detail.incident.environment_id != environment_id
            ):
                _json_response(self, 404, {"error": "not_found"})
                return
            _json_response(
                self,
                200,
                {
                    "project_id": token.project_id,
                    "environment_id": environment_id,
                    "incident": _incident_api_dict(
                        store=store,
                        incident=detail.incident,
                        failures=detail.failures,
                    ),
                    "failures": [
                        _failure_api_dict(failure) for failure in detail.failures
                    ],
                    "evidence": [
                        _evidence_api_dict(evidence) for evidence in detail.evidence
                    ],
                    "lifecycle_events": [
                        _incident_lifecycle_event_api_dict(event)
                        for event in store.list_incident_lifecycle_events(
                            incident_id=detail.incident.id
                        )
                    ],
                    "repair_task": (
                        _repair_task_api_dict(detail.repair_task)
                        if detail.repair_task is not None
                        else None
                    ),
                },
            )

        def log_message(self, format: str, *args: object) -> None:
            click.echo(f"{self.address_string()} - {format % args}", err=True)

    return RetraceAPIHandler


