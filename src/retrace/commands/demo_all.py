"""`retrace demo all` — seed one incident per pillar.

After installing Retrace, a new user wants to see what the product looks
like *across every input class*, not just the replay one. This command
generates one incident from each of:

  - replay (a synthetic rrweb session with a click that errors)
  - UI test (a deliberately failing tester run)
  - API test (a contract test against a 500-returning endpoint)
  - error monitor (a Sentry-compat exception)
  - PR review (a synthetic diff that touches a fake-flaky surface)

…and surfaces them through the unified `qa_incidents` queue so
`retrace qa list` immediately shows five different source kinds.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import click

from retrace.config import load_config
from retrace.failures import CanonicalFailure, stable_failure_public_id
from retrace.incidents import group_failure_into_incident
from retrace.qa_incident_bridge import sync_qa_incident_from_pr_review_finding
from retrace.qa_incidents import (
    Incident,
    IncidentEvidence,
    IncidentSource,
    ReproductionStep,
    incident_from_api_test,
    make_fingerprint,
    make_public_id,
    utc_now_iso,
)
from retrace.storage import Storage


def seed_all_pillars(
    *,
    config_path: Path,
    project_name: str = "Default",
    environment_name: str = "production",
) -> dict[str, str]:
    """Seed one incident per pillar, returning their public ids by source."""
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(
        org_name="Local",
        project_name=project_name,
        environment_name=environment_name,
    )
    project_id = workspace.project_id
    environment_id = workspace.environment_id

    results: dict[str, str] = {}

    # ---- Replay-derived incident ----
    pid = _seed_replay_incident(store, project_id, environment_id)
    if pid:
        results["replay"] = pid
        click.echo(f"  replay        {pid}  (synthetic rrweb session with error toast)")

    # ---- UI test failure ----
    pid = _seed_ui_test_incident(store, project_id, environment_id)
    if pid:
        results["ui_test"] = pid
        click.echo(f"  ui_test       {pid}  (deliberately failing tester run)")

    # ---- API test failure ----
    pid = _seed_api_test_incident(store, project_id, environment_id)
    if pid:
        results["api_test"] = pid
        click.echo(f"  api_test      {pid}  (POST /api/login -> 500)")

    # ---- Error monitor ----
    pid = _seed_monitor_incident(store, project_id, environment_id)
    if pid:
        results["error_monitor"] = pid
        click.echo(f"  error_monitor {pid}  (Sentry-compat: TypeError)")

    # ---- PR review finding ----
    pid = _seed_pr_review_incident(store, project_id, environment_id)
    if pid:
        results["pr_review"] = pid
        click.echo(f"  pr_review     {pid}  (synthetic diff touching login flow)")

    click.echo("")
    click.echo("Next:")
    click.echo("  retrace qa list           # see all five pillars in one queue")
    click.echo("  retrace qa show <INC-id>  # inspect any of them")
    return results


# ---------------------------------------------------------------------------
# Per-pillar seeders. Each returns the qa_incident's public id (or "").
# ---------------------------------------------------------------------------


def _seed_replay_incident(
    store: Storage, project_id: str, environment_id: str
) -> str:
    inc = Incident(
        id=secrets.token_hex(16),
        public_id=make_public_id(),
        project_id=project_id,
        environment_id=environment_id,
        fingerprint=make_fingerprint(["demo", "replay", "checkout-crash"]),
        title="Demo: checkout button crashes on submit",
        summary=(
            "User clicks Submit on /checkout. Console shows "
            "`TypeError: Cannot read property 'total' of undefined`. "
            "Page stays on /checkout."
        ),
        suspected_cause="checkout reducer accesses `total` before order hydration.",
        severity="high",
        confidence="high",
        status="open",
        primary_source_kind="replay",
        sources=[
            IncidentSource(
                kind="replay",
                ref_id="demo-checkout-crash",
                score=1.0,
                note="synthetic replay session",
                created_at=utc_now_iso(),
            )
        ],
        reproduction=[
            ReproductionStep(0, "navigate", "Open /checkout", url="https://demo.retrace.local/checkout"),
            ReproductionStep(1, "click", "Click Submit", target={"role": "button", "text": "Submit"}),
            ReproductionStep(2, "assert", "URL should change to /thanks", target={"url_contains": "/thanks"}),
        ],
        expected_outcome="Land on /thanks",
        actual_outcome="Stays on /checkout; console error.",
        app_url="https://demo.retrace.local",
        evidence=IncidentEvidence(
            replay_session_ids=["demo-checkout-crash"],
            console_excerpts=["TypeError: Cannot read property 'total' of undefined"],
            top_stack_frame="checkoutReducer at client/src/store/checkout.ts:42",
            primary_url="https://demo.retrace.local/checkout",
        ),
        affected_count=3,
        affected_users=3,
        first_seen_ms=0,
        last_seen_ms=0,
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
    )
    _, pid, _ = store.upsert_qa_incident_returning(inc.to_row())
    return pid


def _seed_ui_test_incident(
    store: Storage, project_id: str, environment_id: str
) -> str:
    inc = Incident(
        id=secrets.token_hex(16),
        public_id=make_public_id(),
        project_id=project_id,
        environment_id=environment_id,
        fingerprint=make_fingerprint(["demo", "ui_test", "login-not-visible"]),
        title="Demo: UI test failed — Sign in button missing on mobile",
        summary="Native runner could not find `button[data-testid='signin']` within 5s.",
        suspected_cause="Mobile layout hides the CTA below the fold.",
        severity="medium",
        confidence="high",
        status="reproduced",
        primary_source_kind="ui_test",
        sources=[
            IncidentSource(
                kind="ui_test",
                ref_id="demo-tester-run-1",
                score=1.0,
                note="synthetic tester run",
                created_at=utc_now_iso(),
            )
        ],
        reproduction=[
            ReproductionStep(0, "navigate", "Open /login on a 375px viewport", url="https://demo.retrace.local/login"),
            ReproductionStep(1, "assert", "Sign in button visible", target={"selector": "button[data-testid='signin']"}),
        ],
        expected_outcome="Sign in button visible",
        actual_outcome="Selector not found within 5000ms.",
        app_url="https://demo.retrace.local/login",
        evidence=IncidentEvidence(
            tester_run_ids=["demo-tester-run-1"],
            primary_url="https://demo.retrace.local/login",
        ),
        affected_count=1,
        affected_users=1,
        first_seen_ms=0,
        last_seen_ms=0,
        repro_status="confirmed",
        repro_summary="assertion `text_visible` failed: btn not visible",
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
    )
    _, pid, _ = store.upsert_qa_incident_returning(inc.to_row())
    return pid


def _seed_api_test_incident(
    store: Storage, project_id: str, environment_id: str
) -> str:
    inc = incident_from_api_test(
        project_id=project_id,
        environment_id=environment_id,
        title="Demo: POST /api/login -> 500",
        summary="Login endpoint returns 500 for ~3% of requests under load.",
        method="POST",
        url="https://api.demo.retrace.local/api/login",
        expected_status=200,
        actual_status=500,
        request_body='{"email":"demo@example.com","password":"<masked>"}',
        response_body='{"error":"internal","trace_id":"demo-trace-123"}',
        suspected_cause="Connection pool exhaustion during peak.",
        run_id="demo-api-run-1",
    )
    _, pid, _ = store.upsert_qa_incident_returning(inc.to_row())
    return pid


def _seed_monitor_incident(
    store: Storage, project_id: str, environment_id: str
) -> str:
    """Use the canonical-failure → bridge path so this exercises the
    real monitoring ingest plumbing, not just a direct qa_incident
    write."""
    public_id = stable_failure_public_id(
        project_id=project_id,
        environment_id=environment_id,
        source_type="monitor_incident",
        source_external_id="demo-sentry-event-1",
    )
    failure = CanonicalFailure(
        public_id=public_id,
        project_id=project_id,
        environment_id=environment_id,
        source_type="monitor_incident",
        source_external_id="demo-sentry-event-1",
        fingerprint="demo-monitor-fp-1",
        title="Demo: TypeError in renderCheckout",
        summary="Sentry alert: 17 events in 5 minutes from /checkout.",
        severity="high",
        confidence="high",
        status="new",
        affected_users=14,
        affected_sessions=17,
        first_seen_ms=0,
        last_seen_ms=0,
        metadata={
            "url": "https://demo.retrace.local/checkout",
            "top_stack_frame": "renderCheckout at client/src/pages/Checkout.tsx:88",
            "trace_ids": ["demo-trace-456"],
            "console_excerpts": ["TypeError: Cannot read property 'total' of undefined"],
            "suspected_cause": "Same root cause as the replay incident.",
        },
    )
    failure_id, _evidence, _task = store.upsert_failure_with_evidence_and_repair_task(
        failure=failure, evidence_items=[], repair_task={"title": "Fix renderCheckout TypeError"}
    )
    # Grouping triggers the bridge — landing the incident in qa_incidents.
    result = group_failure_into_incident(store=store, failure_id=failure_id)
    # Pull the QA mirror back out by walking the bridge result. The
    # bridge upserts on fingerprint, so fetching by failure metadata is
    # cleanest: pick the most recent error_monitor incident.
    rows = store.list_qa_incidents(project_id=project_id, environment_id=environment_id, limit=50)
    for row in rows:
        if str(row["primary_source_kind"] or "") == "error_monitor":
            return str(row["public_id"])
    return result.incident_public_id  # fall back to master id; better than empty


def _seed_pr_review_incident(
    store: Storage, project_id: str, environment_id: str
) -> str:
    pid = sync_qa_incident_from_pr_review_finding(
        store=store,
        project_id=project_id,
        environment_id=environment_id,
        title="Demo: PR touches the login flow that prior incidents flagged",
        summary=(
            "Synthetic diff modifies `server/routes/auth.ts` and "
            "`client/src/pages/Login.tsx`. Prior incidents on this surface "
            "are still open."
        ),
        repo="local/demo-checkout",
        pr_number=42,
        files=[
            "server/routes/auth.ts",
            "client/src/pages/Login.tsx",
        ],
        suspected_cause="Regression risk against open replay/API incidents.",
        severity="medium",
    )
    return pid or ""
