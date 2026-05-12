"""Alert fan-out: post fired alerts to Slack / Discord / PagerDuty /
generic webhook destinations.

Wiring:

  monitoring_ingest.ingest_monitoring_webhook
    → alert_rules.evaluate_app_error_alert_rules → AlertRuleDecision
    → if decision.action == "alert": dispatch_alert(...)

`dispatch_alert()` is best-effort. It never raises into the caller —
network failures get logged + persisted on the `alert_dispatches` row
but don't roll back the failure/incident write.

Per-route dedup uses the `alert_dispatches` table: we look up the
last successful send for `(route_id, fingerprint)` within
`route.dedup_window_seconds`. A match suppresses the new send and
records a `status="deduped"` row for audit.

Transport: stdlib `urllib.request` only. No `httpx`/`requests`
dependency on the server side either — matches the Python SDK
philosophy.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from retrace.alert_rules import AlertRuleDecision
from retrace.monitoring_ingest import MonitoringAlert
from retrace.storage import AlertRouteRow, Storage


log = logging.getLogger(__name__)


_TARGET_KINDS = ("slack", "discord", "pagerduty", "webhook")

_SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}

_DEFAULT_HTTP_TIMEOUT = 5.0


@dataclass(frozen=True)
class DispatchResult:
    route_id: str
    route_name: str
    target_kind: str
    status: str  # "sent" | "deduped" | "failed" | "skipped"
    error: str = ""
    status_code: int = 0
    payload: dict = None  # type: ignore[assignment]

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "route_name": self.route_name,
            "target_kind": self.target_kind,
            "status": self.status,
            "error": self.error,
            "status_code": self.status_code,
        }


def dispatch_alert(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    alert: MonitoringAlert,
    decision: AlertRuleDecision,
    timeout: float = _DEFAULT_HTTP_TIMEOUT,
    only_route_ids: Optional[list[str]] = None,
    _post=None,
) -> list[DispatchResult]:
    """Fan out one fired alert to every matching enabled route.

    `_post` is a test seam — when supplied, dispatch calls
    `_post(url, headers, body, timeout)` instead of the real HTTP
    request. The fourth `timeout` argument matches `_real_post`'s
    signature; seams that omit it will hit `TypeError`.

    `only_route_ids` (P1.1 follow-up): when set, dispatch filters the
    matched routes to those id's BEFORE any send — used by
    `retrace monitor route test` so a synthetic alert can't leak to
    other routes.
    """
    if decision.action != "alert" or decision.state == "suppressed":
        return []

    routes = store.list_alert_routes(
        project_id=project_id,
        environment_id=environment_id,
        enabled=True,
        rule_name=decision.rule_name or "",
    )
    if only_route_ids is not None:
        allowed = set(only_route_ids)
        routes = [r for r in routes if r.id in allowed]
    if not routes:
        return []

    results: list[DispatchResult] = []
    sender = _post or _real_post
    for route in routes:
        result = _dispatch_one(
            store=store,
            project_id=project_id,
            environment_id=environment_id,
            alert=alert,
            route=route,
            timeout=timeout,
            sender=sender,
        )
        results.append(result)
    return results


def _dispatch_one(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    alert: MonitoringAlert,
    route: AlertRouteRow,
    timeout: float,
    sender,
) -> DispatchResult:
    # Severity gate: if the route requires `>= high` and the alert is
    # medium, skip. Persist a `skipped` row for audit parity — the
    # operator still wants to see "this dispatch decided not to
    # send" in `list_recent_alert_dispatches`. (CodeRabbit Major
    # catch on PR #131.)
    if route.min_severity and _severity_score(alert.severity) < _severity_score(
        route.min_severity
    ):
        store.record_alert_dispatch(
            route_id=route.id,
            project_id=project_id,
            environment_id=environment_id,
            fingerprint=alert.fingerprint,
            status="skipped",
            target_kind=route.target_kind,
            target_url=route.target_url,
            payload={},
            error=f"below min_severity {route.min_severity!r}",
        )
        return DispatchResult(
            route_id=route.id,
            route_name=route.name,
            target_kind=route.target_kind,
            status="skipped",
            error=f"below min_severity {route.min_severity!r}",
            payload={},
        )

    # Dedup gate: was this exact fingerprint just sent successfully?
    recent = store.recent_alert_dispatch_for(
        route_id=route.id,
        fingerprint=alert.fingerprint,
        within_seconds=route.dedup_window_seconds,
    )
    if recent is not None:
        store.record_alert_dispatch(
            route_id=route.id,
            project_id=project_id,
            environment_id=environment_id,
            fingerprint=alert.fingerprint,
            status="deduped",
            target_kind=route.target_kind,
            target_url=route.target_url,
            payload={},
            error=f"deduped against dispatch id={recent['id']}",
        )
        return DispatchResult(
            route_id=route.id,
            route_name=route.name,
            target_kind=route.target_kind,
            status="deduped",
            payload={},
        )

    # Build the per-target payload + send. Payload build is wrapped in
    # the same try as the send so a `_build_request` / `json.dumps`
    # failure still records a `failed` row and preserves the
    # non-fatal-tail-step contract. (CodeRabbit Critical catch on
    # PR #131.)
    status_code = 0
    error = ""
    json_payload: dict[str, Any] = {}
    try:
        url, headers, body, json_payload = _build_request(route, alert)
        status_code = sender(url, headers, body, timeout)
        status = "sent"
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code or 0)
        error = f"HTTP {exc.code}: {exc.reason}"[:500]
        status = "failed"
    except Exception as exc:  # pragma: no cover - defensive
        error = str(exc)[:500]
        status = "failed"
    store.record_alert_dispatch(
        route_id=route.id,
        project_id=project_id,
        environment_id=environment_id,
        fingerprint=alert.fingerprint,
        status=status,
        target_kind=route.target_kind,
        target_url=route.target_url,
        payload=json_payload,
        error=error,
    )
    if status == "failed":
        log.warning(
            "alert_dispatch: route=%s target=%s failed: %s",
            route.name, route.target_kind, error,
        )
    return DispatchResult(
        route_id=route.id,
        route_name=route.name,
        target_kind=route.target_kind,
        status=status,
        error=error,
        status_code=status_code,
        payload=json_payload,
    )


# ---------------------------------------------------------------------------
# Per-target payload builders
# ---------------------------------------------------------------------------


def _build_request(
    route: AlertRouteRow,
    alert: MonitoringAlert,
) -> tuple[str, dict[str, str], bytes, dict[str, Any]]:
    """Return `(url, headers, body, json_payload)` for this target."""
    if route.target_kind == "slack":
        payload = _slack_payload(alert)
    elif route.target_kind == "discord":
        payload = _discord_payload(alert)
    elif route.target_kind == "pagerduty":
        payload = _pagerduty_payload(alert, route)
    elif route.target_kind == "webhook":
        payload = _generic_webhook_payload(alert)
    else:  # pragma: no cover - schema enforces
        raise ValueError(f"unsupported target_kind: {route.target_kind!r}")
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "User-Agent": "retrace-alert-dispatch/0.1",
    }
    return route.target_url, headers, body, payload


def _slack_payload(alert: MonitoringAlert) -> dict[str, Any]:
    """Slack incoming-webhook block-kit payload.

    Compact card: title, summary line, fingerprint + severity. We
    don't link out — different deployments host their dashboard at
    different URLs and we don't have it in the alert.
    """
    severity_label = alert.severity.upper() if alert.severity else "MEDIUM"
    fields = [
        {"type": "mrkdwn", "text": f"*Severity*\n{severity_label}"},
        {"type": "mrkdwn", "text": f"*Provider*\n{alert.provider or 'generic'}"},
    ]
    if alert.fingerprint:
        fields.append({"type": "mrkdwn", "text": f"*Fingerprint*\n`{alert.fingerprint[:48]}`"})
    return {
        "text": f"Retrace alert: {alert.title or 'unknown'}",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": (alert.title or "Retrace alert")[:150]},
            },
            *(
                [{
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": alert.summary[:2900]},
                }] if alert.summary else []
            ),
            {"type": "section", "fields": fields},
        ],
    }


def _discord_payload(alert: MonitoringAlert) -> dict[str, Any]:
    """Discord webhook embed payload."""
    severity_color = {
        "low": 0x95a5a6,
        "medium": 0xf1c40f,
        "high": 0xe67e22,
        "critical": 0xe74c3c,
    }.get(alert.severity.lower() if alert.severity else "medium", 0xf1c40f)
    return {
        "content": f"Retrace alert: **{(alert.title or 'unknown')[:200]}**",
        "embeds": [
            {
                "title": (alert.title or "Retrace alert")[:256],
                "description": (alert.summary or "")[:4000],
                "color": severity_color,
                "fields": [
                    {"name": "Severity", "value": alert.severity or "medium", "inline": True},
                    {"name": "Provider", "value": alert.provider or "generic", "inline": True},
                    {
                        "name": "Fingerprint",
                        "value": (alert.fingerprint or "")[:1024] or "—",
                        "inline": False,
                    },
                ],
            }
        ],
    }


def _pagerduty_payload(alert: MonitoringAlert, route: AlertRouteRow) -> dict[str, Any]:
    """PagerDuty Events API v2 payload.

    Routing key comes from `route.target_secret`; the `target_url` is
    the PD events endpoint (default `https://events.pagerduty.com/v2/enqueue`).
    """
    severity_map = {
        "low": "warning",
        "medium": "warning",
        "high": "error",
        "critical": "critical",
    }
    return {
        "routing_key": route.target_secret,
        "event_action": "trigger",
        # `dedup_key` lets PD-side dedup mirror ours. PD coalesces
        # alerts with the same dedup_key into a single incident.
        "dedup_key": alert.fingerprint or alert.external_id or "retrace",
        "payload": {
            "summary": (alert.title or "Retrace alert")[:1024],
            "source": "retrace",
            "severity": severity_map.get(
                alert.severity.lower() if alert.severity else "medium",
                "warning",
            ),
            "component": alert.provider or "",
            "group": alert.metadata.get("environment") or "",
            "custom_details": {
                "fingerprint": alert.fingerprint or "",
                "external_id": alert.external_id or "",
                "summary": alert.summary or "",
                "top_stack_frame": alert.metadata.get("top_stack_frame", ""),
            },
        },
    }


def _generic_webhook_payload(alert: MonitoringAlert) -> dict[str, Any]:
    """Generic JSON payload — fully descriptive so a consumer can
    build their own routing on top."""
    return {
        "kind": "retrace.alert",
        "version": 1,
        "provider": alert.provider,
        "external_id": alert.external_id,
        "title": alert.title,
        "summary": alert.summary,
        "severity": alert.severity,
        "fingerprint": alert.fingerprint,
        "occurred_at_ms": alert.occurred_at_ms,
        "metadata": dict(alert.metadata or {}),
        "evidence": dict(alert.evidence or {}),
    }


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _real_post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # Drain the body so the underlying connection is recyclable.
        resp.read()
        return int(resp.status)


def _severity_score(value: Optional[str]) -> int:
    return _SEVERITY_ORDER.get(str(value or "medium").strip().lower(), 2)


# Symbols re-exported for tests + callers that introspect what we
# support without parsing schema.
__all__ = [
    "DispatchResult",
    "dispatch_alert",
    "_TARGET_KINDS",
]
