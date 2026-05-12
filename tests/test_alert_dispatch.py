"""Alert fan-out dispatcher tests (P1.1).

Covers the four behaviors the roadmap asks for:

  1. A fired alert reaches every enabled matching route.
  2. Per-target payloads have the right shape (Slack blocks, Discord
     embed, PagerDuty Events v2 envelope, generic JSON).
  3. Severity floor on a route gates lower-severity alerts.
  4. Dedup window suppresses fast repeats of the same fingerprint.

We don't hit the real network — `dispatch_alert(..., _post=...)`
accepts a test seam that records `(url, headers, body, timeout)`
calls and returns a stub HTTP status.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from retrace.alert_dispatch import dispatch_alert
from retrace.alert_rules import AlertRuleDecision
from retrace.monitoring_ingest import MonitoringAlert
from retrace.storage import Storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store_with_workspace(tmp_path: Path) -> tuple[Storage, str, str]:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    ws = store.ensure_workspace(project_name="P1.1 routes")
    return store, ws.project_id, ws.environment_id


def _alert(
    *,
    fingerprint: str = "fp-abc",
    severity: str = "high",
    title: str = "Test alert",
    summary: str = "Something broke",
) -> MonitoringAlert:
    return MonitoringAlert(
        provider="sentry",
        external_id="ext-1",
        title=title,
        summary=summary,
        severity=severity,
        fingerprint=fingerprint,
        occurred_at_ms=1700000000_000,
        metadata={"environment": "production"},
        evidence={"summary": summary},
    )


def _decision(action: str = "alert", rule_name: str = "") -> AlertRuleDecision:
    return AlertRuleDecision(state="active", action=action, rule_name=rule_name)


def _capture_sender():
    sent: list[dict[str, Any]] = []

    def _post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> int:
        sent.append({"url": url, "headers": dict(headers), "body": body, "timeout": timeout})
        return 200

    return sent, _post


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dispatch_fans_out_to_every_enabled_route(tmp_path: Path):
    store, pid, eid = _store_with_workspace(tmp_path)
    store.upsert_alert_route(
        project_id=pid, environment_id=eid,
        name="slack-oncall",
        target_kind="slack",
        target_url="https://hooks.slack.com/services/T0/B0/X",
    )
    store.upsert_alert_route(
        project_id=pid, environment_id=eid,
        name="webhook-archive",
        target_kind="webhook",
        target_url="https://archive.example.com/alerts",
    )
    sent, post = _capture_sender()
    results = dispatch_alert(
        store=store, project_id=pid, environment_id=eid,
        alert=_alert(), decision=_decision(), _post=post,
    )
    assert {r.route_name for r in results} == {"slack-oncall", "webhook-archive"}
    assert all(r.status == "sent" for r in results)
    # Both targets received POSTs.
    urls = sorted(s["url"] for s in sent)
    assert urls == [
        "https://archive.example.com/alerts",
        "https://hooks.slack.com/services/T0/B0/X",
    ]


def test_dispatch_skips_routes_below_min_severity(tmp_path: Path):
    store, pid, eid = _store_with_workspace(tmp_path)
    store.upsert_alert_route(
        project_id=pid, environment_id=eid,
        name="critical-only",
        target_kind="slack",
        target_url="https://hooks.slack.com/x",
        min_severity="critical",
    )
    sent, post = _capture_sender()
    results = dispatch_alert(
        store=store, project_id=pid, environment_id=eid,
        alert=_alert(severity="high"), decision=_decision(), _post=post,
    )
    assert results[0].status == "skipped"
    assert "min_severity" in results[0].error
    assert sent == []


def test_dispatch_dedupes_within_window(tmp_path: Path):
    store, pid, eid = _store_with_workspace(tmp_path)
    store.upsert_alert_route(
        project_id=pid, environment_id=eid,
        name="dedup-route",
        target_kind="webhook",
        target_url="https://example.com/x",
        dedup_window_seconds=300,
    )
    sent, post = _capture_sender()
    a = _alert(fingerprint="same-fp")

    r1 = dispatch_alert(store=store, project_id=pid, environment_id=eid,
                       alert=a, decision=_decision(), _post=post)
    assert r1[0].status == "sent"
    assert len(sent) == 1
    r2 = dispatch_alert(store=store, project_id=pid, environment_id=eid,
                       alert=a, decision=_decision(), _post=post)
    assert r2[0].status == "deduped"
    assert len(sent) == 1  # second send was suppressed


def test_dispatch_dedup_window_zero_means_always_send(tmp_path: Path):
    store, pid, eid = _store_with_workspace(tmp_path)
    store.upsert_alert_route(
        project_id=pid, environment_id=eid,
        name="no-dedup",
        target_kind="webhook",
        target_url="https://example.com/x",
        dedup_window_seconds=0,
    )
    sent, post = _capture_sender()
    a = _alert(fingerprint="fp1")
    dispatch_alert(store=store, project_id=pid, environment_id=eid,
                   alert=a, decision=_decision(), _post=post)
    dispatch_alert(store=store, project_id=pid, environment_id=eid,
                   alert=a, decision=_decision(), _post=post)
    assert len(sent) == 2


def test_dispatch_respects_rule_name_filter(tmp_path: Path):
    """A route bound to `rule_name="prod-only"` must NOT fire on
    alerts from a different rule (or no rule)."""
    store, pid, eid = _store_with_workspace(tmp_path)
    store.upsert_alert_route(
        project_id=pid, environment_id=eid,
        name="prod-route",
        target_kind="webhook",
        target_url="https://example.com/x",
        rule_name="prod-only",
    )
    store.upsert_alert_route(
        project_id=pid, environment_id=eid,
        name="catch-all",
        target_kind="webhook",
        target_url="https://example.com/y",
        rule_name="",
    )
    sent, post = _capture_sender()
    # A decision tagged with a different rule_name only hits catch-all.
    results = dispatch_alert(
        store=store, project_id=pid, environment_id=eid,
        alert=_alert(),
        decision=_decision(rule_name="other-rule"),
        _post=post,
    )
    names = {r.route_name for r in results}
    # `prod-route` doesn't match (rule_name="prod-only" vs "other-rule"),
    # `catch-all` does (rule_name="").
    assert names == {"catch-all"}


def test_dispatch_skips_disabled_routes(tmp_path: Path):
    store, pid, eid = _store_with_workspace(tmp_path)
    store.upsert_alert_route(
        project_id=pid, environment_id=eid,
        name="off-route",
        target_kind="slack",
        target_url="https://example.com/x",
        enabled=False,
    )
    sent, post = _capture_sender()
    results = dispatch_alert(
        store=store, project_id=pid, environment_id=eid,
        alert=_alert(), decision=_decision(), _post=post,
    )
    assert results == []
    assert sent == []


def test_dispatch_no_op_when_decision_is_suppress(tmp_path: Path):
    store, pid, eid = _store_with_workspace(tmp_path)
    store.upsert_alert_route(
        project_id=pid, environment_id=eid,
        name="any",
        target_kind="webhook",
        target_url="https://example.com/x",
    )
    sent, post = _capture_sender()
    results = dispatch_alert(
        store=store, project_id=pid, environment_id=eid,
        alert=_alert(),
        decision=AlertRuleDecision(state="suppressed", action="suppress"),
        _post=post,
    )
    assert results == []
    assert sent == []


def test_dispatch_records_send_errors_but_doesnt_raise(tmp_path: Path):
    store, pid, eid = _store_with_workspace(tmp_path)
    store.upsert_alert_route(
        project_id=pid, environment_id=eid,
        name="flaky",
        target_kind="webhook",
        target_url="https://example.com/x",
    )

    def _failing(url, headers, body, timeout):
        raise RuntimeError("upstream down")

    results = dispatch_alert(
        store=store, project_id=pid, environment_id=eid,
        alert=_alert(), decision=_decision(), _post=_failing,
    )
    assert results[0].status == "failed"
    assert "upstream down" in results[0].error
    rows = store.list_recent_alert_dispatches(project_id=pid, environment_id=eid)
    assert rows[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# Per-target payload shapes
# ---------------------------------------------------------------------------


def _post_capture_payload(sent: list[dict[str, Any]]):
    def _post(url, headers, body, timeout):
        sent.append({"url": url, "headers": dict(headers), "payload": json.loads(body)})
        return 200

    return _post


def test_slack_payload_has_blocks_and_title(tmp_path: Path):
    store, pid, eid = _store_with_workspace(tmp_path)
    store.upsert_alert_route(
        project_id=pid, environment_id=eid,
        name="slack",
        target_kind="slack",
        target_url="https://hooks.slack.com/x",
    )
    sent: list[dict[str, Any]] = []
    dispatch_alert(
        store=store, project_id=pid, environment_id=eid,
        alert=_alert(title="DB connection lost", summary="reconnect failed"),
        decision=_decision(), _post=_post_capture_payload(sent),
    )
    payload = sent[0]["payload"]
    assert "blocks" in payload
    header_block = payload["blocks"][0]
    assert header_block["type"] == "header"
    assert "DB connection lost" in header_block["text"]["text"]
    # Severity + fingerprint in fields.
    field_text = " ".join(
        f["text"]
        for b in payload["blocks"] if b["type"] == "section" and "fields" in b
        for f in b["fields"]
    )
    assert "HIGH" in field_text


def test_discord_payload_has_embed_and_color(tmp_path: Path):
    store, pid, eid = _store_with_workspace(tmp_path)
    store.upsert_alert_route(
        project_id=pid, environment_id=eid,
        name="discord",
        target_kind="discord",
        target_url="https://discord.com/api/webhooks/x",
    )
    sent: list[dict[str, Any]] = []
    dispatch_alert(
        store=store, project_id=pid, environment_id=eid,
        alert=_alert(severity="critical"),
        decision=_decision(), _post=_post_capture_payload(sent),
    )
    payload = sent[0]["payload"]
    assert payload["embeds"][0]["color"] == 0xe74c3c  # critical → red
    assert payload["embeds"][0]["title"] == "Test alert"


def test_pagerduty_payload_uses_routing_key_and_dedup_key(tmp_path: Path):
    store, pid, eid = _store_with_workspace(tmp_path)
    store.upsert_alert_route(
        project_id=pid, environment_id=eid,
        name="pd",
        target_kind="pagerduty",
        target_url="https://events.pagerduty.com/v2/enqueue",
        target_secret="ROUTING_KEY_12345",
    )
    sent: list[dict[str, Any]] = []
    dispatch_alert(
        store=store, project_id=pid, environment_id=eid,
        alert=_alert(fingerprint="fp-pd"),
        decision=_decision(), _post=_post_capture_payload(sent),
    )
    payload = sent[0]["payload"]
    assert payload["routing_key"] == "ROUTING_KEY_12345"
    assert payload["event_action"] == "trigger"
    assert payload["dedup_key"] == "fp-pd"
    assert payload["payload"]["severity"] == "error"  # "high" → PD "error"


def test_generic_webhook_payload_is_fully_descriptive(tmp_path: Path):
    store, pid, eid = _store_with_workspace(tmp_path)
    store.upsert_alert_route(
        project_id=pid, environment_id=eid,
        name="webhook",
        target_kind="webhook",
        target_url="https://example.com/hook",
    )
    sent: list[dict[str, Any]] = []
    dispatch_alert(
        store=store, project_id=pid, environment_id=eid,
        alert=_alert(), decision=_decision(),
        _post=_post_capture_payload(sent),
    )
    payload = sent[0]["payload"]
    assert payload["kind"] == "retrace.alert"
    assert payload["fingerprint"] == "fp-abc"
    assert payload["severity"] == "high"
    assert payload["metadata"]["environment"] == "production"


# ---------------------------------------------------------------------------
# Wire-up through ingest_monitoring_webhook
# ---------------------------------------------------------------------------


def test_ingest_monitoring_webhook_triggers_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end: posting a Sentry-shape payload through
    `ingest_monitoring_webhook` triggers `dispatch_alert` once we
    create a route. Patches the dispatcher's HTTP sender so the
    test stays in-process."""
    from retrace.monitoring_ingest import ingest_monitoring_webhook

    store, pid, eid = _store_with_workspace(tmp_path)
    store.upsert_alert_route(
        project_id=pid, environment_id=eid,
        name="w",
        target_kind="webhook",
        target_url="https://example.com/x",
    )
    sent: list[dict[str, Any]] = []

    # Patch the dispatcher's `_real_post` so it doesn't hit the
    # network. We import the module and replace the symbol.
    from retrace import alert_dispatch

    def _stub(url, headers, body, timeout):
        sent.append((url, body))
        return 202

    monkeypatch.setattr(alert_dispatch, "_real_post", _stub)

    result = ingest_monitoring_webhook(
        store=store,
        project_id=pid,
        environment_id=eid,
        provider="sentry",
        payload={
            "event": {
                "event_id": "evt-1",
                "level": "error",
                "exception": {"values": [{"type": "TypeError", "value": "boom"}]},
            }
        },
    )
    assert result.failure_id
    assert len(sent) == 1
    assert sent[0][0] == "https://example.com/x"
