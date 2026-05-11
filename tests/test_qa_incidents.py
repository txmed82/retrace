"""Tests for the unified Incident model + storage."""

from __future__ import annotations

import secrets
from pathlib import Path


from retrace.auto_fix import render_fix_prompt
from retrace.auto_repro import _classify_outcome
from retrace.qa_incidents import (
    Incident,
    IncidentEvidence,
    IncidentSource,
    ReproductionStep,
    incident_from_api_test,
    incident_from_tester_run,
    make_fingerprint,
    make_public_id,
    redact_sensitive_text,
    reproduction_prompt_for_incident,
    utc_now_iso,
)
from retrace.storage import Storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_incident(*, fp_seed: str = "default") -> Incident:
    return Incident(
        id=secrets.token_hex(16),
        public_id=make_public_id(),
        project_id="local",
        environment_id="production",
        fingerprint=make_fingerprint([fp_seed]),
        title="Sign-in button does nothing on slow networks",
        summary="Spinner shows then disappears with no error toast.",
        suspected_cause="POST /api/auth/login AbortError swallowed.",
        severity="high",
        confidence="high",
        status="open",
        primary_source_kind="replay",
        sources=[IncidentSource(kind="replay", ref_id="sess-1", score=1.0, note="t")],
        reproduction=[
            ReproductionStep(0, "navigate", "Open /login", url="http://localhost:3000/login"),
            ReproductionStep(1, "input", "Type email", target={"selector": "#email"}, value="a@b.co"),
            ReproductionStep(2, "click", "Click Sign in", target={"role": "button", "text": "Sign in"}),
        ],
        expected_outcome="Land on /dashboard",
        actual_outcome="Stays on /login",
        app_url="http://localhost:3000",
        evidence=IncidentEvidence(
            replay_session_ids=["sess-1"],
            primary_url="http://localhost:3000/login",
            top_stack_frame="handleSubmit at client/src/pages/Login.tsx:88",
            console_excerpts=["AbortError"],
        ),
        affected_count=4,
        affected_users=3,
        first_seen_ms=0,
        last_seen_ms=0,
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
    )


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def test_make_public_id_format():
    pid = make_public_id()
    assert pid.startswith("INC-")
    assert len(pid) == 4 + 6
    # only legibility-safe chars
    assert all(c in "23456789ABCDEFGHJKMNPQRSTUVWXYZ" for c in pid[4:])


def test_fingerprint_is_deterministic_and_short():
    a = make_fingerprint(["x", "y"])
    b = make_fingerprint(["x", "y"])
    c = make_fingerprint(["x", "z"])
    assert a == b
    assert a != c
    assert len(a) == 16


