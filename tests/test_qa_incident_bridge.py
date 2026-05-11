"""Bridge tests: any signal source -> qa_incidents."""

from __future__ import annotations

from pathlib import Path


from retrace.failures import CanonicalFailure, stable_failure_public_id
from retrace.qa_incident_bridge import (
    sync_qa_incident_from_failure,
    sync_qa_incident_from_pr_review_finding,
)
from retrace.qa_incidents import Incident
from retrace.storage import Storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> Storage:
    s = Storage(tmp_path / "retrace.db")
    s.init_schema()
    return s


def _insert_failure(
    store: Storage,
    *,
    source_type: str = "monitor_incident",
    metadata: dict | None = None,
) -> str:
    public_id = stable_failure_public_id(
        project_id="local",
        environment_id="production",
        source_type=source_type,
        source_external_id="ext-1",
    )
    failure = CanonicalFailure(
        public_id=public_id,
        project_id="local",
        environment_id="production",
        source_type=source_type,
        source_external_id="ext-1",
        fingerprint="fp-1",
        title="Login returns 500 under load",
        summary="POST /api/login returns 500 for ~3% of users.",
        severity="high",
        confidence="high",
        status="new",
        affected_users=12,
        affected_sessions=18,
        first_seen_ms=1_700_000_000_000,
        last_seen_ms=1_700_000_300_000,
        metadata=metadata or {},
    )
    failure_id, _evidence_ids, _task_id = store.upsert_failure_with_evidence_and_repair_task(
        failure=failure,
        evidence_items=[],
        repair_task={"title": "Fix login 500"},
    )
    return failure_id


# ---------------------------------------------------------------------------
# Failure -> QA incident
# ---------------------------------------------------------------------------


def test_sync_qa_incident_from_failure_monitor_alert(tmp_path: Path):
    store = _store(tmp_path)
    failure_id = _insert_failure(
        store,
        source_type="monitor_incident",
        metadata={
            "url": "https://app.example.com/login",
            "top_stack_frame": "loginHandler at server/routes/auth.ts:42",
            "trace_ids": ["abc123"],
            "console_excerpts": ["Login failed: 500"],
        },
    )
    pid = sync_qa_incident_from_failure(store=store, failure_id=failure_id)
    assert pid and pid.startswith("INC-")

    row = store.get_qa_incident(pid)
    assert row is not None
    inc = Incident.from_row(row)
    assert inc.primary_source_kind == "error_monitor"
    assert inc.severity == "high"
    assert inc.app_url == "https://app.example.com/login"
    assert inc.evidence.top_stack_frame == "loginHandler at server/routes/auth.ts:42"
    assert "abc123" in inc.evidence.trace_ids


def test_sync_api_test_failure_classified_as_api_test(tmp_path: Path):
    store = _store(tmp_path)
    failure_id = _insert_failure(
        store,
        source_type="api_test",
        metadata={
            "request_method": "POST",
            "request_url": "https://api.example.com/login",
            "expected_status": 200,
            "response_status": 500,
        },
    )
    pid = sync_qa_incident_from_failure(store=store, failure_id=failure_id)
    assert pid

    row = store.get_qa_incident(pid)
    inc = Incident.from_row(row)
    assert inc.primary_source_kind == "api_test"
    # Reproduction recipe was built from method+url+expected/actual metadata.
    actions = [s.action for s in inc.reproduction]
    assert "api_call" in actions
    assert "assert" in actions


def test_sync_returns_none_for_missing_failure(tmp_path: Path):
    store = _store(tmp_path)
    assert sync_qa_incident_from_failure(store=store, failure_id="nope") is None


def test_sync_is_idempotent_on_same_failure(tmp_path: Path):
    store = _store(tmp_path)
    failure_id = _insert_failure(store)
    pid1 = sync_qa_incident_from_failure(store=store, failure_id=failure_id)
    pid2 = sync_qa_incident_from_failure(store=store, failure_id=failure_id)
    assert pid1 and pid2
    # Same fingerprint produces same row — only one qa_incident exists.
    assert len(store.list_qa_incidents()) == 1
    # And — critically — the bridge returns the stable persisted public_id
    # on every sync, NOT a fresh transient one. If this drifts, `retrace
    # review` users get dead INC-XXXX references on rerun.
    assert pid1 == pid2
    assert store.get_qa_incident(pid1) is not None


# ---------------------------------------------------------------------------
# PR-review finding -> QA incident
# ---------------------------------------------------------------------------


def test_sync_qa_incident_from_pr_review_finding(tmp_path: Path):
    store = _store(tmp_path)
    pid = sync_qa_incident_from_pr_review_finding(
        store=store,
        project_id="local",
        environment_id="production",
        title="PR touches login flow but no test covers prior incident INC-AB12",
        summary="Diff modifies server/routes/auth.ts; INC-AB12 is open.",
        repo="org/app",
        pr_number=42,
        files=["server/routes/auth.ts", "client/src/pages/Login.tsx"],
        suspected_cause="Regression risk against INC-AB12",
        severity="medium",
    )
    assert pid and pid.startswith("INC-")
    inc = Incident.from_row(store.get_qa_incident(pid))
    assert inc.primary_source_kind == "error_monitor"
    assert inc.app_url == "https://github.com/org/app/pull/42"
    assert len(inc.reproduction) == 2  # one inspect step per affected file (capped at 8)


# ---------------------------------------------------------------------------
# End-to-end: master's group_failure_into_incident also mirrors into qa
# ---------------------------------------------------------------------------


def test_group_failure_into_incident_also_files_qa_incident(tmp_path: Path):
    """`group_failure_into_incident` is the chokepoint for monitoring/deploy
    ingest. After grouping it must mirror to qa_incidents so `qa list`
    sees these signals next to UI- and replay-derived bugs.
    """
    from retrace.incidents import group_failure_into_incident

    store = _store(tmp_path)
    failure_id = _insert_failure(
        store,
        source_type="monitor_incident",
        metadata={"url": "https://app.example.com/x"},
    )

    before = len(store.list_qa_incidents())
    group_failure_into_incident(store=store, failure_id=failure_id)
    after = store.list_qa_incidents()
    assert len(after) == before + 1
    inc = Incident.from_row(after[0])
    assert inc.title.startswith("Login")
    assert inc.primary_source_kind == "error_monitor"
