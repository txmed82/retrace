"""End-to-end: Sentry envelope w/ breadcrumbs → `IncidentEvidence`.

Walks the same path a real Sentry event takes when it lands on
`/api/sentry/<proj>/envelope/`:

  Sentry SDK / Browser SDK
    → ingest_sentry_compat_request
    → parse_sentry_envelope
    → ingest_monitoring_webhook(provider="sentry", payload={"event": ...})
    → normalize_monitoring_alert → _sentry_alert
    → canonical_failure_from_monitor_incident (failure.metadata)
    → qa_incident_bridge._evidence_for_failure (IncidentEvidence)

This is the canary that proves the Browser SDK's `breadcrumbs` field on
the exception payload survives all the way to the qa_incident UI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from retrace.monitoring_ingest import (
    _breadcrumbs_from_sentry,
    _console_excerpts_from_breadcrumbs,
    _network_failures_from_breadcrumbs,
    ingest_monitoring_webhook,
    normalize_monitoring_alert,
)
from retrace.qa_incident_bridge import sync_qa_incident_from_failure
from retrace.storage import Storage


# ---------------------------------------------------------------------------
# Helpers (pure)
# ---------------------------------------------------------------------------


def _click_console_http_breadcrumbs() -> list[dict[str, Any]]:
    """A realistic 3-step trail: click → failed network → console error."""
    return [
        {
            "timestamp": 1700000000.0,
            "category": "ui.click",
            "message": 'button#checkout "Place order"',
            "level": "info",
            "data": {"tagName": "button"},
        },
        {
            "timestamp": 1700000001.0,
            "category": "http",
            "message": "POST /api/checkout → 500",
            "level": "error",
            "data": {
                "method": "POST",
                "url": "https://app.example.com/api/checkout",
                "status_code": 500,
                "duration_ms": 142,
            },
        },
        {
            "timestamp": 1700000002.0,
            "category": "console",
            "message": "TypeError: Cannot read property 'total' of undefined",
            "level": "error",
        },
    ]


def test_breadcrumbs_from_sentry_accepts_values_wrapper():
    """Standard Sentry shape: `event.breadcrumbs.values = [...]`."""
    event = {"breadcrumbs": {"values": _click_console_http_breadcrumbs()}}
    crumbs = _breadcrumbs_from_sentry(event)
    assert [c["category"] for c in crumbs] == ["ui.click", "http", "console"]


def test_breadcrumbs_from_sentry_accepts_bare_list():
    """Older SDKs / synthetic payloads sometimes send a bare list."""
    event = {"breadcrumbs": _click_console_http_breadcrumbs()}
    crumbs = _breadcrumbs_from_sentry(event)
    assert len(crumbs) == 3


def test_breadcrumbs_from_sentry_drops_non_dict_entries():
    event = {"breadcrumbs": {"values": ["not-a-dict", None, {"category": "ui", "message": "ok"}]}}
    crumbs = _breadcrumbs_from_sentry(event)
    assert [c["category"] for c in crumbs] == ["ui"]


def test_console_excerpts_picks_only_console_lines():
    crumbs = _breadcrumbs_from_sentry({"breadcrumbs": _click_console_http_breadcrumbs()})
    excerpts = _console_excerpts_from_breadcrumbs(crumbs)
    assert excerpts == ["TypeError: Cannot read property 'total' of undefined"]


def test_network_failures_picks_only_failed_http_breadcrumbs():
    crumbs = _breadcrumbs_from_sentry({"breadcrumbs": _click_console_http_breadcrumbs()})
    failures = _network_failures_from_breadcrumbs(crumbs)
    assert failures == [
        {
            "method": "POST",
            "url": "https://app.example.com/api/checkout",
            "status_code": 500,
            "duration_ms": 142,
        }
    ]


def test_network_failures_skips_2xx_breadcrumbs():
    """A successful request must not appear in `network_failures`."""
    crumbs = [
        {"category": "http", "message": "GET /api/healthz → 200",
         "level": "info", "data": {"method": "GET", "url": "/api/healthz", "status_code": 200}},
        {"category": "http", "message": "GET /api/oops → 500",
         "level": "error", "data": {"method": "GET", "url": "/api/oops", "status_code": 500}},
    ]
    failures = _network_failures_from_breadcrumbs(crumbs)
    assert [f["url"] for f in failures] == ["/api/oops"]


def test_network_failures_picks_up_explicit_errors_without_status():
    """A `data.error` (network/CORS error, no status) counts as a failure."""
    crumbs = [
        {"category": "http", "message": "GET /api/blocked (failed: NetworkError)",
         "level": "error", "data": {"method": "GET", "url": "/api/blocked", "error": "NetworkError"}},
    ]
    failures = _network_failures_from_breadcrumbs(crumbs)
    assert failures == [{"method": "GET", "url": "/api/blocked", "error": "NetworkError"}]


# ---------------------------------------------------------------------------
# Normalizer-level test (`_sentry_alert` populates metadata)
# ---------------------------------------------------------------------------


def test_sentry_alert_promotes_breadcrumbs_into_metadata():
    payload = {
        "event": {
            "event_id": "abc123",
            "level": "error",
            "exception": {"values": [{"type": "TypeError", "value": "boom"}]},
            "breadcrumbs": {"values": _click_console_http_breadcrumbs()},
        }
    }
    alert = normalize_monitoring_alert(provider="sentry", payload=payload)
    # Raw trail preserved verbatim.
    assert len(alert.metadata["breadcrumbs"]) == 3
    # Promoted fields are populated.
    assert alert.metadata["console_excerpts"] == [
        "TypeError: Cannot read property 'total' of undefined"
    ]
    failures = alert.metadata["network_failures"]
    assert len(failures) == 1
    assert failures[0]["status_code"] == 500


# ---------------------------------------------------------------------------
# Full pipeline: envelope → failure → qa_incident → evidence
# ---------------------------------------------------------------------------


def test_end_to_end_breadcrumbs_reach_qa_incident_evidence(tmp_path: Path):
    """A 3-step click→network→console trail produces a qa_incident
    whose evidence has `console_excerpts` + `network_failures` populated
    with the right entries, AND the raw breadcrumbs survive in
    `metadata`."""
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    ws = store.ensure_workspace(project_name="P0.4 e2e")
    project_id, environment_id = ws.project_id, ws.environment_id

    payload = {
        "event": {
            "event_id": "abc123",
            "level": "error",
            "exception": {
                "values": [{"type": "TypeError", "value": "Cannot read property 'total' of undefined"}]
            },
            "breadcrumbs": {"values": _click_console_http_breadcrumbs()},
        }
    }
    result = ingest_monitoring_webhook(
        store=store,
        project_id=project_id,
        environment_id=environment_id,
        provider="sentry",
        payload=payload,
    )
    assert result.failure_id

    failure = store.get_failure_by_id(result.failure_id)
    assert failure is not None
    # Metadata carries the raw trail and the promoted lists.
    assert len(failure.metadata.get("breadcrumbs") or []) == 3
    assert "TypeError: Cannot read property 'total' of undefined" in (
        failure.metadata.get("console_excerpts") or []
    )
    assert any(
        nf.get("status_code") == 500 for nf in (failure.metadata.get("network_failures") or [])
    )

    # The bridge promotes them onto IncidentEvidence (via the qa_incident).
    public_id = sync_qa_incident_from_failure(store=store, failure_id=result.failure_id)
    assert public_id is not None
    qa_row = store.get_qa_incident(public_id)
    assert qa_row is not None

    # qa_incident's evidence_json must carry the same lists.
    import json

    evidence = json.loads(qa_row["evidence_json"] or "{}")
    assert evidence.get("console_excerpts") == [
        "TypeError: Cannot read property 'total' of undefined"
    ]
    failures = evidence.get("network_failures") or []
    assert any(nf.get("status_code") == 500 for nf in failures)


def test_breadcrumbs_absent_is_a_no_op(tmp_path: Path):
    """An event with no breadcrumbs must not crash the ingest path
    (regression guard for None-handling in `_breadcrumbs_from_sentry`)."""
    payload = {
        "event": {
            "event_id": "no-breadcrumbs",
            "level": "error",
            "exception": {"values": [{"type": "ValueError", "value": "x"}]},
        }
    }
    alert = normalize_monitoring_alert(provider="sentry", payload=payload)
    # `_without_empty` drops the keys when they're empty.
    assert alert.metadata.get("breadcrumbs", []) == []
    assert alert.metadata.get("console_excerpts", []) == []
    assert alert.metadata.get("network_failures", []) == []