def test_reproduction_prompt_includes_steps_and_outcomes():
    inc = _make_incident()
    prompt = reproduction_prompt_for_incident(inc)
    assert "Sign-in button" in prompt
    assert "Start at: http://localhost:3000" in prompt
    assert "Type email" in prompt
    assert "Click Sign in" in prompt
    assert "Expected: Land on /dashboard" in prompt
    assert "Actual" in prompt


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_storage_upsert_is_idempotent_on_fingerprint(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()

    inc = _make_incident(fp_seed="signup")
    iid_1, ins_1 = store.upsert_qa_incident(inc.to_row())
    iid_2, ins_2 = store.upsert_qa_incident(inc.to_row())

    assert ins_1 is True
    assert ins_2 is False
    assert iid_1 == iid_2

    rows = store.list_qa_incidents()
    assert len(rows) == 1


def test_storage_get_by_public_id_and_internal_id(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    inc = _make_incident(fp_seed="get-test")
    store.upsert_qa_incident(inc.to_row())

    by_pub = store.get_qa_incident(inc.public_id)
    by_id = store.get_qa_incident(inc.id)
    assert by_pub is not None and by_id is not None
    assert by_pub["id"] == by_id["id"]


def test_storage_update_state_partial(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    inc = _make_incident(fp_seed="state-test")
    store.upsert_qa_incident(inc.to_row())

    ok = store.update_qa_incident_state(
        inc.public_id,
        repro_status="confirmed",
        repro_spec_id="spec-1",
        repro_run_id="run-1",
        status="reproduced",
    )
    assert ok is True
    row = store.get_qa_incident(inc.public_id)
    assert row["repro_status"] == "confirmed"
    assert row["repro_spec_id"] == "spec-1"
    assert row["repro_run_id"] == "run-1"
    assert row["status"] == "reproduced"
    # untouched fields preserved
    assert row["fix_status"] == "not_started"


def test_next_open_incident_prioritises_severity_then_status(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()

    low = _make_incident(fp_seed="A")
    low.severity = "low"
    high = _make_incident(fp_seed="B")
    high.severity = "high"
    crit_reproduced = _make_incident(fp_seed="C")
    crit_reproduced.severity = "critical"
    crit_reproduced.status = "reproduced"

    for inc in (low, high, crit_reproduced):
        store.upsert_qa_incident(inc.to_row())

    top = store.next_open_qa_incident()
    assert top is not None
    # status=open beats status=reproduced regardless of severity ordering
    assert top["public_id"] == high.public_id


def test_roundtrip_through_storage(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()

    original = _make_incident(fp_seed="roundtrip")
    store.upsert_qa_incident(original.to_row())
    row = store.get_qa_incident(original.public_id)
    assert row is not None
    revived = Incident.from_row(row)
    assert revived.title == original.title
    assert [s.description for s in revived.reproduction] == [
        s.description for s in original.reproduction
    ]
    assert revived.evidence.top_stack_frame == original.evidence.top_stack_frame


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


def test_incident_from_api_test_marks_500_high_severity():
    inc = incident_from_api_test(
        project_id="local",
        environment_id="production",
        title="POST /api/login -> 500",
        summary="Login returns 500 under load",
        method="POST",
        url="https://app.example/api/login",
        expected_status=200,
        actual_status=500,
        request_body='{"email":"a@b.co"}',
        response_body="Internal server error",
        run_id="api-run-1",
    )
    assert inc.severity == "high"
    assert inc.primary_source_kind == "api_test"
    assert inc.repro_status == "confirmed"
    assert inc.evidence.api_test_run_ids == ["api-run-1"]
    # repro steps should hold both the call and the assertion
    actions = [s.action for s in inc.reproduction]
    assert actions == ["api_call", "assert"]


def test_incident_from_tester_run_uses_first_failed_assertion():
    class _Spec:
        spec_id = "spec-x"
        name = "checkout suite"
        prompt = "exercise checkout"
        app_url = "http://localhost:3000"
        exact_steps = [{"action": "click", "description": "click checkout"}]

    class _Run:
        run_id = "run-x"
        exit_code = 1
        status = "fail"
        error = "click failed"
        assertion_results = [
            {"assertion_type": "visible", "ok": False, "message": "btn not visible"}
        ]

    inc = incident_from_tester_run(_Run(), _Spec(), project_id="local", environment_id="production")
    assert inc.primary_source_kind == "ui_test"
    assert inc.status == "reproduced"
    assert inc.repro_status == "confirmed"
    assert "btn not visible" in inc.summary
    assert inc.evidence.tester_run_ids == ["run-x"]


# ---------------------------------------------------------------------------
# Fix-prompt rendering
# ---------------------------------------------------------------------------


def test_render_fix_prompt_contains_required_sections():
    inc = _make_incident(fp_seed="prompt-render")
    md = render_fix_prompt(inc=inc, candidates=[])
    assert "# Retrace fix request" in md
    assert "## Bug" in md
    assert "## Reproduction" in md
    assert "## Evidence" in md
    assert "## Likely code locations" in md
    assert "## Task" in md
    assert "## Acceptance criteria" in md
    # incident details surfaced
    assert "Sign-in button" in md
    assert inc.public_id in md
    assert "handleSubmit at client/src/pages/Login.tsx:88" in md


def test_render_fix_prompt_handles_no_evidence_gracefully():
    inc = _make_incident(fp_seed="no-ev")
    inc.evidence = IncidentEvidence()
    md = render_fix_prompt(inc=inc, candidates=[])
    assert "_No structured evidence" in md


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------


def test_redact_sensitive_text_masks_secrets_in_json_payloads():
    raw = (
        '{"email":"alice@example.com","password":"hunter2",'
        '"api_key":"sk-abcdef0123456789abcdef0123456789","note":"ok"}'
    )
    redacted = redact_sensitive_text(raw)
    assert "hunter2" not in redacted
    assert "sk-abcdef0123456789abcdef0123456789" not in redacted
    assert "alice@example.com" not in redacted
    # the structural envelope and non-sensitive fields survive
    assert '"note"' in redacted
    assert "<redacted>" in redacted


def test_redact_sensitive_text_masks_bearer_tokens():
    redacted = redact_sensitive_text("Authorization: Bearer abcdefghijklmnop1234567890")
    assert "abcdefghijklmnop1234567890" not in redacted
    assert "<redacted>" in redacted


def test_redact_sensitive_text_is_safe_on_empty():
    assert redact_sensitive_text("") == ""


def test_incident_from_api_test_redacts_request_and_response_bodies():
    inc = incident_from_api_test(
        project_id="local",
        environment_id="production",
        title="POST /api/login -> 500",
        summary="Login returns 500",
        method="POST",
        url="https://app.example/api/login",
        expected_status=200,
        actual_status=500,
        request_body='{"email":"alice@example.com","password":"hunter2"}',
        response_body='{"token":"eyJabcdefghijklmnopqrstuvwxyz1234567890"}',
        run_id="api-run-1",
    )
    # The captured request value should not contain the cleartext password.
    api_step = inc.reproduction[0]
    assert "hunter2" not in api_step.value
    assert "alice@example.com" not in api_step.value
    # The actual_outcome echoes a truncated response; the token must be gone.
    assert "eyJabcdefghijklmnopqrstuvwxyz1234567890" not in inc.actual_outcome


# ---------------------------------------------------------------------------
# Runner outcome classification — don't promote setup errors to "confirmed"
# ---------------------------------------------------------------------------


class _RunStub:
    def __init__(self, *, exit_code: int, error: str = "", assertion_results=None) -> None:
        self.exit_code = exit_code
        self.error = error
        self.assertion_results = assertion_results or []
        self.run_id = "run-x"


def test_classify_outcome_confirmed_on_failed_assertion():
    run = _RunStub(
        exit_code=1,
        assertion_results=[{"assertion_type": "visible", "ok": False, "message": "missing"}],
    )
    confirmed, status, summary = _classify_outcome(run, exact_steps_count=0)
    assert confirmed is True
    assert status == "confirmed"
    assert "missing" in summary


def test_classify_outcome_error_when_runner_crashes_with_no_exact_steps():
    run = _RunStub(exit_code=2, error="harness failed to launch")
    confirmed, status, summary = _classify_outcome(run, exact_steps_count=0)
    assert confirmed is False
    assert status == "error"
    assert "harness failed to launch" in summary


def test_classify_outcome_confirmed_when_runner_fails_with_exact_steps():
    run = _RunStub(exit_code=1, error="step 3 click failed")
    confirmed, status, summary = _classify_outcome(run, exact_steps_count=3)
    assert confirmed is True
    assert status == "confirmed"
    assert "step 3 click failed" in summary


def test_classify_outcome_clean_run_is_not_confirmed():
    run = _RunStub(exit_code=0)
    confirmed, status, summary = _classify_outcome(run, exact_steps_count=2)
    assert confirmed is False
    assert status == "not_confirmed"
    assert "bug did not surface" in summary


# ---------------------------------------------------------------------------
# from_row robustness — single bad JSON cell must not nuke the whole query
# ---------------------------------------------------------------------------


def test_incident_from_row_tolerates_malformed_json(tmp_path: Path):
    import sqlite3

    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    inc = _make_incident(fp_seed="malformed")
    store.upsert_qa_incident(inc.to_row())

    # Corrupt the evidence_json cell out-of-band.
    with sqlite3.connect(str(tmp_path / "retrace.db")) as conn:
        conn.execute(
            "UPDATE qa_incidents SET evidence_json = ? WHERE public_id = ?",
            ("{ this is not json", inc.public_id),
        )

    row = store.get_qa_incident(inc.public_id)
    assert row is not None
    revived = Incident.from_row(row)
    # We should still get a usable Incident with default-empty evidence.
    assert revived.public_id == inc.public_id
    assert revived.evidence.console_excerpts == []
